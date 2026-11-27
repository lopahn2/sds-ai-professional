"""Compare one short crash loop on an existing, approved dev fixture."""
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
from time import monotonic, sleep

from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation import k8s_dev_image_trial as live
from evaluation import k8s_dev_selector_trial as service
from evaluation.dev_restart_case import (
    CASES, PROTOCOL, SIGNALS, container_identity, crash_sample, first_error, load_case, operation_window,
    restart_baseline,
)
from evaluation.k8s_dev_latency_trial import current_fixture
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text, infer
from evaluation.live_repair import TASK
from evaluation.single_baseline import MODELS


def pod_snapshot(case, baseline, pod):
    item = live.get("pod", pod)
    if (item["metadata"]["uid"] != baseline["endpoint_uid"] or item["metadata"].get("deletionTimestamp")
            or item["metadata"]["namespace"] != "dev"):
        raise live.Refused("the pinned dev Pod was replaced")
    statuses = item["status"]["containerStatuses"]
    main, = (s for s in statuses if s["name"] == case.application)
    sidecars = {s["name"]: container_identity(s) for s in statuses
                if s["name"] != case.application and s.get("ready") is True}
    if len(sidecars) + 1 != len(statuses):
        raise live.Refused("a dependency container in the same Pod is unavailable")
    return main, sidecars


def verify_boundary(case, baseline, pod):
    deployment = service.checked_deployment(case, baseline)["spec"]["template"]["spec"]
    main, = (c for c in deployment["containers"] if c["name"] == case.application)
    volumes = {v["name"]: v for v in deployment.get("volumes", [])}
    state = volumes.get("chaos-state", {}).get("emptyDir", {})
    mounts = [m for m in main.get("volumeMounts", []) if m["name"] == "chaos-state"]
    if (deployment.get("restartPolicy") != "Always" or state.get("sizeLimit") != "2Mi"
            or any(set(v) != {"name", "emptyDir"} for v in volumes.values())
            or deployment.get("automountServiceAccountToken") is not False
            or any(deployment.get(flag) for flag in ("hostNetwork", "hostPID", "hostIPC"))
            or mounts != [{"name": "chaos-state", "mountPath": "/var/run/liveprobe-chaos"}]):
        raise live.Refused("the fixture is not confined to approved ephemeral crash state")
    # The fixture image provides these runtime settings. Test only the named boundary without printing environment values.
    check = """set -eu
case ",${SPRING_PROFILES_ACTIVE:-}," in *,chaos,*) ;; *) exit 1;; esac
test "${CHAOS_STATE_DIR:-}" = /var/run/liveprobe-chaos
test -d "$CHAOS_STATE_DIR" && test -w "$CHAOS_STATE_DIR"
"""
    live.command("exec", f"pod/{pod}", "-c", case.application, "--", "sh", "-c", check)


def watcher_epochs():
    endpoint = service.endpoint_poller_epoch()
    info = live.product_status("/api/status")["watchers"]
    epochs = {"endpoint_poller": endpoint}
    for name in ("pod_watch", "event_watch"):
        watch = info.get(name + "/dev") or info.get(name) or {}
        if watch.get("state") != "running" or not watch.get("since"):
            raise live.Refused("dev Pod observation is not continuously available")
        epochs[name] = watch["since"]
    return epochs


