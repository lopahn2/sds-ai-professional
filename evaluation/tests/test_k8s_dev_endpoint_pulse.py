from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation.comparison_summary import dev_pulse_record


def test_frozen_pulses_preserve_original_duration_and_probe():
    assert [(c.workload, c.duration) for c in map(pulse.load_case, pulse.PULSES)] == [
        ("inventory", 7), ("order", 13), ("order", 17),
    ]


def test_standalone_cannot_reset_original_pod_without_a_verified_fixture(monkeypatch):
    preflight = Mock()
    monkeypatch.setattr(pulse, "preflight", preflight)
    monkeypatch.setattr(sys, "argv", ["pulse", "--case", "F4-02", "--arm", "single_sol", "--preflight"])
    with pytest.raises(pulse.live.Refused, match="verified isolated image"):
        pulse.main()
    preflight.assert_not_called()


def test_control_exec_targets_one_pod_and_preserves_the_original_pulse(monkeypatch):
    case = pulse.load_case("F4-02")
    expected = {"mode": "endpoint_off", "operation_id": "c06ad108-3abc-47cf-8501-0a6227ebef75",
                "accepted_at": "2026-11-24T00:00:00Z", "expires_at": "2026-11-24T00:00:07Z"}
    kube = Mock(return_value=json.dumps({**expected, "irrelevant": "discard"}) + "\n202")
    monkeypatch.setattr(pulse.live, "command", kube)
    assert pulse.request(case, "inventory-original", activate=True) == expected
    assert kube.call_args.args[:4] == ("exec", "pod/inventory-original", "-c", "inventory")
    assert json.loads(kube.call_args.args[-1]) == {"mode": "endpoint_off", "duration_seconds": 7}
    start, expires = pulse.receipt_times(case, expected)
    assert expires - start == timedelta(seconds=7)
    with pytest.raises(ValueError, match="different fault or duration"):
        pulse.receipt_times(case, {**expected, "expires_at": "2026-11-24T00:00:08Z"})


def test_execute_does_not_repeat_or_reset_a_timed_out_mutation(monkeypatch, tmp_path):
    case = pulse.load_case("F4-02")
    state_write = Mock(side_effect=OSError)
    monkeypatch.setattr(pulse, "request", state_write)
    original = {"endpoint_uid": "pod", "selector": {"app": "inventory"}}
    recovery = Mock(return_value=[{"ready": 200}, {"ready": 200}])
    monkeypatch.setattr(pulse, "restore_by_expiry", recovery)
    monkeypatch.setattr(pulse.service, "endpoint_poller_epoch", lambda: "starting")
    path = Path(tmp_path)
    result = pulse.execute(case, original, "inventory-original", [], 0, {}, path / "run.json", path / "run.lock")
    assert result["trial_error"] == "OSError"
    assert result["status"] == "trial_unverified"
    assert not (path / "run.lock").exists()
    state_write.assert_called_once_with(case, "inventory-original", activate=True)
    assert recovery.call_args.args[-1] > datetime.now(timezone.utc) + timedelta(seconds=case.duration)


def test_observation_keeps_an_actual_service_and_dependency_error_under_the_same_operation(monkeypatch):
    case = pulse.load_case("F4-08")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(pulse.service, "endpoint_uids", lambda *args: [])
    monkeypatch.setattr(pulse, "primary_error", lambda *args: (case.errors[0], now))
    monkeypatch.setattr(pulse, "upstream_error", lambda case: 503)
    monkeypatch.setattr(pulse, "request", lambda *args: {
        "mode": "endpoint_off", "operation_id": "accepted", "readiness": "REFUSING_TRAFFIC",
    })
    proof = pulse.observe_fault(case, {"endpoint_uid": "original"}, "original", "accepted",
                                now - timedelta(seconds=2), now + timedelta(seconds=7))
    assert (proof["error"], proof["ready_endpoints"], proof["order_dependency"]) == (case.errors[0], 0, 503)
    assert proof["first_503_at"] == now.isoformat()


