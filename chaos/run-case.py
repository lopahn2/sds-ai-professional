#!/usr/bin/env python3
"""KAN-71 operator runner for the 16 Kubernetes-API-owned v1 cases only.

No ground-truth fields leave this process. Do not run alongside another fault worker.
The 44 target-API cases are verified by KAN-96's separate evidence, not this runner.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import fcntl
import json
from math import ceil
import os
from pathlib import Path
import re
import signal as process_signal
import subprocess
import sys
import time

from validate import ROOT, check, validate_catalog

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TARGETS = ("inventory", "order")
CONTEXT = "rancher-desktop"
NS = "liveprobe-target"
MODES = {"bad_image", "selector_mismatch"}


class Stop(RuntimeError):
    pass


class NodeGateStop(Stop):
    def __init__(self, snapshot, breaches):
        super().__init__("KAN-117 node stop gate breached")
        self.snapshot = snapshot
        self.breaches = breaches


def kubectl(*args, timeout=35, stdin=None):
    cmd = ["kubectl", "--context", CONTEXT, "-n", NS, *args]
    result = subprocess.run(cmd, input=stdin, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # kubectl errors may include metadata; never print arbitrary response bodies.
        raise Stop(f"kubectl {' '.join(args[:2])} failed (exit {result.returncode})")
    return result.stdout


def obj(kind, name):
    return json.loads(kubectl("get", kind, name, "-o", "json"))


def endpoints(target):
    slices = json.loads(kubectl("get", "endpointslices", "-l", f"kubernetes.io/service-name={target}", "-o", "json"))
    return sum(e.get("conditions", {}).get("ready") is True
               for item in slices["items"] for e in (item.get("endpoints") or []))


def pod_status(target):
    items = json.loads(kubectl("get", "pods", "-l", f"app={target}", "-o", "json"))["items"]
    return [(p["metadata"]["name"], p["metadata"]["uid"], c)
            for p in items for c in p.get("status", {}).get("containerStatuses", []) if c["name"] == target]


def order_check():
    result = kubectl("exec", "deploy/loadgen", "-c", "loadgen", "--", "curl", "-s", "-o", "/dev/null",
                     "-m", "5", "-w", "%{http_code}", "http://order.liveprobe-target.svc:8080/api/orders/check?item=a")
    return result.strip() == "200"


def default_routes():
    for target in TARGETS:
        code = kubectl("exec", f"deploy/{target}", "-c", target, "--", "curl", "-s", "-o", "/dev/null",
                       "-m", "5", "-w", "%{http_code}", "http://127.0.0.1:8080/internal/chaos/v1/faults")
        if code.strip() != "404":
            raise Stop("default chaos route is not disabled")


def pg_sessions():
    output = subprocess.run(["kubectl", "--context", CONTEXT, "-n", "liveprobe-system", "exec", "postgres-0",
                             "--", "psql", "-U", "liveprobe", "-d", "liveprobe", "-X", "-A", "-t", "-c",
                             "select count(*) from pg_stat_activity where usename='inventory' and backend_type='client backend'"],
                            text=True, capture_output=True, timeout=15)
    if output.returncode or not output.stdout.strip().isdigit():
        raise Stop("PostgreSQL session check unavailable")
    return int(output.stdout.strip())


def kubelet_node_usage(node_name):
    data = json.loads(kubectl("get", "--raw", f"/api/v1/nodes/{node_name}/proxy/stats/summary"))
    node = data["node"]
    cpu, memory = node["cpu"], node["memory"]
    now = datetime.now(timezone.utc)
    for sample in (cpu, memory):
        observed = datetime.fromisoformat(sample["time"].replace("Z", "+00:00"))
        if not -5 <= (now - observed).total_seconds() <= 60:
            raise ValueError("sample outside the observation window")
    cpu_nanos, memory_bytes = cpu["usageNanoCores"], memory["workingSetBytes"]
    if (node.get("nodeName") != node_name or type(cpu_nanos) is not int or cpu_nanos < 0
            or type(memory_bytes) is not int or memory_bytes <= 0):
        raise ValueError("node resource observations incomplete")
    return (cpu_nanos + 999_999) // 1_000_000, (memory_bytes + 1_048_575) // 1_048_576


def kubelet_resource_sample(node_name):
    # The authenticated API server proxy binds these node-wide metrics to the named node.
    resource = kubectl("get", "--raw", f"/api/v1/nodes/{node_name}/proxy/metrics/resource", timeout=16)
    cpu_metric, memory_metric = "node_cpu_usage_seconds_total", "node_memory_working_set_bytes"
    expected = {cpu_metric, memory_metric, "resource_scrape_error"}
    metrics = {}
    for line in resource.splitlines():
        fields = line.split()
        if fields and fields[0] in expected:
            if fields[0] in metrics:
                raise ValueError("ambiguous node resource metrics")
            metrics[fields[0]] = fields[1:]
    cpu, cpu_time = metrics[cpu_metric]
    memory, memory_time = metrics[memory_metric]
    error, = metrics["resource_scrape_error"]
    if Decimal(error) != 0:
        raise ValueError("resource scrape did not succeed")
    timestamp = int(cpu_time)
    cpu, memory = Decimal(cpu), Decimal(memory)
    if (timestamp != int(memory_time) or not -5000 <= time.time() * 1000 - timestamp <= 60_000
            or not cpu.is_finite() or cpu < 0 or not memory.is_finite() or memory <= 0):
        raise ValueError("node resource samples invalid or stale")
    return timestamp, cpu, memory


def kubelet_resource_usage(node_name):
    first_time, first_cpu, first_memory = kubelet_resource_sample(node_name)
    for _ in range(2):
        time.sleep(10)
        timestamp, cpu, memory = kubelet_resource_sample(node_name)
        if timestamp == first_time:
            continue
        elapsed_ms = timestamp - first_time
        if (elapsed_ms < 5000 or time.time() * 1000 - first_time > 60_000
                or cpu < first_cpu):
            raise ValueError("node resource counters not comparable")
        return ceil((cpu - first_cpu) * 1_000_000 / elapsed_ms), ceil(max(first_memory, memory) / 1_048_576)
    raise ValueError("node resource metrics did not advance")


def node_gate():
    node = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    if len(node) != 1:
        raise Stop("node count is not one")
    status = node[0]["status"]
    cond = {v["type"]: v["status"] for v in status["conditions"]}
    try:
        top = kubectl("top", "node", "--no-headers").split()
        if (len(top) != 5 or top[0] != node[0]["metadata"]["name"]
                or not re.fullmatch(r"\d+m", top[1]) or not re.fullmatch(r"\d+Mi", top[3])):
            raise ValueError("unexpected top response")
        cpu, mem = int(top[1][:-1]), int(top[3][:-2])
        source = "metrics_api"
    except (Stop, ValueError, subprocess.TimeoutExpired):
        try:
            cpu, mem = kubelet_node_usage(node[0]["metadata"]["name"])
            source = "kubelet_summary"
        except (Stop, KeyError, AttributeError, TypeError, ValueError, subprocess.TimeoutExpired):
            try:
                cpu, mem = kubelet_resource_usage(node[0]["metadata"]["name"])
                source = "kubelet_resource"
            except (Stop, KeyError, TypeError, ValueError, DecimalException, subprocess.TimeoutExpired):
                raise Stop("node metrics unavailable") from None
    alloc = status["allocatable"]["memory"]
    if not alloc.endswith("Ki"):
        raise Stop("unknown node memory unit")
    capacity = int(alloc[:-2]) / 1024
    # KAN-117 gate was written for a 2-CPU node (1400m = 70%). Keep the same 70% ratio for the actual CPU count.
    cpu_cap = int(status["allocatable"]["cpu"].rstrip("m")) * (1 if status["allocatable"]["cpu"].endswith("m") else 1000)
    sample = {"cpu_m": cpu, "cpu_cap_m": cpu_cap, "mem_mi": mem, "alloc_mi": round(capacity), "metrics_source": source}
    breached = {"cpu": cpu >= cpu_cap * .7, "memory": mem >= capacity * .7,
                "memory_headroom": capacity - mem < 2048, "not_ready": cond.get("Ready") != "True"}
    breached.update({condition: cond.get(condition) != "False"
                     for condition in ("MemoryPressure", "DiskPressure", "PIDPressure")})
    if any(breached.values()):
        raise NodeGateStop(sample, [name for name, failed in breached.items() if failed])
    return sample


def snapshot():
    data = {}
    for target in TARGETS:
        dep = obj("deployment", target)
        svc = obj("service", target)
        containers = [c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == target]
        if len(containers) != 1 or dep["spec"].get("replicas") != 1:
            raise Stop("unexpected deployment shape")
        data[target] = {"image": containers[0]["image"], "selector": svc["spec"]["selector"]}
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", data[target]["image"].split("@")[-1]):
            raise Stop("baseline image is not digest pinned")
        if data[target]["selector"] != {"app": target}:
            raise Stop("unexpected service selector")
    return data


def healthy(before, stable=30, pg_max=None):
    default_routes()
    for target in TARGETS:
        dep = obj("deployment", target)
        image = next(c["image"] for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == target)
        if (image != before[target]["image"] or dep["status"].get("readyReplicas") != 1 or
                dep["status"].get("updatedReplicas") != 1 or obj("service", target)["spec"]["selector"] != before[target]["selector"] or
                endpoints(target) < 1):
            raise Stop(f"baseline state unhealthy: {target}")
    if not all(order_check() for _ in range(3)):
        raise Stop("order data path not 3x200")
    if pg_max is not None and pg_sessions() > pg_max:
        raise Stop("inventory DB sessions above baseline")
    first = {t: [(uid, c.get("restartCount")) for _, uid, c in pod_status(t)] for t in TARGETS}
    if stable:
        time.sleep(stable)
        for target in TARGETS:
            dep = obj("deployment", target)
            if dep["status"].get("readyReplicas") != 1 or endpoints(target) < 1:
                raise Stop("readiness lost during stability wait")
        if first != {t: [(uid, c.get("restartCount")) for _, uid, c in pod_status(t)] for t in TARGETS}:
            raise Stop("restart/Pod identity unstable")
    return True


def save(path, data):
    # Operator-local, fail closed if evidence/journal cannot be persisted.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def restore(before, target, mode):
    if mode == "bad_image":
        current = obj("deployment", target)
        actual = next(c["image"] for c in current["spec"]["template"]["spec"]["containers"] if c["name"] == target)
        if actual != before[target]["image"]:
            kubectl("set", "image", f"deployment/{target}", f"{target}={before[target]['image']}")
        kubectl("rollout", "status", f"deployment/{target}", "--timeout=120s", timeout=135)
    else:
        current = obj("service", target)["spec"].get("selector")
        if current != before[target]["selector"]:
            # Replace the selector map so extra keys introduced during the trial are removed.
            kubectl("patch", "service", target, "--type=json", "-p", json.dumps([
                {"op": "add", "path": "/spec/selector", "value": before[target]["selector"]},
            ]))


def signal(target, mode, old_uids, duration=90):
    deadline = time.monotonic() + duration
    seen = []
    while time.monotonic() < deadline:
        if mode == "bad_image":
            for name, uid, state in pod_status(target):
                reason = state.get("state", {}).get("waiting", {}).get("reason")
                if uid not in old_uids and reason in ("ErrImagePull", "ImagePullBackOff"):
                    return {"new_pod": name, "reason": reason}
        elif endpoints(target) == 0:
            return {"ready_endpoints": 0}
        time.sleep(1)
    raise Stop("expected signal not observed")


def case_file(cid):
    if not re.fullmatch(r"F[14]-\d{2}", cid):
        raise Stop("unsupported case id")
    path = ROOT / f"{cid}.yaml"
    if not path.is_file():
        raise Stop("missing case")
    case = json.loads(path.read_text())
    if check(case) or case["id"] != cid or case["injection"]["mode"] not in MODES:
        raise Stop("invalid or target-owned case")
    return case


def run(cid, evidence):
    case = case_file(cid)
    target, mode = case["target"], case["injection"]["mode"]
    gate = node_gate()
    before = snapshot()
    healthy(before, stable=0)
    pg_base = pg_sessions()
    old_uids = {uid for _, uid, _ in pod_status(target)}
    journal = evidence / "active.json"
    if journal.exists():
        raise Stop("unfinished case journal exists; restore it before another case")
    save(journal, {"id": cid, "target": target, "mode": mode, "snapshot": before})
    symptom, failure = None, None
    try:
        params = case["injection"]["parameters"]
        if mode == "bad_image":
            kubectl("set", "image", f"deployment/{target}", f"{target}={params['tag']}")
        else:
            kubectl("patch", "service", target, "--type=merge", "-p",
                    json.dumps({"spec": {"selector": {"app": params["selector_value"]}}}))
        symptom = signal(target, mode, old_uids)
    except Exception as exc:
        failure = type(exc).__name__ + ": " + str(exc)
    finally:
        try:
            restore(before, target, mode)
            end = time.monotonic() + 120
            while True:
                try:
                    healthy(before, stable=30, pg_max=pg_base)
                    break
                except Stop:
                    if time.monotonic() >= end:
                        raise
                    time.sleep(3)
        except Exception as exc:
            failure = (failure or "") + "; RESTORE FAILED: " + type(exc).__name__ + ": " + str(exc)
    status = "PASS" if symptom and not failure else "FAIL"
    record = {"case": cid, "status": status, "target": target, "mode": mode,
              "node_gate": gate, "signal": symptom, "baseline_restored": "RESTORE FAILED" not in (failure or ""),
              "error": failure}
    with (evidence / "results.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if record["baseline_restored"]:
        journal.unlink()
    print(f"{cid}: {status} (baseline_restored={record['baseline_restored']})", flush=True)
    if status != "PASS":
        raise Stop("case failed; campaign stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, choices=[CONTEXT])
    parser.add_argument("--namespace", required=True, choices=[NS])
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="without this flag, catalog/list only; no kubectl")
    parser.add_argument("ids", nargs="*")
    args = parser.parse_args()
    if args.execute:
        def interrupted(signum, _frame):
            raise Stop(f"interrupted by signal {signum}; restoring")
        for sig in (process_signal.SIGINT, process_signal.SIGTERM):
            process_signal.signal(sig, interrupted)
    errors, _ = validate_catalog()
    if errors:
        raise Stop("catalog failed validation")
    ids = args.ids or [f"F{f}-{n:02d}" for f in (1, 4) for n in range(1, 16, 2)]
    for cid in ids:
        case_file(cid)
    if not args.execute:
        print("validated runner-owned cases: " + ", ".join(ids))
        return
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.evidence.is_symlink() or args.evidence.stat().st_mode & 0o077:
        raise Stop("evidence directory must be private (0700)")
    with (args.evidence / "runner.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("another runner owns this evidence directory")
        if (args.evidence / "active.json").exists():
            raise Stop("unfinished journal; manually verify/restore before rerun")
        for cid in ids:
            run(cid, args.evidence)


if __name__ == "__main__":
    try:
        main()
    except (Stop, subprocess.TimeoutExpired) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        sys.exit(3)
