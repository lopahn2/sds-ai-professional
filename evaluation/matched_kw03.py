"""Run one matched KW-03 arm against a prepared, isolated dev Deployment."""

from __future__ import annotations

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Lock
from time import monotonic, sleep

from evaluation import k8s_dev_image_trial as cluster, k8s_dev_rollout_trial as rollout
from evaluation.dev_rollout_case import VOLUME as CASE
from evaluation.isolated_recovery_agent import Unobserved, run_agent
from evaluation.k8s_dev_volume_fixture import volume_bindings
from evaluation.matched_recovery_campaign import (
    ARMS, CONTEXT, NS, RECOVERY_BUDGET_S, PreflightBlocked, canonical_digest, dev_preflight, matched_start, write_new,
)


def tool(name, description, fields=None):
    properties = fields or {}
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": list(properties), "additionalProperties": False,
    }}


TOOLS = [
    tool("read_deployment", "Show the target Deployment's current claim reference and owned ReplicaSets."),
    tool("read_warning", "Read the scheduled Pod and its current kubelet mount warning."),
    tool("read_storage", "Read the current namespace's provisioned claims and their binding state for this deployment."),
    tool("update_claim_reference", "Update the isolated Deployment's read-only claim reference using its current value.", {
         "current_claim": {"type": "string"}, "claim": {"type": "string"}}),
]
MOUNT_DIGEST_PROGRAM = """import hashlib, json, pathlib
root = pathlib.Path('/fixture-volume')
entries = []
for item in sorted(root.rglob('*')):
    if item.is_symlink() or not (item.is_file() or item.is_dir()):
        raise ValueError('unexpected fixture volume entry')
    path = str(item.relative_to(root))
    if item.is_file():
        if item.stat().st_size > 1048576:
            raise ValueError('fixture file exceeds calibrated size')
        entries.append((path, hashlib.sha256(item.read_bytes()).hexdigest()))
    else:
        entries.append((path, None))
print(hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest())
"""


def independent_health(baseline, deadline):
    proof = rollout.verify(baseline, deadline, CASE)
    deployment = rollout.current(CASE)
    _, pods = rollout.deployment_pods(deployment, baseline, CASE)
    if len(pods) != 1 or pods[0]["metadata"]["uid"] != proof["pod_uid"]:
        raise cluster.Refused("healthy Pod changed during independent volume verification")
    pod = pods[0]["metadata"]["name"]
    digest = cluster.command("exec", f"pod/{pod}", "-c", "verification", "--", "python", "-B", "-c", MOUNT_DIGEST_PROGRAM).strip()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise cluster.Refused("mounted data digest unavailable")
    return {**proof, "mounted_data_sha256": digest, "volume_bindings": volume_bindings()}


def same_data(before, after):
    return all(before[key] == after[key] for key in ("mounted_data_sha256", "volume_bindings"))


class VolumeBroker:
    def __init__(self, baseline, fault, lease, deadline):
        self.baseline, self.fault, self.lease, self.deadline = baseline, fault, lease, deadline
        self.guard = Lock()
        self.closed = False
        self.change_acknowledged = False

    def close(self):
        with self.guard:
            self.closed = True

    def __call__(self, method, args):
        with self.guard:
            if self.closed:
                raise Unobserved("the coordinator is already restoring the fixture")
            return self.request(method, args)

    def request(self, method, args):
        if monotonic() >= self.deadline or method not in {entry["name"] for entry in TOOLS}:
            raise Unobserved("tool is outside the active fixture scope")
        dep = rollout.current(CASE)
        claim = rollout.fixture_path(dep, self.baseline, CASE)
        if method == "update_claim_reference":
            if (set(args) != {"current_claim", "claim"} or args["current_claim"] != claim
                    or (claim, args["claim"]) != (CASE.broken, CASE.healthy) or self.change_acknowledged):
                return {"updated": False, "reason": "the requested claim transition is outside the approved fixture"}
            self.lease.require_active()
            if volume_bindings() != self.baseline["volume_bindings"]:
                raise Unobserved("the approved claim bindings changed before the proposed write")
            rollout.patch_path(dep, self.baseline, claim, args["claim"], CASE)
            self.change_acknowledged = True
            return {"updated": True, "target": CASE.target, "claim": args["claim"]}
        if args:
            raise Unobserved("the read tool takes no arguments")
        if method == "read_storage":
            return {name: {"phase": "Bound", "persistent_volume": row["pv"]} for name, row in volume_bindings().items()}
        if method == "read_warning":
            return {"pod_uid": self.fault["pod_uid"], "message": self.fault["error"], "event": CASE.signal,
                    "pod_scheduled": self.fault["pod_scheduled"], "volume_claim": self.fault["claim"]}
        owned = []
        for replica in cluster.get("rs", selector=CASE.selector)["items"]:
            if rollout.owned_by(replica, "Deployment", self.baseline["uid"]):
                data, key = CASE.field(replica)
                owned.append({"name": replica["metadata"]["name"], "claim": data[key],
                              "available_replicas": (replica.get("status") or {}).get("availableReplicas", 0)})
        return {"deployment": CASE.target, "claim": claim, "replicasets": owned}


