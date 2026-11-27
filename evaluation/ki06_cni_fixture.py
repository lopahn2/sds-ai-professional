"""Exercise a real legacy CNI plugin with two failing Pod sandboxes in offline K3s."""
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep


CASE = "KI-06"
NAMESPACE = "dev-ki06-verify"
NODE = "ki06-fixture"
K8S_VERSION = "v1.34.13+k3s1"
CNI_PATH = Path("/etc/cni/net.d/10-ki06-fixture.conflist")
WORK = Path("/tmp/lp-ki06-k3s")
OBSERVER_SOCKET = Path("/tmp/lp-ki06-observer.sock")
K3S = "/opt/isolated/k3s-arm64"
READY = "0.4.0"
INCOMPATIBLE = "1.0.0"
PLUGIN = Path("/opt/cni/bin/bridge")
OTHER_PLUGIN = Path("/opt/cni/bin/host-local")
IMAGE = "example.invalid/liveprobe-ki10/coredns:1.6.0"
PORT = 10553
ZONE = "ki06.verify.invalid"
QUERY = f"backend.{ZONE}."
ANSWER = "192.0.2.66"
BASELINE = "ki06-control"
TARGETS = ("ki06-alpha", "ki06-beta")
NAMES = (BASELINE, *TARGETS)
CNI = {"name": "ki06-verify", "plugins": [{"type": "bridge", "bridge": "ki06br0",
                                          "isGateway": True, "ipMasq": False,
                                          "ipam": {"type": "host-local", "dataDir": str(WORK / "ipam"),
                                                   "ranges": [[{"subnet": "10.42.0.0/24"}]]}}]}
ZONE_FILES = {"Corefile": f"{ZONE}:{PORT} {{\n    file /dns/zone.db {ZONE}\n}}\n",
              "zone.db": "$TTL 30\n@ IN SOA ns. hostmaster. 1 60 30 3600 30\n@ IN NS ns.\n"
                         f"backend IN A {ANSWER}\n"}


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def config(version):
    return {"cniVersion": version, **CNI}


def cni_version():
    return json.loads(CNI_PATH.read_text())["cniVersion"]


def set_cni(version):
    if version not in (READY, INCOMPATIBLE):
        raise ValueError("unexpected test config version")
    if CNI_PATH.exists():
        before = json.loads(CNI_PATH.read_text())
        assert before == config(before["cniVersion"])
    staged = CNI_PATH.with_suffix(".tmp")
    with staged.open("x") as out:
        out.write(json.dumps(config(version)))
    staged.chmod(0o600)
    staged.replace(CNI_PATH)
    assert json.loads(CNI_PATH.read_text()) == config(version)


def kube(*args, input_obj=None, check=True):
    result = subprocess.run([K3S, "kubectl", "--kubeconfig", str(WORK / "kubeconfig"),
                             "--request-timeout=8s", *args],
                            text=True, input=json.dumps(input_obj) if input_obj is not None else None,
                            capture_output=True, timeout=12)
    if check and result.returncode:
        raise RuntimeError(f"{args[0]}: {result.stderr[:500]}")
    return result.stdout if check else result


def read(*args):
    output = kube("-n", NAMESPACE, "get", *args, "-o", "json")
    return json.loads(output)


def put(obj):
    return json.loads(kube("create", "-f", "-", "-o", "json", input_obj=obj))


def object_metadata(name):
    return {"name": name, "namespace": NAMESPACE, "labels": {"liveprobe.example/fixture": "ki06"}}


def pod_spec(name):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": object_metadata(name), "spec": {
        "nodeName": NODE, "dnsPolicy": "Default", "hostNetwork": False, "automountServiceAccountToken": False,
        "tolerations": [{"operator": "Exists"}], "terminationGracePeriodSeconds": 0,
        "containers": [{"name": "dns", "image": IMAGE, "imagePullPolicy": "Never",
                        "args": ["-conf", "/dns/Corefile"], "resources": {"requests": {"memory": "20Mi", "cpu": "10m"},
                                                                        "limits": {"memory": "96Mi", "cpu": "150m"}},
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "seccompProfile": {"type": "RuntimeDefault"}, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "dns", "mountPath": "/dns", "readOnly": True}]}],
        "volumes": [{"name": "dns", "configMap": {"name": "ki06-dns"}}],
    }}


