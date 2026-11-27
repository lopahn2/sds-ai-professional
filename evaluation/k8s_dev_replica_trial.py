"""Live standalone comparisons on the approved dev KW-04 replica creation fixture."""
from __future__ import annotations

from argparse import ArgumentParser
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
from subprocess import TimeoutExpired
from time import monotonic, sleep

from evaluation import isolated_oom_target, k8s_dev_image_trial as trial
from evaluation.live_repair import RemoteModelSession, TASK
from evaluation.single_baseline import K8S_MAX_TOOL_TURNS, MODELS, estimate


@dataclass(frozen=True)
class ReplicaCase:
    case_id: str = "KW-04"
    workload: str = "lp-eval-kw04"

    @property
    def target(self):
        return f"dev/Deployment/{self.workload}"


CASE = ReplicaCase()
PROTOCOL = "live-repair/v36"
MISSING = "lp-eval-kw04-missing"
ORIGINAL = "default"
ALIASES = ("serviceAccountName", "serviceAccount")
POINTER = "/spec/template/spec/"
SELECTOR = f"app={CASE.workload}"
ERROR_SUFFIX = f'error looking up service account dev/{MISSING}: serviceaccount "{MISSING}" not found'
ERR = re.compile(r'Error creating: pods "lp-eval-kw04-[a-z0-9]+-[a-z0-9]*" is forbidden: ' + re.escape(ERROR_SUFFIX))
SAFE_ID = re.compile(r"[0-9a-f-]{36}")
READABLE_ERRORS = (trial.Refused, OSError, TimeoutExpired, KeyError, TypeError, ValueError)


def hash_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def confirmed_health(receipt):
    return (isinstance(receipt, dict) and receipt.get("contract_ok") is True
            and receipt.get("replicas") == 1 and receipt.get("requests") == [200, 200]
            and type(receipt.get("generation")) is int and bool(receipt.get("pod_uid")))


def fixture_reference(baseline_path, reference_path):
    source = trial.ROOT / "evaluation/catalogs/comparative-2026-11-23.json"
    catalog = json.loads(source.read_text(encoding="utf-8"))
    case, = [row for row in catalog["kubernetes_new"]["cases"] if row["id"] == CASE.case_id]
    if case["environment"] != "isolated-workload":
        raise trial.Refused("catalog no longer approves this isolated-workload case")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    fault = reference.get("fault_before") or {}
    message = fault.get("error")
    if (baseline.get("namespace"), baseline.get("name"), baseline.get("baseline_ready")) != ("dev", CASE.workload, True):
        raise trial.Refused("fixture baseline has different scope")
    if (reference.get("case"), reference.get("arm"), reference.get("target")) != (CASE.case_id, "liveprobe", CASE.target):
        raise trial.Refused("product receipt does not belong to this fixture")
    if (reference.get("fixture_sha256") != baseline.get("template_sha256") or reference.get("trial_error")
            or not confirmed_health(reference.get("healthy_before")) or not confirmed_health(reference.get("restored"))
            or not isinstance(message, str) or not ERR.fullmatch(message) or fault.get("error_sha256") != hash_text(message)):
        raise trial.Refused("product receipt lacks the pinned fixture, original error, or independent cleanup proof")
    return baseline, reference, hash_text(json.dumps(case, sort_keys=True, ensure_ascii=False))


def account(deployment, baseline):
    spec = deployment["spec"]["template"]["spec"]
    name, alias = (spec[field] for field in ALIASES)
    if name != alias or name not in (ORIGINAL, MISSING) or deployment["metadata"]["uid"] != baseline["deployment_uid"]:
        raise trial.Refused("unexpected ServiceAccount aliases or Deployment identity")
    normalized = deepcopy(deployment)
    for field in ALIASES:
        normalized["spec"]["template"]["spec"][field] = ORIGINAL
    if trial.fixture_digest(normalized) != baseline["template_sha256"]:
        raise trial.Refused("fixture changed outside the ServiceAccount aliases")
    return name


