"""Observe the same reservation load through the dev Kubernetes Service proxy."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import cycle
import json
import re
import selectors
from subprocess import PIPE, Popen, TimeoutExpired
from threading import Event, Thread
from time import monotonic, sleep
from urllib.error import URLError
from urllib.parse import urlencode

from app.redact import redact
from evaluation import k8s_dev_image_trial as cluster
from evaluation.dev_image_case import DevImageCase
from evaluation.jdbc_dev_manifest import POOL_BROKEN, POOL_CONFIG, POOL_IMAGE, POOL_NAME, POOL_READY, POOL_VERSION
from evaluation.verify_jdbc_fixture import DATA, POOL, REQUEST_INTERVAL_S, get, invoke


CASE = DevImageCase("K8S-POOL-CONFIG", POOL_NAME, POOL_IMAGE)
SIGNAL = "TargetConnectionPoolExhausted"
RESTART = "kubectl.kubernetes.io/restartedAt"
CONTAINER_ID = "sha256:19f8da9a3bac6147150a4175310b17b859aa37d3a7714518ddfcea1f05b69f10"
ACQUISITION = re.compile(r"java\.sql\.SQLTransientConnectionException: reservation - Connection is not available,"
                         r" request timed out after \d+ms \(total=1, active=1, idle=0, waiting=\d+\)")
CONNECTED_POD = re.compile(r"Starting to serve on 127\.0\.0\.1:([1-9][0-9]+)")


@contextmanager
def service_proxy():
    with Popen([*cluster.kube_prefix(), "proxy", "--address=127.0.0.1", "--port=0"],
               stdout=PIPE, stderr=PIPE, text=True) as process:
        try:
            with selectors.DefaultSelector() as waiting:
                waiting.register(process.stdout, selectors.EVENT_READ)
                if not waiting.select(timeout=8):
                    raise cluster.Refused("the local Kubernetes Service proxy did not start")
            bound = CONNECTED_POD.fullmatch(process.stdout.readline().strip())
            if not bound:
                raise cluster.Refused("the local Kubernetes Service proxy did not bind loopback")
            yield f"http://127.0.0.1:{bound[1]}/api/v1/namespaces/dev/services/{POOL_NAME}:8080/proxy"
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except TimeoutExpired:
                    process.kill()


def observe_metrics(origin):
    status, body = get(origin, "/actuator/prometheus")
    values = {name: float(value) for name, value in POOL.findall(body.decode())}
    if status != 200 or set(values) != {"active", "pending", "max"}:
        raise cluster.Refused("the exact Hikari pool metrics are unavailable")
    return values


def exercise(origin, *, rounds=3):
    with ThreadPoolExecutor(max_workers=6) as requests:
        began = monotonic()
        pending, snapshots = [], []
        for index, row in enumerate(DATA * rounds):
            sleep(max(0, began + index * REQUEST_INTERVAL_S - monotonic()))
            pending.append(requests.submit(invoke, origin, row, "partner"))
            if index in (3, 6):
                snapshots.append(requests.submit(observe_metrics, origin))
        result = [future.result() for future in pending]
        return {"requests": result, "under_load": [future.result() for future in snapshots]}


class SustainedTraffic:
    def __init__(self, origin):
        self.origin = origin
        self.pause = Event()
        self.dropped = 0
        self.sent = 0
        self.failures = 0
        self.thread = Thread(target=self._dispatch, name="pool-load")

    def start(self):
        self.thread.start()

    def stop(self):
        self.pause.set()
        self.thread.join(timeout=8)
        if self.thread.is_alive():
            raise cluster.Refused("the reservation load generator did not stop")

    def _dispatch(self):
        with ThreadPoolExecutor(max_workers=8) as requests:
            in_flight = []
            scheduled = monotonic()
            for row in cycle(DATA):
                if self.pause.wait(max(0, scheduled - monotonic())):
                    return
                for future in in_flight[:]:
                    if future.done():
                        in_flight.remove(future)
                        if future.exception() is not None:
                            self.failures += 1
                if len(in_flight) == 8:
                    self.dropped += 1
                else:
                    in_flight.append(requests.submit(invoke, self.origin, row, "partner"))
                    self.sent += 1
                scheduled += REQUEST_INTERVAL_S

    def evidence(self):
        return {"sent": self.sent, "dropped": self.dropped, "transport_errors": self.failures,
                "interval_ms": round(REQUEST_INTERVAL_S*1000)}


def live_state():
    config = cluster.get("configmap", POOL_CONFIG)
    source = cluster.get("configmap", POOL_VERSION)
    deployment = cluster.get("deployment", POOL_NAME)
    if source.get("immutable") is not True or source.get("data") != POOL_READY or config.get("data") not in (POOL_READY, POOL_BROKEN):
        raise cluster.Refused("the expected ConfigMap versions are unavailable")
    return deployment, config, source


def assert_identity(deployment, config, baseline):
    expected_dep, expected_config = baseline
    if (deployment["metadata"]["uid"], config["metadata"]["uid"]) != (
        expected_dep["metadata"]["uid"], expected_config["metadata"]["uid"]
    ):
        raise cluster.Refused("pool fixture identity changed")
    template = deployment["spec"]["template"]
    expected = expected_dep["spec"]["template"]
    if template["spec"] != expected["spec"] or deployment["spec"]["replicas"] != 1:
        raise cluster.Refused("pool fixture configuration changed outside the restart annotation")


def change_pool(baseline, original, updated):
    deployment, config, _ = live_state()
    assert_identity(deployment, config, baseline)
    if config["data"] != original:
        raise cluster.Refused("the ConfigMap changed before the requested write")
    metadata = config["metadata"]
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/data", "value": original},
        {"op": "replace", "path": "/data", "value": updated},
    ]
    cluster.command("patch", "configmap", POOL_CONFIG, "--type=json", "-p", json.dumps(ops))
    restart_consumer(baseline)


def restart_consumer(baseline):
    deployment, config, _ = live_state()
    assert_identity(deployment, config, baseline)
    metadata = deployment["metadata"]
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "add", "path": "/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt", "value": stamp},
    ]
    cluster.command("patch", "deployment", POOL_NAME, "--type=json", "-p", json.dumps(ops))


def wait_ready(origin, capacity, deadline):
    while monotonic() < deadline:
        try:
            deployment = cluster.get("deployment", POOL_NAME)
            # The configured readiness group excludes the database. Initialize Hikari
            # through the same read-only control request on every new Pod.
            if (cluster.is_settled(deployment) and invoke(origin, DATA[0], "standard")["contract_ok"]
                    and observe_metrics(origin)["max"] == capacity):
                pod_uid = cluster.ready_endpoints(CASE)
                pods = cluster.get("pods", selector=f"app={POOL_NAME}")["items"]
                pod, = [row for row in pods if row["metadata"]["uid"] == pod_uid]
                image_id = pod["status"]["containerStatuses"][0]["imageID"].rsplit("@", 1)[-1].removeprefix("docker://")
                if image_id != CONTAINER_ID:
                    raise ValueError("the ready pool container does not use the sealed image")
                return {"pod": pod["metadata"]["name"], "pod_uid": pod_uid,
                        "generation": deployment["metadata"]["generation"], "capacity": capacity, "image_id": image_id}
        except (cluster.Refused, URLError, TimeoutExpired):
            pass
        sleep(1)
    raise cluster.Refused(f"ready capacity {capacity} not observed in the service")


def actual_error(pod):
    logs = cluster.command("logs", pod, "-c", POOL_NAME, "--tail=200")
    for line in logs.splitlines():
        if ACQUISITION.search(line):
            return redact(line)
    raise cluster.Refused("the expected JDBC acquisition error did not appear in the active Pod")


def confirmed_health(origin, deadline):
    control = wait_ready(origin, 4, deadline)
    evidence = exercise(origin)
    sleep(1.2)
    idle = observe_metrics(origin)
    if not all(row["contract_ok"] for row in evidence["requests"]) or idle != {"active": 0, "pending": 0, "max": 4}:
        raise cluster.Refused("the same reservation requests and idle pool were not verified")
    return {"control": control, **evidence, "idle": idle}


def prom_utilization():
    path = "/api/v1/namespaces/liveprobe-observability/services/prometheus:9090/proxy/api/v1/query?"
    query = f'liveprobe:hikaricp_connections:utilization{{namespace="dev",app="{POOL_NAME}",pool="reservation"}}'
    payload = json.loads(cluster.command("get", "--raw", path + urlencode({"query": query})))
    if payload.get("status") != "success":
        raise cluster.Refused("the pool utilization query failed")
    return [float(row["value"][1]) for row in payload["data"]["result"]]