def pod_state(name):
    obj = read("pod", name)
    ip = obj["status"].get("podIP")
    ready = (obj["status"].get("phase") == "Running"
             and any(c["type"] == "Ready" and c["status"] == "True"
                     for c in (obj["status"].get("conditions") or [])))
    return {"name": name, "uid": obj["metadata"]["uid"], "pod_ip": ip,
            "node": obj["spec"]["nodeName"], "ready": ready, "phase": obj["status"].get("phase")}


def dns_checks(pod):
    if not pod["pod_ip"]:
        return []
    results = []
    for transport in ("udp", "tcp"):
        command = ["dig", "-4", f"@{pod['pod_ip']}", "-p", str(PORT), QUERY, "A", "+time=1", "+tries=1",
                   "+norecurse", "+noall", "+answer", "+tcp" if transport == "tcp" else "+notcp"]
        run = subprocess.run(command, capture_output=True, text=True, timeout=4)
        lines = [line.split() for line in run.stdout.splitlines() if line and not line.startswith(";")]
        valid = len(lines) == 1 and (lines[0][0], *lines[0][2:]) == (QUERY, "IN", "A", ANSWER)
        results.append({"transport": transport, "response": ANSWER if valid else None,
                        "exit_code": run.returncode, "at": now()})
    return results


def now():
    return datetime.now(timezone.utc).isoformat()


def is_dns_healthy(checks):
    return [c["transport"] for c in checks] == ["udp", "tcp"] and all(
        (c["exit_code"], c["response"]) == (0, ANSWER) for c in checks
    )


def fault_events(pods):
    names = {p["uid"]: p["name"] for p in pods}
    results = {}
    for event in read("events")["items"]:
        ref = event.get("involvedObject") or {}
        message = event.get("message", "")
        uid = ref.get("uid")
        if uid not in names or ref.get("name") != names[uid] or ref.get("kind") != "Pod":
            continue
        if event.get("reason") != "FailedCreatePodSandBox" or event.get("type") != "Warning":
            continue
        if "incompatible CNI versions" not in message or "1.0.0" not in message or "0.4.0" not in message:
            continue
        results[names[uid]] = {"pod": names[uid], "pod_uid": uid, "node": (event.get("source") or {}).get("host"),
                               "uid": event["metadata"]["uid"], "message": message, "reason": event["reason"]}
    return [results[name] for name in TARGETS if name in results]


def until(observe, good, description, timeout):
    deadline = monotonic() + timeout
    while True:
        result = observe()
        if good(result):
            return result
        if monotonic() >= deadline:
            raise RuntimeError(f"{description}: {json.dumps(result)[:2500]}")
        sleep(0.75)


def stable_error(event):
    # The sandbox ID and Pod name are generated per retry; this suffix is verbatim from kubelet's Event.
    return event["message"][event["message"].index("incompatible CNI versions"):].strip()