def test_scoped_reset_sends_only_the_matching_operation_and_preserves_rejections(monkeypatch):
    case = pulse.load_case("F4-02")
    operation_id = "c06ad108-3abc-47cf-8501-0a6227ebef75"
    kube = Mock(return_value='{"error":"operation_not_current"}\n409')
    monkeypatch.setattr(pulse.live, "command", kube)
    assert pulse.request(case, "inventory-original", reset_id=operation_id) == {"reset_status": 409}
    assert kube.call_args.args[-2] == "RESET"
    assert json.loads(kube.call_args.args[-1]) == {"expected_operation_id": operation_id}
    kube.return_value = '{"error":"invalid_request"}\n400'
    assert pulse.request(case, "inventory-original", reset_id=operation_id) == {"reset_status": 400}
    assert kube.call_count == 2
    assert all(json.loads(c.args[-1]) == {"expected_operation_id": operation_id} for c in kube.call_args_list)


def test_model_cannot_reset_a_later_operation(monkeypatch):
    case = pulse.load_case("F4-08")
    request = Mock(return_value={"operation_id": "later"})
    monkeypatch.setattr(pulse, "request", request)
    lease = Mock()
    outcome = pulse.model_request(case, "order-original", {"operation_id": "earlier"},
                                  {"tool": "configure", "key": "fault_mode", "value": "off"},
                                  {}, {}, lease)
    assert outcome["accepted"] is False
    request.assert_called_once_with(case, "order-original")
    lease.require_active.assert_not_called()


def test_expiry_and_explicit_reset_are_attributed_using_the_operation_window():
    case = pulse.load_case("F4-08")
    operation = {"mode": "endpoint_off", "operation_id": "c06ad108-3abc-47cf-8501-0a6227ebef75",
                 "accepted_at": "2026-11-24T00:00:00Z", "expires_at": "2026-11-24T00:00:13Z"}
    reset = {"event": "reset", "timestamp": "2026-11-24T00:00:10Z"}
    expiry = {"event": "expired", "timestamp": "2026-11-24T00:00:13.010Z"}
    assert pulse.bounded_termination(case, operation, [reset])[0] == "reset"
    assert pulse.bounded_termination(case, operation, [expiry])[0] == "expired"
    assert pulse.bounded_termination(case, operation, [{**reset, "timestamp": expiry["timestamp"]}]) is None
    assert pulse.bounded_termination(case, operation, [reset, expiry]) is None


def test_dependency_response_may_finish_after_expiry_if_the_request_started_during_the_hold(monkeypatch):
    case = pulse.load_case("F4-02", isolated_inventory=True)
    began = datetime.now(timezone.utc)
    expiry = began + timedelta(seconds=4)
    monkeypatch.setattr(pulse.service, "endpoint_uids", lambda *args: [])
    monkeypatch.setattr(pulse, "primary_error", lambda *args: (case.errors[0], began + timedelta(seconds=1)))
    monkeypatch.setattr(pulse, "timed_state", lambda *args: (
        {"mode": "endpoint_off", "operation_id": "scoped", "readiness": "REFUSING_TRAFFIC"},
        began + timedelta(seconds=2),
    ))
    monkeypatch.setattr(pulse, "timed_upstream_error", lambda *args: (
        500, began + timedelta(seconds=1), expiry + timedelta(seconds=1),
    ))
    proof = pulse.observe_fault(case, {"endpoint_uid": "original"}, "original", "scoped",
                                began - timedelta(seconds=3), expiry)
    assert proof["order_dependency"] == 500
    assert datetime.fromisoformat(proof["observed_at"]) < expiry < datetime.fromisoformat(proof["dependency_finished_at"])


