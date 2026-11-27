"""Observe Kubernetes migrating a legacy EBS PV to the node CSI mount path."""
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


CASE = "KI-11"
KUBERNETES = "v1.35.1+k3s1"
ROOT = Path("/tmp/lp-ki11-k3s")
DRIVER = "ebs.csi.aws.com"
VOLUME_ID = "vol-0000c51f17e000011"
LEGACY_ID = f"aws://us-east-1a/{VOLUME_ID}"
K3S = "/opt/isolated/k3s-arm64"
NODE, NAMESPACE, WORKLOAD = "ki11-fixture", "dev-ki11-verify", "ki11-volume-http"
PV, PVC, VOLUME = "ki11-legacy-pv", "ki11-legacy-pvc", "existing"
DATA = ROOT / "csi-data"
SEED = DATA / VOLUME_ID / "check"
BODY = "ki11-existing-volume-data\n"
KUBELET_ROOT = ROOT / "kubelet-root"
ENDPOINT = ROOT / "csi.sock"
REG_SOCKET = KUBELET_ROOT / "plugins_registry" / f"{DRIVER}-reg.sock"
OBSERVER_SOCKET = Path("/tmp/lp-ki11-observer.sock")
MISSING_DRIVER = f"driver name {DRIVER} not found in the list of registered CSI drivers"
BINARIES = {
    "/opt/isolated/kubelet": "73475c6db8fd8a9780b1b378fa2f917875e6146166c24603c1abc6eafd4493a8",
    "/opt/isolated/hostpathplugin": "bd6bd1ee4d519bc7f7c8ada983ad0a281a927a2d7840f777297f5c774b241dd9",
    "/opt/isolated/csi-node-driver-registrar": "094c82438963af895acbe8e9d25e7d8d41248e0edca7fef41ca507b0f5a22bae",
}
SHUTDOWN = {"namespace_removed": True, "pv_removed": True, "registration_removed": True, "original_data_preserved": True}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def env():
    return {**os.environ, "K3S_DATA_DIR": str(ROOT / "state"), "GOMAXPROCS": "2",
            "PATH": f"{ROOT}/state/data/current/bin:{os.environ['PATH']}"}


def kube(*args, data=None, timeout=14):
    cmd = [K3S, "kubectl", "--request-timeout=9s", "--kubeconfig", str(ROOT / "kubeconfig"), *args]
    result = subprocess.run(cmd, env=env(), capture_output=True, text=True, timeout=timeout,
                            input=json.dumps(data) if data is not None else None)
    if result.returncode:
        raise RuntimeError(f"kubernetes {args[0]}: {result.stderr[-750:]}")
    return result.stdout


def condition(obj, kind):
    return next((row for row in obj["status"].get("conditions", []) if row["type"] == kind), {})


def wait_for(observe, predicate, label, timeout):
    end = monotonic() + timeout
    while True:
        value = observe()
        if predicate(value):
            return value
        if monotonic() >= end:
            raise RuntimeError(f"{label}: {value}")
        sleep(0.8)


def deployment():
    template = {"metadata": {"labels": {"app": WORKLOAD}},
                "spec": {"nodeSelector": {"liveprobe.example/fixture": "ki11"},
                         "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 0, "dnsPolicy": "Default",
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
                                         "volumeMounts": [{"name": VOLUME, "mountPath": "/data", "readOnly": True}]}],
                         "volumes": [{"name": VOLUME, "persistentVolumeClaim": {"claimName": PVC, "readOnly": True}}]}}
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"namespace": NAMESPACE, "name": WORKLOAD},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": WORKLOAD}}, "template": template}}


