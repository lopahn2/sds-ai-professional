"""Compare persistent ConfigMap-induced pool exhaustion on the isolated dev reservation service."""

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

from evaluation import pool_config_observe as pool
from evaluation.isolated_recovery_agent import Unobserved, run_agent
from evaluation.jdbc_dev_manifest import POOL_BROKEN, POOL_CONFIG, POOL_NAME, POOL_READY, POOL_VERSION
from evaluation.matched_recovery_campaign import (
    ARMS, CONTEXT, NS, RECOVERY_BUDGET_S, PreflightBlocked, canonical_digest, dev_preflight, matched_start, write_new,
)


cluster = pool.cluster
CASE = pool.CASE
CALIBRATE = "calibrate"
APP_LOG_WINDOW_S = 120
TOOLS = [
    {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False,
    }}
    for name, description, fields in (
        ("read_workload", "Inspect the reservation Deployment and its Kubernetes configuration references.", {}),
        ("read_configmaps", "Inspect the live referenced ConfigMap and its immutable version.", {}),
        ("read_pool", "Read current Hikari connection metrics and the service error.", {}),
        ("restore_configmap", "Restore the referenced ConfigMap from an immutable version and restart its consumer.", {
            "version": {"type": "string"},
        }),
    )
]
OBSERVABLE_ERRORS = (OSError, ValueError, URLError, TimeoutError, TimeoutExpired, cluster.Refused, PreflightBlocked, Unobserved)


def incidents(cursor):
    return cluster.query(f"""
        SELECT coalesce(json_agg(row_to_json(q)),'[]'::json) FROM (
          SELECT i.id,i.status,i.status_reason,i.signal_reasons,
          (SELECT json_agg(json_build_object('type',a.type,'target',a.target,'status',a.status,'executed',a.executed,
                      'version',a.params->>'version')) FROM actions a WHERE a.incident_id=i.id) AS actions,
          (SELECT json_agg(json_build_object('role',r.role,'model',r.model,'status',r.status,'input_tokens',r.input_tokens,
                      'output_tokens',r.output_tokens,'estimated_usd',r.estimated_cost_usd))
             FROM agent_runs r WHERE r.incident_id=i.id) AS calls
          FROM incidents i WHERE id>{int(cursor)} AND i.namespace='dev' AND i.workload='{POOL_NAME}'
          ORDER BY i.id
        ) q
    """)


def drain_signals(lease):
    # An app-log poll can discover the old failed Pod's exceptions after a rollout.
    # Leave the evaluation lease active until those exceptions leave the polling window.
    started = monotonic()
    log_window_end = started + APP_LOG_WINDOW_S
    deadline = log_window_end + 45
    while monotonic() < deadline:
        lease.require_active()
        if monotonic() < log_window_end:
            sleep(min(10, log_window_end - monotonic()))
            continue
        alerts = json.loads(cluster.command("get", "--raw",
            "/api/v1/namespaces/liveprobe-observability/services/alertmanager:9093/proxy/api/v2/alerts"))
        own_alerts = [entry for entry in alerts if entry["labels"].get("namespace") == NS
                      and entry["labels"].get("app") == POOL_NAME]
        if not own_alerts and not any(value >= 0.95 for value in pool.prom_utilization()):
            return lease.release()
        sleep(3)
    raise cluster.Refused("pool alert remained active during cleanup; retained the journal")


class PoolBroker:
    def __init__(self, baseline, origin, error, lease, deadline):
        self.baseline, self.origin, self.error, self.lease, self.deadline = baseline, origin, error, lease, deadline
        self.guard = Lock()
        self.closed = self.changed = False

    def close(self):
        with self.guard:
            self.closed = True

    def __call__(self, method, args):
        with self.guard:
            if self.closed or monotonic() >= self.deadline:
                raise Unobserved("the active pool trial has ended")
            deployment, config, version = pool.live_state()
            pool.assert_identity(deployment, config, self.baseline)
            if method == "restore_configmap":
                if args != {"version": POOL_VERSION} or config["data"] != POOL_BROKEN or self.changed:
                    return {"restored": False, "reason": "Kubernetes configuration transition is outside the experiment"}
                self.lease.require_active()
                pool.change_pool(self.baseline, POOL_BROKEN, version["data"])
                self.changed = True
                return {"restored": True, "configmap": POOL_CONFIG, "consumer": POOL_NAME}
            if args:
                raise Unobserved("the observation tool takes no arguments")
            if method == "read_workload":
                return {"namespace": NS, "deployment": POOL_NAME, "configmap_refs": [POOL_CONFIG],
                        "ready_replicas": deployment["status"].get("readyReplicas", 0), "image": CASE.bad_image}
            if method == "read_configmaps":
                return {"current": {POOL_CONFIG: config["data"]}, "immutable": {POOL_VERSION: version["data"]}}
            if method == "read_pool":
                try:
                    metrics = pool.observe_metrics(self.origin)
                except (cluster.Refused, URLError, TimeoutExpired):
                    return {"metrics": None, "rollout_ready": cluster.is_settled(deployment), "error": self.error}
                return {"metrics": metrics, "error": self.error}
            raise Unobserved("the operation is outside the pool trial")


