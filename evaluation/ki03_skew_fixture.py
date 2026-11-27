"""Exercise native kubeadm version checks and real workload readiness in one disposable VM."""
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


CASE = "KI-03"
CONTROL_PLANE = "v1.35.1+k3s1"
KUBEADM = "/opt/isolated/kubeadm"
KUBEADM_SHA = "80097a3c4ef824f4edfe131d2bd429772c4be3a460c42a44f2320164a917de32"
RELEASES = {
    "v1.31.0": ("old", "b310da449a9d2f8b928cab5ca12a6772617ba421023894e061ca2647e6d9f1c3"),
    "v1.32.0": ("ready", "bda9b2324c96693b38c41ecea051bab4c7c434be5683050b5e19025b50dbc0bf"),
}
OLD, READY = RELEASES
ROOT = Path("/tmp/lp-ki03-k3s")
K3S = "/opt/isolated/k3s-arm64"
NODE, NAMESPACE, POD = "ki03-fixture", "dev-ki03-verify", "ki03-http"
BODY = "ki03-kubelet-http-ok\n"
RUNTIME = {"api_version": "v1", "version": "v2.1.5-k3s1"}
FAILURE = ('[ERROR KubeletVersion]: Kubelet version "1.31.0" is lower than kubeadm can support. '
           'Please upgrade kubelet')
OBSERVER_SOCKET = Path("/tmp/lp-ki03-observer.sock")
SHUTDOWN = {"node_removed": True, "namespace_removed": True, "sandbox_removed": True}


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(*args, data=None, version=None, timeout=15):
    path = str(kubelet(version).parent) if version else str(ROOT / "state/data/current/bin")
    return subprocess.run(args, input=json.dumps(data) if data is not None else None,
                          env={**os.environ, "PATH": f"{path}:{os.environ['PATH']}",
                               "K3S_DATA_DIR": str(ROOT / "state")}, timeout=timeout,
                          capture_output=True, text=True)


def kubelet(version):
    return Path("/opt/isolated/by-version") / RELEASES[version][0] / "kubelet"


def checked(result, action):
    if result.returncode:
        raise RuntimeError(f"{action}: {result.stderr[-750:]}")
    return result.stdout


def kube(*args, data=None, timeout=14):
    result = command(K3S, "kubectl", "--request-timeout=9s", "--kubeconfig", str(ROOT / "kubeconfig"),
                     *args, data=data, timeout=timeout)
    return checked(result, args[0])


def cri(*args, timeout=14):
    result = command(K3S, "crictl", "--config", str(ROOT / "cri-client.yaml"),
                     "--runtime-endpoint", f"unix://{ROOT}/cri.sock", *args, timeout=timeout)
    return checked(result, args[0])


def runtime():
    fields = dict(tuple(part.strip() for part in line.split(":", 1)) for line in cri("version").splitlines()
                  if ":" in line)
    return {"api_version": fields["RuntimeApiVersion"], "version": fields["RuntimeVersion"]}


def delete_sandboxes():
    def owned():
        return json.loads(cri("pods", "--namespace", NAMESPACE, "-o", "json")).get("items") or []

    for sandbox in owned():
        metadata = sandbox["metadata"]
        assert (metadata["namespace"], metadata["name"]) == (NAMESPACE, POD)
        cri("stopp", sandbox["id"])
        cri("rmp", sandbox["id"])
    assert not owned()


def pod():
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": POD, "namespace": NAMESPACE},
            "spec": {"nodeSelector": {"liveprobe.example/fixture": "ki03"},
                     "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 0,
                     "containers": [{"name": "http", "image": "docker.io/library/busybox:1.37",
                                     "imagePullPolicy": "Never",
                                     "command": ["/bin/busybox", "httpd", "-f", "-p", "18080", "-h", "/data"],
                                     "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                                   "limits": {"cpu": "100m", "memory": "32Mi"}},
                                     "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                         "allowPrivilegeEscalation": False,
                                                         "readOnlyRootFilesystem": True,
                                                         "capabilities": {"drop": ["ALL"]},
                                                         "seccompProfile": {"type": "RuntimeDefault"}},
                                     "volumeMounts": [{"name": "body", "mountPath": "/data", "readOnly": True}]}],
                     "volumes": [{"name": "body", "configMap": {"name": POD}}]}}