def product_candidate():
    status = cluster.product_status("/api/status")
    if (not status.get("ready") or status.get("target_namespaces") != [NS]
            or "pvc_reference_restore" not in status.get("allowlist", [])
            or (status.get("watchers", {}).get("workload_poller/dev") or {}).get("state") != "running"):
        raise cluster.Refused("the scoped LiveProbe detector and PVC reference action are not ready")


def run(arm, baseline, reference, binary, auth, output, journal):
    product_candidate()
    gate = dev_preflight()
    node, = cluster.get("nodes")["items"]
    before = independent_health(baseline, monotonic() + 15)
    if before["volume_bindings"] != baseline["volume_bindings"]:
        raise cluster.Refused("the ready test fixture changed its PVC–PV binding")
    if cluster.query("SELECT count(*) FROM incidents WHERE namespace='dev' AND workload='lp-eval-kw03' "
                     "AND status IN ('open','analyzing','acting','action_pending')"):
        raise cluster.Refused("a previous incident is still open for this fixture")
    cursor = cluster.query("SELECT coalesce(max(id),0) FROM incidents")
    result = {"case": CASE.case_id, "arm": arm, "target": CASE.target, "headroom": gate, "healthy_before": before,
              "status": "unobserved"}
    start = {"context": CONTEXT, "namespace": NS, "node_uid": node["metadata"]["uid"],
             "deployment_uid": baseline["uid"], "fixture_digest": canonical_digest({
                 "spec": baseline["fixture_sha256"], "data": before["mounted_data_sha256"], "bindings": before["volume_bindings"]}),
             "fault_digest": canonical_digest({"field": CASE.pointer, "from": CASE.healthy, "to": CASE.broken}),
             "verifier_digest": sha256(Path(__file__).read_bytes()).hexdigest()}
    if reference and any(reference["start_evidence"].get(key) != value for key, value in start.items()):
        raise cluster.Refused("initial state differs from the reference arm")
    write_new(journal, {"target": CASE.target, "uid": baseline["uid"], "field": CASE.pointer,
                        "original_value": CASE.healthy, "failed_value": CASE.broken, "receipt": str(output)})
    lease, mutation_attempted, clean, future, broker = None, False, False, None, None
    with ThreadPoolExecutor(max_workers=1) as agents:
        try:
            if arm != "liveprobe":
                lease = cluster.WorkloadLease.claim(CASE, arm, ttl=900)
            dev_preflight()
            mutation_attempted = True
            rollout.patch_path(rollout.current(CASE), baseline, CASE.healthy, CASE.broken, CASE)
            began = monotonic()
            deadline, renew_at = began + RECOVERY_BUDGET_S, began + 240
            result["injected_at"] = datetime.now(timezone.utc).isoformat()
            observed = None
            while monotonic() < deadline:
                if lease and monotonic() >= renew_at:
                    lease.renew(900)
                    renew_at = monotonic() + 240
                if observed is None:
                    observed = rollout.observe(baseline, before["generation"], CASE)
                    if observed:
                        fault = rollout.evidence(observed, CASE)
                        result.update(fault=fault, first_error_ms=round((monotonic() - began) * 1000))
                        start["initial_error_digest"] = sha256(fault["error"].encode()).hexdigest()
                        result.update(start_evidence=start, matched_start=matched_start(start))
                        if reference and reference["matched_start"] != result["matched_start"]:
                            raise cluster.Refused("the first native error differs from the reference arm")
                        if lease:
                            broker = VolumeBroker(baseline, fault, lease, deadline)
                            future = agents.submit(run_agent, binary=binary, auth_file=auth, arm=arm,
                                error_line=fault["error"], tools=TOOLS, broker=broker, deadline=deadline)
                incident = rollout.incident(cursor, CASE)
                if lease and incident:
                    raise cluster.Refused("LiveProbe observed the standalone fixture despite the coordinator lease")
                if not lease and incident:
                    result["incident"] = incident
                    if "detected_ms" not in result and CASE.signal in incident["signal_reasons"]:
                        result["detected_ms"] = round((monotonic() - began) * 1000)
                if observed and "recovery_ms" not in result:
                    deployment = rollout.current(CASE)
                    if rollout.fixture_path(deployment, baseline, CASE) == CASE.healthy and cluster.is_settled(deployment):
                        proof = independent_health(baseline, min(deadline, monotonic() + 8))
                        if not same_data(before, proof):
                            raise cluster.Refused("the data or bindings changed before independent verification")
                        result.update(verified_before_cleanup=proof, recovery_ms=round((monotonic() - began) * 1000))
                if future and future.done():
                    result["standalone"] = future.result()
                    if result["standalone"].get("status") != "completed" or not broker.change_acknowledged or "recovery_ms" in result:
                        break
                if incident and incident["status"] not in cluster.NONTERMINAL:
                    break
                sleep(2)
            if future and "standalone" not in result:
                result["standalone"] = future.result(timeout=3)
            attributed = (broker.change_acknowledged and result.get("standalone", {}).get("status") == "completed") if broker else any(
                row["type"] == "pvc_reference_restore" and row["status"] == "succeeded" for row in (result.get("incident", {}).get("actions") or []))
            if "recovery_ms" in result and attributed:
                result["status"] = "recovered"
            elif (observed and "recovery_ms" not in result and (attributed or result.get("incident", {}).get("status") == "handed_off"
                  or result.get("standalone", {}).get("status") == "completed")):
                current = rollout.observe(baseline, before["generation"], CASE)
                if current and current["pod_uid"] == observed["pod_uid"]:
                    result.update(status="unrecovered", still_failed_before_cleanup=rollout.evidence(current, CASE))
        except (cluster.Refused, PreflightBlocked, Unobserved, OSError, ValueError, TimeoutError, TimeoutExpired) as error:
            result.update(status="unobserved", trial_error=str(error))
        finally:
            if broker:
                broker.close()
            try:
                if mutation_attempted:
                    _, restored = rollout.restore(baseline, CASE)
                    after = independent_health(baseline, monotonic() + 12)
                    if not same_data(before, after):
                        raise cluster.Refused("the data or bindings changed after restoring the fixture")
                    result.update(restored=after, cleanup_changed_reference=restored)
                if lease:
                    lease.release()
                clean = True
            finally:
                if not clean:
                    result["status"] = "unobserved"
                    result["cleanup_incomplete"] = True
                write_new(output, result)
                if clean:
                    journal.unlink()
    return result


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--auth", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.arm != "liveprobe" and not all((args.reference, args.binary, args.auth)):
        parser.error("standalone arms require a reference receipt, pinned CLI and independent account")
    base = json.loads(args.baseline.read_text())
    ref = json.loads(args.reference.read_text()) if args.reference else None
    if (base.get("namespace"), base.get("isolated_deployment")) != (NS, CASE.workload):
        parser.error("the sealed KW-03 dev baseline is required")
    if ref and (ref.get("case"), ref.get("target"), ref.get("restored", {}).get("contract_ok")) != (CASE.case_id, CASE.target, True):
        parser.error("the reference must report a verified clean KW-03 ending state")
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
        if journal.exists() or args.output.exists() or not args.output.parent.is_dir():
            parser.error("a previous fault requires reconciliation or output path is unavailable")
        answer = run(args.arm, base, ref, args.binary, args.auth, args.output, journal)
    print(json.dumps({"case": CASE.case_id, "arm": args.arm, "status": answer["status"]}))
    raise SystemExit(2 if answer["status"] == "unobserved" else 0)