def storage():
    driver = {"apiVersion": "storage.k8s.io/v1", "kind": "CSIDriver", "metadata": {"name": DRIVER},
              "spec": {"attachRequired": False, "volumeLifecycleModes": ["Persistent"]}}
    pv = {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": PV},
          "spec": {"capacity": {"storage": "1Mi"}, "accessModes": ["ReadWriteOnce"], "storageClassName": "",
                   "persistentVolumeReclaimPolicy": "Retain", "claimRef": {"namespace": NAMESPACE, "name": PVC},
                   "awsElasticBlockStore": {"volumeID": LEGACY_ID, "fsType": "ext4", "readOnly": True}}}
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"namespace": NAMESPACE, "name": PVC},
           "spec": {"storageClassName": "", "volumeName": PV, "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "1Mi"}}}}
    return (driver, pv, pvc)


def driver_registration():
    row = kube("get", "csinode", NODE, "--ignore-not-found", "-o", "json")
    drivers = json.loads(row)["spec"].get("drivers") or [] if row else []
    return next((driver for driver in drivers if driver["name"] == DRIVER), None)


def http(address):
    nonce = uuid4().hex
    try:
        with urlopen(f"http://{address}:18080/check?nonce={nonce}", timeout=1.2) as response:
            return {"status": response.status, "body": response.read(128).decode(), "nonce": nonce}
    except (URLError, OSError):
        return {"status": None, "body": None, "nonce": nonce}


def seed_identity(path):
    stat = path.stat()
    return {"inode": stat.st_ino, "device": stat.st_dev, "sha256": sha256(path.read_bytes()).hexdigest()}


def blocked(row):
    return (row["driver"] is None and row["pod_phase"] == "Pending" and not row["pod_ready"]
            and row["pod_node"] == NODE and row["mount"] is None and row["pv_bound"] and row["pvc_bound"]
            and row["volume_id"] == LEGACY_ID and row["mount_error"])


def recovered(row):
    request, driver, mount = row["http"], row["driver"], row["mount"]
    return (driver and driver["name"] == DRIVER and driver["nodeID"] == NODE
            and row["pv_bound"] and row["pvc_bound"] and row["volume_id"] == LEGACY_ID
            and row["pod_node"] == NODE and row["pod_phase"] == "Running" and row["pod_ready"]
            and row["driver_publish_observed"] and mount and mount == row["seed"]
            and request and (request["status"], request["body"]) == (200, BODY))


class Trial:
    def __init__(self):
        self.created = []
        self.processes = []
        self.id = uuid4().hex[:12]

    def setup(self):
        assert not subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).strip()
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == KUBERNETES
        for binary, checksum in BINARIES.items():
            assert sha256(Path(binary).read_bytes()).hexdigest() == checksum
        assert condition(json.loads(kube("get", "node", NODE, "-o", "json")), "Ready").get("status") == "True"
        assert driver_registration() is None and not REG_SOCKET.exists() and not ENDPOINT.exists()
        assert not kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name")
        assert not kube("get", "pv", PV, "--ignore-not-found", "-o", "name")
        assert not kube("get", "csidriver", DRIVER, "--ignore-not-found", "-o", "name")
        SEED.parent.mkdir(parents=True, exist_ok=True)
        if not SEED.exists():
            with SEED.open("x") as out:
                out.write(BODY)
        assert SEED.read_text() == BODY
        self.seed = seed_identity(SEED)
        self.contract = {"kubernetes": KUBERNETES, "node": NODE, "driver": DRIVER, "deployment": deployment(),
                         "storage": list(storage()), "binary_sha256": BINARIES, "body_sha256": self.seed["sha256"]}
        self.put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
        for resource in storage():
            self.put(resource)

    def put(self, resource):
        kube("create", "-f", "-", data=resource)
        self.created.append(resource["kind"])

    def active_pod(self):
        items = json.loads(kube("-n", NAMESPACE, "get", "pods", "-l", f"app={WORKLOAD}", "-o", "json"))["items"]
        assert len(items) <= 1
        return items[0] if items else None

    def mount_error(self, uid):
        items = json.loads(kube("-n", NAMESPACE, "get", "events", "-o", "json"))["items"]
        matches = [row for row in items if row.get("reason") == "FailedMount"
                   and row.get("involvedObject", {}).get("uid") == uid
                   and row.get("source", {}).get("component") == "kubelet"
                   and MISSING_DRIVER in row.get("message", "")]
        if not matches:
            return None
        event = min(matches, key=lambda row: row["metadata"]["creationTimestamp"])
        return {"uid": event["metadata"]["uid"], "count": event.get("count", 1), "message": event["message"],
                "first": event["firstTimestamp"], "last": event["lastTimestamp"]}

    def observe(self):
        pod = self.active_pod()
        if pod is None:
            return None
        pv = json.loads(kube("get", "pv", PV, "-o", "json"))
        pvc = json.loads(kube("-n", NAMESPACE, "get", "pvc", PVC, "-o", "json"))
        uid = pod["metadata"]["uid"]
        address, pod_ready = pod["status"].get("podIP"), condition(pod, "Ready").get("status") == "True"
        target = KUBELET_ROOT / "pods" / uid / "volumes/kubernetes.io~csi" / PV / "mount/check"
        plugin_log = ROOT / f"hostpathplugin-{self.id}.log"
        driver_published = plugin_log.exists() and "GRPC call: /csi.v1.Node/NodePublishVolume" in plugin_log.read_text()
        return {"at": datetime.now(timezone.utc).isoformat(), "pod_name": pod["metadata"]["name"], "pod_uid": uid,
                "pod_node": pod["spec"].get("nodeName"), "pod_phase": pod["status"]["phase"], "pod_ready": pod_ready,
                "pod_ip": address, "pv_uid": pv["metadata"]["uid"], "pvc_uid": pvc["metadata"]["uid"],
                "pv_bound": pv["status"]["phase"] == "Bound" and pv["spec"]["claimRef"].get("uid") == pvc["metadata"]["uid"],
                "pvc_bound": pvc["status"]["phase"] == "Bound" and pvc["spec"]["volumeName"] == PV,
                "pv_migrated_to": pv["metadata"].get("annotations", {}).get("pv.kubernetes.io/migrated-to"),
                "volume_id": pv["spec"]["awsElasticBlockStore"]["volumeID"],
                "seed": seed_identity(SEED), "mount": seed_identity(target) if target.is_file() else None,
                "driver_publish_observed": driver_published,
                "driver": driver_registration(), "mount_error": self.mount_error(uid),
                "http": http(address) if address and pod_ready else None}

    def prepare(self):
        self.put(deployment())
        fault = wait_for(self.observe, lambda state: state and blocked(state), "missing native mount fault", 45)
        self.started = monotonic()
        self.fault = fault
        return {"case": CASE, "kubernetes": KUBERNETES, "fixture_contract": self.contract,
                "fixture_sha256": digest([CASE, self.contract]), "initial_error_log": fault["mount_error"]["message"],
                "fault_before": fault}

    def start(self, name, *args):
        binary = f"/opt/isolated/{name}"
        with (ROOT / f"{name}-{self.id}.log").open("x") as out:
            self.processes.append(subprocess.Popen([binary, *args], cwd=ROOT, env=env(),
                                                   stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL))

    def restore(self):
        if self.processes:
            return {"applied": False, "reason": "the trial driver is already starting"}
        state = {"Volumes": [{"VolName": PV, "VolID": VOLUME_ID, "VolSize": 1048576,
                              "VolPath": str(SEED.parent), "VolAccessType": 0}], "Snapshots": [], "GroupSnapshots": []}
        (DATA / "state.json").write_text(json.dumps(state))
        self.start("hostpathplugin", f"--drivername={DRIVER}", f"--statedir={DATA}", f"--nodeid={NODE}",
                   f"--endpoint=unix://{ENDPOINT}", "--v=3")
        wait_for(ENDPOINT.is_socket, bool, "CSI service socket", 10)
        self.start("csi-node-driver-registrar", f"--csi-address={ENDPOINT}", f"--kubelet-registration-path={ENDPOINT}",
                   f"--plugin-registration-path={REG_SOCKET.parent}", "--v=3")
        return {"applied": True, "driver": DRIVER, "node": NODE, "volume_id": LEGACY_ID}

    def verify(self):
        if self.processes:
            first = wait_for(self.observe, recovered, "CSI publish and HTTP did not converge", 85)
            sleep(1.6)
            second = wait_for(self.observe, recovered, "second HTTP failed", 15)
        else:
            first = wait_for(self.observe, blocked, "mount failure did not persist", 15)
            sleep(1.6)
            second = wait_for(self.observe, blocked, "mount failure did not persist", 15)
        return {"independent_probes": [first, second], "wall_duration_ms": round((monotonic() - self.started) * 1000)}

    def cleanup(self):
        if "Namespace" not in self.created:
            return SHUTDOWN
        kube("delete", "namespace", NAMESPACE, "--ignore-not-found", "--timeout=40s", timeout=44)
        if self.processes:
            def unpublished():
                state = json.loads((DATA / "state.json").read_text())["Volumes"][0]
                return not (state.get("Published") or state.get("Staged"))

            wait_for(unpublished, bool, "trial volume still mounted", 40)
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
        self.processes.clear()
        for socket in (REG_SOCKET, ENDPOINT):
            if socket.exists():
                assert socket.is_socket()
                socket.unlink()
        wait_for(driver_registration, lambda driver: driver is None, "old CSI registration still active", 30)
        if "PersistentVolume" in self.created:
            kube("delete", "pv", PV, "--ignore-not-found", "--timeout=20s", timeout=23)
        if "CSIDriver" in self.created:
            kube("delete", "csidriver", DRIVER, "--ignore-not-found", "--timeout=15s", timeout=18)
        self.created.clear()
        assert seed_identity(SEED) == self.seed
        return SHUTDOWN

    def handle(self, request):
        tool = request["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool in {"workload", "probe"}:
            return self.observe()
        if tool == "logs":
            return self.observe()["mount_error"]
        if tool == "source":
            return {"kubernetes": KUBERNETES, "node": NODE, "pv": PV, "legacy_volume": LEGACY_ID,
                    "expected_csi_driver": DRIVER, "installed_driver": driver_registration(),
                    "recovery_setting": "node_csi_driver", "available_driver": DRIVER}
        if tool == "configure":
            if (request.get("key"), request.get("value")) == ("node_csi_driver", DRIVER):
                return self.restore()
            return {"applied": False, "reason": "only the dedicated node CSI driver can be registered"}
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
        observer.NAMESPACE, observer.TARGET = NAMESPACE, WORKLOAD
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
                elif observer and tool == "grant_pvc":
                    scenario_role = json.loads(kube("-n", NAMESPACE, "get", "role", "liveprobe-ki02-reader", "-o", "json"))
                    scenario_role["rules"].append({"apiGroups": [""], "resources": ["persistentvolumeclaims"],
                                                   "resourceNames": [PVC], "verbs": ["get"]})
                    kube("replace", "-f", "-", data=scenario_role)
                    result = {"allowed": kube("-n", NAMESPACE, "auth", "can-i", "get", f"pvc/{PVC}",
                                               "--as", f"system:serviceaccount:{NAMESPACE}:liveprobe-ki02-reader").strip() == "yes"}
                else:
                    result = trial.handle(request)
                print(json.dumps(result), flush=True)
                if tool == "shutdown":
                    break
    finally:
        trial.cleanup()


if __name__ == "__main__":
    main()
