import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from evaluation.comparison_summary import dev_latency_record, repair_rows
from evaluation.dev_latency_case import CASES, PROTOCOL, ROOT, initial_error, load_case
from evaluation import k8s_dev_latency_trial as trial
from evaluation.k8s_dev_latency_trial import GROUP, assessment_rules
from evaluation.live_repair import TASK


def product_receipt():
    case = load_case("F3-11")
    initial = {"deadline_ms": 400, "http_status": 0, "curl_exit": 28, "elapsed_ms": 400.123,
               "stderr": "curl: (28) Operation timed out after 400 milliseconds with 0 bytes received"}
    timed = {"deadline_ms": 400, "http_status": 200, "curl_exit": 0, "elapsed_ms": 10.0}
    probes = {"ready": 200, "business": 200, "contract_ok": True, "endpoint_uid": "dev-test"}
    good = {"probes": [probes] * 2, "timed": [timed] * 2, "contract_ok": True}
    error = initial_error(case, initial)
    return {"case": case.case_id, "case_sha256": case.case_sha256, "arm": "liveprobe", "protocol": PROTOCOL,
            "target": case.target, "status": "latency_observed", "fixture_sha256": "a" * 64,
            "input_sha256": hashlib.sha256((TASK + error).encode()).hexdigest(),
            "operation": {"mode": "latency", "delay_ms": 1100, "accepted_at": "2026-11-24T01:00:00Z",
                          "expires_at": "2026-11-24T01:00:30Z", "operation_id": "17a69ee9-d49b-4b52-a6e7-c2e8ea6fef72"},
            "fault_before": {"error": error, "initial_sample": initial, "contract_ok": False, "endpoint_uid": "dev-test",
                             "first_error_at": "2026-11-24T01:00:02Z", "observed_at": "2026-11-24T01:00:11Z",
                             "delay_samples": [{**timed, "deadline_ms": 3000, "elapsed_ms": 1101.1}] * 5},
            "observation": {"control": {"mode": "latency", "operation_id": "17a69ee9-d49b-4b52-a6e7-c2e8ea6fef72",
                                        "latency_delay_ms": 1100, "readiness": "ACCEPTING_TRAFFIC"},
                            "ready_endpoints": ["dev-test"], "observed_at": "2026-11-24T01:00:11Z"},
            "termination_events": [{"event": "expired", "timestamp": "2026-11-24T01:00:30.002Z"}],
            "healthy_before": good, "restored": good, "recovered_at": "2026-11-24T01:00:32Z",
            "observation_seconds_after_recovery": 75, "latency_rules_checked_after": True,
            "recorded_latency": [{"seconds": .522, "timestamp": 1795482030.0}], "incidents": []}


def test_latency_cases_keep_fixed_parameters_and_actual_stderr(tmp_path):
    assert [load_case(k).delay_ms for k in CASES] == [500, 700, 900, 1100]
    record = product_receipt()
    normalized = dev_latency_record(record, "liveprobe")
    assert normalized["reason_code"] == "latency_above_threshold_without_incident"
    assert normalized["first_error_to_operation_end_ms"] == 28002
    assert normalized.get("wall_duration_ms") is None
    directory = tmp_path / "dev-latency-hold-v1" / "F3-11"
    directory.mkdir(parents=True)
    (directory / "liveprobe.json").write_text(json.dumps(record), encoding="utf-8")
    rows = [r for r in repair_rows(ROOT / "evaluation/catalogs/comparative-2026-11-23.json", tmp_path) if r["case"] == "F3-11"]
    assert len(rows) == 5 and rows[0]["status"] == "verified_natural_recovery"
    record["fault_before"]["initial_sample"]["stderr"] = "a synthetic timeout message"
    with pytest.raises(ValueError, match="actual deadline"):
        dev_latency_record(record, "liveprobe")


def test_latency_receipt_rejects_unmeasured_recovery_and_an_earlier_incident():
    record = product_receipt()
    record["incidents"] = [{"status": "recovery_review", "signal_reasons": ["TargetHighLatency"], "executed_actions": 0,
                            "first_latency_signal_at": "2026-11-24T00:59:59Z"}]
    record["product_terminal_observed_at"] = "2026-11-24T01:00:57Z"
    with pytest.raises(ValueError, match="unrelated"):
        dev_latency_record(record, "liveprobe")
    record["incidents"][0]["first_latency_signal_at"] = "2026-11-24T01:00:31Z"
    assert dev_latency_record(record, "liveprobe")["wall_duration_ms"] == 55000
    record["restored"] = {**record["restored"], "timed": []}
    with pytest.raises(ValueError, match="pinned healthy Pod and deadline"):
        dev_latency_record(record, "liveprobe")


def test_latency_rule_append_retains_operational_rules_and_limits_namespace_and_workload():
    data = json.loads((ROOT / "deploy/environments/observability.json").read_text())["data"]
    added = assessment_rules(data)
    for key in ("recording.yml", "alerts.yml"):
        amended = yaml.safe_load(added[key])["groups"]
        assert amended[:-1] == yaml.safe_load(data[key])["groups"]
        extra = amended[-1]
        expression = extra["rules"][0]["expr"]
        assert extra["name"] == GROUP
        assert 'namespace="dev"' in expression and 'app=~"lp-eval-inventory-pulse|lp-eval-order-pulse"' in expression
        assert "prd" not in expression


def test_active_latency_uses_the_unabridged_control_response(monkeypatch):
    record = product_receipt()
    setup = record["observation"]
    sample = record["fault_before"]
    monkeypatch.setattr(trial, "samples", lambda _case, _pod, _deadline, count:
                        [sample["initial_sample"]] if count == 1 else sample["delay_samples"])
    monkeypatch.setattr(trial.pulse, "control_response", lambda *_: setup["control"])
    monkeypatch.setattr(trial.service, "endpoint_uids", lambda *_: setup["ready_endpoints"])
    now = datetime.now(timezone.utc)
    observations = {}
    fault = trial.observe_fault(load_case("F3-11"), "pod", {"endpoint_uid": "dev-test"}, record["operation"],
                                now - timedelta(seconds=1), now + timedelta(seconds=1), observations)
    assert observations["control"]["latency_delay_ms"] == 1100
    assert observations["observed_at"] == fault["observed_at"]
