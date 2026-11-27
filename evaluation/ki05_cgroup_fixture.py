"""Verify an actual kubelet cgroup v1 startup refusal and cgroup v2 recovery."""
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


CASE = "KI-05"
KUBERNETES = "v1.35.1+k3s1"
NAMESPACE = "dev-ki05-verify"
NODE = "ki05-fixture"
POD = "ki05-http"
WORK = Path("/tmp/lp-ki05-k3s")
K3S = "/opt/isolated/k3s-arm64"
KUBELET = "/opt/isolated/kubelet"
KUBELET_SHA = "73475c6db8fd8a9780b1b378fa2f917875e6146166c24603c1abc6eafd4493a8"
KUBELET_VERSION = "v1.35.1"
CONFIG = WORK / "kubelet.json"
RUNTIME_SOCKET = WORK / "cri.sock"
RUNTIME_VERSION = "v2.1.5-k3s1"
CLIENT_CONFIG = WORK / "cri-client.yaml"
IMAGE = "docker.io/library/busybox:1.37"
BODY = "ki05-cgroup-http-ok\n"
PORT = 18080
OBSERVER_SOCKET = Path("/tmp/lp-ki05-observer.sock")


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
                "restartPolicy": "Always", "nodeSelector": {"liveprobe.example/fixture": "ki05"},
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


def cri(*args, timeout=12):
    result = command(K3S, "crictl", "--config", str(CLIENT_CONFIG), "--runtime-endpoint",
                     f"unix://{RUNTIME_SOCKET}", *args, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"fixture CRI {args[0]} failed: {result.stderr[-440:]}")
    return result.stdout


def runtime_api():
    versions = {key.strip(): value.strip() for line in cri("version").splitlines() if ":" in line
                for key, value in (line.split(":", 1),)}
    if versions.get("RuntimeApiVersion") != "v1" or versions.get("RuntimeVersion") != RUNTIME_VERSION:
        raise RuntimeError("the fixture does not have the expected native CRI v1 runtime")
    return {"name": versions["RuntimeName"], "runtime_version": versions["RuntimeVersion"],
            "api_version": versions["RuntimeApiVersion"]}


def remove_test_sandboxes():
    def list_owned():
        return json.loads(cri("pods", "--namespace", NAMESPACE, "-o", "json")).get("items") or []

    for sandbox in list_owned():
        identity = sandbox["metadata"]
        assert (identity["namespace"], identity["name"]) == (NAMESPACE, POD)
        cri("stopp", sandbox["id"], timeout=20)
        cri("rmp", sandbox["id"], timeout=20)
    assert not list_owned()


def mount_kind(pid, path):
    with Path(f"/proc/{pid}/mountinfo").open() as records:
        for line in records:
            pre, post = line.split(" - ", 1)
            if pre.split()[4] == path:
                return post.split()[0]
    return None


def node():
    result = kube("get", "node", NODE, "--ignore-not-found", "-o", "json")
    if not result:
        return None
    info = json.loads(result)
    assert info["metadata"]["labels"]["liveprobe.example/fixture"] == "ki05"
    return {"uid": info["metadata"]["uid"], "name": NODE,
            "ready": condition(info, "Ready").get("status"),
            "runtime": info["status"].get("nodeInfo", {}).get("containerRuntimeVersion"),
            "kubelet": info["status"].get("nodeInfo", {}).get("kubeletVersion")}


def native_error(line):
    message = "failed to validate kubelet configuration, error: kubelet is configured to not run on a host using cgroup v1"
    index = line.find(message)
    return line[index:].rstrip('"').strip() if index >= 0 else None


def restore_empty_test_hierarchy():
    root = Path("/sys/fs/cgroup")
    assert mount_kind("self", str(root)) == "cgroup2"
    workloads = root / "kubepods"
    if workloads.exists():
        groups = [group for group in workloads.rglob("*") if group.is_dir()]
        groups.sort(key=lambda group: len(group.parts), reverse=True)
        for group in [*groups, workloads]:
            if (group / "cgroup.procs").read_text().strip():
                raise RuntimeError("a live process still owns a cgroup in the discarded trial")
            group.rmdir()
    enabled = root / "cgroup.subtree_control"
    if "cpu" in enabled.read_text().split():
        enabled.write_text("-cpu\n")


