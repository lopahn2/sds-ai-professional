"""Create bounded image filesystem pressure and native Kubernetes eviction in an offline test node."""
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4


CASE = "KW-07"
NAMESPACE = "dev-kw07-verify"
NODE = "kw07-fixture"
WORKLOAD = "kw07-http"
CONTROL = "kw07-control"
KUBERNETES = "v1.34.13+k3s1"
WORK = Path("/tmp/lp-kw07-k3s")
IMAGEFS = WORK / "state/agent/containerd"
FILLER = IMAGEFS / "kw07-pressure.bin"
K3S = "/opt/isolated/k3s-arm64"
OBSERVER_SOCKET = Path("/tmp/lp-kw07-observer.sock")
TAINT = "node.kubernetes.io/disk-pressure"
MIN_HEALTHY = 0.25
THRESHOLD = 0.15
TARGET_HEADROOM = 0.12
MAX_FILL = 180 * 1024**2
LABELS = {"app": WORKLOAD, "liveprobe.example/fixture": "kw07"}
IMAGE = "docker.io/library/busybox:1.37"
BODY = "kw07-probe-ok\n"
PORT = 18080


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def kube(*args, payload=None, timeout=15):
    result = subprocess.run([K3S, "kubectl", "--kubeconfig", str(WORK / "kubeconfig"), "--request-timeout=8s", *args],
                            input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"kubernetes {args[0]} failed: {result.stderr[:420]}")
    return result.stdout


def put(obj):
    return json.loads(kube("create", "-f", "-", "-o", "json", payload=obj))


def read(*args):
    return json.loads(kube("-n", NAMESPACE, "get", *args, "-o", "json"))


def summary():
    response = json.loads(kube("get", f"--raw=/api/v1/nodes/{NODE}/proxy/stats/summary"))["node"]
    disk = response["runtime"]["imageFs"]
    assert disk["capacityBytes"] != response["fs"]["capacityBytes"]
    assert disk["capacityBytes"] == os.statvfs(IMAGEFS).f_blocks * os.statvfs(IMAGEFS).f_frsize
    return {"capacity_bytes": disk["capacityBytes"], "available_bytes": disk["availableBytes"],
            "fs_observed_at": disk["time"], "nodefs_capacity": response["fs"]["capacityBytes"]}


def condition(obj, name):
    return next((c for c in (obj.get("status", {}).get("conditions") or []) if c["type"] == name), {})


def wait_for(observe, condition_matches, label, *, timeout=80):
    end = monotonic() + timeout
    while True:
        sample = observe()
        if condition_matches(sample):
            return sample
        if monotonic() >= end:
            raise RuntimeError(f"{label}: {json.dumps(sample)[:2800]}")
        sleep(0.8)


def ready(pod):
    return (pod["node"] == NODE and bool(pod["pod_ip"])
            and pod["phase"] == "Running" and pod["ready"]
            and pod["http"]["status"] == 200 and pod["http"]["body"] == BODY)


def pod_payload(name):
    metadata = {"name": name, "namespace": NAMESPACE, "labels": LABELS if name == WORKLOAD else {"app": CONTROL}}
    spec = {"hostNetwork": False, "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 0,
            "containers": [{"name": "http", "image": IMAGE, "imagePullPolicy": "Never",
                            "command": ["/bin/busybox", "httpd", "-f", "-p", str(PORT), "-h", "/data"],
                            "resources": {"requests": {"ephemeral-storage": "8Mi", "cpu": "5m", "memory": "8Mi"},
                                          "limits": {"cpu": "100m", "memory": "32Mi"}},
                            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "seccompProfile": {"type": "RuntimeDefault"},
                                                "capabilities": {"drop": ["ALL"]}},
                            "volumeMounts": [{"name": "body", "mountPath": "/data", "readOnly": True}]}],
            "volumes": [{"name": "body", "configMap": {"name": "kw07-http"}}]}
    if name == CONTROL:
        spec["priorityClassName"] = "system-node-critical"
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": spec}


def http_probe(address):
    nonce = uuid4().hex
    try:
        with urlopen(f"http://{address}:{PORT}/check?nonce={nonce}", timeout=1.5) as response:
            return {"status": response.status, "body": response.read(128).decode(), "nonce": nonce}
    except (URLError, OSError):
        return {"status": None, "body": None, "nonce": nonce}


def observation(obj):
    md, status = obj["metadata"], obj["status"]
    pod = {"uid": md["uid"], "name": md["name"], "pod_ip": status.get("podIP"),
           "node": obj["spec"].get("nodeName"), "reason": status.get("reason"),
           "phase": status.get("phase"), "ready": condition(obj, "Ready").get("status") == "True",
           "scheduled": condition(obj, "PodScheduled").get("status"), "priority": obj["spec"].get("priority")}
    pod["http"] = http_probe(pod["pod_ip"]) if pod["pod_ip"] and pod["ready"] and pod["phase"] == "Running" else None
    return pod


