import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evaluation import k8s_dev_selector_trial as trial
from evaluation.comparison_summary import dev_selector_record
from evaluation.dev_selector_case import load_selector_case
from evaluation.live_repair import TASK


CASE = load_selector_case("F4-01")
BASELINE = {"service_uid": "service-a", "selector": {"app": "inventory", "other": "kept"},
            "service_spec": {"selector": {"app": "inventory", "other": "kept"}, "clusterIP": "10.3.0.11"}}


def test_service_write_is_fenced_to_original_uid_revision_and_whole_selector(monkeypatch):
    current = {"metadata": {"uid": "service-a", "resourceVersion": "89"}, "spec": BASELINE["service_spec"]}
    command = Mock()
    monkeypatch.setattr(trial.live, "command", command)
    broken = trial.broken_selector(CASE, BASELINE)
    trial.patch_selector(CASE, current, BASELINE, BASELINE["selector"], broken)
    args = command.call_args.args
    assert args[:5] == ("patch", "service", "inventory", "--type=json", "-p")
    assert json.loads(args[-1]) == [
        {"op": "test", "path": "/metadata/uid", "value": "service-a"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "89"},
        {"op": "test", "path": "/spec/selector", "value": BASELINE["selector"]},
        {"op": "replace", "path": "/spec/selector", "value": {"app": CASE.bad_app, "other": "kept"}},
    ]
    changed = {**current, "spec": {**current["spec"], "clusterIP": "10.3.0.12"}}
    with pytest.raises(trial.live.Refused, match="changed outside"):
        trial.patch_selector(CASE, changed, BASELINE, BASELINE["selector"], broken)
    with pytest.raises(trial.live.Refused, match="unapproved"):
        trial.patch_selector(CASE, current, BASELINE, BASELINE["selector"], {"app": CASE.bad_app})
    assert command.call_count == 1


def test_initial_error_requires_the_actual_service_unavailable_response(monkeypatch):
    response = SimpleNamespace(returncode=1, stderr=f"Error from server (ServiceUnavailable): {CASE.errors[0]}")
    monkeypatch.setattr(trial, "run", lambda *a, **k: response)
    assert trial.proxy_error(CASE) == CASE.errors[0]
    response.stderr = "Error from server (ServiceUnavailable): no endpoints available for service \"different-service\""
    with pytest.raises(trial.live.Refused, match="actual no-endpoints"):
        trial.proxy_error(CASE)


def test_recovery_waits_for_readiness_but_refuses_a_changed_deployment(monkeypatch):
    baseline = {**BASELINE, "endpoint_uid": "original-pod", "deployment_uid": "original-deployment",
                "deployment_sha256": "original-template"}
    deployment = {"metadata": {"uid": "original-deployment"}}
    monkeypatch.setattr(trial, "current_service", lambda case: {"metadata": {"uid": "service-a"},
                                                               "spec": BASELINE["service_spec"]})
    monkeypatch.setattr(trial.live, "get", lambda *args: deployment)
    monkeypatch.setattr(trial.live, "fixture_digest", lambda value: "original-template")
    monkeypatch.setattr(trial.live, "is_settled", Mock(side_effect=[False, True]))
    monkeypatch.setattr(trial, "endpoint_uids", lambda *args: ["original-pod"])
    probe = Mock(return_value={"endpoint_uid": "original-pod"})
    monkeypatch.setattr(trial.live, "probe", probe)

    assert trial.restored_probes(CASE, baseline) is None
    probe.assert_not_called()
    assert trial.restored_probes(CASE, baseline) == [{"endpoint_uid": "original-pod"}] * 2
    deployment["metadata"]["uid"] = "new-deployment"
    with pytest.raises(trial.live.Refused, match="Deployment changed"):
        trial.restored_probes(CASE, baseline)


def receipt(arm):
    fault = {"selector": trial.broken_selector(CASE, BASELINE), "error": CASE.errors[0],
             "ready_endpoints": 0, "http_status": 503, "service_uid": BASELINE["service_uid"], "contract_ok": False}
    up = {"ready": 200, "business": 200, "endpoint_uid": "pod-b", "contract_ok": True, "order_dependency": 200}
    return {"case": CASE.case_id, "arm": arm, "namespace": "dev", "protocol": "live-repair/v36",
            "target": CASE.target, "case_sha256": CASE.case_sha256, "fixture_sha256": "a" * 64,
            "input_sha256": trial.hash_text(TASK + CASE.errors[0]), "fault_before": fault,
            "selector_before": BASELINE["selector"], "service_uid": BASELINE["service_uid"],
            "healthy_before": {"contract_ok": True, "probes": [up, up]}, "restored": [up, up]}


def test_product_handoff_requires_attributed_signal_after_10_minutes_and_actual_failed_service():
    record = receipt("liveprobe")
    record.update(status="unrecovered", injection_started_at="2026-11-24T01:00:00+00:00",
                  incident={"id": 89, "status": "handed_off", "signal_reasons": ["NoEndpoints"],
                            "executed_actions": 0, "first_endpoint_signal_at": "2026-11-24T01:10:01+00:00"},
                  detection_ms=601000, watcher_continuous=True, operator_restored=True,
                  independent_probes=[record["fault_before"]] * 2)
    assert dev_selector_record(record, "liveprobe")["status"] == "unrecovered"
    early = {**record["incident"], "first_endpoint_signal_at": "2026-11-24T01:09:59+00:00"}
    with pytest.raises(ValueError, match="10-minute"):
        dev_selector_record({**record, "incident": early, "detection_ms": 599000}, "liveprobe")
    with pytest.raises(ValueError, match="before cleanup"):
        dev_selector_record({**record, "independent_probes": record["restored"]}, "liveprobe")


def test_standalone_recovery_requires_action_and_http_checks_before_cleanup():
    record = receipt("single_sol")
    record.update(status="verified_recovered", calls=[{}], lease_released=True, interfering_incidents=0,
                  operator_restored=False, verified_before_cleanup=record["restored"],
                  standalone_action={"accepted": True, "action": "selector_app_cas",
                                     "from": CASE.bad_app, "to": CASE.workload})
    assert dev_selector_record(record, "single_sol")["verified_dev_action"] is True
    with pytest.raises(ValueError, match="precede"):
        dev_selector_record({**record, "operator_restored": True}, "single_sol")
    with pytest.raises(ValueError, match="isolation"):
        dev_selector_record({**record, "interfering_incidents": 1}, "single_sol")