class Trial:
    def __init__(self):
        self.worker = None
        self.created = False
        self.source = "v1"
        self.failed_launches = 0
        self.next_log = WORK / f"kubelet-v2-{uuid4().hex[:10]}.log"

    def setup(self):
        assert sha256(Path(KUBELET).read_bytes()).hexdigest() == KUBELET_SHA
        assert command(KUBELET, "--version").stdout.strip() == f"Kubernetes {KUBELET_VERSION}"
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == KUBERNETES
        assert not subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).strip()
        assert mount_kind("self", "/sys/fs/cgroup") == "cgroup2"
        assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == "" and node() is None
        remove_test_sandboxes()
        restore_empty_test_hierarchy()
        self.config_sha = sha256(CONFIG.read_bytes()).hexdigest()
        assert "failCgroupV1" not in json.loads(CONFIG.read_text())
        self.runtime = runtime_api()
        self.contract = {"control_plane": KUBERNETES, "kubelet": KUBELET_VERSION, "kubelet_binary": KUBELET_SHA,
                         "kubelet_config": json.loads(CONFIG.read_text()), "kubelet_config_sha": self.config_sha,
                         "runtime": self.runtime, "node": NODE, "pod": pod(), "probe_response": BODY}
        put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
        self.created = True
        put({"apiVersion": "v1", "kind": "ConfigMap",
             "metadata": {"name": POD, "namespace": NAMESPACE}, "data": {"check": BODY}})
        self.pod_uid = put(pod())["metadata"]["uid"]

    def kubelet_arguments(self):
        return [KUBELET, "--config", str(CONFIG), "--kubeconfig", str(WORK / "node-auth/kubelet.conf"),
                "--root-dir", str(WORK / "kubelet-root"), "--hostname-override", NODE,
                "--node-ip", "192.0.2.10", "--node-labels", "liveprobe.example/fixture=ki05"]

    def exercise_v1(self):
        args = ["unshare", "-m", "sh", str(WORK / "v1-kubelet.sh"), *self.kubelet_arguments()]
        boot = command(*args, timeout=15)
        mounts = [line for line in boot.stdout.splitlines() if " /sys/fs/cgroup/cpu " in line]
        first = next((error for line in boot.stderr.splitlines() if (error := native_error(line))), None)
        if boot.returncode != 1 or len(mounts) != 1 or " - cgroup " not in mounts[0] or not first:
            raise RuntimeError(f"native cgroup v1 refusal did not match: {boot.stderr[-650:]}")
        self.failed_launches += 1
        return {"mount": "cgroup", "mount_path": "/sys/fs/cgroup/cpu",
                "controllers": ["cpu", "cpuacct"], "exit_code": boot.returncode,
                "error": first, "attempt": self.failed_launches}

    def observe(self):
        current = json.loads(kube("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))
        status, info = current["status"], node()
        address = status.get("podIP")
        ready = condition(current, "Ready").get("status") == "True" and status.get("phase") == "Running"
        worker_fs = None
        if self.worker is not None:
            if self.worker.poll() is not None:
                raise RuntimeError(f"cgroup v2 kubelet exited: {self.next_log.read_text().splitlines()[-5:]}")
            try:
                worker_fs = mount_kind(self.worker.pid, "/sys/fs/cgroup")
            except FileNotFoundError:
                pass
        return {"at": now(), "source": self.source, "node": info, "pod_uid": current["metadata"]["uid"],
                "pod_node": current["spec"].get("nodeName"), "pod_ip": address, "pod_ready": ready,
                "pod_phase": status.get("phase"), "http": http(address) if ready and address else None,
                "runtime": runtime_api(), "worker_cgroup": worker_fs,
                "kubelet_config_sha": sha256(CONFIG.read_bytes()).hexdigest()}

    @staticmethod
    def blocked(sample):
        return (sample["source"] == "v1" and sample["node"] is None and sample["pod_phase"] == "Pending"
                and not sample["pod_ready"] and not sample["pod_node"] and not sample["pod_ip"])

    @staticmethod
    def recovered(sample):
        info, runtime, request = sample["node"], sample["runtime"], sample["http"]
        return (sample["source"] == "v2" and sample["worker_cgroup"] == "cgroup2" and info and info["ready"] == "True"
                and info["name"] == sample["pod_node"] == NODE and info["kubelet"] == KUBELET_VERSION
                and info["runtime"] == f"containerd://{RUNTIME_VERSION.removeprefix('v')}"
                and runtime["api_version"] == "v1" and runtime["runtime_version"] == RUNTIME_VERSION
                and sample["pod_ready"] and sample["pod_phase"] == "Running" and sample["pod_ip"]
                and request and request["status"] == 200 and request["body"] == BODY)

    def prepare(self):
        self.first_refusal = self.exercise_v1()
        self.started = monotonic()
        failure = wait_for(self.observe, self.blocked, "node and Pod did not match the observed cgroup startup failure", 14)
        return {"case": CASE, "kubernetes": KUBERNETES, "fixture_contract": self.contract,
                "fixture_sha256": fingerprint([CASE, self.contract]), "initial_error_log": self.first_refusal["error"],
                "fault_before": {**failure, "native_v1_launch": self.first_refusal}}

    def restore(self):
        if self.source != "v1":
            return {"applied": False, "reason": "the cgroup v2 start is already selected"}
        assert mount_kind("self", "/sys/fs/cgroup") == "cgroup2"
        with self.next_log.open("x") as out:
            self.worker = subprocess.Popen(self.kubelet_arguments(),
                                           env={**os.environ, "GOMAXPROCS": "2",
                                                "PATH": f"{WORK}/state/data/current/bin:{os.environ['PATH']}"},
                                           stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.source = "v2"
        return {"applied": True, "before": "v1", "after": "v2", "node": NODE, "kubelet_config_sha": self.config_sha}

    def verify(self):
        if self.source == "v1":
            output = []
            for count in (1, 2):
                failed = self.exercise_v1()
                sample = wait_for(self.observe, self.blocked, f"native v1 check {count}", 20)
                output.append({**sample, "native_v1_launch": failed})
        else:
            first = wait_for(self.observe, self.recovered, "first v2 node and HTTP proof", 90)
            sleep(1.7)
            second = wait_for(self.observe, self.recovered, "second v2 node and HTTP proof", 14)
            output = [first, second]
        assert {p["pod_uid"] for p in output} == {self.pod_uid}
        return {"independent_probes": output, "wall_duration_ms": round((monotonic()-self.started)*1000)}

    def cleanup(self):
        try:
            if self.created:
                kube("delete", "namespace", NAMESPACE, "--timeout=40s", timeout=44)
                assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == ""
                self.created = False
            remove_test_sandboxes()
        finally:
            finish_process(self.worker)
            self.worker = None
        if node() is not None:
            kube("delete", "node", NODE, "--timeout=15s", timeout=20)
            assert node() is None
        restore_empty_test_hierarchy()
        return {"stopped": True, "namespace_removed": True, "node_removed": True}

    def handle(self, record):
        tool = record["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool == "workload":
            return {"kubernetes": KUBERNETES, "node": NODE, "service": self.observe()}
        if tool == "logs":
            return self.first_refusal
        if tool == "source":
            return {"setting": "cgroup_hierarchy", "selected": self.source, "choices": ["v1", "v2"],
                    "template": self.contract["kubelet_config"], "runtime": self.runtime}
        if tool == "probe":
            return self.observe()
        if tool == "configure":
            if (record.get("key"), record.get("value")) != ("cgroup_hierarchy", "v2"):
                return {"applied": False, "reason": "only the cgroup v2 startup is available for the test kubelet"}
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
