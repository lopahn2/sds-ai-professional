"""Live dev Service-selector comparison with exact restoration and independent probes."""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
from subprocess import TimeoutExpired, run
from time import monotonic, sleep

from evaluation import k8s_dev_image_trial as live
from evaluation.dev_selector_case import ENDPOINT_GRACE_SECONDS, PROTOCOL, SELECTOR_WORKLOADS, load_selector_case
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text, infer, measured_sum
from evaluation.live_repair import TASK
from evaluation.single_baseline import MODELS


ENDPOINT_REASONS = {"NoEndpoints", "TargetServiceNoEndpoints"}
HANDOFF_STATES = {"handed_off"}
BASELINE_PATH = "/spec/selector"
MAX_PRODUCT_SECONDS = ENDPOINT_GRACE_SECONDS + 360
UNDO_SECONDS = 90


def broken_selector(case, baseline):
    return {**baseline["selector"], "app": case.bad_app}


def checked_selector(current, baseline):
    metadata = current["metadata"]
    normal = deepcopy(current["spec"])
    actual = normal.get("selector")
    normal["selector"] = baseline["selector"]
    if (metadata["uid"] != baseline["service_uid"] or metadata.get("deletionTimestamp")
            or normal != baseline["service_spec"]):
        raise live.Refused("the monitored Service changed outside the approved selector field")
    return actual


def selector(case, current, baseline):
    actual = checked_selector(current, baseline)
    if actual not in (baseline["selector"], broken_selector(case, baseline)):
        raise live.Refused("the monitored Service changed outside the approved selector field")
    return actual


def patch_selector(case, current, baseline, expected, replacement, *, dry_run=False):
    if ((expected, replacement) not in ((baseline["selector"], broken_selector(case, baseline)),
                                         (broken_selector(case, baseline), baseline["selector"]))
            or selector(case, current, baseline) != expected):
        raise live.Refused("unapproved Service selector transition")
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": baseline["service_uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
        {"op": "test", "path": BASELINE_PATH, "value": expected},
        {"op": "replace", "path": BASELINE_PATH, "value": replacement},
    ]
    args = ("patch", "service", case.workload, "--type=json", "-p", json.dumps(operations))
    if dry_run:
        admitted = json.loads(live.command(*args, "--dry-run=server", "-o", "json"))
        if selector(case, admitted, baseline) != replacement:
            raise live.Refused("server dry-run returned a different Service change")
    else:
        live.command(*args)


def current_service(case):
    return live.get("service", case.workload)


def endpoint_uids(case, baseline):
    groups = live.get("endpointslice", selector=f"kubernetes.io/service-name={case.workload}")["items"]
    ready = []
    for group in groups:
        if not any(ref.get("kind") == "Service" and ref.get("uid") == baseline["service_uid"]
                   for ref in group["metadata"].get("ownerReferences", [])):
            raise live.Refused("EndpointSlice is outside the original Service identity")
        for endpoint in group.get("endpoints") or []:
            flags = endpoint.get("conditions") or {}
            if flags.get("ready") is True and flags.get("terminating") is not True:
                target = endpoint.get("targetRef") or {}
                if target.get("kind") != "Pod" or not target.get("uid"):
                    raise live.Refused("ready endpoint is missing its server-side Pod identity")
                ready.append(target["uid"])
    return ready


def checked_deployment(case, baseline):
    deployment = live.get("deployment", case.workload)
    if (deployment["metadata"]["uid"] != baseline["deployment_uid"]
            or live.fixture_digest(deployment) != baseline["deployment_sha256"]):
        raise live.Refused("the original dev Deployment changed")
    return deployment


def unchanged_deployment(case, baseline):
    if not live.is_settled(checked_deployment(case, baseline)):
        raise live.Refused("the original dev Deployment is unhealthy")


