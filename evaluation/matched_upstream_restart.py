"""Compare caller recovery after a real outage of the isolated dev inventory Service."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
from hashlib import sha256
import json
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Lock
from time import monotonic, sleep
from urllib.error import URLError
from urllib.parse import urlencode

from evaluation import matched_upstream_observe as runtime
from evaluation.isolated_recovery_agent import Unobserved, run_agent
from evaluation.matched_recovery_campaign import (
    ARMS, CONTEXT, NS, RECOVERY_BUDGET_S, PreflightBlocked, canonical_digest, dev_preflight, matched_start, write_new,
)


cluster = runtime.cluster
fixture = runtime.fixture
CASE = runtime.CASE
SIGNAL = "TargetHigh5xxRate"
FAULT_RESPONSE = (503, {"error": "inventory check unavailable"})
TOOLS = [
    {"name": name, "description": description,
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
    for name, description in (
        ("read_caller", "Read the dev caller Deployment, its active Pod, and the first logged error."),
        ("check_services", "Check the caller response and query the real inventory dependency independently."),
        ("restart_caller", "Restart the same dev caller Deployment, preserving its image and configuration."),
    )
]
OBSERVATION_ERRORS = (cluster.Refused, Unobserved, PreflightBlocked, OSError, TimeoutExpired, ValueError)


def caller_spec(deployment):
    spec = deepcopy(deployment["spec"])
    spec["template"]["metadata"]["annotations"].pop(runtime.RESTART, None)
    return spec


def current_caller(initial):
    current = cluster.get("deployment", CASE.workload)
    if (current["metadata"]["uid"] != initial["metadata"]["uid"] or caller_spec(current) != caller_spec(initial)
            or current["metadata"]["labels"] != initial["metadata"]["labels"]):
        raise cluster.Refused("caller ownership or template changed during the trial")
    return current


def incidents(cursor):
    return cluster.query(f"""
      SELECT coalesce(json_agg(row_to_json(q)), '[]'::json) FROM (
        SELECT i.id,i.status,i.status_reason,i.signal_reasons,
          EXISTS (SELECT 1 FROM audit_log l WHERE l.incident_id=i.id AND l.event='signal_observed'
                    AND l.detail->>'reason'='{SIGNAL}' AND l.detail#>>'{{signal,source}}'='alertmanager'
                    AND l.detail#>>'{{signal,detail,labels,namespace}}'='dev'
                    AND l.detail#>>'{{signal,detail,labels,app}}'='{CASE.workload}'
                    AND l.detail#>>'{{signal,detail,labels,evaluation_case}}'='upstream') AS native_alert,
          (SELECT json_agg(json_build_object('type',a.type,'target',a.target,'status',a.status,'executed',a.executed))
             FROM actions a WHERE a.incident_id=i.id) AS actions,
          (SELECT json_agg(json_build_object('role',r.role,'model',r.model,'status',r.status,'input_tokens',r.input_tokens,
                    'output_tokens',r.output_tokens,'estimated_usd',r.estimated_cost_usd))
             FROM agent_runs r WHERE r.incident_id=i.id) AS calls
        FROM incidents i WHERE id>{int(cursor)} AND namespace='dev' AND workload='{CASE.workload}' ORDER BY id
      ) q
    """)


def residual_alert():
    path = "/api/v1/namespaces/liveprobe-observability/services/alertmanager:9093/proxy/api/v2/alerts"
    alerts = json.loads(cluster.command("get", "--raw", path))
    return any((a["labels"].get("namespace"), a["labels"].get("app")) == (NS, CASE.workload) for a in alerts)


def fault_series():
    path = "/api/v1/namespaces/liveprobe-observability/services/prometheus:9090/proxy/api/v1/query?"
    query = ('sum(rate(http_server_requests_seconds_count'
             '{namespace="dev",app="lp-process",method="GET",uri="/api/orders/check",status=~"5.."}[1m])) > 0')
    result = json.loads(cluster.command("get", "--raw", path + urlencode({"query": query})))
    if result.get("status") != "success":
        raise cluster.Refused("caller error metrics are unavailable")
    return result["data"]["result"]


def release(lease):
    deadline = monotonic() + 100
    while monotonic() < deadline:
        lease.require_active()
        if not residual_alert() and not fault_series():
            return lease.release()
        sleep(3)
    raise cluster.Refused("stale caller alerts still need to drain")


def preflight(address, artifact):
    gate = dev_preflight()
    caller = cluster.get("deployment", CASE.workload)
    fixture.require_owner(caller, CASE.workload)
    if ((caller["metadata"]["uid"], cluster.image(CASE, caller)) != (artifact["uid"], artifact["image"])
            or not cluster.is_settled(caller)):
        raise cluster.Refused("the caller does not match the sealed dev rollout")
    status = cluster.product_status("/api/status")
    if not status.get("ready") or NS not in status.get("target_namespaces", []) or "rollout_restart" not in status.get("allowlist", []):
        raise cluster.Refused("native intake or caller restart is not available")
    alias = cluster.get("service", fixture.ROUTE)
    stock_service = cluster.get("service", fixture.STOCK)
    stock_dep = cluster.get("deployment", fixture.STOCK)
    data = runtime.stock(address, fixture.STOCK)
    before = runtime.pod()
    if (alias["spec"]["selector"] != stock_service["spec"]["selector"] or runtime.stock(address) != data
            or not all(runtime.healthy_call(address, data) for _ in range(2)) or runtime.pod() != before):
        raise cluster.Refused("the initial caller and inventory responses differ")
    if any(i["status"] in cluster.NONTERMINAL for i in incidents(0)) or residual_alert() or fault_series():
        raise cluster.Refused("an earlier caller incident or alert still affects this fixture")
    node, = cluster.get("nodes")["items"]
    starting = {"context": CONTEXT, "namespace": NS, "node_uid": node["metadata"]["uid"],
                "deployment_uid": caller["metadata"]["uid"], "fixture_digest": canonical_digest({
                    "caller": caller_spec(caller), "alias": [alias["metadata"]["uid"], alias["spec"]],
                    "inventory": [stock_dep["metadata"]["uid"], stock_dep["spec"], stock_service["spec"]],
                    "business_data": data}),
                "fault_digest": canonical_digest({"add_selector": runtime.DISABLED, "value": "true",
                                                  "restore_after_caller_error": True}),
                "verifier_digest": sha256(Path(__file__).read_bytes()+Path(runtime.__file__).read_bytes()).hexdigest()}
    return caller, alias, data, before, gate, starting


class Broker:
    def __init__(self, address, baseline, first, error, lease, deadline):
        self.address, self.baseline, self.first, self.error = address, baseline, first, error
        self.lease, self.deadline = lease, deadline
        self.lock = Lock()
        self.closed = self.changed = False

    def __call__(self, method, args):
        with self.lock:
            if self.closed or args or monotonic() >= self.deadline:
                raise Unobserved("the caller tool is outside the active experiment")
            state = current_caller(self.baseline)
            if method == "read_caller":
                return {"deployment": CASE.target, "image": cluster.image(CASE, state),
                        "ready_replicas": state["status"].get("readyReplicas", 0), "error": self.error,
                        "pod": self.first["name"], "upstream": fixture.ROUTE}
            if method == "check_services":
                caller = runtime.request(self.address, CASE.workload, runtime.CALL)
                stock = runtime.request(self.address, fixture.ROUTE, runtime.STOCK)
                return {"caller": {"status": caller[0], "body": caller[1]}, "inventory": {"status": stock[0], "body": stock[1]}}
            if method != "restart_caller":
                raise Unobserved("unknown caller tool")
            if self.changed or runtime.pod() != self.first:
                return {"accepted": False, "reason": "the caller has already been replaced"}
            self.lease.require_active()
            runtime.restart(state["metadata"]["uid"])
            self.changed = True
            return {"accepted": True, "deployment": CASE.target}

    def close(self):
        with self.lock:
            self.closed = True


def recoveries(rows):
    return [a for row in rows for a in row["actions"] or []
            if a["target"] == CASE.target and a["type"] == "rollout_restart"
            and a["executed"] is True and a["status"] == "succeeded"]


def observe(args, address, reference, journal):
    baseline, alias, data, first, gate, starting = preflight(address, args.artifact)
    if reference and any(reference["start_evidence"].get(k) != v for k, v in starting.items()):
        raise cluster.Refused("the caller fixture differs from the reference before starting")
    uid = baseline["metadata"]["uid"]
    old_selector = alias["spec"]["selector"]
    alias_uid = alias["metadata"]["uid"]
    result = {"case": CASE.case_id, "arm": args.arm, "target": CASE.target, "status": "unobserved",
              "healthy_before": {"caller": first, "inventory": data}, "headroom": gate}
    write_new(journal, {"target": CASE.target, "uid": uid, "selector_alias_uid": alias_uid,
                        "selector": old_selector, "receipt": str(args.output)})
    broker = lease = future = None
    mutated = clean = False
    with ThreadPoolExecutor(max_workers=1) as agents:
        try:
            if args.arm != "liveprobe":
                lease = cluster.WorkloadLease.claim(CASE, args.arm, ttl=900)
            cursor = cluster.query("SELECT coalesce(max(id),0) FROM incidents")
            dev_preflight()
            current_caller(baseline)
            mutated = True
            began, fault = runtime.induce(address, data, first)
            starting["initial_error_digest"] = sha256(fault["error"].encode()).hexdigest()
            result.update(fault=fault, start_evidence=starting, matched_start=matched_start(starting),
                          first_error_ms=round((monotonic()-began)*1000), injected_at=fault["injected_at"])
            if reference and result["matched_start"] != reference["matched_start"]:
                raise cluster.Refused("the native caller error differs from the reference")
            deadline, renew = began+RECOVERY_BUDGET_S, began+240
            if lease:
                broker = Broker(address, baseline, first, fault["error"], lease, deadline)
                future = agents.submit(run_agent, binary=args.binary, auth_file=args.auth, arm=args.arm,
                                       error_line=fault["error"], tools=TOOLS, broker=broker, deadline=deadline)
            rows = []
            while monotonic() < deadline:
                if lease and monotonic() >= renew:
                    lease.renew(900)
                    renew = monotonic()+240
                try:
                    if "recovery_ms" not in result and runtime.healthy_call(address, data):
                        current_caller(baseline)
                        proof = runtime.independent_recovery(address, data, first, 4)
                        result.update(verified_before_cleanup=proof, recovery_ms=round((monotonic()-began)*1000))
                except (cluster.Refused, URLError, TimeoutExpired, ValueError):
                    pass
                rows = incidents(cursor)
                if rows and lease:
                    raise Unobserved("a LiveProbe incident opened during standalone isolation")
                if rows:
                    result["incidents"] = rows
                    if any(row["native_alert"] for row in rows) and "detected_ms" not in result:
                        result["detected_ms"] = round((monotonic()-began)*1000)
                if future and future.done():
                    result["standalone"] = future.result()
                    if not broker.changed or result["standalone"]["status"] != "completed" or "recovery_ms" in result:
                        break
                if "detected_ms" in result and all(row["status"] not in cluster.NONTERMINAL for row in rows):
                    if "recovery_ms" in result or not recoveries(rows):
                        break
                sleep(0.9)
            if future and "standalone" not in result:
                result["standalone"] = future.result(timeout=3)
            completed = result.get("standalone", {}).get("status") == "completed"
            attributable = completed and broker.changed if broker else ("detected_ms" in result and len(recoveries(rows)) == 1)
            if attributable and "recovery_ms" in result:
                result["status"] = "recovered"
            elif ("recovery_ms" not in result and (completed or "detected_ms" in result)
                    and runtime.request(address, CASE.workload, runtime.CALL) == FAULT_RESPONSE
                    and runtime.stock(address) == data and runtime.pod() == first):
                result.update(status="unrecovered", still_failed_before_cleanup=True)
            result["trial_elapsed_ms"] = round((monotonic()-began)*1000)
        except OBSERVATION_ERRORS as error:
            result.update(status="unobserved", trial_error=str(error))
        finally:
            if broker:
                broker.close()
            try:
                if mutated:
                    selected = cluster.get("service", fixture.ROUTE)
                    if selected["metadata"]["uid"] != alias_uid or selected["spec"]["selector"] != old_selector:
                        raise cluster.Refused("the upstream Service requires reconciliation")
                    current_caller(baseline)
                    if not runtime.healthy_call(address, data):
                        stalled = runtime.pod()
                        runtime.restart(uid)
                        runtime.independent_recovery(address, data, stalled, 70)
                    if runtime.stock(address) != data or not all(runtime.healthy_call(address, data) for _ in range(2)):
                        raise cluster.Refused("caller cleanup did not preserve the original business requests")
                    result["restored"] = {"caller": runtime.pod(), "inventory": data, "responses": [200, 200]}
                if lease:
                    release(lease) if mutated else lease.release()
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
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--arm", required=True, choices=ARMS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--auth", type=Path)
    args = parser.parse_args()
    args.artifact = json.loads(args.artifact.read_text())[CASE.workload]
    if args.arm != "liveprobe" and not all((args.reference, args.binary, args.auth)):
        parser.error("the standalone model requires the LiveProbe reference and pinned independent CLI")
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference and (reference.get("case"), reference.get("arm"), bool(reference.get("restored"))) != (CASE.case_id, "liveprobe", True):
        parser.error("the reference must be a measured LiveProbe caller trial with verified cleanup")
    journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists() or not args.output.parent.is_dir():
            parser.error("the output or a previous trial requires reconciliation")
        with runtime.origin() as address:
            outcome = observe(args, address, reference, journal)
    print(json.dumps({"arm": args.arm, "status": outcome["status"]}))
    return 2 if outcome["status"] == "unobserved" else 0


if __name__ == "__main__":
    raise SystemExit(main())
