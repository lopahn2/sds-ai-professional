"""Compare an approved native OOM on an already isolated dev target."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import fcntl
from functools import partial
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from time import monotonic, sleep

from scripts import family2_live as family2
from evaluation import isolated_oom_target
from evaluation import k8s_dev_crash_trial as restart
from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation import k8s_dev_image_trial as live
from evaluation import k8s_dev_selector_trial as service
from evaluation.dev_oom_case import (
    ALERT, ALERT_FRESHNESS_SECONDS, CASES, KSM, KSM_TERMINATED_AT, LIMIT_MIB, MIB, PROTOCOL, RECOVERY_OBSERVER,
    expiry_abort, first_error, load_case, oom_sample, operation_window,
)
from evaluation.dev_restart_case import container_identity, restart_baseline
from evaluation.k8s_dev_latency_trial import current_fixture, prometheus
from evaluation.k8s_dev_replica_trial import PROTOCOL as MODEL_PROTOCOL, READABLE_ERRORS, hash_text, infer
from evaluation.live_repair import RemoteModelSession, TASK
from evaluation.single_baseline import MODELS


HANDLED = (*READABLE_ERRORS, family2.tx.UnsafeState, family2.gate.GateError, live.CHAOS.Stop)


def authority():
    target = isolated_oom_target.active()
    if not target:
        return family2.DevFaultAuthority(live.CONTEXT)
    return family2.DevFaultAuthority(isolated_oom_target.CONTEXT, kubeconfig=target.kubeconfig,
                                    jobs=partial(family2.active_jobs, run=target.command), check_scope=target.check_scope)


def cgroup_boundary(case, pod, sidecars):
    values = {}
    for name in (case.application, *sidecars):
        output = live.command("exec", f"pod/{pod}", "-c", name, "--", "cat",
                              "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current")
        limit, current = map(int, output.split())
        expected = LIMIT_MIB if name == case.application else 64
        if limit != expected * MIB or not 0 < current <= limit:
            raise live.Refused("container memory is not confined to the approved cgroup")
        values[name] = {"limit_bytes": limit, "current_bytes": current}
    return values


def metric(query):
    result = prometheus("query", query=query)
    if result["resultType"] != "vector":
        raise live.Refused("Prometheus did not return a current instant vector")
    return result["result"]


def fresh_metric(query, pod, container, *, since=None):
    series = [row for row in metric(query)
              if all(row["metric"].get(k) == v for k, v in {"namespace": "dev", "pod": pod, "container": container}.items())]
    if len(series) > 1:
        raise live.Refused("more than one current watcher sample matched the exact container")
    if not series:
        return None
    evaluated, value = map(float, series[0]["value"])
    if value <= 0 or (since is not None and not since.timestamp() <= value <= evaluated):
        return None
    return {"evaluated_at": evaluated, "sample_timestamp": value} if since is not None else value


def metric_queries(pod, container, uid):
    # Build selectors from the verified container and pinned Pod identity.
    labels = f'namespace="dev",pod="{pod}",container="{container}"'
    return (f'{KSM}{{{labels},uid="{uid}",reason="OOMKilled"}}',
            f'ALERTS{{{labels},alertname="{ALERT}",alertstate="firing"}}')


def signal_preflight(case, pod, baseline, *, standalone):
    selector = '{namespace=~"dev|prd"}'
    reason = KSM + '{namespace=~"dev|prd",reason="OOMKilled"}'
    age = "time() - " + KSM_TERMINATED_AT + selector
    fresh_rule = (f"max by (namespace,pod,container) (({reason} > 0)"
                  f" and on (namespace,pod,uid,container) ({age} >= 0 and {age} < {ALERT_FRESHNESS_SECONDS}))")
    legacy_rule = f"max by (namespace,pod,container) ({reason}) > 0"
    rules = [rule for g in prometheus("rules")["groups"] if g["name"] == "p18-environments"
             for rule in g["rules"] if rule["name"] == ALERT]
    supported = (fresh_rule,) if standalone else (fresh_rule, legacy_rule)
    if (len(rules) != 1 or rules[0]["health"] != "ok" or rules[0]["duration"] != 0
            or "".join(rules[0]["query"].split()) not in {"".join(query.split()) for query in supported}
            or not any(float(item["value"][1]) == 1 for item in metric('up{job=~".*kube-state.*"}'))):
        raise live.Refused("the current dev OOM alert and kube-state-metrics path are not ready")
    for name, namespace in (("prometheus", "liveprobe-observability"), ("alertmanager", "liveprobe-observability"),
                            ("liveprobe-api", "liveprobe-system")):
        if not live.is_settled(live.get("deployment", name, namespace=namespace)):
            raise live.Refused("the OOM monitoring or incident receiver deployment is not ready")
    service.checked_deployment(case, baseline)
    rule = rules[0]
    scope = {"namespace": "dev", "pod": pod, "container": case.application}
    if any(all(alert["labels"].get(key) == value for key, value in scope.items()) for alert in rule["alerts"]):
        raise live.Refused("the pinned Pod already has a firing OOM alert")
    if KSM_TERMINATED_AT not in rule["query"]:
        reason_query, _ = metric_queries(pod, case.application, baseline["endpoint_uid"])
        if fresh_metric(reason_query, pod, case.application):
            raise live.Refused("the legacy rule cannot distinguish the pinned Pod's previous OOM")
    return rule["query"]


def preflight(case, authority, *, standalone=False):
    gate = live.CHAOS.node_gate()
    authority.check()
    baseline, pod, probes, cursor = pulse.preflight(case)
    target = isolated_oom_target.active()
    approval = {"approval": target.approval(case, baseline, pod)} if target else {}
    pulse.verify_fixture(current_fixture(case), baseline, pod, **approval)
    restart.verify_boundary(case, baseline, pod)
    before, peers = restart.pod_snapshot(case, baseline, pod)
    last = ((before.get("lastState") or {}).get("terminated") or {})
    if last.get("reason") == "OOMKilled":
        ended = datetime.fromisoformat(last["finishedAt"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ended).total_seconds()
        labels = f'namespace="dev",pod="{pod}",container="{case.application}",uid="{baseline["endpoint_uid"]}"'
        known = fresh_metric(f"{KSM_TERMINATED_AT}{{{labels}}}", pod, case.application)
        if known != ended.timestamp() or age < ALERT_FRESHNESS_SECONDS:
            raise live.Refused("the previous native OOM has not aged out of the verified alert window")
    running, eligible = restart_baseline(before)
    if datetime.now(timezone.utc) < eligible:
        raise live.Refused(f"the pinned container is cooling down until {eligible.isoformat()}")
    cgroups = cgroup_boundary(case, pod, peers)
    rule = signal_preflight(case, pod, baseline, standalone=standalone)
    return baseline, pod, probes, cursor, before, peers, {
        "node_gate": gate, "cgroups": cgroups, "rule_expression": rule,
        "running_since": running.isoformat(), "restart_backoff_eligible_at": eligible.isoformat(),
        "watcher_epochs": restart.watcher_epochs(),
    }


def observe(case, baseline, pod, before, peers, start, expiry, samples):
    deadline = monotonic() + 35
    original, _ = container_identity(before)
    while monotonic() < deadline:
        status, current_peers = restart.pod_snapshot(case, baseline, pod)
        if current_peers != peers:
            raise live.Refused("an unrelated dependency container restarted")
        sample = oom_sample(status, before, start, expiry)
        if sample["terminations"]:
            samples.append({**sample, "observed_at": datetime.now(timezone.utc).isoformat()})
        native = next((row for observation in samples for row in observation["terminations"]
                       if row["container_id"] == original), None)
        if native and not service.endpoint_uids(case, baseline):
            try:
                unavailable = service.proxy_error(case)
            except live.Refused:
                unavailable = None
            if unavailable:
                return {"error": first_error(native), "first_termination": native, "first_error_at": native["finished_at"],
                        "endpoint_uid": baseline["endpoint_uid"], "contract_ok": False, "ready_endpoints": 0,
                        "http_status": 503, "service_error": unavailable, "unavailable_at": datetime.now(timezone.utc).isoformat()}
        sleep(.7)
    raise live.Refused("the original app container did not show a native OOM and observed service outage")


def model_request(case, pod, operation, peers, request, baseline, fault, lease):
    if request["tool"] == "source":
        spec = service.checked_deployment(case, baseline)["spec"]["template"]["spec"]
        app, = (container for container in spec["containers"] if container["name"] == case.application)
        return {"restart_policy": spec["restartPolicy"], "memory_limit": app["resources"]["limits"]["memory"],
                "test_control": {"configure_key": "fault_mode", "configure_value": "off",
                                 "scope": "the same active operation only"}}
    return restart.model_request(case, pod, operation, peers, request, baseline, fault, lease)


def product_outcome(case, pod, uid, cursor, start, epochs):
    began = monotonic()
    terminal_at = None
    signals = {}
    requests = metric_queries(pod, case.application, uid)
    while monotonic() - began < 180:
        for name, query in zip(("kube_state_oom", "firing_oom_alert"), requests):
            if name not in signals and (sample := fresh_metric(f"timestamp({query} > 0)", pod, case.application, since=start)):
                signals[name] = sample
        rows = service.incident_rows(case, cursor)
        for row in rows:
            if terminal_at is None and ALERT in (row.get("signal_reasons") or []) and row["status"] not in live.NONTERMINAL:
                terminal_at = datetime.now(timezone.utc).isoformat()
        if monotonic() - began >= 75 and len(signals) == 2 and terminal_at is not None:
            break
        sleep(5)
    if restart.watcher_epochs() != epochs:
        raise live.Refused("dev Pod signal collection restarted during OOM observation")
    return rows, signals, terminal_at, round(monotonic() - began)


def await_standalone_alert_settle(case, pod, uid, operation, lease):
    _, alert = metric_queries(pod, case.application, uid)
    # Retain the scoped lease through Prometheus freshness, rule evaluation, and Alertmanager dispatch.
    quiet_after = datetime.fromisoformat(operation["expires_at"]) + timedelta(seconds=ALERT_FRESHNESS_SECONDS + 60)
    deadline = quiet_after + timedelta(seconds=30)
    while datetime.now(timezone.utc) < deadline:
        lease.require_active()
        if datetime.now(timezone.utc) >= quiet_after and not fresh_metric(alert, pod, case.application):
            return datetime.now(timezone.utc).isoformat()
        sleep(8)
    raise live.Refused("standalone OOM alerts have not settled under the leased workload")


def private_write(path, value):
    # Publish only a complete receipt and preserve any previously recorded attempt.
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", mode="w", encoding="utf-8") as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
        os.link(out.name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def check_isolated_model(target, arm, evidence):
    model, effort, _ = MODELS["single_luna" if arm == "liveprobe" else arm]
    path = evidence / f"model-ready-{model}-{effort}.json"
    if path.is_file():
        saved = json.loads(path.read_text())
        target.verify_receipt(saved)
        if (saved.get("model"), saved.get("effort")) != (model, effort):
            raise live.Refused("the isolated model readiness receipt records another model")
        return
    command = target.model_command(model, effort, MODEL_PROTOCOL)
    session = RemoteModelSession(model, effort, MODEL_PROTOCOL, command=command, timeout=50)
    try:
        reply = session.turn(model, effort, "Isolated evaluation transport check. No incident or action. Finish.", [])
    finally:
        session.close()
    if (reply.get("status"), reply.get("model"), reply.get("effort")) != ("ok", model, effort):
        raise live.Refused("the isolated receiver did not complete a model round trip before the fault")
    private_write(path, {"isolated_target": target.scope(), "model": model, "effort": effort,
                         "observed_at": datetime.now(timezone.utc).isoformat()})


def ready_to_write(case, baseline, pod, original, previous, peers, authority, eval_lease, wait_s):
    started = monotonic()
    deadline = started + wait_s
    while True:
        try:
            if wait_s:
                live.CHAOS.node_gate()
            authority.check(owned=True)
            if eval_lease:
                eval_lease.require_active()
            status, now_peers = restart.pod_snapshot(case, baseline, pod)
            if (container_identity(status) != (original, previous) or now_peers != peers or status.get("ready") is not True
                    or not pulse.state_is_off(pulse.request(case, pod))):
                raise live.Refused("the OOM target changed immediately before the request")
            service.checked_deployment(case, baseline)
            gate = live.CHAOS.node_gate()
            authority.lease.assert_owned()
            return gate, round((monotonic() - started) * 1000)
        except live.CHAOS.NodeGateStop as error:
            remaining = deadline - monotonic()
            if remaining <= 0 or set(error.breaches) - {"cpu", "memory", "memory_headroom"}:
                raise
            sleep(min(8, remaining))


def verify_operation_end(case, pod, result, start, expiry):
    operation = result["operation"]
    recovered = datetime.fromisoformat(result["recovered_at"])
    logs = pulse.scoped_events(case, pod, operation, ("expired", "reset", "oom_alloc_start", "oom_not_killed"))
    result["termination_events"] = events = [row for row in logs if row["event"] in ("expired", "reset")]
    if events:
        event, = events
        ended_at = datetime.fromisoformat(event["timestamp"])
        if (ended_at <= recovered and (event["event"] == "expired" and ended_at >= expiry or
                event["event"] == "reset" and start <= ended_at < expiry and result.get("standalone_action"))):
            return
    elif result.get("fault_before") and not result.get("standalone_action"):
        aborts = [row for row in logs if row["event"] == "oom_not_killed"]
        if len(aborts) == 1:
            readback = {
                "allocations": [row["timestamp"] for row in logs if row["event"] == "oom_alloc_start"],
                "intent": json.loads(live.command("exec", f"pod/{pod}", "-c", case.application, "--", "cat",
                                                  "/var/run/liveprobe-chaos/intent.json")),
                "control_off": pulse.state_is_off(pulse.request(case, pod)),
            }
            first_oom_at = datetime.fromisoformat(result["fault_before"]["first_error_at"])
            if (expiry_abort(case, operation, first_oom_at, aborts[0], readback)
                    and datetime.fromisoformat(aborts[0]["timestamp"]) <= recovered):
                result.update(termination_events=aborts, abort_readback=readback)
                return
    raise live.Refused("the OOM operation did not prove exact clearing")


def observe_independent_recovery(case, pod, peers, baseline, stop, armed_at):
    """Read the pinned workload without waiting for the model or the incident to finish."""
    deadline = monotonic() + 110
    while not stop.is_set() and monotonic() < deadline:
        probe_started = datetime.now(timezone.utc)
        try:
            checks = restart.healthy(case, pod, peers, baseline, min(deadline, monotonic() + .1))
        except READABLE_ERRORS:
            stop.wait(2)
            continue
        return {'observer': RECOVERY_OBSERVER, 'armed_at': armed_at.isoformat(),
                'probe_started_at': probe_started.isoformat(), 'verified_at': datetime.now(timezone.utc).isoformat(),
                'checks': checks}
    return None


def record_independent_recovery(result, future):
    try:
        reading = future.result()
    except Exception as error:
        result['independent_recovery_error'] = type(error).__name__
        return
    restored = result.get('restored')
    events = result.get('termination_events') or []
    if reading is None or not restored or len(events) != 1:
        return
    stamp = datetime.fromisoformat(reading['verified_at'])
    ended = datetime.fromisoformat(events[0]['timestamp'])
    last = datetime.fromisoformat(result['recovered_at'])
    if (ended <= stamp <= last
            and all(reading['checks'][key] == restored[key] for key in ('container_id', 'restarts'))):
        result['independent_recovery'] = reading


def execute(case, arm, authority, state, output, journal, reference, *, node_settle_s=0):
    baseline, pod, probes, cursor, before, peers, gates = state
    digest = hash_text(json.dumps(baseline, sort_keys=True))
    if reference and (reference["case"], reference["case_sha256"], reference["fixture_sha256"], reference["protocol"]) != (
            case.case_id, case.case_sha256, digest, PROTOCOL):
        raise live.Refused("the product receipt belongs to a different OOM fixture")
    original, previous = container_identity(before)
    result = {"status": "trial_unverified", "case": case.case_id, "arm": arm, "target": case.target, "protocol": PROTOCOL,
              "case_sha256": case.case_sha256, "fixture_sha256": digest, "gate_evidence": gates, "observations": [],
              "healthy_before": {"probes": probes, "container_id": original, "restarts": previous, "contract_ok": True}}
    target = isolated_oom_target.active()
    if target:
        result["isolated_target"] = target.scope()
    begin = datetime.now(timezone.utc)
    eval_lease = None
    attempted = False
    available = False
    journal_written = False
    collector = recovery = None
    stop = Event()
    authority.lease.acquire()
    try:
        private_write(journal, {"target": case.target, "started_at": begin.isoformat(), "pod_uid": baseline["endpoint_uid"],
                                "fixture_sha256": digest, "maximum_duration_s": case.duration, "only_fault": "oom",
                                "container_id": original, "baseline_restarts": previous,
                                "lease_uid": authority.lease.uid, "raw_receipt": str(output)})
        journal_written = True
        if reference:
            eval_lease = live.WorkloadLease.claim(case, arm)
        result["node_gate"], result["node_gate_wait_ms"] = ready_to_write(
            case, baseline, pod, original, previous, peers, authority, eval_lease, node_settle_s)
        result["request_started_at"] = datetime.now(timezone.utc).isoformat()
        attempted = True
        operation = pulse.control_response(case, pod, "POST", json.dumps({"mode": "oom", "duration_seconds": case.duration}), "202")
        start, expiry = operation_window(case, operation)
        result["operation"] = operation
        fault = observe(case, baseline, pod, before, peers, start, expiry, result["observations"])
        result["fault_before"] = fault
        result["input_sha256"] = hash_text(TASK + fault["error"])
        collector = ThreadPoolExecutor(max_workers=1, thread_name_prefix='oom-independent-recovery')
        recovery = collector.submit(observe_independent_recovery, case, pod, peers, baseline,
                                    stop, datetime.now(timezone.utc))
        if eval_lease:
            if result["input_sha256"] != reference["input_sha256"]:
                raise live.Refused("initial native termination differs from the product arm")
            first = datetime.fromisoformat(fault["first_error_at"])
            model_started_at = datetime.now(timezone.utc)
            result['model_evaluation_started_at'] = model_started_at.isoformat()
            model_start = monotonic() - (model_started_at - first).total_seconds()
            infer(result, arm, baseline, fault, eval_lease, model_start + pulse.MODEL_BUDGET_SECONDS,
                  apply_request=partial(model_request, case, pod, operation, peers),
                  verify=partial(restart.healthy, case, pod, peers))
            result["model_evaluation_duration_ms"] = round((monotonic() - model_start) * 1000)
    except HANDLED as error:
        result["trial_error"] = str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__
        if isinstance(error, live.CHAOS.NodeGateStop):
            result["node_gate_rejection"] = {"node": error.snapshot, "breaches": error.breaches}
    finally:
        try:
            if attempted:
                if "operation" not in result:
                    # A lost reply can leave a native fault armed even while the control says off.
                    raise live.Refused("OOM request acceptance could not be verified")
                if not result.get("standalone_action"):
                    sleep(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()))
                result["restored"] = restart.healthy(case, pod, peers, baseline, monotonic() + 110)
                result["recovered_at"] = datetime.now(timezone.utc).isoformat()
                verify_operation_end(case, pod, result, start, expiry)
            available = True
        except HANDLED as error:
            result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
        finally:
            stop.set()
            if collector is not None:
                collector.shutdown(wait=True)
                if recovery is not None:
                    record_independent_recovery(result, recovery)
    try:
        if available and eval_lease and attempted:
            result["standalone_alert_settled_at"] = await_standalone_alert_settle(
                case, pod, baseline["endpoint_uid"], result["operation"], eval_lease)
            result["incidents"] = service.incident_rows(case, cursor)
            if result["incidents"]:
                raise live.Refused("LiveProbe handled the standalone OOM trial")
        if available and "fault_before" in result:
            if eval_lease is None:
                rows, samples, terminal_at, seconds = product_outcome(
                    case, pod, baseline["endpoint_uid"], cursor, start, gates["watcher_epochs"])
                result.update(incidents=rows, signal_samples=samples, product_terminal_observed_at=terminal_at,
                              observation_seconds=seconds)
            if not result.get("trial_error"):
                result["status"] = "oom_observed"
    except HANDLED as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        alerts_settled = not (eval_lease and attempted) or "standalone_alert_settled_at" in result
        if available and alerts_settled:
            try:
                if eval_lease:
                    eval_lease.release()
                    result["lease_released"] = True
                authority.lease.release()
                result["kubernetes_lease_released"] = True
            except HANDLED as error:
                result["cleanup_error"] = type(error).__name__
        private_write(output, result)
        if result.get("kubernetes_lease_released") and journal_written:
            journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--arm", default="liveprobe", choices=("liveprobe", *MODELS))
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--node-settle-seconds", type=int, default=0)
    isolated_oom_target.arguments(parser)
    args = parser.parse_args()
    if (args.arm == "liveprobe") != (args.reference is None):
        parser.error("standalone arms require the actual product receipt")
    if not 0 <= args.node_settle_seconds <= 75:
        parser.error("node settle wait must be between 0 and 75 seconds")
    case = load_case(args.case)
    if args.isolated_kubeconfig and args.case not in isolated_oom_target.CASES:
        parser.error("isolated mode is scoped to the four pending OOM cases")
    target = isolated_oom_target.bind(args)
    if target:
        target.require_case(args.case)
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference:
        if target:
            target.verify_receipt(reference)
        from evaluation.comparison_summary import dev_oom_record
        if dev_oom_record(reference, "liveprobe")["status"] != "verified_natural_recovery":
            raise live.Refused("the product OOM arm has no terminal receipt")
    evidence = target.evidence(live.EVIDENCE) if target else live.EVIDENCE
    journal = evidence / "dev-k8s-fault-active.json"
    active_authority = authority()
    if not args.execute:
        baseline, pod, probes, _, _, _, gates = preflight(case, active_authority, standalone=reference is not None)
        report = {"status": "dry_run", "case": case.case_id, "target": case.target, "pod_uid": baseline["endpoint_uid"],
                  "healthy_before": probes, "gate_evidence": gates, "writes": 0}
        private_write(args.output, report)
        print(json.dumps(report))
        return 0
    if target:
        evidence.mkdir(mode=0o700, exist_ok=True)
    with family2.gate.local_lease(), (evidence / "dev-k8s-fault.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise live.Refused("an active trial or saved OOM receipt requires review")
        state = preflight(case, active_authority, standalone=reference is not None)
        if target:
            check_isolated_model(target, args.arm, evidence)
        result = execute(case, args.arm, active_authority, state, args.output, journal, reference,
                         node_settle_s=args.node_settle_seconds)
    print(json.dumps({k: result.get(k) for k in ("case", "arm", "status", "trial_error", "cleanup_error", "model_failed")}))
    return 0 if result["status"] == "oom_observed" and result.get("kubernetes_lease_released") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HANDLED as error:
        print(json.dumps({"eligible": False, "reason": str(error) if isinstance(
            error, (live.Refused, live.CHAOS.Stop, family2.tx.UnsafeState, family2.gate.GateError,
                    isolated_oom_target.TargetError)) else type(error).__name__}))
        raise SystemExit(2) from None