def restored_probes(case, baseline):
    current = current_service(case)
    if checked_selector(current, baseline) != baseline["selector"]:
        raise live.Refused("selector has not returned to its original value")
    if not live.is_settled(checked_deployment(case, baseline)):
        return None
    if endpoint_uids(case, baseline) != [baseline["endpoint_uid"]]:
        return None
    probes = [live.probe(case), live.probe(case)]
    if not all(row["endpoint_uid"] == baseline["endpoint_uid"] for row in probes):
        raise live.Refused("service probe did not use the original endpoint")
    return probes


def wait_for_healthy(case, baseline, deadline):
    while monotonic() < deadline:
        probes = restored_probes(case, baseline)
        if probes is not None:
            return probes
        sleep(3)
    raise live.Refused("the original endpoint did not return")


def restore(case, baseline):
    current = current_service(case)
    actual = selector(case, current, baseline)
    operator = actual != baseline["selector"]
    if operator:
        try:
            patch_selector(case, current, baseline, actual, baseline["selector"])
        except (live.Refused, TimeoutExpired):
            # An interrupted PATCH can already have been accepted, so read the same UID again.
            current = current_service(case)
            if selector(case, current, baseline) != baseline["selector"]:
                patch_selector(case, current, baseline, actual, baseline["selector"])
    return wait_for_healthy(case, baseline, monotonic() + UNDO_SECONDS), operator


def proxy_error(case):
    path = f"/api/v1/namespaces/dev/services/{case.workload}:8080/proxy/actuator/health/readiness"
    result = run([*live.kube_prefix(), "--request-timeout=8s", "get", "--raw", path],
                 text=True, capture_output=True, timeout=10)
    for message in case.errors:
        if result.returncode and f"Error from server (ServiceUnavailable): {message}" in result.stderr.splitlines():
            return message
    raise live.Refused("the Service proxy did not report an actual no-endpoints 503")


def fault_snapshot(case, baseline):
    current = current_service(case)
    if selector(case, current, baseline) != broken_selector(case, baseline):
        raise live.Refused("trial selector was replaced unexpectedly")
    if endpoint_uids(case, baseline):
        return None
    return {"error": proxy_error(case), "ready_endpoints": 0, "selector": current["spec"]["selector"],
            "http_status": 503, "service_uid": baseline["service_uid"], "contract_ok": False}


def probe_fault(case, baseline, deadline):
    while monotonic() < deadline:
        fault = fault_snapshot(case, baseline)
        if fault is not None:
            unchanged_deployment(case, baseline)
            return fault
        sleep(2)
    raise live.Refused("the selector mismatch never removed all ready endpoints")


def model_operation(case, request, baseline, fault, lease):
    tool = request["tool"]
    if tool == "source":
        return {"service_selector": selector(case, current_service(case), baseline),
                "previous_healthy_selector": baseline["selector"], "configure_key": "selector_app"}
    if tool in ("workload", "logs", "probe"):
        return fault_snapshot(case, baseline) or {"error": "expected Service outage was not observable"}
    if tool == "finish":
        return {"finished": True}
    if tool != "configure" or (request.get("key"), request.get("value")) != ("selector_app", case.workload):
        return {"accepted": False, "reason": "the fixture permits restoring only the original Service app selector"}
    lease.require_active()
    unchanged_deployment(case, baseline)
    patch_selector(case, current_service(case), baseline, broken_selector(case, baseline), baseline["selector"])
    return {"accepted": True, "action": "selector_app_cas", "from": case.bad_app, "to": case.workload}