class Trial:
    def __init__(self):
        self.created = False
        self.started = None
        self.checksums = {p.name: sha256(p.read_bytes()).hexdigest() for p in (PLUGIN, OTHER_PLUGIN)}
        self.versions = json.loads(subprocess.check_output([str(PLUGIN)], env={"CNI_COMMAND": "VERSION"}, text=True))
        assert self.versions["supportedVersions"][-1] == READY
        assert INCOMPATIBLE not in self.versions["supportedVersions"]

    def setup(self):
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == K8S_VERSION
        assert not subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).strip()
        assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == ""
        set_cni(READY)
        put({"kind": "Namespace", "apiVersion": "v1", "metadata": {"name": NAMESPACE}})
        self.created = True
        put({"kind": "ConfigMap", "apiVersion": "v1", "metadata": object_metadata("ki06-dns"), "data": ZONE_FILES})
        put(pod_spec(BASELINE))
        self.healthy = until(lambda: self.probe(False),
                             lambda r: r["control"]["ready"] and is_dns_healthy(r["control_dns"]),
                             "healthy Pod and its network were not ready", 90)
        self.contract = {"configuration": CNI, "plugin_checksums": self.checksums,
                         "plugin_supported_versions": self.versions["supportedVersions"],
                         "pod_image": IMAGE, "zone": ZONE_FILES, "pod_names": list(NAMES)}

    def probe(self, targets=True):
        control = pod_state(BASELINE)
        control_dns = dns_checks(control) if control["ready"] else []
        pods = [pod_state(name) for name in TARGETS] if targets else []
        return {"at": now(), "cni_version": cni_version(), "control": control, "control_dns": control_dns,
                "targets": pods, "target_dns": {pod["name"]: dns_checks(pod) for pod in pods if pod["ready"]},
                "events": fault_events(pods)}

    @staticmethod
    def pending(sample):
        return (sample["cni_version"] == INCOMPATIBLE and is_dns_healthy(sample["control_dns"])
                and len(sample["targets"]) == len(sample["events"]) == 2
                and all(pod["phase"] == "Pending" and not pod["ready"] and not pod["pod_ip"] for pod in sample["targets"]))

    @staticmethod
    def recovered(sample):
        return (sample["cni_version"] == READY and is_dns_healthy(sample["control_dns"])
                and len(sample["targets"]) == len(sample["target_dns"]) == 2
                and all(pod["phase"] == "Running" and pod["ready"] and pod["pod_ip"] for pod in sample["targets"])
                and all(is_dns_healthy(checks) for checks in sample["target_dns"].values()))

    def prepare(self):
        set_cni(INCOMPATIBLE)
        sleep(2)
        for name in TARGETS:
            put(pod_spec(name))
        faulty = until(self.probe, self.pending, "the two real kubelet sandbox faults were not observed", 75)
        first = stable_error(faulty["events"][0])
        assert all(stable_error(e) == first for e in faulty["events"])
        self.started = monotonic()
        return {"case": CASE, "kubernetes": K8S_VERSION, "initial_error_log": first,
                "healthy_before": self.healthy, "fault_before": faulty, "fixture_contract": self.contract,
                "fixture_sha256": fingerprint([CASE, K8S_VERSION, self.contract])}

    def verify(self):
        status = self.recovered if cni_version() == READY else self.pending
        first = until(self.probe, status, "independent sandbox/network verification", 80)
        sleep(1.5)
        second = until(self.probe, status, "repeated independent sandbox/network verification", 15)
        return {"independent_probes": [first, second], "wall_duration_ms": round((monotonic()-self.started)*1000)}

    def cleanup(self):
        if self.created:
            set_cni(READY)
            kube("delete", "namespace", NAMESPACE, "--timeout=40s")
            assert kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name") == ""
            self.created = False
        return {"stopped": True, "resources_removed": True}

    def handle(self, request):
        tool = request["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool == "probe":
            return self.probe()
        if tool == "workload":
            return {"node": NODE, "pods": [pod_state(name) for name in NAMES], "kubernetes": K8S_VERSION}
        if tool == "logs":
            return {"sandbox_events": fault_events([pod_state(name) for name in TARGETS])}
        if tool == "source":
            return {"cni_setting": "cniVersion", "current": json.loads(CNI_PATH.read_text()),
                    "plugin_versions": self.versions["supportedVersions"]}
        if tool == "configure":
            if request.get("key") != "cniVersion" or request.get("value") != READY or cni_version() != INCOMPATIBLE:
                return {"applied": False, "reason": "only the fixture CNI specification version can be restored"}
            set_cni(READY)
            return {"applied": True, "before": INCOMPATIBLE, "after": READY, "network_unchanged": True}
        if tool == "verify":
            return self.verify()
        if tool == "shutdown":
            return self.cleanup()
        raise ValueError("invalid fixture operation")


def main():
    observer = None
    if sys.argv[1:] == ["--observe"]:
        spec = importlib.util.spec_from_file_location("observer", WORK / "observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        observer.NAMESPACE = NAMESPACE
        observer.TARGET = BASELINE
        observer.OBSERVER_SOCKET = OBSERVER_SOCKET
        observer.kubeconfig = WORK / "kubeconfig"
    trial = Trial()
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