def available_ratio(disk):
    return disk["available_bytes"] / disk["capacity_bytes"]


class Trial:
    def __init__(self):
        self.created = False
        self.file_id = None
        self.started = None
        self.initial_pod = None

    def setup(self):
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == KUBERNETES
        assert not subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).strip()
        self.disk_device = IMAGEFS.stat().st_dev
        assert self.disk_device != WORK.stat().st_dev and not FILLER.exists()
        assert len(json.loads(kube("get", "node", "-o", "json"))["items"]) == 1
        node = read("node", NODE)
        assert condition(node, "Ready")["status"] == "True" and condition(node, "DiskPressure")["status"] == "False"
        assert available_ratio(summary()) > MIN_HEALTHY
        put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
        self.created = True
        put({"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": "kw07-http", "namespace": NAMESPACE}, "data": {"check": BODY}})
        control = put(pod_payload(CONTROL))
        assert control["spec"]["priority"] >= 2_000_000_000
        template = pod_payload(WORKLOAD)
        deployment_spec = {"replicas": 1, "selector": {"matchLabels": LABELS},
                           "template": {"metadata": {"labels": LABELS}, "spec": template["spec"]}}
        deployment = put({"apiVersion": "apps/v1", "kind": "Deployment",
                          "metadata": {"name": WORKLOAD, "namespace": NAMESPACE}, "spec": deployment_spec})
        self.contract = {"node": NODE, "target_namespace": NAMESPACE, "deployment": deployment_spec,
                         "control": pod_payload(CONTROL)["spec"], "storage_threshold": THRESHOLD,
                         "test_growth_target": TARGET_HEADROOM, "response_body": BODY}
        self.deployment_uid = deployment["metadata"]["uid"]
        self.baseline = wait_for(self.observe, self.healthy, "native node and workload did not become ready")
        self.initial_pod = next(p["uid"] for p in self.baseline["pods"] if ready(p))

    def observe(self):
        node = read("node", NODE)
        no_schedule_taints = sorted(t["key"] for t in node["spec"].get("taints") or [] if t["effect"] == "NoSchedule")
        workload = read("deployment", WORKLOAD)
        pods = [observation(p) for p in read("pods", "-l", f"app={WORKLOAD}")["items"]]
        events = []
        pod_uids = {p["uid"] for p in pods} | {self.initial_pod}
        for event in read("events")["items"]:
            ref, message = event.get("involvedObject") or {}, event.get("message", "")
            if ref.get("uid") not in pod_uids or event.get("type") != "Warning":
                continue
            if (event.get("reason") == "FailedScheduling" and "untolerated taint" in message
                    or event.get("reason") == "Evicted" and "ephemeral-storage" in message):
                events.append({"uid": event["metadata"]["uid"], "pod_uid": ref["uid"], "pod": ref["name"],
                               "reason": event["reason"], "message": message})
        return {"observed_at": now(), "node_uid": node["metadata"]["uid"], "node_ready": condition(node, "Ready"),
                "disk_pressure": condition(node, "DiskPressure"), "taint": TAINT in no_schedule_taints,
                "no_schedule_taints": no_schedule_taints,
                "disk": summary(), "control": observation(read("pod", CONTROL)), "pods": pods, "events": events,
                "replicas_ready": workload["status"].get("readyReplicas", 0),
                "deployment_uid": workload["metadata"]["uid"]}

    @staticmethod
    def healthy(sample):
        return (sample["node_ready"].get("status") == "True" and sample["disk_pressure"].get("status") == "False"
                and not sample["taint"] and available_ratio(sample["disk"]) > MIN_HEALTHY
                and sample["replicas_ready"] == 1 and ready(sample["control"])
                and sum(ready(pod) for pod in sample["pods"]) == 1)

    @staticmethod
    def fault(sample):
        return (sample["disk_pressure"].get("status") == "True" and sample["no_schedule_taints"] == [TAINT]
                and available_ratio(sample["disk"]) < THRESHOLD and sample["replicas_ready"] == 0
                and ready(sample["control"]) and any(p["phase"] == "Pending" and not p["node"] for p in sample["pods"])
                and {"Evicted", "FailedScheduling"} <= {event["reason"] for event in sample["events"]})

    def allocate(self):
        assert IMAGEFS.stat().st_dev == self.disk_device and self.disk_device != WORK.stat().st_dev
        fs = os.statvfs(IMAGEFS)
        initial, capacity = fs.f_bavail * fs.f_frsize, fs.f_blocks * fs.f_frsize
        growth = initial - round(capacity * TARGET_HEADROOM)
        if not (16 * 1024**2 <= growth <= MAX_FILL and initial / capacity > MIN_HEALTHY):
            raise RuntimeError("image disk capacity is outside the disposable fixture range")
        fd = os.open(FILLER, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, mode=0o600)
        try:
            info = os.fstat(fd)
            self.file_id = (info.st_dev, info.st_ino)
            os.posix_fallocate(fd, 0, growth)
            assert os.fstat(fd).st_blocks * 512 >= growth
        finally:
            os.close(fd)

    def release(self):
        if self.file_id is None:
            return None
        info = FILLER.lstat()
        assert stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == self.file_id
        assert info.st_dev == self.disk_device and info.st_size <= MAX_FILL
        FILLER.unlink()
        self.file_id = None
        return info.st_size

    def pressure_sample(self):
        # Garbage collection can free image space while a trial warms up. Keep the same
        # fixture target until the first real fault is captured, using only the owned file.
        fs = os.statvfs(IMAGEFS)
        capacity, available = fs.f_blocks * fs.f_frsize, fs.f_bavail * fs.f_frsize
        if available / capacity >= THRESHOLD:
            additional = available - round(capacity * TARGET_HEADROOM)
            fd = os.open(FILLER, os.O_NOFOLLOW | os.O_WRONLY)
            try:
                info = os.fstat(fd)
                assert stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == self.file_id
                if not 0 < info.st_size + additional <= MAX_FILL:
                    raise RuntimeError("image garbage collection exceeded the bounded pressure budget")
                os.posix_fallocate(fd, info.st_size, additional)
                assert os.fstat(fd).st_blocks * 512 >= info.st_size + additional
            finally:
                os.close(fd)
        return self.observe()

    def prepare(self):
        self.allocate()
        failed = wait_for(self.pressure_sample, self.fault,
                          "native disk pressure, eviction and scheduling failure did not converge", timeout=100)
        event = next(e for e in failed["events"] if e["reason"] == "FailedScheduling")
        self.started = monotonic()
        return {"case": CASE, "kubernetes": KUBERNETES, "fixture_contract": self.contract,
                "fixture_sha256": sha([CASE, KUBERNETES, self.contract]),
                "healthy_before": self.baseline, "fault_before": failed,
                "original_pod_uid": self.initial_pod, "filler_bytes": FILLER.stat().st_size,
                "initial_event": event, "initial_error_log": f"{event['reason']}: {event['message']}"}

    def verify(self):
        predicate = self.fault if self.file_id is not None else self.healthy
        first = wait_for(self.observe, predicate, "independent native recovery observation")
        # Require a distinct kubelet image filesystem sample, not a second copy of the cache.
        second = wait_for(self.observe, lambda current: predicate(current) and
                          current["disk"]["fs_observed_at"] != first["disk"]["fs_observed_at"],
                          "second independent image filesystem and workload observation", timeout=20)
        assert {first["deployment_uid"], second["deployment_uid"]} == {self.deployment_uid}
        return {"independent_probes": [first, second], "wall_duration_ms": round((monotonic() - self.started)*1000)}

    def cleanup(self):
        if self.file_id is not None:
            self.release()
        if self.created:
            kube("delete", "namespace", NAMESPACE, "--timeout=42s", timeout=46)
            assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == ""
            self.created = False
        assert not FILLER.exists()
        return {"stopped": True, "namespace_removed": True, "filler_removed": True}

    def handle(self, request):
        tool = request["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool == "probe":
            return self.observe()
        if tool == "workload":
            return {"node": NODE, "namespace": NAMESPACE, "deployment": WORKLOAD, "state": self.observe()}
        if tool == "source":
            return {"imagefs": self.observe()["disk"], "eviction_threshold": THRESHOLD, "fixture_file": FILLER.name,
                    "fixture_file_exists": FILLER.exists(), "approved_setting": "release_diagnostic_filler"}
        if tool == "logs":
            return self.observe()["events"]
        if tool == "configure":
            if request.get("key") != "release_diagnostic_filler" or request.get("value") != "true":
                return {"applied": False, "reason": "only removal of the proven current diagnostic filler is supported"}
            freed = self.release()
            return {"applied": freed is not None, "freed_bytes": freed, "file": FILLER.name}
        if tool == "verify":
            return self.verify()
        if tool == "shutdown":
            return self.cleanup()
        raise ValueError("unknown trial operation")


def main():
    observer = None
    trial = Trial()
    if sys.argv[1:] == ["--observe"]:
        spec = importlib.util.spec_from_file_location("observer", WORK / "observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        observer.NAMESPACE, observer.TARGET = NAMESPACE, WORKLOAD
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