def incident_rows(case, cursor):
    return live.query(f"""SELECT coalesce(json_agg(row_to_json(records)), '[]'::json) FROM (
      SELECT i.id, i.status, i.severity_level, i.signal_reasons,
        (SELECT min(log.detail#>>'{{signal,received_at}}') FROM audit_log log
             WHERE log.incident_id=i.id AND log.event='signal_observed'
               AND log.detail->>'object'='Service/{case.workload}'
               AND log.detail->>'reason' IN ('NoEndpoints','TargetServiceNoEndpoints')) AS first_endpoint_signal_at,
        (SELECT min((log.detail#>>'{{signal,received_at}}')::timestamptz) FROM audit_log log
             WHERE log.incident_id=i.id AND log.event='signal_observed'
               AND log.detail->>'reason'='TargetConnectionPoolExhausted') AS first_pool_signal_at,
        (SELECT min((log.detail#>>'{{signal,received_at}}')::timestamptz) FROM audit_log log
             WHERE log.incident_id=i.id AND log.event='signal_observed'
               AND log.detail->>'reason'='TargetHighLatency') AS first_latency_signal_at,
        (SELECT min((log.detail#>>'{{signal,received_at}}')::timestamptz) FROM audit_log log
             WHERE log.incident_id=i.id AND log.event='signal_observed'
               AND log.detail->>'reason' IN ('CrashLoopBackOff','TargetWorkloadCrashLooping')) AS first_restart_signal_at,
        (SELECT min((log.detail#>>'{{signal,received_at}}')::timestamptz) FROM audit_log log
             WHERE log.incident_id=i.id AND log.event='signal_observed'
               AND log.detail->>'reason'='TargetWorkloadOOMKilled') AS first_oom_signal_at,
        (SELECT count(*) FROM actions a WHERE a.incident_id=i.id AND a.executed) AS executed_actions,
        (SELECT coalesce(json_agg(json_build_object('role', r.role, 'model', r.model, 'duration_ms', r.duration_ms,
             'input_tokens', r.input_tokens, 'output_tokens', r.output_tokens, 'estimated_usd', r.estimated_cost_usd)),
             '[]'::json) FROM agent_runs r WHERE r.incident_id=i.id) AS calls
      FROM incidents i WHERE i.id>{int(cursor)} AND i.namespace='dev' AND i.workload='{case.workload}'
      ORDER BY i.id
    ) records""")


def endpoint_incidents(rows):
    return [row for row in rows if ENDPOINT_REASONS.intersection(row.get("signal_reasons") or [])]


def product_outcome(case, baseline, result, cursor, began):
    epoch = endpoint_poller_epoch()
    while monotonic() - began < MAX_PRODUCT_SECONDS:
        matches = endpoint_incidents(incident_rows(case, cursor))
        if len(matches) > 1:
            raise live.Refused("more than one Service-endpoint incident matched the same trial")
        if matches:
            signal_at = matches[0]["first_endpoint_signal_at"]
            if not signal_at:
                raise live.Refused("the endpoint incident has no attributed signal timestamp")
            delay = datetime.fromisoformat(signal_at) - datetime.fromisoformat(result["injection_started_at"])
            result["detection_ms"] = round(delay.total_seconds() * 1000)
            if result["detection_ms"] < ENDPOINT_GRACE_SECONDS * 1000:
                raise live.Refused("a new endpoint incident arrived before the configured grace period")
            result["incident"] = matches[0]
        fault = fault_snapshot(case, baseline)
        if fault is None:
            raise live.Refused("endpoint loss was not continuous")
        if matches and matches[0]["status"] not in live.NONTERMINAL:
            if endpoint_poller_epoch() != epoch:
                raise live.Refused("endpoint watcher restarted during the comparison")
            result["watcher_continuous"] = True
            if matches[0]["status"] in HANDOFF_STATES and matches[0]["executed_actions"] == 0:
                result.update(status="unrecovered", independent_probes=[fault, fault_snapshot(case, baseline)])
            return
        sleep(8)
    result["observation_seconds"] = round(monotonic() - began)
    if "incident" not in result and endpoint_poller_epoch() == epoch:
        result.update(status="product_observation_only", watcher_continuous=True,
                      independent_probes=[fault_snapshot(case, baseline)])


