from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest

from evaluation import k8s_admission_request_trial as trial
from evaluation.comparison_summary import kubernetes_admission_request_proof, product_nondetection, repair_rows
from evaluation.ki02_admission_fixture import API_SERVER_VERSION, API_VERSION, CASE, OLD_VERSION, PROTOCOL, WEBHOOK, update_versions
from evaluation.live_repair import TASK
from evaluation.single_baseline import estimate


def uid(number):
    return str(UUID(int=number))


@pytest.fixture
def repaired():
    initial = ('Error from server (InternalError): error when creating "STDIN": Internal error occurred: '
               'failed calling webhook "review.ki02.verify.invalid": failed to call webhook: '
               'the server rejected our request for an unknown reason')
    fault = {"stored": False, "sent_review_version": OLD_VERSION, "message": initial}
    usage = {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 15, "reasoning_output_tokens": 10}
    scope = "b" * 64
    before = {"uid": uid(1), "versions": ["v1beta1"], "scope_sha256": scope, "resource_version": "11"}
    after = {**before, "versions": ["v1"], "resource_version": "13"}
    applied = {"applied": True, "before": before, "after": after}
    call = {"model": "gpt-6.2-astra", "effort": "xhigh", "usage": usage, "status": "ok", "duration_ms": 50}

    def stored(number, reviews):
        return {"stored": True, "webhook_requests": reviews, "uid": uid(number)}

    return {"case": CASE, "protocol": PROTOCOL, "api_version": API_SERVER_VERSION, "arm": "single_astra",
            "model": call["model"], "effort": call["effort"], "model_failed": None, "cleanup_verified": True,
            "status": "verified_recovered", "initial_error_log": initial, "injected_request": fault, "fault_before": fault,
            "input_sha256": sha256((TASK + initial).encode()).hexdigest(), "fixture_sha256": trial.fixture_digest(scope),
            "baseline_request": stored(2, [API_VERSION]), "initial_registration": before,
            "unmatched_request_during_fault": stored(3, []), "unmatched_request": stored(4, []),
            "investigation": [{"request": {"tool": "configure", "key": "admissionReviewVersions", "value": '["v1"]'},
                               "result": applied}],
            "independent_probes": [{"registration": after, "request": stored(number, [API_VERSION])} for number in (5, 6)],
            "calls": [call], "usage": usage, "model_duration_ms": 50, "wall_duration_ms": 75,
            "estimated_usd": estimate("single_astra", usage)}


def test_requires_distinct_writes_and_records_verified_recovery(repaired, tmp_path):
    assert kubernetes_admission_request_proof(repaired)
    catalog = Path(__file__).resolve().parents[1] / "catalogs" / "comparative-2026-11-23.json"
    directory = tmp_path / PROTOCOL.replace("/", "-") / CASE
    directory.mkdir(parents=True)
    (directory / "single_astra.json").write_text(json.dumps(repaired), encoding="utf-8")
    row = next(r for r in repair_rows(catalog, tmp_path) if r["case"] == CASE and r["arm"] == "single_astra")
    assert row["verified_local_recovery"] is True and row["total_tokens"] == 115
    repaired["independent_probes"][1]["request"]["uid"] = repaired["independent_probes"][0]["request"]["uid"]
    assert not kubernetes_admission_request_proof(repaired)


def test_requires_a_model_scoped_write(repaired):
    repaired["investigation"][0]["request"]["key"] = "failurePolicy"
    assert not kubernetes_admission_request_proof(repaired)


def test_can_record_two_actual_rejections_when_the_model_does_not_repair(repaired):
    repaired["status"] = "unrecovered"
    repaired["investigation"] = [{"request": {"tool": "finish"}, "result": {"finished": True}}]
    repaired["independent_probes"] = [{"registration": repaired["initial_registration"],
                                       "request": repaired["fault_before"]} for _ in range(2)]
    assert kubernetes_admission_request_proof(repaired)
    repaired["independent_probes"].pop()
    assert not kubernetes_admission_request_proof(repaired)


def test_product_miss_needs_live_watch_and_persistent_admission_rejection(repaired):
    record = {key: value for key, value in repaired.items()
              if key not in {"model", "effort", "usage", "model_duration_ms", "estimated_usd", "investigation"}}
    record.update(arm="liveprobe", status="unrecovered", reason_code="admission_request_not_detected",
                  product_source_image=trial.IMAGE_SHA, deployed_ready_image=trial.IMAGE_SHA,
                  calls=[], agent_runs_after=0, matched_incident_count_after=0,
                  wall_duration_ms=None, observation_seconds=82,
                  native_watch_epoch={name: "2026-11-24T09:00:00Z"
                                      for name in ("pod_watch", "event_watch", "endpoint_poller")},
                  native_watch_witnesses=[{"phase": "before", "uid": uid(8)}, {"phase": "after", "uid": uid(9)}],
                  deployed_prometheus_rules={"rules_sha256": "c" * 64, "rule_count": 1,
                                             "alert_names": ["TargetServiceNoEndpoints"], "admission_rule_names": []},
                  independent_probes=[{"registration": repaired["initial_registration"],
                                       "request": repaired["fault_before"]} for _ in range(2)],
                  rejection_counter=[f'apiserver_admission_webhook_rejection_count{{name="{WEBHOOK}"}} 4'])
    assert kubernetes_admission_request_proof(record)
    assert product_nondetection(record)
    assert not kubernetes_admission_request_proof({**record, "native_watch_witnesses": record["native_watch_witnesses"][:1]})
    assert not kubernetes_admission_request_proof({**record, "rejection_counter": []})
    assert not kubernetes_admission_request_proof({**record, "matched_incident_count_after": 1})
    assert not kubernetes_admission_request_proof({**record, "observation_seconds": 0})


def test_first_model_call_receives_only_the_error_and_verifier_is_independent(monkeypatch, repaired):
    applied = repaired["investigation"][0]
    deadline_ms = repaired["wall_duration_ms"]
    requested = []

    class Fixture:
        def request(self, packet):
            requested.append(packet)
            if packet["tool"] == "prepare":
                return {key: repaired[key] for key in ("case", "api_version", "baseline_request", "injected_request",
                                                       "initial_registration", "unmatched_request_during_fault")}
            if packet["tool"] == "configure":
                return applied["result"]
            return {"independent_probes": repaired["independent_probes"], "unmatched_request": repaired["unmatched_request"],
                    "wall_duration_ms": deadline_ms}

    class Model:
        def __init__(self, model, effort, protocol):
            assert (model, effort, protocol) == (repaired["model"], repaired["effort"], PROTOCOL)

        def turn(self, model, effort, initial, history):
            assert initial == repaired["initial_error_log"] and history == []
            return {**repaired["calls"][0], "data": applied["request"]}

        def close(self):
            pass

    monkeypatch.setattr(trial, "RemoteModelSession", Model)
    result = trial.run_fixture(Fixture(), "single_astra")
    assert requested == [{"tool": "prepare"}, applied["request"], {"tool": "verify"}]
    assert kubernetes_admission_request_proof({**result, "cleanup_verified": True})


@pytest.mark.parametrize("key, value", [("failurePolicy", '"Ignore"'), ("admissionReviewVersions", '"v1"'),
                                          ("admissionReviewVersions", '["invalid"]'),
                                          ("admissionReviewVersions", '["v1", "v1"]')])
def test_broker_rejects_attempts_outside_its_version_field(key, value):
    assert update_versions(key, value)["applied"] is False