def observe(case, baseline, pod, before, sidecars, start, expiry, samples):
    deadline = monotonic() + 35
    original_id, original_restarts = container_identity(before)
    while monotonic() < deadline:
        status, current_sidecars = pod_snapshot(case, baseline, pod)
        if current_sidecars != sidecars:
            raise live.Refused("an unrelated dependency container restarted")
        sample = crash_sample(status, before, start, expiry)
        observed_at = datetime.now(timezone.utc).isoformat()
        if sample["terminations"] or sample["waiting"] == "CrashLoopBackOff":
            samples.append({**sample, "observed_at": observed_at})
        primary = next((e for s in samples for e in s["terminations"] if e["container_id"] == original_id), None)
        repeats = {e["container_id"] for s in samples for e in s["terminations"]}
        if (primary and len(repeats) >= 2 and
                (sample["waiting"] == "CrashLoopBackOff" or sample["restarts"] >= original_restarts + 2)
                and not service.endpoint_uids(case, baseline)):
            try:
                unavailable = service.proxy_error(case)
            except live.Refused:
                unavailable = None
            if unavailable:
                return {"error": first_error(primary), "first_termination": primary, "first_error_at": primary["finished_at"],
                        "endpoint_uid": baseline["endpoint_uid"], "contract_ok": False, "ready_endpoints": 0,
                        "http_status": 503, "service_error": unavailable, "unavailable_at": datetime.now(timezone.utc).isoformat()}
        sleep(0.7)
    raise live.Refused("no repeated crash from the original native exit was observed within the approved TTL")


def healthy(case, pod, initial_sidecars, baseline, deadline):
    probes = service.wait_for_healthy(case, baseline, deadline)
    status, sidecars = pod_snapshot(case, baseline, pod)
    if sidecars != initial_sidecars or status.get("ready") is not True or not pulse.state_is_off(pulse.request(case, pod)):
        raise live.Refused("the pinned Pod did not return to healthy operation")
    if case.dependency_caller != case.workload and pulse.inventory_caller_baseline(case) != baseline["dependency_caller"]:
        raise live.Refused("the original test caller changed")
    container, restarts = container_identity(status)
    return {"probes": probes, "container_id": container, "restarts": restarts}


def model_request(case, pod, operation, initial_sidecars, request, baseline, fault, lease):
    tool = request["tool"]
    if tool == "finish":
        return {"finished": True}
    status, sidecars = pod_snapshot(case, baseline, pod)
    if sidecars != initial_sidecars:
        raise live.Refused("an unrelated dependency container restarted")
    if tool == "source":
        return {"restart_policy": "Always", "test_control": {"configure_key": "fault_mode", "configure_value": "off",
                                                               "scope": "the same active operation only"}}
    if tool in ("workload", "logs", "probe") and status.get("ready") is not True:
        return {"first_error": fault["error"], "restart_count": status["restartCount"],
                "waiting": ((status.get("state") or {}).get("waiting") or {}).get("reason"),
                "ready": False, "ready_endpoints": len(service.endpoint_uids(case, baseline))}
    if tool == "configure" and status.get("ready") is not True:
        return {"accepted": False, "reason": "the original container's scoped API is unavailable during restart"}
    return pulse.model_request(case, pod, operation, request, baseline, fault, lease)


def product_outcome(case, cursor, epochs):
    began = monotonic()
    terminal_at = None
    while True:
        rows = service.incident_rows(case, cursor)
        if terminal_at is None and any(SIGNALS.intersection(row.get("signal_reasons") or [])
                                       and row["status"] not in live.NONTERMINAL for row in rows):
            terminal_at = datetime.now(timezone.utc).isoformat()
        elapsed = monotonic() - began
        if elapsed >= 180 or (elapsed >= 75 and all(r["status"] not in live.NONTERMINAL for r in rows)):
            break
        sleep(5)
    if watcher_epochs() != epochs:
        raise live.Refused("dev Pod signal collection restarted during observation")
    return rows, terminal_at, round(monotonic() - began)


