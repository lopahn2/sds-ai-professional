"""Run one bounded application delay against the existing isolated dev fixtures."""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
import re
import subprocess
from time import monotonic
from urllib.parse import urlencode

import yaml

from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation import k8s_dev_image_trial as live
from evaluation import k8s_dev_pool_trial as pool
from evaluation import k8s_dev_selector_trial as service
from evaluation.dev_latency_case import (
    ALERT, CASES, DEADLINE_MS, DURATION, PROTOCOL, REQUEST, TIMEOUT, initial_error, load_case, operation_window,
)
from evaluation.dev_endpoint_pulse_case import INVENTORY_FIXTURE, ORDER_FIXTURE
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text, infer
from evaluation.live_repair import TASK
from evaluation.single_baseline import MODELS


RECORDING_RULE = "liveprobe:http_server_requests_latency_seconds:avg_rate1m"
GROUP = "liveprobe-dev-latency-assessment"


def assessment_rules(data):
    """Add disjoint dev fixture selectors to the existing 500ms, one-minute policy."""
    recording = yaml.safe_load(data["recording.yml"])
    alerts = yaml.safe_load(data["alerts.yml"])
    recording_source, = [g for g in recording["groups"] if any(r.get("record") == RECORDING_RULE for r in g["rules"])]
    existing_recording, = [r for r in recording_source["rules"] if r.get("record") == RECORDING_RULE]
    p18, = [g for g in alerts["groups"] if g["name"] == "p18-environments"]
    existing_alert, = [r for r in p18["rules"] if r.get("alert") == ALERT]
    if (not existing_alert["expr"].endswith(" > 0.5") or existing_alert.get("for") != "0s"
            or existing_recording["expr"].count("[1m]") != 2):
        raise ValueError("the operational latency policy differs from the approved assessment policy")
    added = {}
    for key, content, original, expected in (("recording.yml", recording, existing_recording, 2),
                                               ("alerts.yml", alerts, existing_alert, 1)):
        if any(group["name"] == GROUP for group in content["groups"]):
            raise ValueError("the latency assessment group already exists")
        rule = deepcopy(original)
        expression, namespaces = re.subn(r'namespace=~?"[^"]+"', 'namespace="dev"', original["expr"])
        expression, apps = re.subn(r'app=~?"[^"]+"', f'app=~"{INVENTORY_FIXTURE}|{ORDER_FIXTURE}"', expression)
        if namespaces != expected or apps != expected:
            raise ValueError("could not scope the existing latency policy exactly")
        rule["expr"] = expression
        group = {"name": GROUP, "rules": [rule]}
        if key == "recording.yml" and "interval" in recording_source:
            group["interval"] = recording_source["interval"]
        if key == "alerts.yml" and "interval" in p18:
            group["interval"] = p18["interval"]
        content["groups"].append(group)
        added[key] = yaml.safe_dump(content, sort_keys=False)
    return added


def prometheus(path, **params):
    ns = pool.PROMETHEUS_NS
    base = f"/api/v1/namespaces/{ns}/services/prometheus:9090/proxy/api/v1/"
    response = json.loads(live.command("get", "--raw", base + path + "?" + urlencode(params), namespace=ns))
    if response.get("status") != "success":
        raise live.Refused("latency metrics are unavailable")
    return response["data"]


def check_rules():
    groups = [group for group in prometheus("rules")["groups"] if group["name"] == GROUP]
    if (len(groups) != 2 or {rule["name"] for group in groups for rule in group["rules"]} != {RECORDING_RULE, ALERT}
            or any(rule.get("health") != "ok" for group in groups for rule in group["rules"])):
        raise live.Refused("the deployed assessment latency rules are not healthy")


def current_fixture(case):
    # The latest stock and order pool trials recorded the current healthy Pods.
    receipt_case = "F3-14" if case.application == "inventory" else "F3-08"
    return pool.PoolFixture(pool.load_pool_case(receipt_case))


