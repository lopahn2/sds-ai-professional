"""Compare recovery of a real Kubernetes reservation queue under unchanged demand."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
from subprocess import TimeoutExpired
from threading import Lock
from time import monotonic, sleep
from urllib.error import URLError

from app.redact import redact
from evaluation import calibrate_backlog_dev as runtime
from evaluation.isolated_recovery_agent import Unobserved, run_agent
from evaluation.matched_backlog_load import ReservationTraffic, stable_recovery
from evaluation.matched_recovery_campaign import (
    ARMS, CONTEXT, NS, RECOVERY_BUDGET_S, PreflightBlocked, canonical_digest, dev_preflight, matched_start, write_new,
)
from evaluation.verify_backlog import ARRIVAL_MS, values
from evaluation.verify_jdbc_fixture import DATA, invoke


cluster = runtime.cluster
CASE = runtime.CASE
SIGNAL = "TargetHighLatency"
WARNING = re.compile(r"reservation request exceeded queue delay target: waiting_ms=(\d+)")
RESTART = "kubectl.kubernetes.io/restartedAt"
ERRORS = (cluster.Refused, Unobserved, PreflightBlocked, TimeoutExpired, ValueError, OSError)
TOOLS = [
    {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False,
    }}
    for name, description, fields in (
        ("read_deployment", "Read the reservation Deployment, its desired replicas, and recent request warning.", {}),
        ("read_workers", "Read queue counts and database timings from the Ready reservation Pods.", {}),
        ("scale_deployment", "Change replica count of the isolated reservation Deployment in its permitted range.", {
            "replicas": {"type": "integer"},
        }),
    )
]


def comparable_spec(obj):
    spec = deepcopy(obj["spec"])
    spec.pop("replicas", None)
    spec["template"]["metadata"]["annotations"].pop(RESTART, None)
    return spec


def owned(initial):
    state = runtime.deployment()
    if (state["metadata"]["uid"] != initial["metadata"]["uid"] or comparable_spec(state) != comparable_spec(initial)
            or state["metadata"]["labels"] != initial["metadata"]["labels"] or state["spec"]["replicas"] not in (1, 2)):
        raise cluster.Refused("the exact reservation workload changed outside the allowed replication")
    return state


def ready_count(state):
    status = state["status"]
    wanted = state["spec"]["replicas"]
    if status.get("observedGeneration") != state["metadata"]["generation"]:
        return 0
    return wanted if all(status.get(k) == wanted for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")) else 0


def metrics(address, count):
    slices = cluster.get("endpointslice", selector=f"kubernetes.io/service-name={CASE.workload}")["items"]
    endpoints = {(ep["targetRef"]["uid"], ep["targetRef"]["name"]) for row in slices for ep in row.get("endpoints", [])
                 if ep.get("conditions", {}).get("ready") is True and not ep["conditions"].get("terminating")
                 and ep.get("targetRef", {}).get("kind") == "Pod"}
    if len(endpoints) != count:
        raise cluster.Refused("the Service does not have the expected stable Ready Pods")
    workers = []
    for uid, name in sorted(endpoints):
        base = f"{address}/api/v1/namespaces/dev/pods/{name}:8080/proxy"
        workers.append({"uid": uid, "pod": name, **values(base)})
    return workers


def incidents(after):
    return cluster.query(f"""
      SELECT coalesce(json_agg(row_to_json(q)), '[]'::json) FROM (
        SELECT i.id,i.status,i.status_reason,i.signal_reasons,
          EXISTS (SELECT 1 FROM audit_log l WHERE l.incident_id=i.id AND l.event='signal_observed'
                    AND l.detail->>'reason'='{SIGNAL}' AND l.detail#>>'{{signal,source}}'='alertmanager'
                    AND l.detail#>>'{{signal,detail,labels,namespace}}'='dev'
                    AND l.detail#>>'{{signal,detail,labels,app}}'='{CASE.workload}'
                    AND l.detail#>>'{{signal,detail,labels,evaluation_case}}'='backlog') AS native_alert,
          (SELECT json_agg(json_build_object('type',a.type,'target',a.target,'status',a.status,'executed',a.executed,
                     'replicas',a.params->>'replicas')) FROM actions a WHERE a.incident_id=i.id) AS actions,
          (SELECT json_agg(json_build_object('role',r.role,'model',r.model,'status',r.status,'input_tokens',r.input_tokens,
                     'output_tokens',r.output_tokens,'estimated_usd',r.estimated_cost_usd))
             FROM agent_runs r WHERE r.incident_id=i.id) AS calls
        FROM incidents i WHERE id>{int(after)} AND i.namespace='dev' AND i.workload='{CASE.workload}' ORDER BY i.id
      ) q
    """)


def active_alert():
    path = "/api/v1/namespaces/liveprobe-observability/services/alertmanager:9093/proxy/api/v2/alerts"
    alerts = json.loads(cluster.command("get", "--raw", path))
    return any((row["labels"].get("namespace"), row["labels"].get("app")) == (NS, CASE.workload) for row in alerts)


def cleanup_lease(lease):
    logs_expire = monotonic() + 120
    until = logs_expire + 55
    while monotonic() < until:
        lease.require_active()
        if monotonic() >= logs_expire and not active_alert():
            return lease.release()
        sleep(5)
    raise cluster.Refused("the current backlog alerts have not drained")


def initial_proof(address, origin, initial):
    deadline = monotonic()+45
    while monotonic() < deadline:
        try:
            state = owned(initial)
            if ready_count(state) != 1:
                sleep(1)
                continue
            routes = [invoke(origin, row, "standard") for row in DATA]
            pods = metrics(address, 1)
            if all(x["contract_ok"] for x in routes) and pods[0]["queued"] == 0:
                return {"pods": pods, "requests": routes}
        except (cluster.Refused, ValueError, OSError, TimeoutExpired):
            pass
        sleep(1)
    raise cluster.Refused("one unqueued reservation replica did not pass the baseline")


def prepare(address, origin, artifact):
    gate = dev_preflight()
    deployment = runtime.deployment()
    runtime.fixture.require_owner(deployment, CASE.workload)
    if (deployment["metadata"]["uid"], cluster.image(CASE, deployment)) != (artifact["uid"], artifact["image"]):
        raise cluster.Refused("the current reservation release is outside the tested image scope")
    status = cluster.product_status("/api/status")
    if not status.get("ready") or NS not in status.get("target_namespaces", []) or "scale_replicas" not in status.get("allowlist", []):
        raise cluster.Refused("native intake or replica action is unavailable")
    if active_alert() or any(row["status"] in cluster.NONTERMINAL for row in incidents(0)):
        raise cluster.Refused("an existing active alert or incident affects the backlog fixture")
    baseline = initial_proof(address, origin, deployment)
    node, = cluster.get("nodes")["items"]
    config = cluster.get("configmap", runtime.fixture.CAPACITY_DATA+"-schema")
    svc = cluster.get("service", CASE.workload)
    start = {"context": CONTEXT, "namespace": NS, "node_uid": node["metadata"]["uid"],
             "deployment_uid": deployment["metadata"]["uid"], "fixture_digest": canonical_digest({
                 "workload": comparable_spec(deployment), "replicas": 1, "data": config["data"],
                 "service": [svc["metadata"]["uid"], svc["spec"]], "journey": DATA}),
             "fault_digest": canonical_digest({"request_interval_ms": ARRIVAL_MS, "replicas_at_start": 1}),
             "verifier_digest": sha256(Path(__file__).read_bytes() + Path(ReservationTraffic.dispatch.__code__.co_filename).read_bytes()).hexdigest()}
    return deployment, baseline, gate, start


class Broker:
    def __init__(self, address, initial, lease, error, deadline):
        self.address, self.initial, self.lease, self.error = address, initial, lease, error
        self.deadline = deadline
        self.lock = Lock()
        self.changed = self.closed = False

    def close(self):
        with self.lock:
            self.closed = True

    def __call__(self, method, args):
        with self.lock:
            if self.closed or monotonic() >= self.deadline:
                raise Unobserved("the backlog test has ended")
            state = owned(self.initial)
            count = state["spec"]["replicas"]
            if method == "scale_deployment":
                if args != {"replicas": 2} or self.changed or count != 1:
                    return {"accepted": False, "reason": "outside the allowed workload replica change"}
                self.lease.require_active()
                runtime.scale(state["metadata"]["uid"], 1, 2)
                self.changed = True
                return {"accepted": True, "deployment": CASE.target, "replicas": 2}
            if args:
                raise Unobserved("the read tool has no parameters")
            if method == "read_deployment":
                return {"deployment": CASE.target, "desired_replicas": count,
                        "ready": state["status"].get("readyReplicas", 0), "error": self.error}
            if method == "read_workers":
                try:
                    return {"workers": metrics(self.address, count)}
                except (cluster.Refused, OSError, ValueError, TimeoutExpired):
                    return {"workers": None, "rollout_pending": not ready_count(state)}
            raise Unobserved("unknown reservation tool")


def safe_scale_seen(rows):
    return any(action["type"] == "scale_replicas" and action["target"] == CASE.target and action["replicas"] == "2"
               and action["executed"] is True and action["status"] == "succeeded"
               for row in rows for action in row["actions"] or [])


def observe(args, address, origin, reference, journal):
    initial, healthy, gate, start = prepare(address, origin, args.artifact)
    if reference and any(reference["start_evidence"].get(key) != val for key, val in start.items()):
        raise cluster.Refused("the backlog fixture changed from the comparison reference")
    uid = initial["metadata"]["uid"]
    write_new(journal, {"target": CASE.target, "uid": uid, "restore_replicas": 1, "receipt": str(args.output)})
    result = {"case": CASE.case_id, "arm": args.arm, "headroom": gate, "healthy_before": healthy, "status": "unobserved"}
    broker = future = lease = None
    mutated = clean = False
    traffic = ReservationTraffic(origin)
    witnesses = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            if args.arm != "liveprobe":
                lease = cluster.WorkloadLease.claim(CASE, args.arm, ttl=900)
            cursor = cluster.query("SELECT coalesce(max(id),0) FROM incidents")
            dev_preflight()
            result["injected_at"] = datetime.now(timezone.utc).isoformat()
            began = traffic.start()
            mutated = True
            deadline, renewal, sample_after = began+RECOVERY_BUDGET_S, began+240, began
            double_since, double_ids = None, None
            records = []
            while monotonic() < deadline:
                active_at = monotonic()
                if lease and active_at >= renewal:
                    lease.renew(900)
                    renewal = active_at+240
                state = owned(initial)
                count = ready_count(state)
                if count and active_at >= sample_after:
                    sample_after = active_at+3
                    try:
                        samples = metrics(address, count)
                    except (cluster.Refused, ValueError, OSError, TimeoutExpired):
                        samples = []
                    if samples:
                        ids = [entry["uid"] for entry in samples]
                        witness = {"time": active_at, "pods": ids, "queues": [row["queued"] for row in samples]}
                        witnesses.append(witness)
                        if count == 2 and ids != double_ids:
                            double_since, double_ids = active_at, ids
                        if count == 1 and "fault" not in result and max(witness["queues"]) >= 2:
                            log = cluster.command("logs", samples[0]["pod"], "-c", CASE.workload,
                                                  "--since-time="+result["injected_at"], "--tail=100")
                            first = next((redact(line) for line in log.splitlines() if WARNING.search(line)), None)
                            if first:
                                start["initial_error_digest"] = sha256(b"reservation request exceeded queue delay target").hexdigest()
                                result.update(fault={"first_error": first, "worker_snapshot": samples},
                                              first_error_ms=round((monotonic()-began)*1000),
                                              start_evidence=start, matched_start=matched_start(start))
                                if reference and result["matched_start"] != reference["matched_start"]:
                                    raise Unobserved("the native reservation error changed")
                                if lease:
                                    broker = Broker(address, initial, lease, first, deadline)
                                    future = executor.submit(run_agent, binary=args.binary, auth_file=args.auth, arm=args.arm,
                                                             error_line=first, tools=TOOLS, broker=broker, deadline=deadline)
                if count != 2:
                    double_since, double_ids = None, None
                if double_since and "fault" in result and "recovery_ms" not in result:
                    proof = stable_recovery(traffic, double_since, active_at, witnesses, double_ids)
                    if proof:
                        result.update(recovery_ms=round((monotonic()-began)*1000), verified_before_cleanup=proof)
                records = incidents(cursor)
                if records and lease:
                    raise Unobserved("product intake occurred during a standalone trial")
                if records:
                    result["incidents"] = records
                    if any(row["native_alert"] for row in records) and "detected_ms" not in result:
                        result["detected_ms"] = round((monotonic()-began)*1000)
                if future and future.done():
                    result["standalone"] = future.result()
                    if not broker.changed or result["standalone"]["status"] != "completed" or "recovery_ms" in result:
                        break
                if "fault" in result and "detected_ms" in result and all(r["status"] not in cluster.NONTERMINAL for r in records):
                    if "recovery_ms" in result or not safe_scale_seen(records):
                        break
                sleep(1)
            if future and "standalone" not in result:
                result["standalone"] = future.result(timeout=3)
            completed = result.get("standalone", {}).get("status") == "completed"
            attributed = (completed and broker.changed) if broker else ("detected_ms" in result and safe_scale_seen(records))
            if attributed and "recovery_ms" in result:
                result["status"] = "recovered"
            elif "fault" in result and "recovery_ms" not in result and (completed or "detected_ms" in result):
                if ready_count(owned(initial)) == 1 and max(row["queued"] for row in metrics(address, 1)) >= 2:
                    result.update(status="unrecovered", pressure_before_cleanup=True)
            result["trial_elapsed_ms"] = round((monotonic()-began)*1000)
        except ERRORS as exc:
            result.update(status="unobserved", trial_error=str(exc))
        finally:
            if broker:
                broker.close()
            try:
                traffic.stop()
                if mutated:
                    count = owned(initial)["spec"]["replicas"]
                    if count == 2:
                        runtime.scale(uid, 2, 1)
                        runtime.roll()
                    result["restored"] = initial_proof(address, origin, initial)
                    result["max_queue_seen"] = max((max(w["queues"]) for w in witnesses), default=None)
                if lease:
                    cleanup_lease(lease) if mutated else lease.release()
                clean = True
            finally:
                if not clean:
                    result.update(status="unobserved", cleanup_incomplete=True)
                write_new(args.output, result)
                if clean:
                    journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--auth", type=Path)
    args = parser.parse_args()
    args.artifact = json.loads(args.artifact.read_text())[CASE.workload]
    if args.arm != "liveprobe" and not all((args.reference, args.binary, args.auth)):
        parser.error("standalone arms require a verified reference and isolated CLI")
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference and (reference.get("case"), reference.get("arm"), bool(reference.get("restored"))) != (CASE.case_id, "liveprobe", True):
        parser.error("the reference needs an original and restored LiveProbe backlog observation")
    journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists() or not args.output.parent.is_dir():
            parser.error("a previous run or destination needs reconciliation")
        with runtime.service_proxy() as base:
            address = base.split("/api/v1/namespaces/dev/services/", 1)[0]
            origin = f"{address}/api/v1/namespaces/dev/services/{CASE.workload}:8080/proxy"
            result = observe(args, address, origin, reference, journal)
    print(json.dumps({"arm": args.arm, "status": result["status"]}))
    return 2 if result["status"] == "unobserved" else 0


if __name__ == "__main__":
    raise SystemExit(main())
