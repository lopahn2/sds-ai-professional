"""Observe the original short dev Service faults with one bounded application operation."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
import re
from subprocess import run
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID

from evaluation import k8s_dev_image_trial as live
from evaluation import k8s_dev_selector_trial as service
from evaluation.dev_endpoint_pulse_case import (
    DEPENDENCY_PROBE_LIMIT_SECONDS, INVENTORY_CALLER, INVENTORY_FIXTURE, OBSERVE_AFTER_RECOVERY,
    ORDER_FIXTURE, PROTOCOL, PULSES, bounded_termination, load_case, receipt_times, standalone_completion,
)
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text, infer
from evaluation.live_repair import TASK
from evaluation.single_baseline import MODELS


ORDER_FIXTURE_RECEIPT = live.EVIDENCE / "dev-isolated-order-pulse-fixture.json"
INVENTORY_FIXTURE_RECEIPT = live.EVIDENCE / "dev-isolated-inventory-pulse-fixture.json"
SIX_POOL_RECEIPT = live.EVIDENCE / "dev-isolated-inventory-pool-six-fixture.json"
ORDER_POOL_RECEIPT = live.EVIDENCE / "dev-isolated-order-pool-eight-fixture.json"
FIXTURE_RECEIPTS = {
    ORDER_FIXTURE: ORDER_FIXTURE_RECEIPT,
    INVENTORY_FIXTURE: INVENTORY_FIXTURE_RECEIPT,
}
MODEL_BUDGET_SECONDS = 250
CONTROL = """set -eu
test -n "$CHAOS_OPERATOR_TOKEN"
url=http://127.0.0.1:8080/internal/chaos/v1
if [ "$1" = GET ]; then
  printf 'Authorization: Bearer %s\n' "$CHAOS_OPERATOR_TOKEN" | curl -sS --max-time 5 -H @- \
    -w '\n%{http_code}' "$url/faults"
else
  path=faults
  if [ "$1" = RESET ]; then path=reset; fi
  printf 'Authorization: Bearer %s\n' "$CHAOS_OPERATOR_TOKEN" | curl -sS --max-time 5 -H @- \
    -H 'Content-Type: application/json' --data-binary "$2" -w '\n%{http_code}' "$url/$path"