def patch_account(deployment, baseline, expected, replacement):
    if {expected, replacement} != {ORIGINAL, MISSING} or account(deployment, baseline) != expected:
        raise trial.Refused("a scoped ServiceAccount transition could not be proved")
    def operation(op, path, value):
        return {"op": op, "path": path, "value": value}
    operations = [operation("test", "/metadata/uid", baseline["deployment_uid"]),
                  operation("test", "/metadata/resourceVersion", deployment["metadata"]["resourceVersion"])]
    operations.extend(operation("test", POINTER + field, expected) for field in ALIASES)
    operations.extend(operation("replace", POINTER + field, replacement) for field in ALIASES)
    trial.command("patch", "deployment", CASE.workload, "--type=json", "-p", json.dumps(operations))


def current():
    return trial.get("deployment", CASE.workload)


def replica(baseline, expected):
    matches = [item for item in trial.get("replicaset", selector=SELECTOR)["items"]
               if item["spec"]["template"]["spec"].get("serviceAccountName") == expected
               and any(o.get("controller") and o.get("kind") == "Deployment" and o.get("uid") == baseline["deployment_uid"]
                       for o in item["metadata"].get("ownerReferences", []))]
    if len(matches) > 1:
        raise trial.Refused("ambiguous fixture ReplicaSet")
    return matches[0] if matches else None


def healthy(deployment, baseline):
    if account(deployment, baseline) != ORIGINAL or not trial.is_settled(deployment):
        raise trial.Refused("original ServiceAccount and current rollout generation must be healthy")
    rs = replica(baseline, ORIGINAL)
    if rs is None:
        raise trial.Refused("healthy fixture ReplicaSet is absent")
    pods = [pod for pod in trial.get("pod", selector=SELECTOR)["items"]
            if not pod["metadata"].get("deletionTimestamp") and any(
                o.get("controller") and o.get("kind") == "ReplicaSet" and o.get("uid") == rs["metadata"]["uid"]
                for o in pod["metadata"].get("ownerReferences", []))
            and any(c.get("name") == "verification" and c.get("ready") is True
                    for c in pod.get("status", {}).get("containerStatuses", []))]
    if len(pods) != 1:
        raise trial.Refused("exactly one ready fixture Pod is required")
    pod = pods[0]["metadata"]
    code = ("from urllib.request import urlopen; "
            "print([(c.status,c.read().decode().strip()) for c in "
            "[urlopen('http://127.0.0.1:8080/ready',timeout=3) for _ in range(2)]])")
    reply = trial.command("exec", f'pod/{pod["name"]}', "-c", "verification", "--", "python", "-B", "-c", code).strip()
    if reply != "[(200, 'kw04-ready'), (200, 'kw04-ready')]":
        raise trial.Refused("both independent fixture HTTP requests must match the baseline")
    return {"generation": deployment["metadata"]["generation"], "replicas": 1, "pod_uid": pod["uid"],
            "requests": [200, 200], "contract_ok": True}


def event_count(event):
    return event.get("count") or (event.get("series") or {}).get("count") or 1


def owned_events(rs):
    uid = rs["metadata"]["uid"]
    if not SAFE_ID.fullmatch(uid):
        raise trial.Refused("unexpected fixture ReplicaSet UID")
    payload = json.loads(trial.command("get", "events", "--field-selector", f"involvedObject.uid={uid}", "-o", "json"))
    return [event for event in payload["items"] if event.get("reason") == "FailedCreate"
            and event.get("involvedObject", {}).get("uid") == uid]


def fresh_failure_event(events, prior_events):
    matching = [event for event in events if event.get("type") == "Warning"
                and event_count(event) > prior_events.get(event["metadata"]["uid"], 0)
                and ERR.fullmatch(event.get("message", ""))]
    if not matching:
        return None
    return min(matching, key=lambda event: (event.get("firstTimestamp") or event["metadata"]["creationTimestamp"],
                                            event["metadata"]["uid"]))