def ready_status(obj):
    return next((entry["status"] == "True" for entry in obj["status"].get("conditions", [])
                 if entry["type"] == "Ready"), False)


def node():
    result = kube("get", "node", NODE, "--ignore-not-found", "-o", "json")
    if not result:
        return None
    obj = json.loads(result)
    assert obj["metadata"]["labels"]["liveprobe.example/fixture"] == "ki03"
    return {"uid": obj["metadata"]["uid"], "name": NODE, "ready": ready_status(obj),
            "kubelet": obj["status"]["nodeInfo"]["kubeletVersion"]}


def http(address):
    nonce = uuid4().hex
    try:
        with urlopen(f"http://{address}:18080/check?nonce={nonce}", timeout=1.2) as response:
            return {"status": response.status, "body": response.read(128).decode(), "nonce": nonce}
    except (URLError, OSError):
        return {"status": None, "body": None, "nonce": nonce}


def blocked(state):
    return (state["version"] == OLD and state["node"] is None and state["phase"] == "Pending"
            and not any(state[key] for key in ("pod_node", "pod_ip", "pod_ready", "http")))


def recovered(state):
    observed, response = state["node"], state["http"]
    return (state["version"] == READY and observed and observed["ready"] and observed["kubelet"] == READY
            and state["pod_node"] == NODE and state["phase"] == "Running" and state["pod_ip"]
            and state["pod_ready"] and response and (response["status"], response["body"]) == (200, BODY))