def test_comparison_record_distinguishes_expiry_and_model_attribution():
    case = pulse.load_case("F4-08", isolated_order=True)
    operation_id = "c06ad108-3abc-47cf-8501-0a6227ebef75"
    healthy = [{"ready": 200, "business": 200, "endpoint_uid": "original", "contract_ok": True,
                "order_dependency": 200}] * 2
    initial = case.errors[0]
    receipt = {"case": case.case_id, "target": case.target, "case_sha256": case.case_sha256,
               "protocol": pulse.PROTOCOL, "fixture_sha256": "a" * 64, "arm": "liveprobe", "duration_seconds": 13,
               "healthy_before": {"probes": healthy, "contract_ok": True}, "restored": healthy, "incidents": [],
               "status": "product_pulse_observed", "input_sha256": pulse.hash_text(pulse.TASK + initial),
               "endpoint_watcher_epoch": "2026-11-23T23:59:00Z", "observation_seconds_after_recovery": 75,
               "operation": {"mode": "endpoint_off", "operation_id": operation_id,
                             "accepted_at": "2026-11-24T00:00:00Z", "expires_at": "2026-11-24T00:00:13Z"},
               "fault_before": {"error": initial, "ready_endpoints": 0, "http_status": 503, "order_dependency": 503,
                                "contract_ok": False, "first_503_at": "2026-11-24T00:00:03Z",
                                "observed_at": "2026-11-24T00:00:04Z"},
               "recovered_observed_at": "2026-11-24T00:00:16Z",
               "termination_events": [{"event": "expired", "timestamp": "2026-11-24T00:00:13.020Z"}]}
    observed = dev_pulse_record(receipt, "liveprobe")
    assert (observed["status"], observed["wall_duration_ms"], observed["first_error_to_operation_end_ms"]) == (
        "verified_natural_recovery", None, 10020,
    )
    model = {**receipt, "arm": "single_sol", "status": "verified_model_pulse_recovery", "calls": [{}],
             "lease_released": True, "model_evaluation_duration_ms": 11000, "verified_before_cleanup": healthy,
             "termination_events": [{"event": "reset", "timestamp": "2026-11-24T00:00:10.020Z"}],
             "standalone_action": {"accepted": True, "action": "scoped_operation_reset", "operation_id": operation_id}}
    assert dev_pulse_record(model, "single_sol")["verified_dev_action"] is True
    with pytest.raises(ValueError, match="model reset"):
        dev_pulse_record({**model, "termination_events": receipt["termination_events"]}, "single_sol")
    with pytest.raises(ValueError, match="different endpoint"):
        dev_pulse_record({**receipt, "restored": [{**healthy[0], "endpoint_uid": "different"}] * 2}, "liveprobe")
    dependency = {**receipt["fault_before"], "control_observed_at": "2026-11-24T00:00:04Z",
                  "observed_at": "2026-11-24T00:00:12Z", "dependency_started_at": "2026-11-24T00:00:12Z",
                  "dependency_finished_at": "2026-11-24T00:00:15Z"}
    assert dev_pulse_record({**receipt, "fault_before": dependency}, "liveprobe")["status"] == "verified_natural_recovery"
    with pytest.raises(ValueError, match="dependency did not start"):
        dev_pulse_record({**receipt, "fault_before": {**dependency, "dependency_started_at": "2026-11-24T00:00:14Z"}},
                         "liveprobe")
    limit_result = {**receipt, "arm": "single_luna", "status": "trial_unverified", "lease_released": True,
                    "model_evaluation_duration_ms": 20000, "calls": [
                        {"status": "ok", "model": "gpt-6-luna", "effort": "low"},
                    ] * 8, "investigation": [{"request": {"tool": "logs"}, "result": {}}] * 8}
    natural = dev_pulse_record(limit_result, "single_luna")
    assert natural["status"] == "verified_natural_recovery"
    assert natural["reason_code"] == "standalone_tool_limit_without_conclusion"
    assert not natural.get("verified_dev_action")
    with pytest.raises(ValueError, match="model reset"):
        dev_pulse_record({**limit_result, "calls": limit_result["calls"][:-1],
                          "investigation": limit_result["investigation"][:-1]}, "single_luna")