def observe_fault(deployment, baseline, prior_events, prior_generation):
    if account(deployment, baseline) != MISSING:
        return None
    rs = replica(baseline, MISSING)
    if not rs:
        return None
    meta, status = rs["metadata"], rs.get("status", {})
    revision = meta["annotations"].get("deployment.kubernetes.io/revision")
    condition = next((c for c in status.get("conditions", []) if c.get("type") == "ReplicaFailure"
                      and c.get("reason") == "FailedCreate" and c.get("status") == "True"), None)
    if (not revision or deployment["metadata"]["annotations"].get("deployment.kubernetes.io/revision") != revision
            or condition is None or status.get("observedGeneration") != meta.get("generation")
            or rs["spec"]["replicas"] != 1 or status.get("replicas", 0) != 0):
        return None
    events = owned_events(rs)
    event = fresh_failure_event(events, prior_events)
    source = "new_event"
    if event is None and meta["generation"] > prior_generation and condition.get("lastTransitionTime"):
        # The event series can stop incrementing even while this newly reconciled ReplicaSet fails.
        # Use the same original benchmark line only when the current controller reports its exact error.
        message = condition.get("message") or ""
        messages = {message, f"Error creating: {message}"}
        event = next((item for item in events if item.get("type") == "Warning"
                      and item.get("message") in messages and ERR.fullmatch(item["message"])), None)
        source = "current_replicaset_condition"
    if event is None:
        return None
    return {"replica_set": meta["name"], "uid": meta["uid"], "revision": revision, "replicas": 0, "desired": 1,
            "event_uid": event["metadata"]["uid"], "event_count": event_count(event), "error": event["message"],
            "error_sha256": hash_text(event["message"]), "error_source": source,
            "replicaset_generation": meta["generation"], "condition_transition": condition.get("lastTransitionTime")}


def require_reference_error(fault, reference):
    expected = reference["fault_before"]
    if fault["error"] != expected["error"] or fault["error_sha256"] != expected["error_sha256"]:
        raise trial.Refused("first live FailedCreate error differs from the product receipt")
    return fault["error"]


def safe_state(baseline):
    deployment = current()
    name = account(deployment, baseline)
    return {"namespace": "dev", "deployment": CASE.workload, "service_account": name,
            "desired_replicas": 1, "ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
            "current_replica_set_account": name}


def existing_account(name):
    return trial.command("get", "sa", name, "--ignore-not-found", "-o", "name").strip() == f"serviceaccount/{name}"


def operate(request, baseline, fault, lease):
    tool = request.get("tool")
    if tool == "logs":
        return {"reason": "FailedCreate", "error": fault["error"]}
    if tool == "workload":
        return {**safe_state(baseline), "failed_replica_set": fault["replica_set"], "failed_replica_count": 0}
    if tool == "source":
        dep = current()
        name = account(dep, baseline)
        original = replica(baseline, ORIGINAL)
        return {"current_service_account": name, "configure_field": "service_account",
                "previous_owned_service_account": ORIGINAL if original else None,
                "current_account_exists": existing_account(name),
                "previous_account_exists": bool(original and existing_account(ORIGINAL))}
    if tool == "probe":
        return safe_state(baseline)
    if tool == "finish":
        return {"finished": True}
    if tool != "configure" or request.get("key") != "service_account" or request.get("value") != ORIGINAL:
        return {"accepted": False, "reason": "outside the allowed ServiceAccount field or previous known version"}
    lease.require_active()
    patch_account(current(), baseline, MISSING, ORIGINAL)
    return {"accepted": True, "action": "service_account_alias_cas", "from": MISSING, "to": ORIGINAL}


def wait_for_healthy(baseline, deadline):
    while monotonic() < deadline:
        dep = current()
        if account(dep, baseline) != ORIGINAL:
            raise trial.Refused("the model's ServiceAccount was replaced")
        if trial.is_settled(dep):
            return healthy(dep, baseline)
        sleep(3)
    raise trial.Refused("model repair did not settle before the deadline")


def restore(baseline):
    dep = current()
    operator_restored = account(dep, baseline) == MISSING
    if operator_restored:
        for attempt in range(3):
            try:
                patch_account(dep, baseline, MISSING, ORIGINAL)
                break
            except (trial.Refused, TimeoutExpired):
                dep = current()
                if account(dep, baseline) == ORIGINAL:
                    break
                if attempt == 2:
                    raise
    return wait_for_healthy(baseline, monotonic() + 70), operator_restored


def measured_sum(values):
    return sum(values) if values and all(value is not None for value in values) else None


