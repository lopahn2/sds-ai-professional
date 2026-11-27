"""KI-10 in a disposable, offline single-node K3s cluster."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep


CASE = "KI-10"
NAMESPACE = "dev-ki10-verify"
WORKLOAD = "ki10-coredns"
NODE = "ki10-fixture"
API_VERSION = "v1.34.13+k3s1"
REPOSITORY = "example.invalid/liveprobe-ki10/coredns"
VERSIONS = ("1.5.2", "1.6.0")
LEGACY = ["-cpu", "1"]
ERROR = "flag provided but not defined: -cpu"
ZONE = "ki10.verify.invalid"
ANSWER = "192.0.2.77"
QUERY = f"order.{ZONE}."
PORT = 10553
COMMAND = ["-conf", "/dns/Corefile"]
FILES = {
    "Corefile": f"{ZONE}:{PORT} {{\n    bind 127.0.0.1\n    file /dns/zone.db {ZONE}\n}}\n",
    "zone.db": "$TTL 30\n@ IN SOA ns. hostmaster. 1 60 30 3600 30\n@ IN NS ns.\n"
               f"order IN A {ANSWER}\n",
}
LABELS = {"app": WORKLOAD, "app.kubernetes.io/part-of": "liveprobe-ki10"}
K3S = "/opt/isolated/k3s-arm64"
STATE = Path("/tmp/lp-ki10-k3s")


def kubectl(*args, obj=None, required=True):
    result = subprocess.run(
        [K3S, "kubectl", "--kubeconfig", str(STATE / "kubeconfig"), "--request-timeout=8s", *args],
        input=json.dumps(obj) if obj is not None else None, capture_output=True, text=True, timeout=12,
    )
    if required and result.returncode:
        raise RuntimeError(f"test Kubernetes command failed: {args[0]}: {result.stderr[:300]}")
    return result.stdout if required else result


def get(*args):
    return json.loads(kubectl("-n", NAMESPACE, "get", *args, "-o", "json"))


def put(obj):
    return json.loads(kubectl("create", "-f", "-", "-o", "json", obj=obj))


def metadata(name):
    return {"namespace": NAMESPACE, "name": name, "labels": LABELS}


def deployment(version):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata(WORKLOAD),
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": WORKLOAD}}, "strategy": {"type": "Recreate"},
                 "template": {"metadata": {"labels": LABELS}, "spec": {
                     "nodeName": NODE, "hostNetwork": True, "dnsPolicy": "Default",
                     "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 1,
                     "containers": [{"name": "coredns", "image": f"{REPOSITORY}:{version}", "imagePullPolicy": "Never",
                                     "args": [*LEGACY, *COMMAND],
                                     "resources": {"requests": {"memory": "24Mi", "cpu": "10m"},
                                                   "limits": {"memory": "128Mi", "cpu": "200m"}},
                                     "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                         "capabilities": {"drop": ["ALL"]},
                                                         "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                         "seccompProfile": {"type": "RuntimeDefault"}},
                                     "volumeMounts": [{"name": "dns", "mountPath": "/dns", "readOnly": True}]}],
                     "volumes": [{"name": "dns", "configMap": {"name": WORKLOAD}}]}}},
    }


def probes():
    result = []
    for transport in ("udp", "tcp"):
        run = subprocess.run(
            ["dig", "-4", "@127.0.0.1", "-p", str(PORT), QUERY, "A", "+time=1", "+tries=1", "+norecurse",
             "+noall", "+answer", "+tcp" if transport == "tcp" else "+notcp"],
            text=True, capture_output=True, timeout=4,
        )
        rows = [line.split() for line in run.stdout.splitlines() if line and not line.startswith(";")]
        answer = ANSWER if len(rows) == 1 and (rows[0][0], *rows[0][2:]) == (QUERY, "IN", "A", ANSWER) else None
        result.append({"transport": transport, "exit_code": run.returncode, "answer": answer})
    return result


def ready(checks):
    return [row["transport"] for row in checks] == ["udp", "tcp"] and all(
        (row["exit_code"], row["answer"]) == (0, ANSWER) for row in checks)


def failed(checks):
    return [row["transport"] for row in checks] == ["udp", "tcp"] and all(
        (row["exit_code"], row["answer"]) == (9, None) for row in checks)


def state():
    dep = get("deployment", WORKLOAD)
    container, = dep["spec"]["template"]["spec"]["containers"]
    matches = [pod for pod in get("pods", "-l", f"app={WORKLOAD}")["items"]
               if not pod["metadata"].get("deletionTimestamp") and
               pod["spec"]["containers"][0]["image"] == container["image"] and
               pod["spec"]["containers"][0]["args"] == container["args"]]
    pod, = matches if len(matches) == 1 else [{}]
    running = (pod.get("status", {}).get("containerStatuses") or [{}])[0]
    return {"deployment_uid": dep["metadata"]["uid"], "revision": dep["metadata"].get(
                "annotations", {}).get("deployment.kubernetes.io/revision"),
            "resource_version": dep["metadata"]["resourceVersion"], "image": container["image"], "args": container["args"],
            "generation": dep["metadata"]["generation"], "observed_generation": dep.get("status", {}).get("observedGeneration"),
            "available_replicas": dep.get("status", {}).get("availableReplicas", 0),
            "pod": pod.get("metadata", {}).get("name"), "pod_uid": pod.get("metadata", {}).get("uid"),
            "replica_set_uid": (pod.get("metadata", {}).get("ownerReferences") or [{}])[0].get("uid"),
            "image_id": running.get("imageID"), "ready": running.get("ready"),
            "container_state": running.get("state", {}), "last_state": running.get("lastState", {})}


def wait_state(predicate, *, timeout=80):
    deadline = monotonic() + timeout
    last = {}
    while monotonic() < deadline:
        last = state()
        if predicate(last):
            return last
        sleep(.35)
    raise RuntimeError(f"test Deployment did not reach its expected state: {json.dumps(last)[:650]}")


def ready_state():
    return wait_state(lambda s: s["ready"] is True and s["observed_generation"] == s["generation"]
                      and s["available_replicas"] == 1 and s["pod_uid"] and s["replica_set_uid"])


def patch(change):
    previous = state()
    guard = {"op": "test", "path": "/metadata/resourceVersion", "value": previous["resource_version"]}
    prefix = "/spec/template/spec/containers/0/"
    fields = [{"op": "replace", "path": prefix + key, "value": value} for key, value in change.items()]
    result = kubectl("-n", NAMESPACE, "patch", "deployment", WORKLOAD, "--type=json", "-p",
                     json.dumps([guard, *fields]), "-o", "json", required=False)
    if result.returncode:
        raise RuntimeError("the guarded fixture update was rejected")
    return previous


def initial_error(info):
    if not info["pod"]:
        return None
    for options in ([], ["--previous"]):
        result = kubectl("-n", NAMESPACE, "logs", info["pod"], *options, "-c", "coredns", required=False)
        lines = result.stdout.splitlines() if result.returncode == 0 else []
        if lines and lines[0] == ERROR:
            return lines[0]
    return None


def scope():
    config = get("configmap", WORKLOAD)
    spec = get("deployment", WORKLOAD)["spec"]
    container, = spec["template"]["spec"]["containers"]
    container.pop("args")
    container.pop("image")
    assert config["data"] == FILES
    return {"config_uid": config["metadata"]["uid"],
            "sha256": sha256(json.dumps({"config": FILES, "deployment": spec}, sort_keys=True).encode()).hexdigest()}


def prepare(*, namespace_prepared=False):
    assert json.loads(kubectl("get", "--raw=/version"))["gitVersion"] == API_VERSION
    node, = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    assert node["metadata"]["name"] == NODE
    assert failed(probes())
    if not namespace_prepared:
        put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": LABELS}})
    put({"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(WORKLOAD), "immutable": True, "data": FILES})
    put(deployment(VERSIONS[0]))
    baseline = ready_state()
    baseline_checks = probes()
    assert ready(baseline_checks)
    scope_before = scope()
    patch({"image": f"{REPOSITORY}:{VERSIONS[1]}"})
    fault = wait_state(lambda row: row["pod_uid"] != baseline["pod_uid"] and
                       any(s.get("terminated", {}).get("exitCode", 0) != 0 for s in (row["container_state"], row["last_state"]))
                       and initial_error(row) == ERROR)
    first = monotonic()
    fault_checks = probes()
    assert failed(fault_checks) and scope() == scope_before
    return {"case": CASE, "kubernetes": API_VERSION, "scope_before": scope_before,
            "baseline": {"state": baseline, "probes": baseline_checks},
            "fault": {"state": fault, "first_error": ERROR, "probes": fault_checks}}, first


def cleanup():
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", "--timeout=55s")
    assert not kubectl("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name").strip()
    assert failed(probes())


def serve():
    armed = json.loads(sys.stdin.readline()) == {"tool": "prepare"}
    if not armed:
        raise ValueError("expected test preparation")
    try:
        result, started = prepare()
        print(json.dumps(result), flush=True)
        for item in sys.stdin:
            entry = json.loads(item)
            tool = entry.get("tool")
            if tool == "workload":
                response = state()
            elif tool == "logs":
                response = {"startup_error": initial_error(state())}
            elif tool == "source":
                response = {"launchArguments": state()["args"][:-len(COMMAND)], "dns_config": FILES}
            elif tool == "probe":
                response = {"probes": probes()}
            elif tool == "configure":
                current = state()
                try:
                    arguments = json.loads(entry.get("value") or "null")
                except (TypeError, ValueError):
                    arguments = None
                permitted = (entry.get("key") == "launchArguments" and arguments == []
                             and current["args"] == [*LEGACY, *COMMAND]
                             and current["image"] == f"{REPOSITORY}:{VERSIONS[1]}")
                if permitted:
                    patch({"args": COMMAND})
                response = {"applied": permitted, "launchArguments": [] if permitted else current["args"][:-len(COMMAND)]}
            elif tool == "verify":
                corrected = state()["args"] == COMMAND
                final = ready_state() if corrected else state()
                queries = probes()
                assert (ready(queries) if corrected else failed(queries)) and scope() == result["scope_before"]
                response = {"recovery": {"state": final, "probes": queries}, "scope_after": scope(),
                            "wall_duration_ms": round((monotonic() - started) * 1000)}
            elif tool == "shutdown":
                break
            else:
                response = {"accepted": False, "reason": "unrecognized fixture operation"}
            print(json.dumps(response), flush=True)
    finally:
        cleanup()
    print(json.dumps({"removed": True, "dns_absent": True}), flush=True)


if __name__ == "__main__":
    # The broker is entered only through the dedicated VM's K3s network namespace.
    if os.geteuid() != 0 or os.environ.get("K3S_DATA_DIR") != str(STATE / "state"):
        raise SystemExit("requires the owned offline fixture")
    serve()
