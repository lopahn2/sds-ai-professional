"""Use real K3s kubelet processes to verify CRI negotiation and recovery on one offline node."""
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4


CASE = "KI-04"
KUBERNETES = "v1.34.13+k3s1"
NAMESPACE = "dev-ki04-verify"
NODE = "ki04-fixture"
POD = "ki04-http"
WORK = Path("/tmp/lp-ki04-k3s")
K3S = "/opt/isolated/k3s-arm64"
OLD = WORK / "legacy-bin/containerd"
OLD_CLIENT = WORK / "legacy-bin/crictl"
OLD_SOCKET = WORK / "old-containerd.sock"
NEW_SOCKET = Path("/run/k3s/containerd/containerd.sock")
LEGACY_VERSION = "v1.5.8-k3s2"
LEGACY_SHA = "5cf007c9a23c6a8bc4cb8934be41d3672318f91567b79ce982b2dd9dfce64ec6"
MODERN_VERSION = "v2.2.10-k3s1"
CLIENT_CONFIG = WORK / "cri-client.yaml"
IMAGE = "docker.io/library/busybox:1.37"
BODY = "ki04-cri-http-ok\n"
PORT = 18080
OBSERVER_SOCKET = Path("/tmp/lp-ki04-observer.sock")


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(*args, data=None, timeout=12):
    env = {**os.environ, "K3S_DATA_DIR": str(WORK / "state")}
    return subprocess.run(args, input=json.dumps(data) if data is not None else None,
                          env=env, capture_output=True, text=True, timeout=timeout)


def kube(*args, data=None, timeout=13):
    result = command(K3S, "kubectl", "--kubeconfig", str(WORK / "kubeconfig"), "--request-timeout=8s",
                     *args, data=data, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"kubernetes {args[0]}: {result.stderr[:400]}")
    return result.stdout


def put(obj):
    return json.loads(kube("create", "-f", "-", "-o", "json", data=obj))