def infer(result, arm, baseline, fault, lease, deadline, *, apply_request=operate, verify=wait_for_healthy):
    model, effort, _ = MODELS[arm]
    result.update(model=model, effort=effort, calls=[], investigation=[])
    target = isolated_oom_target.active()
    options = {"command": target.model_command(model, effort, PROTOCOL)} if target else {}
    session = RemoteModelSession(model, effort, PROTOCOL, **options)
    try:
        for _ in range(K8S_MAX_TOOL_TURNS):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            lease.require_active()
            session.timeout = min(50, remaining)
            answer = session.turn(model, effort, fault["error"], result["investigation"])
            result["calls"].append({k: v for k, v in answer.items() if k != "data"})
            if (answer.get("model"), answer.get("effort")) != (model, effort):
                raise trial.Refused("standalone model identity differs from the catalog")
            if answer.get("status") != "ok":
                result["model_failed"] = answer.get("status")
                break
            data = answer["data"]
            value = apply_request(data, baseline, fault, lease)
            result["investigation"].append({"request": data, "result": value})
            if value.get("accepted") is True:
                result["standalone_action"] = value
                result["verified_before_cleanup"] = verify(baseline, deadline)
                break
            if data.get("tool") == "finish":
                break
    finally:
        session.close()
        calls = result["calls"]
        usages = [call.get("usage") or {} for call in calls]
        result["usage"] = {k: measured_sum([usage.get(k) for usage in usages])
                           for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}
        result["model_duration_ms"] = measured_sum([call.get("duration_ms") for call in calls])
        result["estimated_usd"] = measured_sum([estimate(arm, usage) for usage in usages])


def preflight(baseline):
    info = trial.product_status("/api/status")
    if (info.get("ready") is not True or info.get("target_namespaces") != ["dev"]
            or info.get("watchers", {}).get("workload_poller/dev", {}).get("state") != "running"):
        raise trial.Refused("dev workload observation runtime is not ready")
    dep = current()
    before = healthy(dep, baseline)
    if not existing_account(ORIGINAL) or existing_account(MISSING):
        raise trial.Refused("fixture ServiceAccounts do not match the controlled starting state")
    previous = replica(baseline, MISSING)
    if previous and (previous["spec"]["replicas"] != 0
                     or any(c.get("type") == "ReplicaFailure" for c in previous.get("status", {}).get("conditions", []))):
        raise trial.Refused("an earlier failed ReplicaSet has not returned to its idle baseline")
    prior = {event["metadata"]["uid"]: event_count(event) for event in owned_events(previous)} if previous else {}
    prior_generation = previous["metadata"]["generation"] if previous else 0
    gate = trial.CHAOS.node_gate()
    cursor = trial.query("SELECT coalesce(max(id),0) FROM incidents")
    return dep, gate, before, prior, prior_generation, cursor


def new_incidents(cursor):
    return trial.query(f"SELECT coalesce(json_agg(id ORDER BY id),'[]'::json) FROM incidents "
                       f"WHERE id>{int(cursor)} AND namespace='dev' AND workload='{CASE.workload}'")