def samples(case, pod, deadline_ms, count):
    script = '''for i in $(seq "$1"); do
  curl -sS --max-time "$2" -o /dev/null -w '%{http_code} %{time_total} ' "$3"
  printf '%s\\n' "$?"
done
'''
    url = "http://127.0.0.1:8080" + REQUEST[case.application]
    args = ["kubectl", "--context", live.CONTEXT, "-n", "dev", "exec", f"pod/{pod}", "-c", case.application,
            "--", "sh", "-c", script, "latency-observer", str(count), f"{deadline_ms / 1000:g}", url]
    response = subprocess.run(args, capture_output=True, text=True, timeout=25)
    if response.returncode:
        raise live.Refused("the timed business probe could not run on the pinned Pod")
    results = response.stdout.splitlines()
    if len(results) != count:
        raise live.Refused("the timed business probe returned an incomplete batch")
    out = []
    for line in results:
        http, elapsed, code = line.split()
        out.append({"http_status": int(http), "elapsed_ms": round(float(elapsed) * 1000, 3),
                    "curl_exit": int(code), "deadline_ms": deadline_ms})
    if count == 1:
        errors = [line for line in response.stderr.splitlines() if TIMEOUT.fullmatch(line)]
        if len(errors) == 1:
            out[0]["stderr"] = errors[0]
    return out


def healthy_deadline(case, pod):
    observed = samples(case, pod, DEADLINE_MS, 2)
    if any((s["http_status"], s["curl_exit"]) != (200, 0) or s["elapsed_ms"] >= DEADLINE_MS for s in observed):
        raise live.Refused("the original deadline was not met by two successful requests")
    return observed


def healthy(case, pod, baseline, deadline):
    probes = service.wait_for_healthy(case, baseline, deadline)
    return {"probes": probes, "timed": healthy_deadline(case, pod)}


def observe_fault(case, pod, baseline, operation, start, end, observations):
    first = samples(case, pod, DEADLINE_MS, 1)[0]
    observations["initial_sample"] = first
    first_at = datetime.now(timezone.utc)
    error = initial_error(case, first)
    slow = samples(case, pod, 3000, 5)
    observations["delay_samples"] = slow
    control = pulse.control_response(case, pod, "GET", "", "200")
    keys = ("mode", "operation_id", "latency_delay_ms", "readiness")
    current = {key: control.get(key) for key in keys}
    endpoints = service.endpoint_uids(case, baseline)
    observed_at = datetime.now(timezone.utc)
    observations.update(control=current, ready_endpoints=endpoints, observed_at=observed_at.isoformat())
    endpoint_uid = baseline["endpoint_uid"]
    if (not start <= first_at <= observed_at < end
            or any(s["curl_exit"] or s["http_status"] != 200 or s["elapsed_ms"] < case.delay_ms for s in slow)
            or (current.get("mode"), current.get("operation_id"), current.get("latency_delay_ms"), current.get("readiness"))
            != ("latency", operation["operation_id"], case.delay_ms, "ACCEPTING_TRAFFIC")
            or endpoints != [endpoint_uid]):
        raise live.Refused("the original ready Pod and delayed traffic were not observed during the hold")
    return {"error": error, "initial_sample": first, "delay_samples": slow, "endpoint_uid": endpoint_uid,
            "first_error_at": first_at.isoformat(), "observed_at": observed_at.isoformat(), "contract_ok": False}