def execute(case, arm, baseline, pod, probes, cursor, output, journal, reference):
    digest = hash_text(json.dumps(baseline, sort_keys=True))
    if reference and (reference["case"], reference["case_sha256"], reference["fixture_sha256"], reference["protocol"]) != (
            case.case_id, case.case_sha256, digest, PROTOCOL):
        raise live.Refused("the product receipt belongs to a different crash fixture")
    verify_boundary(case, baseline, pod)
    before, sidecars = pod_snapshot(case, baseline, pod)
    original, previous = container_identity(before)
    running_since, eligible_at = restart_baseline(before)
    if datetime.now(timezone.utc) < eligible_at:
        raise live.Refused(f"the pinned container needs a reset restart backoff before injection: {eligible_at.isoformat()}")
    epochs = watcher_epochs()
    gate = live.CHAOS.node_gate()
    result = {"status": "trial_unverified", "case": case.case_id, "arm": arm, "target": case.target,
              "protocol": PROTOCOL, "case_sha256": case.case_sha256, "fixture_sha256": digest, "node_gate": gate,
              "healthy_before": {"probes": probes, "container_id": original, "restarts": previous,
                                 "running_since": running_since.isoformat(), "restart_backoff_eligible_at": eligible_at.isoformat()},
              "watcher_epochs": epochs, "observations": []}
    journal_start = datetime.now(timezone.utc)
    expiry = journal_start + timedelta(seconds=case.duration + 20)
    lease = None
    attempted = False
    live.write_once(journal, {"target": case.target, "started_at": journal_start.isoformat(),
                              "only_fault": "crash", "maximum_duration_s": case.duration,
                              "receipt": str(output), "pod_uid": baseline["endpoint_uid"]})
    try:
        if reference:
            lease = live.WorkloadLease.claim(case, arm)
            lease.require_active()
        attempted = True
        operation = pulse.control_response(case, pod, "POST", json.dumps({"mode": "crash",
                                                                          "duration_seconds": case.duration}), "202")
        start, expiry = operation_window(case, operation)
        result["operation"] = operation
        fault = observe(case, baseline, pod, before, sidecars, start, expiry, result["observations"])
        result["fault_before"] = fault
        result["input_sha256"] = hash_text(TASK + fault["error"])
        if lease:
            if result["input_sha256"] != reference["input_sha256"]:
                raise live.Refused("initial termination differs from the product arm")
            first = datetime.fromisoformat(fault["first_error_at"])
            started = monotonic() - (datetime.now(timezone.utc) - first).total_seconds()
            infer(result, arm, baseline, fault, lease, started + pulse.MODEL_BUDGET_SECONDS,
                  apply_request=partial(model_request, case, pod, operation, sidecars),
                  verify=partial(healthy, case, pod, sidecars))
            result["model_evaluation_duration_ms"] = round((monotonic() - started) * 1000)
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        try:
            if attempted and not result.get("standalone_action"):
                sleep(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()))
            result["restored"] = healthy(case, pod, sidecars, baseline, monotonic() + 100)
            result["recovered_at"] = datetime.now(timezone.utc).isoformat()
        except READABLE_ERRORS as error:
            result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    try:
        if "fault_before" in result and "recovered_at" in result:
            result["termination_events"] = pulse.scoped_termination(case, pod, operation)
            if lease:
                result["incidents"] = service.incident_rows(case, cursor)
                if result["incidents"]:
                    raise live.Refused("LiveProbe handled the standalone trial")
            else:
                result["incidents"], result["product_terminal_observed_at"], result["observation_seconds"] = (
                    product_outcome(case, cursor, epochs))
            if not result.get("trial_error"):
                result["status"] = "crash_observed"
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
    live.write_once(output, result)
    if not result.get("cleanup_error") and (not attempted or ("operation" in result and "recovered_at" in result)):
        journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--arm", default="liveprobe", choices=("liveprobe", *MODELS))
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.arm == "liveprobe") != (args.reference is None):
        parser.error("standalone arms require the actual product receipt")
    case = load_case(args.case)
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference:
        from evaluation.comparison_summary import dev_crash_record
        if dev_crash_record(reference, "liveprobe")["status"] != "verified_natural_recovery":
            raise live.Refused("the product crash arm has no terminal receipt")
    journal = live.EVIDENCE / "dev-k8s-fault-active.json"
    with (live.EVIDENCE / "dev-k8s-fault.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise live.Refused("an active trial or saved crash attempt requires review")
        baseline, pod, probes, cursor = pulse.preflight(case)
        pulse.verify_fixture(current_fixture(case), baseline, pod)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = execute(case, args.arm, baseline, pod, probes, cursor, args.output, journal, reference)
    print(json.dumps({k: result.get(k) for k in ("case", "arm", "status", "trial_error", "cleanup_error")}))
    return 0 if result["status"] == "crash_observed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False,
                          "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