class Trial:
    def __init__(self):
        self.version = OLD
        self.pid = None
        self.created = False
        self.preflights = 0
        self.log = ROOT / f"trial-{uuid4().hex[:10]}.log"

    def setup(self):
        assert not checked(command("ip", "-4", "route", "show", "default"), "read VM routes").strip()
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == CONTROL_PLANE
        assert sha256(Path(KUBEADM).read_bytes()).hexdigest() == KUBEADM_SHA
        assert checked(command(KUBEADM, "version", "-o=short"), "kubeadm version").strip() == "v1.35.1"
        for version, (_, digest) in RELEASES.items():
            binary = kubelet(version)
            assert sha256(binary.read_bytes()).hexdigest() == digest
            assert checked(command(str(binary), "--version"), "kubelet version").strip() == f"Kubernetes {version}"
        assert not kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") and node() is None
        delete_sandboxes()
        self.contract = {"control_plane": CONTROL_PLANE, "kubeadm_sha": KUBEADM_SHA,
                         "kubelet_sha": {version: data[1] for version, data in RELEASES.items()},
                         "kubelet_config_sha": sha256((ROOT / "kubelet.json").read_bytes()).hexdigest(),
                         "join_config_sha": sha256((ROOT / "join.yaml").read_bytes()).hexdigest(),
                         "runtime": runtime(), "pod": pod(), "http_body": BODY}
        assert self.contract["runtime"] == RUNTIME
        kube("create", "namespace", NAMESPACE)
        self.created = True
        kube("-n", NAMESPACE, "create", "configmap", POD, f"--from-literal=check={BODY}")
        self.pod_uid = json.loads(kube("create", "-f", "-", "-o", "json", data=pod()))["metadata"]["uid"]

    def preflight(self, version):
        self.preflights += 1
        result = command(KUBEADM, "join", "phase", "preflight", "--config", str(ROOT / "join.yaml"),
                         version=version, timeout=22)
        errors = [line.strip() for line in result.stderr.splitlines() if line.lstrip().startswith("[ERROR ")]
        fetched = 'Reading configuration from the "kubeadm-config" ConfigMap' in result.stdout
        expected = (1, [FAILURE], False) if version == OLD else (0, [], True)
        if (result.returncode, errors, fetched) != expected:
            raise RuntimeError(f"kubeadm {version}: {result.stderr[-1000:]}")
        return {"version": version, "exit": result.returncode, "errors": errors,
                "fetched_cluster_config": fetched, "attempt": self.preflights}

    def observe(self):
        if self.pid and self.pid.poll() is not None:
            raise RuntimeError(f"kubelet exited: {self.log.read_text().splitlines()[-6:]}")
        current = json.loads(kube("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))
        address = current["status"].get("podIP")
        result = {"at": datetime.now(timezone.utc).isoformat(), "version": self.version,
                  "node": node(), "pod_uid": current["metadata"]["uid"],
                  "pod_node": current["spec"].get("nodeName"), "pod_ip": address,
                  "pod_ready": ready_status(current), "phase": current["status"]["phase"],
                  "runtime": runtime(), "http": http(address) if address and ready_status(current) else None}
        assert result["pod_uid"] == self.pod_uid
        return result

    def wait(self, predicate, timeout):
        deadline = monotonic() + timeout
        while True:
            result = self.observe()
            if predicate(result):
                return result
            if monotonic() >= deadline:
                raise RuntimeError(f"node or workload did not converge: {result}")
            sleep(0.65)

    def prepare(self):
        self.first = self.preflight(OLD)
        self.started = monotonic()
        return {"case": CASE, "kubernetes": CONTROL_PLANE, "fixture_contract": self.contract,
                "fixture_sha256": fingerprint([CASE, self.contract]), "initial_error_log": FAILURE,
                "fault_before": {**self.wait(blocked, 12), "preflight": self.first}}

    def restore(self):
        if self.version == READY:
            return {"applied": False, "reason": "selected kubelet is already running"}
        self.accepted = self.preflight(READY)
        with self.log.open("x") as out:
            self.pid = subprocess.Popen(
                [str(kubelet(READY)), "--config", str(ROOT / "kubelet.json"),
                 "--kubeconfig", str(ROOT / "node-auth/kubelet.conf"), "--root-dir", str(ROOT / "kubelet-root"),
                 "--hostname-override", NODE, "--node-ip", "192.0.2.10", "--node-labels",
                 "liveprobe.example/fixture=ki03"],
                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env={**os.environ, "GOMAXPROCS": "2", "PATH": f"{ROOT}/state/data/current/bin:{os.environ['PATH']}"})
        self.version = READY
        return {"applied": True, "before": OLD, "after": READY, "node": NODE, "preflight": self.accepted}

    def verify(self):
        if self.version == OLD:
            samples = []
            for _ in range(2):
                result = self.preflight(OLD)
                samples.append({**self.wait(blocked, 12), "preflight": result})
        else:
            first = self.wait(recovered, 60)
            sleep(1.5)
            second = self.wait(recovered, 10)
            samples = [{**sample, "preflight": self.accepted} for sample in (first, second)]
        return {"independent_probes": samples, "wall_duration_ms": round((monotonic() - self.started) * 1000)}

    def cleanup(self):
        try:
            if self.created:
                kube("delete", "namespace", NAMESPACE, "--timeout=35s", timeout=39)
                self.created = False
            delete_sandboxes()
        finally:
            if self.pid and self.pid.poll() is None:
                self.pid.terminate()
                try:
                    self.pid.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    self.pid.kill()
                    self.pid.wait(timeout=5)
            self.pid = None
        if node():
            kube("delete", "node", NODE, "--timeout=12s")
        assert node() is None and not kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name")
        return SHUTDOWN

    def handle(self, request):
        tool = request["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool in {"workload", "probe"}:
            return {"control_plane": CONTROL_PLANE, **self.observe()}
        if tool == "logs":
            return self.first
        if tool == "source":
            return {"node": NODE, "selected": self.version, "versions_available": list(RELEASES),
                    "runtime": self.contract["runtime"]}
        if tool == "configure":
            if (request.get("key"), request.get("value")) == ("kubelet_version", READY):
                return self.restore()
            return {"applied": False, "reason": "only the tested kubelet release may be selected"}
        if tool == "verify":
            return self.verify()
        if tool == "shutdown":
            return self.cleanup()
        raise ValueError("unknown trial operation")


def main():
    trial = Trial()
    observer = None
    if sys.argv[1:] == ["--observe"]:
        spec = importlib.util.spec_from_file_location("observer", ROOT / "observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        observer.NAMESPACE, observer.TARGET = NAMESPACE, POD
        observer.kubeconfig = ROOT / "kubeconfig"
        observer.OBSERVER_SOCKET = OBSERVER_SOCKET
    try:
        trial.setup()
        with (observer.observer_transport() if observer else nullcontext()):
            for line in sys.stdin:
                request = json.loads(line)
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