def endpoint_poller_epoch():
    state = live.product_status("/api/status")
    watchers = state.get("watchers") or {}
    endpoint = watchers.get("endpoint_poller") or watchers.get("endpoint_poller/dev") or {}
    if (state.get("ready") is not True or state.get("target_namespaces") != ["dev"]
            or endpoint.get("state") != "running" or not endpoint.get("since")):
        raise live.Refused("the dev-only LiveProbe endpoint watcher is not ready")
    return endpoint["since"]


def baseline_for(case):
    endpoint_poller_epoch()
    service = current_service(case)
    deployment = live.get("deployment", case.workload)
    spec = service["spec"]
    original = spec.get("selector") or {}
    pod_labels = deployment["spec"]["template"]["metadata"].get("labels") or {}
    if (original.get("app") != case.workload or spec.get("publishNotReadyAddresses") is True
            or spec.get("type") == "ExternalName" or spec.get("clusterIP") in (None, "None")
            or not all(pod_labels.get(key) == value for key, value in original.items())
            or not live.is_settled(deployment) or live.application_owned(deployment)):
        raise live.Refused("the original Service and ready Deployment do not form an eligible fixture")
    baseline = {"service_uid": service["metadata"]["uid"], "service_spec": spec, "selector": original,
                "deployment_uid": deployment["metadata"]["uid"], "deployment_sha256": live.fixture_digest(deployment)}
    uid, = endpoint_uids(case, baseline)
    baseline["endpoint_uid"] = uid
    probes = restored_probes(case, baseline)
    if not live.successful_probes(probes):
        raise live.Refused("the baseline two independent HTTP requests did not succeed")
    cursor = live.query(f"""SELECT json_build_object('cursor', coalesce(max(i.id),0), 'active',
        count(*) FILTER (WHERE i.status IN ('open','analyzing','action_pending','acting')), 'active_actions',
        (SELECT count(*) FROM actions a WHERE a.target IN ('{case.target}','dev/Deployment/{case.workload}')
         AND a.status IN ('approved','pending_approval','executing','indeterminate')))
      FROM incidents i WHERE i.namespace='dev' AND i.workload='{case.workload}'""")
    if cursor["active"] or cursor["active_actions"]:
        raise live.Refused("a current LiveProbe incident or write affects the fixture")
    return baseline, probes, cursor["cursor"]