def preflight(origin):
    gate = dev_preflight()
    deployment, config, version = pool.live_state()
    if config["data"] != POOL_READY:
        raise cluster.Refused("the original fixture ConfigMap is not in use")
    if any(row["status"] in cluster.NONTERMINAL for row in incidents(0)):
        raise cluster.Refused("the fixture already has an active incident")
    baseline = pool.confirmed_health(origin, monotonic() + 35)
    if not pool.prom_utilization():
        raise cluster.Refused("the live pooled metric is not being scraped")
    spec = deepcopy(deployment["spec"])
    spec["template"]["metadata"]["annotations"].pop(pool.RESTART, None)
    node, = cluster.get("nodes")["items"]
    schema = cluster.get("configmap", POOL_NAME + "-schema")
    starting = {"context": CONTEXT, "namespace": NS, "node_uid": node["metadata"]["uid"],
                "deployment_uid": deployment["metadata"]["uid"], "fixture_digest": canonical_digest({
                    "spec": spec, "config_uid": config["metadata"]["uid"], "version_uid": version["metadata"]["uid"],
                    "config": config["data"], "schema_data": schema["data"],
                    "runtime_image": baseline["control"]["image_id"],
                }),
                "fault_digest": canonical_digest({"path": f"ConfigMap/{POOL_CONFIG}/data",
                                                   "from": POOL_READY, "to": POOL_BROKEN}),
                "verifier_digest": sha256(Path(__file__).read_bytes() + Path(pool.__file__).read_bytes()).hexdigest()}
    return (deployment, config), baseline, gate, starting


def fault_batch(origin):
    measured = pool.exercise(origin)
    rows = measured["requests"]
    failed = sum(row["status"] == 500 for row in rows)
    if not 3 <= failed < len(rows) or not all(row["contract_ok"] or row["status"] == 500 for row in rows):
        raise cluster.Refused("repeatable reservation errors in the narrowed pool were not observed")
    if not any(row["active"] == row["max"] == 1 for row in measured["under_load"]):
        raise cluster.Refused("the narrowed pool was not saturated under the same requests")
    return measured


def restore(origin, baseline):
    deployment, config, _ = pool.live_state()
    pool.assert_identity(deployment, config, baseline)
    if config["data"] == POOL_BROKEN:
        pool.change_pool(baseline, POOL_BROKEN, POOL_READY)
    elif cluster.is_settled(deployment):
        try:
            if pool.observe_metrics(origin)["max"] == 1:
                pool.restart_consumer(baseline)
        except (URLError, TimeoutExpired, cluster.Refused):
            pass
    return pool.confirmed_health(origin, monotonic() + 75)


def verify_after_restart(origin, deadline):
    while monotonic() < deadline:
        try:
            return pool.confirmed_health(origin, deadline)
        except (cluster.Refused, URLError, TimeoutExpired):
            if monotonic() < deadline:
                sleep(1)
    raise cluster.Refused("the restarted pool did not pass the same reservation workload")