fi
"""


def pulse_pod(case, baseline):
    items = live.get("pods", selector=f"app={case.workload}")["items"]
    matched = [p["metadata"]["name"] for p in items
               if p["metadata"]["uid"] == baseline["endpoint_uid"] and not p["metadata"].get("deletionTimestamp")]
    if len(matched) != 1:
        raise live.Refused("the original endpoint Pod is no longer available")
    return matched[0]


def request(case, pod, *, activate=False, reset_id=None):
    action, body, expected_status = "GET", "", "200"
    if activate:
        action, expected_status = "POST", "202"
        body = json.dumps({"mode": "endpoint_off", "duration_seconds": case.duration})
    elif reset_id is not None:
        if str(UUID(reset_id)) != reset_id:
            raise live.Refused("reset request lacks a canonical operation id")
        action, body = "RESET", json.dumps({"expected_operation_id": reset_id})
    data = control_response(case, pod, action, body, expected_status)
    keys = ("mode", "operation_id", "accepted_at", "expires_at", "readiness", "reset_operation_id", "reset_status")
    return {key: data[key] for key in keys if key in data}


def control_response(case, pod, action, body, expected_status):
    response = live.command("exec", f"pod/{pod}", "-c", case.application, "--", "sh", "-c", CONTROL,
                            "liveprobe-pulse", action, body)
    document, _, code = response.rstrip().rpartition("\n")
    if action == "RESET" and code in ("400", "409"):
        return {"reset_status": int(code)}
    if code != expected_status:
        raise live.Refused("test-only workload control did not accept the operation")
    data = json.loads(document)
    if not isinstance(data, dict):
        raise live.Refused("invalid workload control reply")
    return data


def state_is_off(state):
    return (state.get("mode"), state.get("readiness"), state.get("operation_id")) == ("off", "ACCEPTING_TRAFFIC", None)


def preflight(case):
    baseline, probes, cursor = service.baseline_for(case)
    pod = pulse_pod(case, baseline)
    if not state_is_off(request(case, pod)):
        raise live.Refused("another fault is active on the dev fixture")
    if case.workload == INVENTORY_FIXTURE:
        baseline["dependency_caller"] = inventory_caller_baseline(case)
    return baseline, pod, probes, cursor


def inventory_caller_baseline(case):
    name = case.dependency_caller
    if case.workload != INVENTORY_FIXTURE or name != INVENTORY_CALLER:
        raise live.Refused("the isolated stock Service has a different caller")
    route = live.get("service", name)
    workload = live.get("deployment", name)
    selector = route["spec"]["selector"]
    app, = workload["spec"]["template"]["spec"]["containers"]
    labels = workload["spec"]["template"]["metadata"]["labels"]
    inventory_url = f"http://{case.workload}:8080"
    if (selector.get("app") != name or not all(labels.get(k) == v for k, v in selector.items())
            or not live.is_settled(workload) or app["name"] != "order"
            or [e for e in app["env"] if e["name"] == "INVENTORY_URL"] != [
                {"name": "INVENTORY_URL", "value": inventory_url},
            ]):
        raise live.Refused("the isolated order caller does not use the intended inventory Service")
    endpoint_uid, = service.endpoint_uids(SimpleNamespace(workload=name), {"service_uid": route["metadata"]["uid"]})
    return {"service_uid": route["metadata"]["uid"], "deployment_uid": workload["metadata"]["uid"],
            "deployment_sha256": live.fixture_digest(workload), "endpoint_uid": endpoint_uid}


def fixture_receipt(case):
    if (case.case_id, case.workload) == ("F3-14", INVENTORY_FIXTURE):
        return SIX_POOL_RECEIPT
    if (case.case_id, case.workload) == ("F3-08", ORDER_FIXTURE):
        return ORDER_POOL_RECEIPT
    return FIXTURE_RECEIPTS.get(case.workload)


def verify_fixture(case, baseline, pod, *, approval=None):
    receipt = fixture_receipt(case)
    if receipt is None:
        return
    if approval is None:
        approval = json.loads(receipt.read_text())
    statuses = [item for item in live.get("pod", pod)["status"]["containerStatuses"]
                if item["name"] == case.application and item.get("ready")]
    if ((approval.get("namespace"), approval.get("workload"), approval.get("ready")) != ("dev", case.workload, True)
            or approval.get("fixture_sha256") != hash_text(json.dumps(baseline, sort_keys=True))
            or len(statuses) != 1 or not statuses[0]["imageID"].endswith(approval["image"]["candidate_id"])):
        raise live.Refused("the isolated fixture baseline or verified image changed")


def upstream_error(case):
    target = case.dependency_caller
    url = f"/api/v1/namespaces/dev/services/{target}:8080/proxy/api/orders/check?item=a"
    result = run([*live.kube_prefix(), "--request-timeout=4s", "get", "--raw", url],
                 capture_output=True, text=True, timeout=5)
    mapping = {"InternalError": 500, "BadGateway": 502, "ServiceUnavailable": 503, "GatewayTimeout": 504}
    for line in result.stderr.splitlines():
        match = re.fullmatch(r"Error from server \(([A-Za-z]+)\): .{1,768}", line)
        if result.returncode and match and match[1] in mapping:
            return mapping[match[1]]
    return None


def primary_error(case):
    error = service.proxy_error(case)
    return error, datetime.now(timezone.utc)


def timed_upstream_error(case):
    started = datetime.now(timezone.utc)
    status = upstream_error(case)
    return status, started, datetime.now(timezone.utc)


def timed_state(case, pod):
    state = request(case, pod)
    return state, datetime.now(timezone.utc)


def observe_fault(case, baseline, pod, operation_id, start, expires):
    while datetime.now(timezone.utc) < expires:
        ready = service.endpoint_uids(case, baseline)
        if ready not in ([], [baseline["endpoint_uid"]]):
            raise live.Refused("EndpointSlice changed to another workload")
        if not ready:
            # The shortest pulse can have less than a second left after readiness changes.
            with ThreadPoolExecutor(max_workers=3) as probes:
                primary = probes.submit(primary_error, case)
                upstream = probes.submit(timed_upstream_error, case)
                control = probes.submit(timed_state, case, pod)
                error, first_503_at = primary.result()
                status, dependency_started, dependency_finished = upstream.result()
                state, control_observed = control.result()
            observed_at = max(first_503_at, control_observed, dependency_started)
            if (status and (state.get("mode"), state.get("operation_id"), state.get("readiness"))
                    == ("endpoint_off", operation_id, "REFUSING_TRAFFIC")
                    and start <= min(first_503_at, control_observed, dependency_started) <= observed_at < expires
                    and 0 <= (dependency_finished - dependency_started).total_seconds() <= DEPENDENCY_PROBE_LIMIT_SECONDS):
                return {"error": error, "ready_endpoints": 0, "http_status": 503, "order_dependency": status,
                        "contract_ok": False, "first_503_at": first_503_at.isoformat(),
                        "observed_at": observed_at.isoformat(), "control_observed_at": control_observed.isoformat(),
                        "dependency_started_at": dependency_started.isoformat(),
                        "dependency_finished_at": dependency_finished.isoformat()}
        sleep(0.3)
    raise live.Refused("original pulse elapsed before concurrent endpoint loss and upstream 5xx were proved")


def restore_by_expiry(case, baseline, pod, expires):
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        sleep(remaining)
    probes = service.wait_for_healthy(case, baseline, monotonic() + 60)
    if not state_is_off(request(case, pod)):
        raise live.Refused("the scoped operation failed to expire")
    if case.workload == INVENTORY_FIXTURE and inventory_caller_baseline(case) != baseline["dependency_caller"]:
        raise live.Refused("the restored requests changed to another dependency caller")
    return probes


def scoped_termination(case, pod, receipt):
    return scoped_events(case, pod, receipt, ("expired", "reset"))


def scoped_events(case, pod, receipt, kinds):
    lines = live.command("logs", f"pod/{pod}", "-c", case.application,
                         "--since-time=" + receipt["accepted_at"], "--limit-bytes=262144").splitlines()
    events = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (isinstance(item, dict) and item.get("operation_id") == receipt["operation_id"]
                and item.get("mode") == receipt.get("mode", "endpoint_off")
                and item.get("chaos_event") in kinds):
            events.append({"event": item["chaos_event"], "timestamp": item.get("@timestamp")})
    return events


def model_request(case, pod, operation, data, baseline, fault, lease):
    operation_id = operation["operation_id"]
    if data["tool"] == "finish":
        return {"finished": True}
    if data["tool"] == "source":
        if service.checked_selector(service.current_service(case), baseline) != baseline["selector"]:
            raise live.Refused("the original Service selector changed during the pulse")
        return {"service_selector": baseline["selector"], "test_control": {
            "configure_key": "fault_mode", "configure_value": "off", "scope": "the same active operation only",
        }}
    if data["tool"] in ("logs", "workload", "probe"):
        observation = {"first_failure": fault, "current_control": request(case, pod),
                       "current_ready_endpoints": len(service.endpoint_uids(case, baseline))}
        if data["tool"] == "logs":
            observation["termination_events"] = scoped_termination(case, pod, operation)
        return observation
    if data["tool"] != "configure" or (data.get("key"), data.get("value")) != ("fault_mode", "off"):
        return {"accepted": False, "reason": "only a reset of the same active test operation is in scope"}
    if request(case, pod).get("operation_id") != operation_id:
        return {"accepted": False, "reason": "the originally observed operation is no longer current"}
    lease.require_active()
    reply = request(case, pod, reset_id=operation_id)
    if (reply.get("mode"), reply.get("reset_operation_id")) != ("off", operation_id):
        return {"accepted": False, "reason": "conditional reset did not confirm the exact operation",
                "http_status": reply.get("reset_status")}
    return {"accepted": True, "action": "scoped_operation_reset", "operation_id": operation_id}


def observe_product(case, cursor, epoch):
    end = monotonic() + OBSERVE_AFTER_RECOVERY
    while monotonic() < end:
        sleep(min(8, end - monotonic()))
    if service.endpoint_poller_epoch() != epoch:
        raise live.Refused("endpoint watcher lost continuity after the short pulse")
    return service.incident_rows(case, cursor)


def execute(case, baseline, pod, probes, cursor, gate, output, journal, *, arm="liveprobe", reference=None):
    result = {"case": case.case_id, "arm": arm, "protocol": PROTOCOL, "target": case.target,
              "case_sha256": case.case_sha256, "fixture_sha256": hash_text(json.dumps(baseline, sort_keys=True)),
              "healthy_before": {"probes": probes, "contract_ok": True}, "node_gate": gate,
              "duration_seconds": case.duration, "status": "trial_unverified"}
    if arm != "liveprobe" and (not reference or reference.get("arm") != "liveprobe"
            or reference.get("status") != "product_pulse_observed"
            or any(result[key] != reference.get(key) for key in ("case", "case_sha256", "fixture_sha256", "protocol"))):
        raise live.Refused("the live product reference does not match this exact dev fixture")
    epoch = service.endpoint_poller_epoch()
    result["endpoint_watcher_epoch"] = epoch
    began = datetime.now(timezone.utc)
    # If the POST response is lost, wait through the maximum bounded TTL rather than issuing an unscoped reset.
    expires = datetime.fromtimestamp(began.timestamp() + case.duration + 8, tz=timezone.utc)
    live.write_once(journal, {"target": case.target, "pod_uid": baseline["endpoint_uid"],
                              "only_fault": "endpoint_off", "maximum_duration_s": case.duration,
                              "started_at": began.isoformat(), "receipt": str(output)})
    lease = None
    observed = None
    try:
        if arm != "liveprobe":
            lease = live.WorkloadLease.claim(case, arm)
            lease.require_active()
        receipt = request(case, pod, activate=True)
        start, expires = receipt_times(case, receipt)
        result["operation"] = receipt
        result["fault_before"] = observe_fault(case, baseline, pod, receipt["operation_id"], start, expires)
        result["first_error_ms"] = round((datetime.fromisoformat(result["fault_before"]["first_503_at"]) - start)
                                         .total_seconds() * 1000)
        result["input_sha256"] = hash_text(TASK + result["fault_before"]["error"])
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(result["fault_before"]["first_503_at"])
        observed = monotonic() - max(0, elapsed.total_seconds())
        if lease:
            if result["input_sha256"] != reference.get("input_sha256"):
                raise live.Refused("first error differs from the live product reference")
            infer(result, arm, baseline, result["fault_before"], lease, observed + MODEL_BUDGET_SECONDS,
                  apply_request=partial(model_request, case, pod, receipt),
                  verify=partial(service.wait_for_healthy, case))
            result["model_evaluation_duration_ms"] = round((monotonic() - observed) * 1000)
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        try:
            wait_until = datetime.now(timezone.utc) if result.get("standalone_action") else expires
            result["restored"] = restore_by_expiry(case, baseline, pod, wait_until)
            result["recovered_observed_at"] = datetime.now(timezone.utc).isoformat()
        except READABLE_ERRORS as error:
            result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    try:
        if "operation" in result and "restored" in result:
            result["termination_events"] = scoped_termination(case, pod, result["operation"])
            termination = bounded_termination(case, result["operation"], result["termination_events"])
            if termination and "fault_before" in result:
                started = datetime.fromisoformat(result["fault_before"]["first_503_at"])
                if termination[1] >= started:
                    result["scoped_termination_kind"] = termination[0]
                    result["first_error_to_operation_end_ms"] = round((termination[1] - started).total_seconds() * 1000)
        if "trial_error" not in result and "restored" in result:
            if lease:
                result["incidents"] = service.incident_rows(case, cursor)
                if result["incidents"]:
                    raise live.Refused("LiveProbe interfered while the standalone lease was held")
                actor = result.get("scoped_termination_kind")
                if actor == "reset" and live.successful_probes(result.get("verified_before_cleanup")):
                    result["status"] = "verified_model_pulse_recovery"
                elif actor == "expired" and not result.get("standalone_action") and not result.get("model_failed"):
                    completion = standalone_completion(result)
                    if completion:
                        result.update(status="verified_natural_pulse_recovery", reason_code=completion)
            else:
                result["incidents"] = observe_product(case, cursor, epoch)
                result["observation_seconds_after_recovery"] = OBSERVE_AFTER_RECOVERY
                result["status"] = "product_pulse_observed"
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        if lease:
            try:
                lease.require_active()
                lease.release()
                result["lease_released"] = True
            except READABLE_ERRORS as error:
                result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    if observed is not None:
        result["wall_duration_ms"] = round((monotonic() - observed) * 1000)
    live.write_once(output, result)
    if "restored" in result and not result.get("cleanup_error"):
        journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=tuple(PULSES))
    isolated = parser.add_mutually_exclusive_group()
    isolated.add_argument("--isolated-order", action="store_true")
    isolated.add_argument("--isolated-inventory", action="store_true")
    parser.add_argument("--arm", default="liveprobe", choices=("liveprobe", *MODELS))
    parser.add_argument("--reference", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--output", type=Path)
    args = parser.parse_args()
    case = load_case(args.case, isolated_order=args.isolated_order, isolated_inventory=args.isolated_inventory)
    if args.arm != "liveprobe" and case.workload == case.application:
        raise live.Refused("standalone pulse repairs require a verified isolated image with conditional reset")
    if args.preflight:
        baseline, pod, probes, _ = preflight(case)
        verify_fixture(case, baseline, pod)
        print(json.dumps({"case": case.case_id, "duration_seconds": case.duration, "healthy": bool(probes),
                          "fixture_sha256": hash_text(json.dumps(baseline, sort_keys=True))}))
        return 0
    journal = live.EVIDENCE / "dev-k8s-fault-active.json"
    reference = json.loads(args.reference.read_text()) if args.reference else None
    with (live.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise live.Refused("a current experiment or saved receipt needs review")
        baseline, pod, probes, cursor = preflight(case)
        verify_fixture(case, baseline, pod)
        gate = live.CHAOS.node_gate()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = execute(case, baseline, pod, probes, cursor, gate, args.output, journal, arm=args.arm, reference=reference)
    print(json.dumps({key: result.get(key) for key in ("case", "status", "trial_error", "cleanup_error")}))
    completed = {"product_pulse_observed", "verified_model_pulse_recovery", "verified_natural_pulse_recovery"}
    return 0 if result["status"] in completed and not result.get("cleanup_error") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False,
                          "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