def execute(case, arm, baseline, probes, cursor, gate, reference, output, journal):
    digest = hash_text(json.dumps(baseline, sort_keys=True))
    if reference and (reference.get("case"), reference.get("arm"), reference.get("fixture_sha256"),
                      reference.get("case_sha256"), reference.get("protocol")) != (
                          case.case_id, "liveprobe", digest, case.case_sha256, PROTOCOL):
        raise live.Refused("the LiveProbe receipt is for a different fixture")
    result = {"case": case.case_id, "arm": arm, "protocol": PROTOCOL, "namespace": "dev", "target": case.target,
              "case_sha256": case.case_sha256, "fixture_sha256": digest, "status": "trial_unverified",
              "healthy_before": {"contract_ok": True, "probes": probes}, "node_gate": gate,
              "calls": [], "selector_before": baseline["selector"], "service_uid": baseline["service_uid"]}
    live.write_once(journal, {"target": case.target, "service_uid": baseline["service_uid"],
                             "restore_only_if_value": broken_selector(case, baseline),
                             "original_selector": baseline["selector"], "receipt": str(output),
                             "started_at": datetime.now(timezone.utc).isoformat()})
    lease = None
    began = None
    attempted = False
    try:
        if reference is not None:
            lease = live.WorkloadLease.claim(case, arm)
            lease.require_active()
        current = current_service(case)
        attempted = True
        began = monotonic()
        result["injection_started_at"] = datetime.now(timezone.utc).isoformat()
        patch_selector(case, current, baseline, baseline["selector"], broken_selector(case, baseline))
        fault = probe_fault(case, baseline, began + 45)
        result.update(fault_before=fault, input_sha256=hash_text(TASK + fault["error"]))
        if reference is None:
            product_outcome(case, baseline, result, cursor, began)
        else:
            if reference.get("input_sha256") != result["input_sha256"]:
                raise live.Refused("independently observed initial error differs from the LiveProbe run")
            infer(result, arm, baseline, fault, lease, monotonic() + 210,
                  apply_request=partial(model_operation, case), verify=partial(wait_for_healthy, case))
            if result.get("verified_before_cleanup"):
                result["status"] = "verified_recovered"
            elif result["calls"] and not result.get("model_failed"):
                result.update(status="unrecovered", independent_probes=[fault_snapshot(case, baseline),
                                                                      fault_snapshot(case, baseline)])
    except READABLE_ERRORS as error:
        result.update(status="trial_unverified",
                      trial_error=str(error) if isinstance(error, live.Refused) else type(error).__name__)
    finally:
        if began is not None:
            result["wall_duration_ms"] = round((monotonic() - began) * 1000)
        try:
            if attempted:
                result["restored"], result["operator_restored"] = restore(case, baseline)
        except READABLE_ERRORS as error:
            result.update(status="cleanup_not_verified", cleanup_error=type(error).__name__)
        if lease is not None:
            try:
                result["interfering_incidents"] = len(endpoint_incidents(incident_rows(case, cursor)))
                if result["interfering_incidents"]:
                    result["status"] = "trial_unverified"
            except READABLE_ERRORS as error:
                result.update(status="trial_unverified", observation_error=type(error).__name__)
            try:
                lease.release()
                result["lease_released"] = True
            except READABLE_ERRORS as error:
                result.update(status="lease_not_verified", lease_error=type(error).__name__)
        elif result.get("incident"):
            calls = result["incident"]["calls"]
            result.update(calls=calls, model_duration_ms=measured_sum([c["duration_ms"] for c in calls]),
                          estimated_usd=measured_sum([c["estimated_usd"] for c in calls]),
                          usage={key: measured_sum([c[key] for c in calls]) for key in ("input_tokens", "output_tokens")})
        live.write_once(output, result)
        if (not attempted or result.get("restored")) and (lease is None or result.get("lease_released")):
            journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=SELECTOR_WORKLOADS, required=True)
    parser.add_argument("--arm", choices=("liveprobe", *MODELS), default="liveprobe")
    parser.add_argument("--reference", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.arm == "liveprobe") != (args.reference is None):
        parser.error("a product reference is required for a standalone arm")
    case = load_selector_case(args.case)
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference is not None:
        from evaluation.comparison_summary import dev_selector_record

        dev_selector_record(reference, "liveprobe")
    journal = live.EVIDENCE / "dev-k8s-fault-active.json"
    if args.preflight:
        baseline, _, _ = baseline_for(case)
        patch_selector(case, current_service(case), baseline, baseline["selector"], broken_selector(case, baseline),
                       dry_run=True)
        if selector(case, current_service(case), baseline) != baseline["selector"]:
            raise live.Refused("original selector changed during dry-run")
        try:
            gate = live.CHAOS.node_gate()
        except live.CHAOS.Stop as error:
            print(json.dumps({"eligible": False, "target": case.target, "server_dry_run": True, "reason": str(error)}))
            return 2
        print(json.dumps({"eligible": True, "target": case.target, "service_uid": baseline["service_uid"],
                          "server_dry_run": True, "node_gate": gate}))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with (live.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.output.exists() or journal.exists():
            raise live.Refused("an output or unresolved fault journal already exists")
        baseline, probes, cursor = baseline_for(case)
        result = execute(case, args.arm, baseline, probes, cursor, live.CHAOS.node_gate(), reference, args.output, journal)
    print(json.dumps({"status": result["status"], "incident": (result.get("incident") or {}).get("id"),
                      "restored": bool(result.get("restored"))}))
    return 0 if result["status"] in ("unrecovered", "verified_recovered", "product_observation_only") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False, "reason": str(error)
                          if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