def observe_trial(args, origin, baseline, starting, result, lease, executor, reference):
    cursor = cluster.query("SELECT coalesce(max(id),0) FROM incidents")
    dev_preflight()
    pool.change_pool(baseline, POOL_READY, POOL_BROKEN)
    began = monotonic()
    deadline, renew_at = began + RECOVERY_BUDGET_S, began + 240
    result["injected_at"] = datetime.now(timezone.utc).isoformat()
    failed_pod = pool.wait_ready(origin, 1, min(deadline, began + 75))
    first = fault_batch(origin)
    error = pool.actual_error(failed_pod["pod"])
    result.update(fault={"control": failed_pod, "first": first, "error": error},
                  first_error_ms=round((monotonic()-began)*1000))
    line = pool.ACQUISITION.search(error)[0]
    normalized = re.sub(r"waiting=\d+", "waiting=*", line)
    normalized = re.sub(r"after \d+ms", "after timeout", normalized)
    starting["initial_error_digest"] = sha256(normalized.encode()).hexdigest()
    result.update(start_evidence=starting, matched_start=matched_start(starting))
    if reference and reference["matched_start"] != result["matched_start"]:
        raise cluster.Refused("the observed error and fixture are not equal to the reference")
    broker = future = None
    traffic = pool.SustainedTraffic(origin)
    traffic.start()
    try:
        if args.arm not in (CALIBRATE, "liveprobe"):
            broker = PoolBroker(baseline, origin, error, lease, deadline)
            future = executor.submit(run_agent, binary=args.binary, auth_file=args.auth, arm=args.arm,
                                     error_line=error, tools=TOOLS, broker=broker, deadline=deadline)
        while monotonic() < deadline:
            if lease and monotonic() >= renew_at:
                lease.renew(900)
                renew_at = monotonic() + 240
            rows = incidents(cursor)
            if lease and rows:
                raise cluster.Refused("LiveProbe opened an incident during a trial under evaluation lease")
            if rows:
                result["incidents"] = rows
                if "detected_ms" not in result and any(pool.SIGNAL in row["signal_reasons"] for row in rows):
                    result["detected_ms"] = round((monotonic()-began)*1000)

            deployment, config, _ = pool.live_state()
            pool.assert_identity(deployment, config, baseline)
            if config["data"] == POOL_READY and cluster.is_settled(deployment) and "recovery_ms" not in result:
                traffic.stop()
                proof = verify_after_restart(origin, min(deadline, monotonic()+26))
                if proof["control"]["pod_uid"] == failed_pod["pod_uid"]:
                    raise cluster.Refused("the running application retained the failed Pod")
                result.update(verified_before_cleanup=proof, recovery_ms=round((monotonic()-began)*1000))
            elif config["data"] == POOL_BROKEN and cluster.is_settled(deployment):
                values = pool.prom_utilization()
                if not result.get("scraped_saturation") and any(value >= 0.95 for value in values):
                    result["scraped_saturation"] = values
                if args.arm == CALIBRATE and result.get("scraped_saturation"):
                    result["status"] = "calibrated"
                    return

            if future and future.done():
                result["standalone"] = future.result()
                if result["standalone"]["status"] != "completed" or not broker.changed or "recovery_ms" in result:
                    break
            if args.arm == CALIBRATE and monotonic() > began + 115:
                raise cluster.Refused("real app saturation did not reach the configured Prometheus recorder")
            if rows and all(row["status"] not in cluster.NONTERMINAL for row in rows) and "detected_ms" in result:
                if config["data"] == POOL_BROKEN or "recovery_ms" in result:
                    break
            sleep(1.2)

        if future and "standalone" not in result:
            result["standalone"] = future.result(timeout=3)
        result["trial_elapsed_ms"] = round((monotonic()-began)*1000)
        completed = result.get("standalone", {}).get("status") == "completed"
        actions = [a for row in result.get("incidents", []) for a in row.get("actions") or [] if a.get("executed")]
        recorded = (len(actions) == 1 and actions[0].get("type") == "configmap_restore"
                    and actions[0].get("target") == f"dev/ConfigMap/{POOL_CONFIG}"
                    and actions[0].get("version") == POOL_VERSION and actions[0].get("status") == "succeeded")
        if "recovery_ms" in result and ((completed and broker.changed) if broker else ("detected_ms" in result and recorded)):
            result["status"] = "recovered"
        elif "recovery_ms" not in result and (completed or "detected_ms" in result):
            _, config, _ = pool.live_state()
            if config["data"] == POOL_BROKEN and pool.observe_metrics(origin)["max"] == 1:
                traffic.stop()
                result.update(status="unrecovered", failed_before_cleanup=fault_batch(origin))
    finally:
        if broker:
            broker.close()
            result["model_config_write_acknowledged"] = broker.changed
        traffic.stop()
        result["sustained_load"] = traffic.evidence()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=(*ARMS, CALIBRATE), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--auth", type=Path)
    args = parser.parse_args()
    if args.arm not in (CALIBRATE, "liveprobe") and not all((args.reference, args.binary, args.auth)):
        parser.error("standalone arms require a calibrated reference, fixed CLI and independent account")
    reference = json.loads(args.reference.read_text()) if args.reference else None
    result = {"case": CASE.case_id, "arm": args.arm, "status": "unobserved"}
    journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists() or not args.output.parent.is_dir():
            parser.error("an existing journal or output requires attention")
        with pool.service_proxy() as origin, ThreadPoolExecutor(max_workers=1) as executor:
            baseline, healthy, gate, starting = preflight(origin)
            if reference:
                approved_reference = ((reference.get("arm") == CALIBRATE and reference.get("status") == "calibrated")
                                      or (reference.get("arm") == "liveprobe" and reference.get("status")
                                          in ("recovered", "unrecovered")))
                if (not approved_reference or reference.get("case") != CASE.case_id or not reference.get("restored")
                        or any(reference["start_evidence"].get(key) != value for key, value in starting.items())):
                    raise cluster.Refused("the healthy fixture does not match the reference before injection")
            result.update(healthy_before=healthy, headroom=gate)
            write_new(journal, {"target": CASE.target, "configmap": POOL_CONFIG, "uid": baseline[1]["metadata"]["uid"],
                                "original": POOL_READY, "faulted": POOL_BROKEN, "receipt": str(args.output)})
            lease = None
            clean = False
            try:
                if args.arm != "liveprobe":
                    lease = cluster.WorkloadLease.claim(CASE, args.arm, ttl=900)
                observe_trial(args, origin, baseline, starting, result, lease, executor, reference)
            except OBSERVABLE_ERRORS as exc:
                result.update(status="unobserved", trial_error=str(exc))
            finally:
                try:
                    result["restored"] = restore(origin, baseline)
                    if lease:
                        drain_signals(lease)
                    clean = True
                finally:
                    if not clean:
                        result.update(status="unobserved", cleanup_incomplete=True)
                    write_new(args.output, result)
                    if clean:
                        journal.unlink()
    print(json.dumps({"arm": args.arm, "status": result["status"]}))
    return 2 if result["status"] == "unobserved" else 0


if __name__ == "__main__":
    raise SystemExit(main())