def execute(case, arm, baseline, pod, probes, cursor, gate, output, journal, reference):
    result = {"case": case.case_id, "arm": arm, "protocol": PROTOCOL, "target": case.target,
              "case_sha256": case.case_sha256, "fixture_sha256": hash_text(json.dumps(baseline, sort_keys=True)),
              "healthy_before": {"probes": probes, "timed": healthy_deadline(case, pod), "contract_ok": True}, "node_gate": gate,
              "status": "trial_unverified"}
    if arm != "liveprobe" and (not reference or reference.get("arm") != "liveprobe"
            or any(reference.get(key) != result[key] for key in ("case", "case_sha256", "fixture_sha256", "protocol"))):
        raise live.Refused("the saved product reference is for a different latency case or fixture")
    gate = live.CHAOS.node_gate()
    result["node_gate"] = gate
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=DURATION + 8)
    epoch = service.endpoint_poller_epoch()
    lease = None
    live.write_once(journal, {"target": case.target, "started_at": now.isoformat(), "only_fault": "latency",
                              "maximum_duration_s": DURATION, "receipt": str(output), "pod_uid": baseline["endpoint_uid"]})
    try:
        if arm != "liveprobe":
            lease = live.WorkloadLease.claim(case, arm)
            lease.require_active()
        operation = pulse.control_response(case, pod, "POST", json.dumps({"mode": "latency", "delay_ms": case.delay_ms,
                                                                         "duration_seconds": DURATION}), "202")
        accepted, expires = operation_window(case, operation)
        result["operation"] = operation
        result["observation"] = {}
        fault = observe_fault(case, pod, baseline, operation, accepted, expires, result["observation"])
        result["fault_before"] = fault
        result["input_sha256"] = hash_text(TASK + fault["error"])
        if lease:
            if result["input_sha256"] != reference.get("input_sha256"):
                raise live.Refused("the initial client error differs from the product reference")
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(fault["first_error_at"])).total_seconds()
            started = monotonic() - max(0, elapsed)
            infer(result, arm, baseline, fault, lease, started + pulse.MODEL_BUDGET_SECONDS,
                  apply_request=partial(pulse.model_request, case, pod, operation),
                  verify=partial(healthy, case, pod))
            result["model_evaluation_duration_ms"] = round((monotonic() - started) * 1000)
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        try:
            when = datetime.now(timezone.utc) if result.get("standalone_action") else expires
            result["restored"] = {"probes": pulse.restore_by_expiry(case, baseline, pod, when), "timed": healthy_deadline(case, pod)}
            result["recovered_at"] = datetime.now(timezone.utc).isoformat()
        except READABLE_ERRORS as error:
            result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    try:
        if "fault_before" in result and "recovered_at" in result:
            result["termination_events"] = pulse.scoped_termination(case, pod, operation)
            if lease:
                result["incidents"] = service.incident_rows(case, cursor)
                if result["incidents"]:
                    raise live.Refused("LiveProbe interfered with the standalone latency trial")
            else:
                result["incidents"], result["observation_seconds_after_recovery"], result["product_terminal_observed_at"] = (
                    pool.observe_product(cursor, epoch, case, alert_reason=ALERT))
                check_rules()
                result["latency_rules_checked_after"] = True
                metric = f'{RECORDING_RULE}{{namespace="dev",app="{case.workload}"}}'
                result["recorded_latency"] = [
                    {"timestamp": stamp, "seconds": float(value)}
                    for row in prometheus("query_range", query=metric, start=accepted.isoformat(),
                                          end=datetime.now(timezone.utc).isoformat(), step="15s")["result"]
                    for stamp, value in row["values"] if value not in {"NaN", "+Inf", "-Inf"}
                ]
            if not result.get("trial_error"):
                result["status"] = "latency_observed"
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
    if "recovered_at" in result and not result.get("cleanup_error"):
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
    journal = live.EVIDENCE / "dev-k8s-fault-active.json"
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference:
        from evaluation.comparison_summary import dev_latency_record
        dev_latency_record(reference, "liveprobe")
    with (live.EVIDENCE / "dev-k8s-fault.lock").open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise live.Refused("an active trial or saved result requires review")
        check_rules()
        baseline, pod, probes, cursor = pulse.preflight(case)
        pulse.verify_fixture(current_fixture(case), baseline, pod)
        gate = live.CHAOS.node_gate()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = execute(case, args.arm, baseline, pod, probes, cursor, gate, args.output, journal, reference)
    print(json.dumps({k: result.get(k) for k in ("case", "arm", "status", "trial_error", "cleanup_error")}))
    return 0 if result["status"] == "latency_observed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False,
                          "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