def pod():
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": POD, "namespace": NAMESPACE},
            "spec": {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 0,
                "restartPolicy": "Always", "nodeSelector": {"liveprobe.example/fixture": "ki04"},
                "containers": [{"name": "http", "image": IMAGE, "imagePullPolicy": "Never",
                    "command": ["/bin/busybox", "httpd", "-f", "-p", str(PORT), "-h", "/data"],
                    "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                  "limits": {"cpu": "100m", "memory": "32Mi"}},
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                        "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "seccompProfile": {"type": "RuntimeDefault"}, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "body", "mountPath": "/data", "readOnly": True}]}],
                "volumes": [{"name": "body", "configMap": {"name": POD}}]}}


def http(address):
    token = uuid4().hex
    try:
        with urlopen(f"http://{address}:{PORT}/check?nonce={token}", timeout=1.3) as response:
            return {"status": response.status, "body": response.read(128).decode(), "nonce": token}
    except (URLError, OSError):
        return {"status": None, "body": None, "nonce": token}


def wait_for(observe, predicate, label, timeout):
    deadline = monotonic() + timeout
    while True:
        value = observe()
        if predicate(value):
            return value
        if monotonic() > deadline:
            raise RuntimeError(f"{label}: {json.dumps(value, default=str)[:1500]}")
        sleep(0.65)


def finish_process(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


def condition(object_, kind):
    return next((c for c in (object_.get("status", {}).get("conditions") or []) if c["type"] == kind), {})


def runtime_api(legacy):
    executable, socket = (([str(OLD_CLIENT)], OLD_SOCKET) if legacy else
                          ([K3S, "crictl", "--config", str(CLIENT_CONFIG)], NEW_SOCKET))
    try:
        result = command(*executable, "--runtime-endpoint", f"unix://{socket}", "version", timeout=7)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    fields = {key.strip(): value.strip() for line in result.stdout.splitlines() if ":" in line
              for key, value in (line.split(":", 1),)}
    if not all(fields.get(name) for name in ("RuntimeName", "RuntimeVersion", "RuntimeApiVersion")):
        raise RuntimeError("CRI client did not return native version fields")
    return {"runtimeName": fields["RuntimeName"], "runtimeVersion": fields["RuntimeVersion"],
            "runtimeApiVersion": fields["RuntimeApiVersion"]}


def node():
    result = kube("get", "node", NODE, "--ignore-not-found", "-o", "json")
    if not result:
        return None
    info = json.loads(result)
    assert info["metadata"]["labels"]["liveprobe.example/fixture"] == "ki04"
    return {"uid": info["metadata"]["uid"], "name": NODE,
            "ready": condition(info, "Ready").get("status"),
            "runtime": info["status"].get("nodeInfo", {}).get("containerRuntimeVersion"),
            "kubelet": info["status"].get("nodeInfo", {}).get("kubeletVersion")}


def legacy_error(line):
    # Keep the agent's verbatim CRI failure and trim the generated logging prefix.
    index = line.find("Waiting for CRI startup:")
    if index < 0:
        index = line.find("CRI v1 runtime API")
    if index < 0 or "unknown service runtime.v1.RuntimeService" not in line:
        return None
    return line[index:].rstrip('"').strip()


class Trial:
    def __init__(self):
        self.legacy = None
        self.agent = None
        self.created = False
        self.source = "legacy"
        self.sequence = uuid4().hex[:10]
        self.log_dir = WORK / f"trial-{self.sequence}"
        self.log_dir.mkdir(mode=0o700)

    def spawn(self, name, args, data_dir):
        output = self.log_dir / f"{name}.log"
        with output.open("x") as pipe:
            proc = subprocess.Popen(args, env={**os.environ, "K3S_DATA_DIR": str(data_dir), "GOMAXPROCS": "2"},
                                    stdin=subprocess.DEVNULL, stdout=pipe, stderr=subprocess.STDOUT)
        return proc, output

    def setup(self):
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == KUBERNETES
        assert not subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).strip()
        assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == "" and node() is None
        assert not OLD_SOCKET.exists()
        assert sha256(OLD.read_bytes()).hexdigest() == LEGACY_SHA
        CLIENT_CONFIG.write_text("{}\n")
        config = self.log_dir / "containerd.toml"
        config.write_text(f'version = 2\nroot = "{self.log_dir}/legacy-root"\nstate = "{self.log_dir}/legacy-state"\n'
                          f'[grpc]\n  address = "{OLD_SOCKET}"\n'
                          '[plugins."io.containerd.grpc.v1.cri".containerd]\n  snapshotter = "native"\n')
        self.legacy, self.legacy_log = self.spawn("legacy-runtime", [str(OLD), "--config", str(config)], WORK)
        self.legacy_version = wait_for(lambda: runtime_api(True), bool, "legacy CRI socket did not answer", 35)
        assert self.legacy_version.get("runtimeApiVersion") == "v1alpha2"
        assert self.legacy_version.get("runtimeVersion", "").removeprefix("v") == LEGACY_VERSION.removeprefix("v")
        self.contract = {"kubernetes": KUBERNETES, "node": NODE, "pod": pod(), "probe_response": BODY,
                         "legacy": self.legacy_version, "legacy_binary": LEGACY_SHA,
                         "modern_runtime": MODERN_VERSION}
        put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
        self.created = True
        put({"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": POD, "namespace": NAMESPACE}, "data": {"check": BODY}})
        self.pod_uid = put(pod())["metadata"]["uid"]

    def agent_command(self):
        args = [K3S, "agent", "--data-dir", str(WORK / "agent-state"),
                "--server", "https://192.0.2.10:6443", "--token-file", str(WORK / "state/server/node-token"),
                "--node-name", NODE, "--node-ip", "192.0.2.10", "--lb-server-port", "17444", "--snapshotter", "native",
                "--node-label", "liveprobe.example/fixture=ki04", "--kubelet-arg", "cgroup-driver=cgroupfs"]
        if self.source == "legacy":
            args += ["--container-runtime-endpoint", f"unix://{OLD_SOCKET}"]
        return args

    def start_agent(self):
        self.agent, self.agent_log = self.spawn(f"{self.source}-agent", self.agent_command(), WORK / "agent-state")

    def first_line(self):
        for line in self.agent_log.read_text().splitlines():
            error = legacy_error(line)
            if error:
                return error
        if self.agent.poll() is not None:
            raise RuntimeError(f"kubelet exited before CRI probe: {self.agent_log.read_text().splitlines()[-3:]}")
        return None

    def observe(self):
        current = json.loads(kube("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))
        status, info = current["status"], node()
        address = status.get("podIP")
        is_ready = condition(current, "Ready").get("status") == "True" and status.get("phase") == "Running"
        legacy = runtime_api(True) if self.source == "legacy" else self.legacy_version
        refused = (sum(bool(legacy_error(line)) for line in self.agent_log.read_text().splitlines())
                   if self.source == "legacy" else None)
        return {"at": now(), "source": self.source, "node": info, "pod_uid": current["metadata"]["uid"],
                "pod_node": current["spec"].get("nodeName"), "pod_ip": address, "pod_ready": is_ready,
                "pod_phase": status.get("phase"), "http": http(address) if is_ready and address else None,
                "legacy_runtime": legacy, "agent_cri_refusals": refused,
                "active_runtime": runtime_api(False) if self.source == "bundled" else None}

    @staticmethod
    def blocked(sample):
        return (sample["source"] == "legacy" and sample["agent_cri_refusals"] and sample["legacy_runtime"]
                and sample["legacy_runtime"]["runtimeApiVersion"] == "v1alpha2"
                and sample["node"] is None and sample["pod_phase"] == "Pending"
                and not sample["pod_ready"] and not sample["pod_node"] and not sample["pod_ip"])

    @staticmethod
    def recovered(sample):
        info, runtime, request = sample["node"], sample["active_runtime"], sample["http"]
        return (sample["source"] == "bundled" and info and info["ready"] == "True"
                and info["name"] == sample["pod_node"] == NODE and info["kubelet"] == KUBERNETES
                and info["runtime"] == f"containerd://{MODERN_VERSION.removeprefix('v')}"
                and runtime and runtime["runtimeApiVersion"] == "v1"
                and runtime["runtimeVersion"].removeprefix("v") == MODERN_VERSION.removeprefix("v")
                and sample["pod_ready"] and sample["pod_phase"] == "Running" and sample["pod_ip"]
                and request and request["status"] == 200 and request["body"] == BODY)

    def prepare(self):
        self.start_agent()
        first = wait_for(self.first_line, bool, "K3s agent produced no native CRI negotiation error", 50)
        self.started = monotonic()
        attempted = command(K3S, "crictl", "--config", str(CLIENT_CONFIG), "--runtime-endpoint",
                            f"unix://{OLD_SOCKET}", "version", timeout=12)
        if attempted.returncode == 0 or "unknown service runtime.v1.RuntimeService" not in attempted.stderr:
            raise RuntimeError(f"CRI v1 client's refusal did not match: {attempted.stderr[-550:]}")
        failure = wait_for(self.observe, self.blocked, "the node or Pod did not show the matching native fault", 20)
        return {"case": CASE, "kubernetes": KUBERNETES, "fixture_contract": self.contract,
                "fixture_sha256": fingerprint([CASE, self.contract]), "initial_error_log": first,
                "fault_before": failure, "agent_command_flags": {"external_runtime_endpoint": f"unix://{OLD_SOCKET}"},
                "direct_cri_v1_refusal": {"exit_code": attempted.returncode,
                                           "error": "unknown service runtime.v1.RuntimeService"}}

    def restore(self):
        if self.source != "legacy":
            return {"applied": False, "reason": "the bundled runtime is already selected"}
        finish_process(self.agent)
        self.source = "bundled"
        self.start_agent()
        return {"applied": True, "before": "legacy", "after": "bundled",
                "node": NODE, "change": "container-runtime-endpoint"}

    def verify(self):
        predicate = self.recovered if self.source == "bundled" else self.blocked
        first = wait_for(self.observe, predicate, "first independent workload check", 95)
        sleep(1.7)
        second = wait_for(self.observe, predicate, "second independent workload check", 12)
        assert {first["pod_uid"], second["pod_uid"]} == {self.pod_uid}
        return {"independent_probes": [first, second], "wall_duration_ms": round((monotonic()-self.started)*1000)}

    def cleanup(self):
        try:
            if self.created:
                kube("delete", "namespace", NAMESPACE, "--timeout=40s", timeout=44)
                assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == ""
                self.created = False
        finally:
            finish_process(self.agent)
            self.agent = None
            finish_process(self.legacy)
            self.legacy = None
        if node() is not None:
            kube("delete", "node", NODE, "--timeout=15s", timeout=20)
            assert node() is None
        OLD_SOCKET.unlink(missing_ok=True)
        return {"stopped": True, "namespace_removed": True, "node_removed": True, "legacy_socket_removed": True}

    def handle(self, record):
        tool = record["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool == "workload":
            return {"kubernetes": KUBERNETES, "node": NODE, "service": self.observe()}
        if tool == "logs":
            return {"kubelet": self.first_line() if self.source == "legacy" else None}
        if tool == "source":
            return {"setting": "runtime", "selected": self.source, "choices": ["legacy", "bundled"],
                    "legacy_api": self.legacy_version, "bundled_release": MODERN_VERSION}
        if tool == "probe":
            return self.observe()
        if tool == "configure":
            if (record.get("key"), record.get("value")) != ("runtime", "bundled"):
                return {"applied": False, "reason": "only the bundled runtime may be selected for this test kubelet"}
            return self.restore()
        if tool == "verify":
            return self.verify()
        if tool == "shutdown":
            return self.cleanup()
        raise ValueError("unknown trial operation")


def main():
    trial = Trial()
    observer = None
    if sys.argv[1:] == ["--observe"]:
        spec = importlib.util.spec_from_file_location("observer", WORK / "observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        observer.NAMESPACE, observer.TARGET = NAMESPACE, POD
        observer.kubeconfig = WORK / "kubeconfig"
        observer.OBSERVER_SOCKET = OBSERVER_SOCKET
    try:
        trial.setup()
        with (observer.observer_transport() if observer else nullcontext()):
            for raw in sys.stdin:
                request = json.loads(raw)
                tool = request["tool"]
                if observer and tool == "access":
                    result = observer.read_only_observer()
                elif observer and tool == "sentinel":
                    result = observer.sentinel(request["phase"])
                else:
                    result = trial.handle(request)
                print(json.dumps(result), flush=True)
                if tool == "shutdown":
                    break
    finally:
        trial.cleanup()


if __name__ == "__main__":
    main()
