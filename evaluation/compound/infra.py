"""Local reproduction building blocks: containers on one docker network plus host-side emulators.

Only the platform control loops are emulated (kubelet liveness probing and restart, a Service's endpoint
set, operational Jobs). Applications, the database and every log line they write are real processes.
Containers and the network are named `lpcb-*` and removed by `cleanup()`.
"""
from __future__ import annotations

import csv
import json
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

NET = "lpcb"
IMAGE = "lpcb/shopfront:1.4.2"
JRE = "eclipse-temurin:21-jre"
PG = "postgres:17-alpine"
PROXY_IMAGE = "python:3.12-slim-bookworm"
FIXTURE = Path(__file__).resolve().parent / "fixture"


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(at: datetime | None = None) -> str:
    return (at or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(*args: str, check: bool = True, input: str | None = None, timeout: float = 120) -> str:
    result = subprocess.run(list(args), input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def docker(*args: str, check: bool = True, input: str | None = None, timeout: float = 120) -> str:
    return sh("docker", *args, check=check, input=input, timeout=timeout)


def use_network(name: str) -> None:
    """One docker network per scenario: Service-like aliases (stock, order) must never resolve across runs."""
    global NET
    NET = name


def cleanup(prefix: str = "lpcb-") -> None:
    """Remove this run's containers and its network."""
    names = docker("ps", "-aq", "--filter", f"name=^{prefix}", check=False).split()
    if names:
        docker("rm", "-f", *names, check=False)
    docker("network", "rm", NET, check=False)


def network() -> None:
    if not docker("network", "ls", "-q", "--filter", f"name=^{NET}$", check=False).strip():
        docker("network", "create", NET)


def build_image(jar: Path, workdir: Path) -> None:
    """Bake the fixture JAR into a JRE image (the VM cannot see the host scratch directory)."""
    context = workdir / "image"
    context.mkdir(parents=True, exist_ok=True)
    (context / "app.jar").write_bytes(jar.read_bytes())
    (context / "Dockerfile").write_text(
        f"FROM {JRE}\nCOPY app.jar /app.jar\nUSER 10001\nEXPOSE 8080\n"
        'ENTRYPOINT ["java", "-XX:ActiveProcessorCount=2", "-Duser.timezone=UTC", "-jar", "/app.jar"]\n')
    docker("build", "--pull=false", "-q", "-t", IMAGE, str(context), timeout=600)


def container_ip(name: str) -> str:
    return docker("inspect", "-f", f"{{{{(index .NetworkSettings.Networks \"{NET}\").IPAddress}}}}", name).strip()


def host_port(name: str, port: int = 8080) -> int:
    return int(docker("port", name, f"{port}/tcp").split(":")[-1].split()[0])


# --- PostgreSQL ------------------------------------------------------------------------------------------

@dataclass
class Postgres:
    name: str
    alias: str
    settings: dict = field(default_factory=dict)

    def start(self) -> None:
        flags = {"log_line_prefix": "%m [%p] %q%u@%d ", "log_connections": "off", **self.settings}
        args = [f"-c{k}={v}" for k, v in flags.items()]
        docker("run", "-d", "--name", self.name, "--network", NET, "--network-alias", self.alias,
               "--hostname", self.alias, "--cpus", "0.5", "-m", "384m",
               "-e", "POSTGRES_PASSWORD=platform-local", PG, *args)
        for _ in range(60):
            if docker("exec", self.name, "pg_isready", "-U", "postgres", check=False).find("accepting") >= 0:
                return
            time.sleep(1)
        raise RuntimeError(f"{self.name} not ready")

    def psql(self, sql: str, db: str = "postgres", user: str = "postgres") -> str:
        return docker("exec", "-i", self.name, "psql", "-XqAt", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db,
                      input=sql)

    def bootstrap(self) -> None:
        self.psql((FIXTURE / "bootstrap.sql").read_text())

    def allow_replication(self) -> None:
        self.psql("create role replicator with replication login password 'replica-local';")
        docker("exec", "-u", "postgres", self.name, "sh", "-c",
               "echo 'host replication replicator all scram-sha-256' >> /var/lib/postgresql/data/pg_hba.conf")
        self.psql("select pg_reload_conf();")


@dataclass
class Standby(Postgres):
    """A streaming hot standby cloned from `primary` with pg_basebackup."""
    primary: str = ""
    hba_clients: str = ""  # replica access profile: when set, only this role may connect over TCP

    def start(self) -> None:
        prefix = "%m [%p] %q%u@%d "
        profile = (f"sed -i 's/^host all all all scram-sha-256/host all {self.hba_clients} all scram-sha-256/' "
                   "/var/lib/postgresql/data/pgdata/pg_hba.conf; ") if self.hba_clients else ""
        script = (f"until pg_basebackup -h {self.primary} -U replicator -D /var/lib/postgresql/data/pgdata -R -X stream; "
                  "do rm -rf /var/lib/postgresql/data/pgdata; sleep 2; done; " + profile +
                  f"exec postgres -D /var/lib/postgresql/data/pgdata -c 'log_line_prefix={prefix}' -c hot_standby=on")
        docker("run", "-d", "--name", self.name, "--network", NET, "--network-alias", self.alias,
               "--hostname", self.alias.split(".")[0], "--cpus", "0.5", "-m", "384m", "-u", "postgres",
               "-e", "PGPASSWORD=replica-local", "--entrypoint", "sh", PG, "-c", script)
        for _ in range(240):
            if docker("exec", self.name, "pg_isready", "-U", "postgres", check=False).find("accepting") >= 0:
                return
            time.sleep(1)
        raise RuntimeError(f"{self.name} not ready")


class HttpService:
    """An HTTP Service in front of pod containers: round robin per request, no retries (kube-proxy semantics)."""
    NGINX = "nginx:1.29-alpine"

    def __init__(self, name: str, alias: str, pods: list[str]):
        self.name, self.alias = name, alias
        docker("run", "-d", "--name", name, "--network", NET, "--network-alias", alias, "--cpus", "0.5", "-m", "96m",
               self.NGINX, "sh", "-c", "sleep infinity")
        self.set_pods(pods, start=True)

    def set_pods(self, pods: list[str], start: bool = False) -> None:
        servers = "".join(f"server {pod}:8080 max_fails=0;" for pod in pods)
        conf = ("worker_processes 1; events { worker_connections 4096; } http { access_log off; "
                f"upstream backend {{ {servers} }} server {{ listen 8080; location / {{ proxy_pass http://backend; "
                "proxy_next_upstream off; proxy_connect_timeout 1s; proxy_read_timeout 60s; "
                "proxy_http_version 1.1; proxy_set_header Connection \"\"; } } }")
        docker("exec", "-i", self.name, "sh", "-c", "cat > /etc/nginx/nginx.conf", input=conf)
        if start:
            docker("exec", "-d", self.name, "nginx", "-g", "daemon off;")
            time.sleep(1)
        else:
            docker("exec", self.name, "nginx", "-s", "reload")


class ServiceProxy:
    """A Service's endpoint set (see svcproxy.py); `alias` is the Service DNS name."""

    def __init__(self, name: str, alias: str, endpoints: list[str]):
        self.name, self.alias = name, alias
        script = (Path(__file__).resolve().parent / "svcproxy.py").read_text()
        docker("run", "-d", "--name", name, "--network", NET, "--network-alias", alias, "--cpus", "0.3", "-m", "96m",
               "-p", "127.0.0.1::9000", "-e", f"ENDPOINTS={','.join(endpoints)}", PROXY_IMAGE, "python", "-c", script)
        for _ in range(30):
            try:
                self.endpoints()
                return
            except (urllib.error.URLError, OSError, RuntimeError, ValueError):
                time.sleep(0.5)
        raise RuntimeError("service proxy not ready")

    def control(self) -> str:
        return f"http://127.0.0.1:{host_port(self.name, 9000)}/"

    def endpoints(self) -> list[str]:
        with urllib.request.urlopen(self.control(), timeout=2) as response:
            return [e for e in response.read().decode().split(",") if e]

    def set_endpoints(self, endpoints: list[str]) -> None:
        request = urllib.request.Request(self.control(), data=",".join(endpoints).encode(), method="PUT")
        with urllib.request.urlopen(request, timeout=2):
            pass


# --- Application containers ------------------------------------------------------------------------------

@dataclass
class App:
    """One pod-like container. `hostname` is the pod name; restarts keep the same container (pod)."""
    name: str
    hostname: str
    alias: str
    profile: str
    props: dict
    cpus: str = "1.0"
    memory: str = "512m"
    publish: bool = True

    def start(self) -> None:
        args = [f"--{k}={v}" for k, v in self.props.items()]
        publish = ["-p", "127.0.0.1::8080"] if self.publish else []
        docker("run", "-d", "--name", self.name, "--network", NET, "--network-alias", self.alias,
               "--hostname", self.hostname, "--cpus", self.cpus, "-m", self.memory, *publish,
               IMAGE, f"--spring.profiles.active={self.profile}", *args)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{host_port(self.name)}{path}"

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.url("/actuator/health/readiness"), timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError, RuntimeError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError(f"{self.name} not ready")

    def redeploy(self, props: dict | None = None, hostname: str | None = None, cpus: str | None = None) -> None:
        """A rollout: the old pod goes away and a new pod (new container) starts with the new template."""
        docker("rm", "-f", self.name)
        if props is not None:
            self.props = props
        if hostname:
            self.hostname = hostname
        if cpus:
            self.cpus = cpus
        self.start()

    def metric(self, name: str) -> float | None:
        try:
            with urllib.request.urlopen(self.url("/actuator/prometheus"), timeout=3) as response:
                for line in response.read().decode().splitlines():
                    if line.startswith(name) and not line.startswith("#"):
                        return float(line.rsplit(" ", 1)[1])
        except (urllib.error.URLError, OSError, RuntimeError, ValueError):
            return None
        return None


# --- Kubernetes event log (emulated control loops write here) --------------------------------------------

class Events:
    """`kubectl get events` rows written by the emulated kubelet, controllers and Jobs."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.rows: list[dict] = []
        self.lock = threading.Lock()

    def add(self, kind: str, obj: str, type_: str, reason: str, message: str, source: str) -> None:
        with self.lock:
            for row in self.rows:
                if (row["object"], row["reason"], row["message"]) == (f"{kind}/{obj}", reason, message):
                    row["count"] += 1
                    row["last"] = stamp()
                    return
            self.rows.append({"first": stamp(), "last": stamp(), "type": type_, "reason": reason,
                              "object": f"{kind}/{obj}", "source": source, "message": message, "count": 1})

    def dump(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.rows]


# --- kubelet liveness probe -------------------------------------------------------------------------------

class LivenessProber(threading.Thread):
    """kubelet httpGet probe: period 10s, timeout 1s, failureThreshold 3; restart = SIGTERM, grace 30s."""

    def __init__(self, app: App, path: str, events: Events, pod_ip: str, period: float = 10, timeout: float = 1,
                 failures: int = 3):
        super().__init__(daemon=True)
        self.app, self.path, self.events, self.pod_ip = app, path, events, pod_ip
        self.period, self.timeout, self.failures = period, timeout, failures
        self.stop_flag = threading.Event()
        self.restarts = 0
        self.restart_times: list[str] = []

    def run(self) -> None:
        consecutive = 0
        while not self.stop_flag.wait(self.period):
            try:
                with urllib.request.urlopen(self.app.url(self.path), timeout=self.timeout) as response:
                    ok = 200 <= response.status < 400
                    detail = f"HTTP probe failed with statuscode: {response.status}"
            except urllib.error.HTTPError as error:
                ok, detail = False, f"HTTP probe failed with statuscode: {error.code}"
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                ok = False
                reason = str(getattr(error, "reason", error))
                detail = (f'Get "http://{self.pod_ip}:8080{self.path}": context deadline exceeded '
                          "(Client.Timeout exceeded while awaiting headers)") if "timed out" in reason else \
                         f'Get "http://{self.pod_ip}:8080{self.path}": dial tcp {self.pod_ip}:8080: connect: connection refused'
            except RuntimeError:
                ok, detail = False, f'Get "http://{self.pod_ip}:8080{self.path}": dial tcp {self.pod_ip}:8080: connect: connection refused'
            if ok:
                consecutive = 0
                continue
            consecutive += 1
            self.events.add("Pod", self.app.hostname, "Warning", "Unhealthy", f"Liveness probe failed: {detail}", "kubelet")
            if consecutive >= self.failures:
                self.events.add("Pod", self.app.hostname, "Normal", "Killing",
                                f"Container {self.app.alias} failed liveness probe, will be restarted", "kubelet")
                docker("stop", "-t", "30", self.app.name, check=False, timeout=60)
                docker("start", self.app.name, check=False)
                self.restarts += 1
                self.restart_times.append(stamp())
                self.events.add("Pod", self.app.hostname, "Normal", "Pulled",
                                f'Container image "registry.shop.internal/shopfront/{self.app.alias}:1.4.2" already present on machine',
                                "kubelet")
                self.events.add("Pod", self.app.hostname, "Normal", "Created", f"Created container: {self.app.alias}", "kubelet")
                self.events.add("Pod", self.app.hostname, "Normal", "Started", f"Started container {self.app.alias}", "kubelet")
                consecutive = 0
                self.stop_flag.wait(20)  # initialDelaySeconds

    def stop(self) -> None:
        self.stop_flag.set()


# --- Load generator ---------------------------------------------------------------------------------------

class Load(threading.Thread):
    """Open-loop order traffic. `mix` is a list of (rate_per_second, channel, skus)."""

    def __init__(self, order: App, mix: list[tuple[float, str, list[str]]], out: Path, timeout: float = 10):
        super().__init__(daemon=True)
        self.order, self.mix, self.out, self.timeout = order, mix, out, timeout
        self.stop_flag = threading.Event()
        self.rows: list[tuple] = []
        self.lock = threading.Lock()
        self.base = None

    def shoot(self, channel: str, sku: str) -> None:
        started = time.time()
        status = 0
        try:
            request = urllib.request.Request(f"{self.base}/api/orders?sku={sku}&channel={channel}", method="POST")
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except (urllib.error.URLError, OSError, TimeoutError):
            status = -1
        with self.lock:
            self.rows.append((round(started, 3), channel, sku, status, round(time.time() - started, 3)))

    def run(self) -> None:
        self.base = f"http://127.0.0.1:{host_port(self.order.name)}"
        rng = random.Random(7)
        next_at = {i: time.monotonic() for i in range(len(self.mix))}
        while not self.stop_flag.is_set():
            current = time.monotonic()
            for i, (rate, channel, skus) in enumerate(self.mix):
                if rate > 0 and current >= next_at[i]:
                    threading.Thread(target=self.shoot, args=(channel, rng.choice(skus)), daemon=True).start()
                    next_at[i] = current + rng.expovariate(rate)
            time.sleep(0.01)

    def set_mix(self, mix: list[tuple[float, str, list[str]]]) -> None:
        self.mix = mix

    def stop(self) -> None:
        self.stop_flag.set()
        time.sleep(self.timeout + 1)
        with self.out.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("t", "channel", "sku", "status", "seconds"))
            with self.lock:
                writer.writerows(sorted(self.rows))

    def window(self, since: float, until: float | None = None) -> dict:
        with self.lock:
            rows = [r for r in self.rows if r[0] >= since and (until is None or r[0] < until)]
        failed = [r for r in rows if r[3] not in (201, 202, 409)]
        return {"requests": len(rows), "failed": len(failed),
                "error_rate": round(len(failed) / len(rows), 4) if rows else None}


# --- Log collection -------------------------------------------------------------------------------------

def all_logs(name: str) -> str:
    """Every line the container wrote since creation (across restarts), in write order, with runtime stamps."""
    result = subprocess.run(["docker", "logs", "-t", name], text=True, capture_output=True, timeout=60)
    return result.stdout + result.stderr


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