def attempt(arm, baseline, reference, case_hash, *, output, journal, limit):
    dep, gate, before, prior_events, prior_generation, cursor = preflight(baseline)
    result = {"case": CASE.case_id, "arm": arm, "target": CASE.target, "protocol": PROTOCOL,
              "case_sha256": case_hash, "fixture_sha256": baseline["template_sha256"], "healthy_before": before,
              "node_gate": gate, "status": "trial_unverified", "replicaset_generation_before": prior_generation,
              "reference_incident": (reference.get("incident") or {}).get("id")}
    trial.write_once(journal, {"target": CASE.target, "uid": baseline["deployment_uid"], "arm": arm,
                              "original_service_account": ORIGINAL, "restore_only_if_service_account": MISSING,
                              "receipt": str(output), "started_at": datetime.now(timezone.utc).isoformat()})
    lease = None
    attempted = False
    start = monotonic()
    deadline = start + limit
    try:
        lease = trial.WorkloadLease.claim(CASE, arm)
        lease.require_active()
        attempted = True
        patch_account(dep, baseline, ORIGINAL, MISSING)
        while monotonic() < deadline:
            fault = observe_fault(current(), baseline, prior_events, prior_generation)
            if fault:
                observed = monotonic()
                result["fault_before"] = fault
                result["first_error_ms"] = round((observed - start) * 1000)
                initial = require_reference_error(fault, reference)
                result["initial_error_log"] = initial
                result["input_sha256"] = hash_text(TASK + initial)
                infer(result, arm, baseline, fault, lease, deadline)
                if "verified_before_cleanup" in result:
                    result["first_error_to_verified_ms"] = round((monotonic() - observed) * 1000)
                else:
                    a = observe_fault(current(), baseline, prior_events, prior_generation)
                    sleep(2)
                    b = observe_fault(current(), baseline, prior_events, prior_generation)
                    if a and b and a["uid"] == b["uid"] == fault["uid"]:
                        result["still_failed_before_cleanup"] = [a, b]
                break
            sleep(3)
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, trial.Refused) else type(error).__name__
    finally:
        result["wall_duration_ms"] = round((monotonic() - start) * 1000)
        if attempted:
            try:
                lease.require_active()
            except READABLE_ERRORS as error:
                result["lease_error"] = type(error).__name__
            try:
                # An expired suppression lease cannot prevent a safe operator restore of our exact field.
                result["restored"], result["operator_restored"] = restore(baseline)
                result["liveprobe_incidents"] = new_incidents(cursor)
            except READABLE_ERRORS as error:
                result["cleanup_error"] = str(error) if isinstance(error, trial.Refused) else type(error).__name__
        if lease is not None and (not attempted or "restored" in result):
            try:
                lease.release()
                result["lease_released"] = True
            except READABLE_ERRORS as error:
                result["lease_error"] = type(error).__name__
        if "cleanup_error" in result:
            result["status"] = "cleanup_not_verified"
        elif "lease_error" in result:
            result["status"] = "lease_not_verified"
        elif result.get("liveprobe_incidents"):
            result["status"] = "interfered_by_liveprobe"
        elif result.get("lease_released") and confirmed_health(result.get("restored")) and not result.get("trial_error"):
            if (result.get("standalone_action", {}).get("accepted") is True
                    and confirmed_health(result.get("verified_before_cleanup")) and not result.get("operator_restored")):
                result["status"] = "verified_dev_recovered"
            elif result.get("still_failed_before_cleanup") and not result.get("model_failed"):
                result["status"] = "observed_terminal_unrecovered"
        trial.write_once(output, result)
        if (not attempted or "restored" in result) and (lease is None or result.get("lease_released") is True):
            journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=tuple(MODELS), required=True)
    parser.add_argument("--reference", type=Path, default=trial.EVIDENCE / "dev-kw04-product-alias-retest.json")
    parser.add_argument("--baseline", type=Path, default=trial.EVIDENCE / "dev-kw04-baseline-verified.json")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hold-seconds", type=int, default=210)
    args = parser.parse_args()
    if args.preflight == args.execute or args.execute and args.output is None or not 30 <= args.hold_seconds <= 240:
        parser.error("choose --preflight or --execute --output PATH with hold between 30 and 240 seconds")
    baseline, reference, case_hash = fixture_reference(args.baseline, args.reference)
    if args.preflight:
        _, gate, *_ = preflight(baseline)
        print(json.dumps({"eligible": True, "target": CASE.target, "gate": gate}))
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal = trial.EVIDENCE / "dev-k8s-fault-active.json"
    with (trial.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise trial.Refused("an unfinished fault journal or prior receipt requires review")
        outcome = attempt(args.arm, baseline, reference, case_hash, output=args.output, journal=journal,
                          limit=args.hold_seconds)
    print(json.dumps({"case": CASE.case_id, "arm": args.arm, "status": outcome["status"],
                      "restored": confirmed_health(outcome.get("restored")), "lease_released": outcome.get("lease_released")}))
    return 0 if outcome["status"] in ("verified_dev_recovered", "observed_terminal_unrecovered") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, trial.CHAOS.Stop) as error:
        reason = str(error) if isinstance(error, (trial.Refused, trial.CHAOS.Stop)) else type(error).__name__
        print(json.dumps({"eligible": False, "reason": reason}))
        raise SystemExit(2) from None
