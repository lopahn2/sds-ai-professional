"""Regression tests for the log-only repair proof and expression boundary."""
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

import pytest

from evaluation.application_fixture_replay import fixture_review, replay, runtime_evidence_rows
from evaluation.comparison_summary import (
    background_jobs_recovered,
    catalog_release_recovered,
    dependency_probe_recovered,
    delivery_invariant_recovered,
    delivery_memory_recovered,
    delivery_release_recovered,
    kubernetes_preflight_proof,
    notification_mixed_symptoms_persist,
    notification_probe_recovered,
    observed_catalog_release_faults,
    observed_dual_payment_code_fault,
    observed_dependency_and_delay,
    observed_delivery_invariant_failure,
    observed_delivery_memory_faults,
    observed_delivery_release_faults,
    observed_intermittent_delivery,
    observed_startup_and_readiness_faults,
    observed_mixed_notification_fault,
    observed_order_release_fault,
    observed_order_runtime_fault,
    observed_payment_cpu_fault,
    observed_payment_pool_and_exception,
    observed_timeout,
    order_release_recovered,
    paired_repair_coverage,
    runtime_handoff_observations,
    runtime_handoff_proof,
    recovery_action_matches,
    repair_rows,
    within_original_deadline,
)
from evaluation import k8s_compatibility_trial as compatibility
from evaluation import live_repair
from evaluation.live_repair import (
    DeliveryReleaseTrial,
    PROGRAM,
    PaymentDualReleaseTrial,
    PaymentReleaseTrial,
    RemoteModelSession,
    validated_dual_expression,
    validated_payment_expression,
)


@pytest.mark.parametrize("expression", [
    '__import__("os")',
    'payment.__class__',
    'payment.get.__self__',
    'payment.get',
    'open("app.py")',
    '[value for value in payment]',
    '(payment := {})',
    'payment.get(key="fee")',
])
def test_source_repair_rejects_operations_outside_the_expression_boundary(expression):
    with pytest.raises(ValueError):
        validated_payment_expression(expression)


def test_source_repair_accepts_both_subscription_and_dict_get():
    result = validated_payment_expression("payment['amount'] if payment.get('status') == 'paid' else 0")
    assert result == "payment['amount'] if payment.get('status') == 'paid' else 0"


def test_code_recovery_requires_the_applied_source_version_and_business_regression():
    before, after = "a" * 64, "b" * 64
    regression = {"suite": "test_business.py", "exit_code": 0, "tests": 4}
    record = {
        "protocol": "live-repair/v4",
        "successful_source_writes": 1,
        "original_source_sha256": before,
        "final_source_sha256": after,
        "investigation": [{"request": {"tool": "patch"},
                           "result": {"accepted": True, "from_sha256": before, "to_sha256": after}}],
        "business_regression_before": regression,
        "business_regression_after": dict(regression),
        "refund_journey": {"code_defect_mode_active": True, "cancel_status": 200, "cancelled_refund_status": 200,
                           "cancelled_amount": 0, "cancelled_fee": 0, "missing_payment_status": 404},
    }
    assert recovery_action_matches(record)
    for invalid in ({"final_source_sha256": before}, {"successful_source_writes": 0},
                    {"refund_journey": None}, {"business_regression_after": None}):
        assert not recovery_action_matches({**record, **invalid})
    wrong_refund = deepcopy(record)
    wrong_refund["refund_journey"]["cancelled_amount"] = 32000
    assert not recovery_action_matches(wrong_refund)


def test_release_fixture_preserves_the_existing_chaos_branch_and_adds_a_separate_source_regression():
    trial = PaymentReleaseTrial()
    try:
        assert trial.original_source.replace(
            "        elif payment['status'] == 'paid' and payment['amount'] >= 30000:\n"
            "            refundable = payment['refundable_amount']\n", "", 1,
        ) == trial.healthy_source
        assert trial.fault_state_verified({"active": []})
        assert not trial.fault_state_verified({"active": ["code_bug_validation"]})
    finally:
        trial.close()


def test_dual_payment_patch_preserves_both_assignments_and_utf8_source(monkeypatch):
    trial = PaymentDualReleaseTrial()
    try:
        trial.source_path.write_text(trial.original_source, encoding="utf-8")
        monkeypatch.setattr(trial, "restart", lambda *, fault: None)
        monkeypatch.setattr(trial, "probe", lambda: {})
        first = trial.patch("calculate_fee.fee", "amount * self.settings['fee_percent'] // 100")
        second = trial.patch("receipt.amount_text", """f"{payment['charged_amount']:,}원" """)
        fixed = trial.source_path.read_text()
        assert first["accepted"] and second["accepted"]
        assert first["to_sha256"] == second["from_sha256"]
        assert "fee = amount * self.settings['fee_percent'] // 100" in fixed
        assert "amount_text = '{:,}원'.format(payment['charged_amount'])" in fixed
        assert compile(fixed, "app.py", "exec")
    finally:
        trial.close()


@pytest.mark.parametrize("expression", [
    """f"{payment.__class__:,}원" """,
    """f"{payment['charged_amount']:{payment['format']}}원" """,
    "'{0.__class__}원'.format(payment['charged_amount'])",
])
def test_dual_payment_patch_only_accepts_literal_money_formatting(expression):
    with pytest.raises(ValueError):
        validated_dual_expression(expression, {"payment"})


def test_dual_payment_ledger_requires_both_failures_and_contiguous_source_changes():
    original, partial, fixed = ("a" * 64, "b" * 64, "c" * 64)
    failed = {"ready": 200, "business": 500, "fee_status": 500, "fee_exception": "ZeroDivisionError",
              "receipt_exception": "ValueError", "failed_payment_left_ledger_intact": True, "contract_ok": False}
    recovered = {"ready": 200, "business": 200, "fee_status": 201, "payment_fee": 975, "charged_amount": 32500,
                 "receipt_amount_text": "32,000원", "ledger_intact": True, "contract_ok": True}
    frames = [{"path": "app.py", "line": 74, "function": "calculate_fee", "exception": "ZeroDivisionError",
               "message": "integer division or modulo by zero"},
              {"path": "app.py", "line": 101, "function": "receipt", "exception": "ValueError",
               "message": "invalid formatter"}]
    regression = {"suite": "payment/tests", "exit_code": 0, "tests": 51}
    stages = [{"request": {"tool": "patch"}, "result": {"accepted": True, "assignment": name,
               "from_sha256": old, "to_sha256": new}} for name, old, new in
              (("calculate_fee.fee", original, partial), ("receipt.amount_text", partial, fixed))]
    record = {"protocol": "live-repair/v12", "fault_before": failed, "runtime_faults_before": [],
              "original_exception_frames": frames, "original_source_sha256": original, "final_source_sha256": fixed,
              "business_regression_before": regression, "business_regression_after": dict(regression),
              "successful_source_writes": 2, "investigation": stages, "verified": [recovered, dict(recovered)]}
    assert observed_dual_payment_code_fault(record)
    assert recovery_action_matches(record)
    assert not recovery_action_matches({**record, "original_exception_frames": frames[:1]})
    assert not recovery_action_matches({**record, "verified": [recovered, {**recovered, "ledger_intact": False}]})
    assert not recovery_action_matches({**record, "verified": [recovered, {**recovered, "receipt_amount_text": "0원"}]})
    stages[1]["result"]["from_sha256"] = original
    assert not recovery_action_matches(record)


def test_release_candidate_can_add_a_test_class_without_replacing_the_existing_suite(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "liveprobe"))
    from evaluation.application_release_trial import preserves_existing_tests

    original = "class HttpCase:\n    def test_existing(self):\n        assert self.request() == 200\n"
    added = "\nclass PaymentContractTests(HttpCase):\n    def test_fee(self):\n        assert self.fee() == 975\n"
    assert preserves_existing_tests(original, original + added)
    assert not preserves_existing_tests(original, original.replace("200", "500") + added)
    assert not preserves_existing_tests(original, original + added.replace("PaymentContractTests", "HttpCase"))
    assert not preserves_existing_tests(original, original + added.replace("def test_fee", "def setUp"))


def test_catalog_release_requires_two_prices_and_two_separate_index_builds():
    stack = [{"path": "app.py", "line": 83, "function": "price_response", "exception": "ValueError",
              "message": "Cannot specify ',' with 's'."},
             {"path": "app.py", "line": 155, "function": "background_job", "exception": "IndexRebuildError",
              "message": "missing index source"}]
    fault = {"ready": 200, "business": 500, "exception": "ValueError", "inventory_intact": True, "contract_ok": False,
             "job": {"status": "failed", "exception": "IndexRebuildError", "sequence": 1, "frame": stack[-1]}}
    record = {"protocol": "live-repair/v16", "fault_before": fault, "original_exception_frames": stack,
              "runtime_faults_before": []}
    assert observed_catalog_release_faults(record)
    assert not observed_catalog_release_faults({**record, "runtime_faults_before": ["background_job_failure"]})
    assert not observed_catalog_release_faults({**record, "original_exception_frames": stack[:1]})
    first = {"ready": 200, "business": 200, "inventory_intact": True, "contract_ok": True,
             "requested_price": 79000, "price_text": "79,000원",
             "job": {"status": "passed", "index_size": 6, "index_contract_ok": True,
                     "sequence": 1, "observed_at_monotonic": 16.1}}
    second = {**first, "requested_price": 81050, "price_text": "81,050원",
              "job": {**first["job"], "sequence": 2, "observed_at_monotonic": 31.1}}
    assert catalog_release_recovered([first, second])
    assert not catalog_release_recovered([first, {**second, "price_text": "79,000원"}])
    assert not catalog_release_recovered([first, {**second, "job": first["job"]}])
    assert not catalog_release_recovered([first, {**second, "job": {**second["job"], "index_size": 5}}])


def test_delivery_release_requires_two_distinct_shipments_and_consistent_history():
    errors = [{"path": "business.js", "function": "parseArrivalDate", "exception": "RangeError",
               "line": 10, "message": "Invalid time value"},
              {"path": "business.js", "function": "Object.handle", "exception": "StatusRegressionError",
               "line": 93, "message": "status differs from the event"}]
    fault = {"ready": 200, "business": 500, "submit_status": 500, "stored_status": 404, "stored_exactly": False,
             "seed_status": "접수", "seed_event": "출고", "neighbor_intact": True, "contract_ok": False}
    record = {"protocol": "live-repair/v17", "fault_before": fault, "original_exception_frames": errors,
              "runtime_faults_before": []}
    assert observed_delivery_release_faults(record)
    assert not observed_delivery_release_faults({**record, "fault_before": {**fault, "stored_status": 200}})
    first = {**fault, "business": 200, "submit_status": 201, "stored_status": 200, "seed_status": "출고",
             "stored_exactly": True, "contract_ok": True, "tracking": "LP-EVAL-25-1", "requested_due_at": "2032-02-29T12:34:56.000Z"}
    second = {**first, "tracking": "LP-EVAL-25-2", "requested_due_at": "2032-02-29T13:14:15.000Z"}
    assert delivery_release_recovered([first, second])
    assert not delivery_release_recovered([first, {**second, "tracking": first["tracking"]}])
    assert not delivery_release_recovered([first, {**second, "seed_status": "접수"}])
    assert not delivery_release_recovered([first, {**second, "requested_due_at": first["requested_due_at"]}])


def test_delivery_release_accepts_the_existing_seed_event_without_allowing_history_changes(monkeypatch):
    trial = DeliveryReleaseTrial()
    try:
        trial.source_path.write_text(trial.original_source, encoding="utf-8")
        monkeypatch.setattr(trial, "restart", lambda *, fault: None)
        monkeypatch.setattr(trial, "probe", lambda: {})
        assert not trial.patch("createBusiness.deliveries[0].events[0].status", "releaseStatus")["accepted"]
        assert trial.patch("createBusiness.releaseStatus", "deliveries[0].events[0].status")["accepted"]
        source = trial.source_path.read_text(encoding="utf-8")
        assert "const releaseStatus = deliveries[0].events[0].status;" in source
        assert "deliveries[0].status = releaseStatus;" in source
    finally:
        trial.close()


def test_delivery_memory_recovery_requires_actual_allocation_and_both_repairs():
    initial_memory = {"retained_bytes": 0, "array_buffer_bytes": 50000, "rss_bytes": 60000000}
    leaked = {name: count + 12 * 1024 * 1024 for name, count in initial_memory.items()}
    fault = {"ready": 200, "created": 201, "dispatched": 200, "business": 500, "saved_status": 200,
             "tracking": "LP-EVAL-29-1", "saved_tracking": "LP-EVAL-29-1", "saved_stage": "출고",
             "events": ["접수", "출고"], "seed_intact": True, "memory_before": initial_memory,
             "memory_after": leaked, "contract_ok": False}
    observation = {"protocol": "live-repair/v18", "runtime_faults_before": [], "fault_before": fault,
                   "original_exception_frames": [{"path": "business.js", "function": "transition",
                                                  "exception": "TypeError", "line": 18, "message": "undefined"}]}
    assert observed_delivery_memory_faults(observation)
    assert not observed_delivery_memory_faults({
        **observation, "fault_before": {**fault, "memory_after": {**leaked, "array_buffer_bytes": 50000}},
    })
    first = {**fault, "business": 200, "saved_stage": "배송 중", "events": ["접수", "출고", "배송 중"],
             "memory_after": initial_memory, "transition_note": "단계 변경", "contract_ok": True}
    second = {**first, "tracking": "LP-EVAL-29-2", "saved_tracking": "LP-EVAL-29-2"}
    assert delivery_memory_recovered([first, second])
    assert not delivery_memory_recovered([first, {**second, "events": ["접수", "출고"]}])
    assert not delivery_memory_recovered([first, {**second, "memory_after": leaked}])
    no_growth_metric = {**initial_memory, "array_buffer_bytes": None}
    assert not delivery_memory_recovered([first, {**second, "memory_after": no_growth_metric}])

    before_regression = {"exit_code": 0, "tests": 62}
    first_edit = {"accepted": True, "assignment": "transition.releaseEventIndex",
                  "from_sha256": "a" * 64, "to_sha256": "b" * 64}
    second_edit = {"accepted": True, "assignment": "rememberResponse.keepResponse",
                   "from_sha256": "b" * 64, "to_sha256": "c" * 64}
    record = {**observation, "verified": [first, second], "original_source_sha256": "a" * 64,
              "final_source_sha256": "c" * 64, "business_regression_before": before_regression,
              "business_regression_after": before_regression, "successful_source_writes": 2,
              "investigation": [{"request": {"tool": "patch"}, "result": edit}
                                for edit in (first_edit, second_edit)]}
    assert recovery_action_matches(record)
    assert not recovery_action_matches({**record, "successful_source_writes": 1, "final_source_sha256": "b" * 64,
                                        "investigation": record["investigation"][:1]})


def test_delivery_invariant_detects_error_even_when_both_http_responses_are_successful():
    samples = [{"status": 200, "tracking": tracking, "stage": stage, "stages": [stage], "shipment_intact": True}
               for tracking, stage in (("LP1001", "출고"), ("LP1002", "배송 중"))]
    fault = {"ready": 200, "business": 200, "samples": samples, "contract_ok": False, "errors_before": 0, "errors_after": 2}
    fixed = {**fault, "contract_ok": True, "errors_before": 4, "errors_after": 4}
    data = {"protocol": "live-repair/v19", "fault_before": fault, "verified": [fixed, fixed],
            "original_exception_frame": {"path": "business.js", "function": "checkIntegrity", "line": 6,
                                         "exception": "StatusRegressionError", "message": "shipment stage disagrees"}}
    assert observed_delivery_invariant_failure(data)
    assert delivery_invariant_recovered(data)
    assert not delivery_invariant_recovered({**data, "verified": [fixed, {**fixed, "errors_after": 5}]})
    assert not delivery_invariant_recovered({
        **data, "verified": [fixed, {**fixed, "samples": [{**samples[0], "stages": ["접수"]}, samples[1]]}],
    })


def test_order_recovery_requires_the_arithmetic_failure_and_stock_behaviour():
    before = {"ready": 200, "business": 500, "unavailable_status": 500, "missing_status": 500,
              "error": "ArithmeticException", "stock_intact": False, "out_of_stock_intact": False, "contract_ok": False}
    healthy = {**before, "business": 200, "unavailable_status": 200, "missing_status": 404, "error": None,
               "stock_intact": True, "out_of_stock_intact": True, "contract_ok": True}
    record = {"protocol": "live-repair/v20", "runtime_faults_before": [], "fault_before": before,
              "original_exception_frames": [
                  {"path": "src/main/java/io/liveprobe/target/order/AppFaultBusiness.java", "line": 13,
                   "function": "calculateOrderAmount", "exception": "ArithmeticException", "message": "/ by zero"}]}
    assert observed_order_release_fault(record)
    assert not observed_order_release_fault({**record, "runtime_faults_before": ["code_bug"]})
    assert order_release_recovered([healthy, healthy])
    assert not order_release_recovered([healthy, {**healthy, "out_of_stock_intact": False}])
    assert not order_release_recovered([healthy, {**healthy, "missing_status": 200}])
    created = {**healthy, "create_status": 201, "create_error": None, "order_id": 2, "stored_status": 200,
               "stored_intact": True, "seed_intact": True, "invalid_item_status": 400}
    second = {**created, "order_id": 3}
    assert order_release_recovered([created, second], dual=True)
    assert not order_release_recovered([created, created], dual=True)
    assert not order_release_recovered([created, {**second, "stored_intact": False}], dual=True)


def test_order_runtime_recovery_requires_the_original_client_deadline():
    healthy = {"ready": 200, "business": 200, "liveness": 200, "same_instance": True, "contract_ok": True,
               "unavailable_status": 200, "missing_status": 404, "stock_intact": True, "out_of_stock_intact": True,
               "duration_ms": 8, "deadline_ms": 1000, "deadline_timeout": False}
    failed = {**healthy, "business": None, "contract_ok": False, "duration_ms": 1003, "deadline_timeout": True}
    record = {"case": "order-01", "protocol": "live-repair/v24", "healthy_before": healthy, "fault_before": failed,
              "first_observation": {"event": "client_timeout", "exception": "TimeoutError", "deadline_ms": 1000},
              "successful_config_writes": 1, "investigation": [{"request": {"tool": "configure"},
                  "result": {"accepted": True, "setting": "latency", "still_active": []}}],
              "verified": [healthy, healthy]}
    assert observed_order_runtime_fault(record)
    assert recovery_action_matches(record)
    assert not recovery_action_matches({**record, "verified": [healthy, {**healthy, "duration_ms": 1000}]})
    assert not recovery_action_matches({**record, "verified": [healthy, {**healthy, "same_instance": False}]})
    assert runtime_handoff_observations(record, failed, healthy, [failed, failed], [healthy, healthy])
    assert not runtime_handoff_observations(record, failed, healthy, [failed, healthy], [healthy, healthy])
    assert not runtime_handoff_observations(record, failed, healthy, [failed, failed],
                                            [healthy, {**healthy, "same_instance": False}])


def test_review_requires_exact_nonempty_citations_and_retains_unobserved_status():
    evidence = {"E1": "upstream connection refused"}
    role = {"role": "analyst", "result": {"citations": [
        {"ref": "E1", "quote": "connection refused"}, {"ref": "E1", "quote": ""},
        {"ref": "E2", "quote": "connection refused"}, {"ref": "E1", "quote": "connection accepted"}]}}
    assert [item["grounded"] for item in fixture_review(role, evidence)["citations"]] == [True, False, False, False]
    assert [item["grounded"] for item in fixture_review(role, None)["citations"]] == [None] * 4


def test_public_cause_candidates_preserve_rank_and_their_own_grounding():
    from app.application.analysis_schema import ANALYSIS
    from jsonschema import Draft202012Validator

    observed = {"E1": "upstream connection refused", "E2": "second unrelated observation"}
    role = {"role": "analyst", "result": {
        "summary_ko": "연결 상태 확인", "confidence": .9, "fixable": False, "unresolved": [],
        "citations": [{"ref": "E1", "quote": "connection refused"}],
        "cause_candidates": [
            {"cause_ko": "업스트림 연결 거부", "evidence_refs": ["E1"]},
            {"cause_ko": "확인하지 못한 추가 후보", "evidence_refs": ["E1", "E2"]},
        ],
    }}
    assert Draft202012Validator(ANALYSIS).is_valid(role["result"])
    reviewed = fixture_review(role, observed)["cause_candidates"]
    assert [(row["rank"], row["grounded"]) for row in reviewed] == [(1, True), (2, False)]
    assert [row["grounded"] for row in fixture_review(role, None)["cause_candidates"]] == [None, None]
    role["result"]["cause_candidates"] *= 2
    assert not Draft202012Validator(ANALYSIS).is_valid(role["result"])


def test_liveprobe_citation_evidence_tracks_the_exact_attempt():
    from evaluation.application_fixture_replay import prompt_evidence

    first = {"id": "E1", "text": "the dependency refused the connection"}
    followup = {"id": "fixture-trace", "text": "the arithmetic operation divided by zero"}

    def prompt(*observations):
        context = {"collection": {"evidence": list(observations)}, "source_snapshot": {
            "files": {"app.py": "return total // amount"}, "file_sha256": {"app.py": "c" * 64},
        }}
        return f"Worker\nRole: analyst\n{json.dumps(context)}\nInstruction"

    raw = {"role": "analyst", "result": {"citations": [
        {"ref": "fixture-trace", "quote": "divided by zero"},
        {"ref": "source:app.py", "quote": "total // amount"},
        {"ref": "file_sha256:app.py", "quote": "c" * 64},
        None,
    ]}}

    earlier = fixture_review(raw, prompt_evidence(prompt(first), "analyst"))
    later = fixture_review(raw, prompt_evidence(prompt(first, followup), "analyst"))
    assert [row["grounded"] for row in earlier["citations"]] == [False, True, True, False]
    assert [row["grounded"] for row in later["citations"]] == [True, True, True, False]
    assert prompt_evidence(prompt(first, first), "analyst") is None
    assert prompt_evidence(prompt(first), "classifier") is None


def test_call_citation_metrics_keeps_failed_attempts_and_missing_coverage():
    from evaluation.comparison_summary import citation_metrics

    def call(role, citations):
        return {"role": role, "citation_review": {"scope": "evidence_as_presented_to_model",
                "evidence_observed": True, "citations": citations}}

    reviews = [call("classifier", [False]), call("classifier", [True]), call("analyst", []),
               {"role": "critic", "error_kind": "timeout"}, {"role": "patch"}]
    assert citation_metrics(reviews) == {"citation_reviewed_calls": 3, "citation_missing_calls": 2,
                                         "citations_grounded": 1, "citations_ungrounded": 1}
    assert citation_metrics([{"role": "classifier"}]) == {}


def test_runtime_replay_keeps_original_log_verbatim_for_citation():
    log = '{"event":"deadline_exceeded","status":200}'
    rows = runtime_evidence_rows({"first_log": log, "observed_health": {"duration_ms": 1001}})
    assert rows[0] == log
    assert json.loads(rows[1]) == {"observed_health": {"duration_ms": 1001}}


def test_payment_cpu_recovery_requires_measured_cpu_and_the_unchanged_ledger():
    healthy = {"ready": 200, "business": 200, "liveness": 200, "same_instance": True, "business_intact": True,
               "cpu_seconds": 0.02, "sample_seconds": 2, "cpu_core_fraction": 0.01, "contract_ok": True}
    failed = {**healthy, "cpu_seconds": 1.88, "cpu_core_fraction": 0.94, "contract_ok": False}
    record = {"case": "payment-08", "protocol": "live-repair/v27", "healthy_before": healthy, "fault_before": failed,
              "first_observation": {"level": "ERROR", "service": "test-application", "event": "process_cpu_high",
                                    "sample_seconds": 2, "threshold_percent_of_core": 65},
              "successful_config_writes": 1, "investigation": [{"request": {"tool": "configure"},
                  "result": {"accepted": True, "setting": "cpu_spin", "still_active": []}}],
              "verified": [healthy, healthy]}
    assert observed_payment_cpu_fault(record)
    assert recovery_action_matches(record)
    assert not observed_payment_cpu_fault({**record, "fault_before": {**failed, "cpu_seconds": 0}})
    assert not recovery_action_matches({**record, "verified": [healthy, {**healthy, "business_intact": False}]})


def test_release_runner_proof_requires_native_reproduction_and_the_healthy_artifact():
    test_id = "tests/test_refund_release.py::test_refund"
    patch = {"source_snapshot_id": "source", "diff": "diff", "diff_sha256": sha256(b"diff").hexdigest(),
             "tests_added": [test_id]}
    paid = [{"ready": 200, "business": 200, "contract_ok": True,
             "refund": {"payment_id": "2", "refundable_amount": 32000, "fee_refund": 960}} for _ in range(2)]
    record = {
        "protocol": "live-repair/v5", "arm": "liveprobe", "patch": patch,
        "baseline_image": "liveprobe-eval/payment@sha256:" + "a" * 64,
        "baseline_probe": {"runtime_faults": [], "paid": [{"business": 500, "ready": 200}] * 2},
        "verified": paid,
        "candidate_probe": {"paid": paid, "runtime_faults": [], "cancel_status": 200, "cancelled": "cancelled",
                            "cancelled_refund_status": 200, "missing_status": 404,
                            "cancelled_refund": {"payment_id": "1", "refundable_amount": 0, "fee_refund": 0}},
        "business_regression_before": {"tests": 51},
        "test_build": {"ok": True,
                       "artifact": {"image": "liveprobe-eval/payment@sha256:" + "b" * 64,
                                    "source_snapshot_id": "source", "diff_sha256": patch["diff_sha256"]},
                       "tests": {"before_failed": True, "after_passed": True, "regression_executed": True,
                                 "failed": 0, "total": 52, "executed_tests": [
                                    {"id": test_id, "phase": "before", "status": "failed"},
                                    {"id": test_id, "phase": "after", "status": "passed"}]}},
    }
    assert recovery_action_matches(record)
    missing_regression = deepcopy(record)
    missing_regression["test_build"]["tests"]["executed_tests"].pop()
    assert not recovery_action_matches(missing_regression)
    wrong_artifact = deepcopy(record)
    wrong_artifact["test_build"]["artifact"]["image"] = wrong_artifact["baseline_image"]
    assert not recovery_action_matches(wrong_artifact)
    with_runner_report = {**record, "application_state": "dev_targets_collecting",
                          "native_build_reports": [{"reported_ok": True, "diff_sha256": patch["diff_sha256"],
                                                    "state": "dev_targets_collecting"}]}
    assert recovery_action_matches(with_runner_report)
    assert not recovery_action_matches({**with_runner_report, "application_state": "handoff"})


def test_timeout_recovery_requires_the_original_deadline_and_cleared_fault():
    failure = {"ready": 200, "business": None, "transport": "request_timeout",
               "duration_ms": 1003, "deadline_ms": 1000}
    assert observed_timeout(failure)
    assert not observed_timeout({**failure, "transport": "connection_failed"})
    assert not observed_timeout({**failure, "duration_ms": 50})

    result = {"ready": 200, "business": 200, "transport": None, "duration_ms": 12, "deadline_ms": 1000}
    assert within_original_deadline(result, failure)
    assert not within_original_deadline({**result, "deadline_ms": 3000}, failure)
    assert not within_original_deadline({**result, "duration_ms": 1001}, failure)

    attempt = {"protocol": "live-repair/v6", "successful_config_writes": 1,
               "investigation": [{"request": {"tool": "configure"},
                                  "result": {"accepted": True, "still_active": []}}]}
    assert recovery_action_matches(attempt)
    attempt["investigation"][0]["result"]["still_active"] = ["hang"]
    assert not recovery_action_matches(attempt)


def test_notification_dependency_trial_checks_the_upstream_and_latency_independently():
    failed = {"ready": 200, "business": 200, "enqueue_status": 502, "exception": "DependencyError",
              "failed_request_left_no_item": True, "upstream_received": False, "seed_preserved": True,
              "enqueue_ms": 2034, "lookup_ms": 2005}
    assert observed_dependency_and_delay(failed)
    assert not observed_dependency_and_delay({**failed, "lookup_ms": 10})
    assert not observed_dependency_and_delay({**failed, "failed_request_left_no_item": False})
    fixed = {**failed, "enqueue_status": 202, "exception": None, "upstream_received": True,
             "queued_once": True, "enqueue_ms": 6, "lookup_ms": 2}
    assert dependency_probe_recovered(fixed)
    for partial in ({"lookup_ms": 2005}, {"upstream_received": False}, {"queued_once": False},
                    {"seed_preserved": False}):
        assert not dependency_probe_recovered({**fixed, **partial})
    actions = [{"request": {"tool": "configure"}, "result": {"accepted": True, "setting": "latency",
                                                            "active_impact": ["dependency_failure"]}},
               {"request": {"tool": "configure"}, "result": {"accepted": True, "setting": "dependency_failure",
                                                            "active_impact": []}}]
    record = {"protocol": "live-repair/v13", "successful_config_writes": 2, "investigation": actions}
    assert recovery_action_matches(record)
    actions[1]["result"]["setting"] = "latency"
    assert not recovery_action_matches(record)


def test_startup_and_readiness_recovery_must_preserve_the_fixed_release_setting():
    record = {
        "protocol": "live-repair/v14", "fault_before": {"transport": "service_not_listening"},
        "startup_diagnostic": {"exit_code": 1, "error": "KeyError", "frame": "load_settings",
                               "missing_config_key": "NOTIFICATION_SENDER_NAME"},
        "masked_readiness": {"ready": 503, "business": 200, "liveness": 200, "seed_preserved": True,
                             "release_active": ["not_ready", "startup_config_error"]},
        "successful_config_writes": 2,
    }
    verified = {"ready": 200, "business": 200, "liveness": 200, "seed_preserved": True,
                "contract_ok": True, "release_active": ["startup_config_error"]}
    record["verified"] = [dict(verified), dict(verified)]
    record["investigation"] = [{"request": {"tool": "configure"},
                                "result": {"accepted": True, "setting": key, "sender_configured": True}}
                               for key in ("NOTIFICATION_SENDER_NAME", "release.not_ready")]
    assert observed_startup_and_readiness_faults(record)
    assert recovery_action_matches(record)
    assert not recovery_action_matches({**record, "masked_readiness": {**record["masked_readiness"], "ready": 200}})
    assert not recovery_action_matches({**record, "verified": [verified, {**verified, "release_active": []}]})
    fault = {**record["fault_before"], "contract_ok": False}
    restored = {**verified, "release_active": []}
    native = {**record, "case": "notification-30", "fault_before": fault}
    assert runtime_handoff_observations(native, fault, restored, [fault, fault], [restored, restored])
    assert not runtime_handoff_observations(native, fault, restored, [fault, restored], [restored, restored])
    assert not runtime_handoff_observations(native, fault, restored, [fault, fault], [restored, verified])


def test_native_member_crash_evidence_requires_restart_and_a_survived_crash_interval():
    healthy = dict.fromkeys(("ready", "business", "liveness", "login", "profile"), 200)
    healthy.update(unknown_profile=404, data_intact=True, contract_ok=True)
    fault = {"ready": None, "business": None, "process_exit": 1, "connection_refused": True, "contract_ok": False}
    record = {"case": "member-03", "protocol": "live-repair/v28", "fault_before": fault, "healthy_before": healthy,
              "process_exits": [{"exit_code": 1, "runtime_exception_observed": True}] * 2,
              "fixture_cleanup_elapsed_ms": 3270, "fixture_resets": [{"restarted": True}]}
    assert runtime_handoff_observations(record, fault, healthy, [fault, fault], [healthy, healthy])
    assert not runtime_handoff_observations({**record, "fixture_cleanup_elapsed_ms": 2800}, fault, healthy,
                                            [fault, fault], [healthy, healthy])
    assert not runtime_handoff_observations({**record, "fixture_resets": []}, fault, healthy,
                                            [fault, fault], [healthy, healthy])


def test_delivery_repair_requires_the_setting_and_the_original_shipment_without_new_exceptions():
    healthy_row = {"status": 200, "shipment_intact": True}
    failed = {"ready": 200, "business": 500, "exception_count": 1,
              "samples": [{"status": 500, "shipment_intact": False}, healthy_row]}
    assert observed_intermittent_delivery(failed)
    assert not observed_intermittent_delivery({**failed, "exception_count": 0})
    fixed = {**failed, "business": 200, "contract_ok": True, "samples": [healthy_row, healthy_row]}
    native = {"case": "delivery-07"}
    second = {**failed, "exception_count": 3}
    third = {**failed, "exception_count": 4}
    cleanup = [{**fixed, "exception_count": 5}] * 2
    assert runtime_handoff_observations(native, failed, {}, [second, third], cleanup)
    assert not runtime_handoff_observations(native, failed, {}, [second, second], cleanup)
    entry = {"request": {"tool": "configure"}, "result": {"accepted": True, "effective_ratio": 0}}
    record = {"protocol": "live-repair/v15", "successful_config_writes": 1, "investigation": [entry],
              "verified": [fixed, dict(fixed)]}
    assert recovery_action_matches(record)
    assert not recovery_action_matches({**record, "verified": [fixed, {**fixed, "exception_count": 2}]})
    entry["result"]["effective_ratio"] = 0.5
    assert not recovery_action_matches(record)


def test_shared_model_connection_preserves_the_log_only_opening_and_followup():
    program = (
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'pid': os.getpid(), 'packet': json.loads(line)}), flush=True)\n"
    )
    session = RemoteModelSession("model", "low", "live-repair/v6", timeout=5,
                                 command=[sys.executable, "-B", "-c", program])
    try:
        first = session.turn("model", "low", "first error log", [])
        history = [{"request": {"tool": "logs"}, "result": {"exception": "TimeoutError"}}]
        next_turn = session.turn("model", "low", "first error log", history)
        assert first["pid"] == next_turn["pid"]
        assert set(first["packet"]) == {"task", "initial_error_log"}
        assert next_turn["packet"] == {**first["packet"], "investigation": history}
    finally:
        session.close()
    assert session.process.returncode == 0


def test_remote_model_command_preserves_source_and_arguments_below_linux_exec_limit(tmp_path, monkeypatch):
    script = tmp_path / "benchmark.py"
    script.write_text("#" + "🧪" * 40_000 + "\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    monkeypatch.setattr(live_repair, "__file__", str(script))
    arguments = ["--stream-in-pod", "gpt-6-luna", "low", "live-repair/v36"]
    command = live_repair.model_command(*arguments[1:], stream=True)
    assert all(len(argument.encode("utf-8")) < 131_072 for argument in command)
    executable_arguments = command[command.index("--") + 2:]
    response = subprocess.check_output([sys.executable, *executable_arguments], text=True)
    assert json.loads(response) == arguments


def test_mixed_notification_recovery_requires_both_independent_symptoms_to_clear():
    intact = {"status": 200, "duration_ms": 1, "notification_intact": True}
    slow = {**intact, "duration_ms": 3011}
    rejected = {"status": 503, "duration_ms": 1, "notification_intact": False}
    assert observed_mixed_notification_fault({"ready": 200, "samples": [rejected, slow]})
    assert not observed_mixed_notification_fault({"ready": 200, "samples": [rejected, intact]})
    assert notification_probe_recovered({"samples": [intact, intact]})
    assert not notification_probe_recovered({"samples": [intact, slow]})
    latest = [{"ready": 200, "samples": [intact, slow]}, {"ready": 200, "samples": [rejected, intact]}]
    assert notification_mixed_symptoms_persist(latest)
    assert not notification_mixed_symptoms_persist(latest[:1])
    assert not notification_mixed_symptoms_persist([latest[0], latest[0]])

    first = {"request": {"tool": "configure"},
             "result": {"accepted": True, "setting": "error_rate", "active_impact": ["latency_spike"]}}
    second = {"request": {"tool": "configure"},
              "result": {"accepted": True, "setting": "latency_spike", "active_impact": []}}
    attempt = {"protocol": "live-repair/v7", "successful_config_writes": 2, "investigation": [first, second]}
    assert recovery_action_matches(attempt)
    assert not recovery_action_matches({**attempt, "successful_config_writes": 1, "investigation": [second]})


def test_kubernetes_preflight_proof_requires_server_acceptance_without_a_persisted_resource():
    manifest = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress"}
    allowed = {"accepted": True, "resource_absent": True, "contract_ok": True, "observed": manifest}
    attempt = {"case": "KI-01", "protocol": "live-repair/v8", "resource_absent_after": True, "arm": "single_astra",
               "api_discovery": {"served_versions": ["networking.k8s.io/v1"]},
               "healthy_before": allowed, "fault_before": {"failure": "gvk_not_served"},
               "status": "verified_manifest_only", "manifest_probes": [allowed, allowed],
               "investigation": [{"request": {"tool": "configure"}, "result": allowed}]}
    assert kubernetes_preflight_proof(attempt)
    assert not kubernetes_preflight_proof({**attempt, "resource_absent_after": False})
    assert not kubernetes_preflight_proof({**attempt, "status": "verified_recovered"})
    assert not kubernetes_preflight_proof({**attempt, "case": "KW-04"})
    assert not kubernetes_preflight_proof({**attempt, "manifest_probes": [allowed]})


def test_webhook_preflight_keeps_rules_and_rejects_recovery_credit():
    from copy import deepcopy
    from evaluation.k8s_compatibility_trial import CASES, manifest_for

    case = CASES["KI-02"]
    expected = manifest_for(case, corrected=True)
    observation = {"accepted": True, "contract_ok": True, "resource_absent": True, "observed": expected}
    attempt = {"case": case.id, "protocol": case.protocol, "arm": "single_sol",
               "status": "verified_manifest_only", "resource_absent_after": True,
               "healthy_before": observation, "fault_before": {"failure": case.reason, "error_log": case.log},
               "api_discovery": {"served_versions": ["admissionregistration.k8s.io/v1"],
                                 "fixture_webhook_review_versions": ["v1"]},
               "investigation": [{"request": {"tool": "configure"}, "result": observation}],
               "manifest_probes": [observation, observation]}
    assert kubernetes_preflight_proof(attempt)
    assert not kubernetes_preflight_proof({**attempt, "status": "verified_recovered"})
    relaxed_policy = deepcopy(attempt)
    relaxed_policy["healthy_before"]["observed"]["webhooks"][0]["failurePolicy"] = "Ignore"
    assert not kubernetes_preflight_proof(relaxed_policy)


def test_pod_admission_broker_keeps_the_frozen_security_and_image_settings(monkeypatch):
    case = compatibility.CASES["KW-04"]
    expected = compatibility.manifest_for(case, corrected=True)
    submitted = []

    def check(case, candidate):
        submitted.append(candidate)
        return {"contract_ok": True}

    monkeypatch.setattr(compatibility, "check_manifest", check)
    grant_token = deepcopy(expected)
    grant_token["spec"]["automountServiceAccountToken"] = True
    rejected, _ = compatibility.operate(case, {"tool": "configure", "value": json.dumps(grant_token)}, {}, None)
    assert not rejected["accepted"]
    assert submitted == []

    defaults = deepcopy(expected)
    del defaults["spec"]["serviceAccountName"]
    _, candidate = compatibility.operate(case, {"tool": "configure", "value": json.dumps(defaults)}, {}, None)
    assert candidate == expected
    assert submitted == [expected]


def test_fixture_subprocess_program_compiles_with_the_scheduler_observer():
    compile(PROGRAM, "fixture subprocess", "exec")


def test_payment_compound_proof_requires_both_observed_failures_and_both_changes():
    first = {"ready": 200, "business": 503, "exception": "WorkerPoolExhausted"}
    masked = {"ready": 200, "business": 500, "exception": "TransientStateError"}
    record = {"fault_before": first, "masked_fault_observation": masked}
    assert observed_payment_pool_and_exception(record)
    assert not observed_payment_pool_and_exception({**record, "masked_fault_observation": first})
    changes = [
        {"request": {"tool": "configure"},
         "result": {"accepted": True, "setting": "worker_exhaustion", "active_impact": ["intermittent_exception"]}},
        {"request": {"tool": "configure"},
         "result": {"accepted": True, "setting": "intermittent_exception", "active_impact": []}},
    ]
    attempt = {"protocol": "live-repair/v10", "successful_config_writes": 2, "investigation": changes}
    assert recovery_action_matches(attempt)
    assert not recovery_action_matches({**attempt, "successful_config_writes": 1, "investigation": changes[:1]})


def test_native_payment_handoff_requires_fenced_intake_and_same_failed_requests():
    first_log = "initial payment error"
    fault = {"ready": 200, "business": 503, "exception": "WorkerPoolExhausted", "contract_ok": False}
    preflight = {"ready": 200, "business": 200, "ledger_intact": True, "contract_ok": True}
    intake = {"intake_state": "collecting", "initial_error_sha256": sha256(first_log.encode()).hexdigest(),
              "reports": [{"kind": "collect", "state": "source_pinning", "result_sha256": "b" * 64},
                          {"kind": "source", "state": "analysis_queued", "result_sha256": "c" * 64}]}
    record = {"case": "payment-27", "runtime_intake": intake, "initial_error_log": first_log, "fault_before": fault,
              "masked_fault_observation": {"ready": 200, "business": 500, "exception": "TransientStateError"},
              "healthy_before": preflight, "fixture_cleanup": [preflight] * 2, "application_state": "handoff",
              "reason_code": "high_risk_handoff", "successful_config_writes": 0, "calls": [{"role": "classifier"}],
              "fixture_resets": [{"accepted": True, "setting": "worker_exhaustion", "active_impact": ["intermittent_exception"]},
                                 {"accepted": True, "setting": "intermittent_exception", "active_impact": []}],
              "independent_probes": [fault] * 2,
              "runtime_configuration_before_cleanup": {"worker_exhaustion": {}, "intermittent_exception": {"ratio": 0.5}}}
    assert runtime_handoff_proof(record)
    assert not runtime_handoff_proof({**record, "runtime_intake": {**intake, "reports": intake["reports"][:1]}})
    assert not runtime_handoff_proof({**record, "independent_probes": [fault, preflight]})
    assert not runtime_handoff_proof({**record, "reason_code": "source_image_mismatch"})
    citation_failure = {**record, "reason_code": "citation_not_grounded"}
    assert not runtime_handoff_proof(citation_failure)
    citation_failure["calls"] = [{"role": "classifier", "citation_review": {
        "scope": "evidence_as_presented_to_model", "evidence_observed": True, "citations": [False, True],
    }}]
    assert runtime_handoff_proof(citation_failure)
    single = {**record, "case": "payment-02", "initial_log_observation": {"event": "http_access", "method": "GET", "status": 503},
              "runtime_configuration_before_cleanup": {"error_rate": {"ratio": 1.0}},
              "fixture_resets": [{"accepted": True, "setting": "error_rate", "active_impact": []}]}
    assert runtime_handoff_proof(single)
    assert not runtime_handoff_proof({**single, "initial_log_observation": None})

    cpu_normal = {"ready": 200, "business": 200, "liveness": 200, "same_instance": True,
                  "business_intact": True, "contract_ok": True, "sample_seconds": 2.0,
                  "cpu_seconds": 0.02, "cpu_core_fraction": 0.01}
    cpu_fault = {**cpu_normal, "contract_ok": False, "cpu_seconds": 1.8, "cpu_core_fraction": 0.9}
    cpu = {**record, "case": "payment-08", "protocol": "live-repair/v27", "healthy_before": cpu_normal,
           "fault_before": cpu_fault, "independent_probes": [cpu_fault] * 2, "fixture_cleanup": [cpu_normal] * 2,
           "first_observation": {"level": "ERROR", "service": "test-application", "event": "process_cpu_high",
                                 "sample_seconds": 2, "threshold_percent_of_core": 65},
           "fixture_resets": [{"accepted": True, "setting": "cpu_spin", "active_impact": []}],
           "runtime_configuration_before_cleanup": {"cpu_spin": {"threads": 1}}}
    assert runtime_handoff_proof(cpu)
    assert not runtime_handoff_proof({**cpu, "independent_probes": [cpu_normal, cpu_fault]})


def test_runtime_followup_relays_fresh_observation_through_the_product(monkeypatch, capsys):
    from datetime import datetime, timezone
    from app.llm.base import LLMResult

    first_log = "level=ERROR fixture payment status=503"
    source = "def read_payment():\n    return 503\n"
    packet = {"case": "payment-02", "source_path": "runtime.py", "source": source,
              "source_sha256": sha256(source.encode()).hexdigest(),
              "runtime_intake": {"versioned_source": source, "fixture_sha256": "d" * 64},
              "observations": {"first_log": first_log, "observed_health": {"business": 503}}}
    followup = {"observed_at": datetime.now(timezone.utc).isoformat(),
                "facts": {"observed_health": {"business": 503}, "worker_slots": {"available": 0}}}

    class Provider:
        def complete(self, role, prompt, schema, model, effort, timeout):
            assert role == "classifier"
            resumed = '"review_evidence"' in prompt
            if resumed:
                assert 'worker_slots' in prompt and 'followup:' in prompt and first_log in prompt
            data = {"level": 3 if resumed else 1, "confidence": .95 if resumed else .7,
                    "risk": "high" if resumed else "medium", "reason_ko": "결제 근거 확인",
                    "citations": [{"ref": "E1", "quote": first_log}]}
            return LLMResult(json.dumps(data), data, model=model, effort=effort)

    monkeypatch.setattr("app.llm.codex_cli.CodexCLIProvider", lambda *a, **k: Provider())
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(packet) + "\n" + json.dumps(followup) + "\n"))
    replay(interactive=True)
    request, result = map(json.loads, capsys.readouterr().out.splitlines())
    assert request["event"] == "runtime_followup"
    assert result["application_state"] == "handoff"
    assert result["followup_collection"]["report_state"] == "analysis_queued"
    assert len(result["followup_collection"]["evidence_refs"]) == 1
    assert len(result["calls"]) == 2


def test_successful_http_alone_cannot_prove_two_new_background_job_runs():
    fault = {"ready": 200, "business": 200,
             "job": {"status": "failed", "exception": "IndexRebuildError", "sequence": 2}}
    first_job = {"status": "passed", "sequence": 3, "observed_at_monotonic": 100.0,
                 "index_size": 6, "index_contract_ok": True}
    second_job = {**first_job, "sequence": 4, "observed_at_monotonic": 115.0}
    assert background_jobs_recovered([{"job": first_job}, {"job": second_job}], fault)
    assert not background_jobs_recovered([{"business": 200}, {"business": 200}], fault)
    assert not background_jobs_recovered([{"job": first_job}, {"job": first_job}], fault)
    assert not background_jobs_recovered([{"job": first_job}, {"job": {**second_job, "index_size": 5}}], fault)
    first_failure = {**fault, "job": {**fault["job"], "sequence": 3}}
    second_failure = {**fault, "job": {**fault["job"], "sequence": 4}}
    cleanup = [{"contract_ok": True, "job": {**first_job, "sequence": 5}},
               {"contract_ok": True, "job": {**second_job, "sequence": 6}}]
    data = {"case": "catalog-14"}
    assert runtime_handoff_observations(data, fault, {}, [first_failure, second_failure], cleanup)
    assert not runtime_handoff_observations(data, fault, {}, [second_failure, second_failure], cleanup)


def test_cost_target_is_unverified_when_only_a_subset_has_paired_repair_outcomes():
    def row(case, arm, status, price, high_calls):
        return {"case": case, "cohort": "application", "arm": arm, "status": status,
                "estimated_usd": price, "high_model_calls": high_calls,
                "total_tokens": 100, "wall_duration_ms": 10}

    rows = [
        row("real", "liveprobe", "verified_recovered", 0.2, 1),
        row("real", "single_astra", "verified_recovered", 0.4, 4),
        row("partial", "liveprobe", "analysis_replay_only", 0.01, 0),
        row("partial", "single_astra", "verified_recovered", 0.6, 7),
    ]
    report = paired_repair_coverage(rows)["paired_standalone_comparisons"]["single_astra"]
    assert report["paired_cases"] == ["real"]
    assert report["excluded_status_pairs"] == {"analysis_replay_only / verified_recovered": 1}
    assert report["estimated_usd"]["sample_reduction_percent"] == 50
    assert report["estimated_usd"]["verdict"] == "unverified"
    assert report["high_model_calls"]["sample_reduction_percent"] == 75
    assert report["high_model_calls"]["verdict"] == "unverified"


def test_product_nondetection_is_excluded_from_inference_cost_but_keeps_physical_recovery():
    baseline = {"cohort": "basic", "status": "verified_natural_recovery",
                "first_error_to_operation_end_ms": 28_000}
    missing = {"case": "latency", "arm": "liveprobe", **baseline, "estimated_usd": 0.0, "total_tokens": 0,
               "reason_code": "latency_below_alert_threshold", "high_model_calls": 0}
    verified = {"case": "rule-only", "arm": "liveprobe", **baseline, "estimated_usd": 0.01, "total_tokens": 500,
                "reason_code": "pool_expired_with_product_incident", "high_model_calls": 0}
    singles = [{"case": row["case"], "arm": "single_astra", **baseline,
                "estimated_usd": 0.1, "total_tokens": 1_000, "high_model_calls": 2} for row in (missing, verified)]
    coverage = paired_repair_coverage([missing, verified, *singles])
    assert coverage["liveprobe_nondetection_cases"] == ["latency"]
    assert coverage["liveprobe_detection_exclusions"] == {"latency_below_comparison_threshold": ["latency"]}
    report = coverage["paired_standalone_comparisons"]["single_astra"]
    assert report["paired_outcomes"] == {"verified_natural_recovery / verified_natural_recovery": 2}
    assert report["estimated_usd"]["measured_pairs"] == 1
    assert report["high_model_calls"]["measured_pairs"] == 1
    assert report["first_error_to_operation_end_ms"]["measured_pairs"] == 2
    assert report["estimated_usd"]["verdict"] == "unverified"


def test_verified_recovery_time_requires_the_independent_measurement_in_both_arms():
    metric = 'first_error_to_verified_recovery_ms'
    base = {'cohort': 'basic', 'status': 'verified_natural_recovery', 'wall_duration_ms': 10000}
    rows = [
        {**base, 'case': 'new', 'arm': 'liveprobe', metric: 1000},
        {**base, 'case': 'new', 'arm': 'single_astra', metric: 2000},
        {**base, 'case': 'historic', 'arm': 'liveprobe'},
        {**base, 'case': 'historic', 'arm': 'single_astra', metric: 1000},
    ]
    report = paired_repair_coverage(rows)['paired_standalone_comparisons']['single_astra']
    assert report[metric] == {'measured_pairs': 1, 'liveprobe_sum': 1000, 'standalone_sum': 2000,
                              'sample_reduction_percent': 50, 'complete': False}
    assert report['wall_duration_ms']['measured_pairs'] == 2


def test_resource_cost_for_common_recovery_excludes_natural_and_one_sided_outcomes():
    rows = []
    fixtures = [
        ("restored", "verified_recovered", "verified_recovered", 3, 5),
        ("self_resolved", "verified_natural_recovery", "verified_natural_recovery", 0, 20),
        ("liveprobe_failed", "unrecovered", "verified_recovered", 1, 30),
    ]
    for cid, live_status, model_status, live_cost, model_cost in fixtures:
        rows.extend([{"case": cid, "cohort": "basic", "arm": "liveprobe",
                      "status": live_status, "estimated_usd": live_cost},
                     {"case": cid, "cohort": "basic", "arm": "single_astra",
                      "status": model_status, "estimated_usd": model_cost}])
    report = paired_repair_coverage(rows)["paired_standalone_comparisons"]["single_astra"]
    assert report["estimated_usd"]["measured_pairs"] == 3
    recovery = report["common_verified_recovery"]
    assert recovery["cases"] == ["restored"]
    assert recovery["cases_by_cohort"] == {"basic": 1}
    assert recovery["metrics"]["estimated_usd"] == {"measured_pairs": 1, "liveprobe_sum": 3,
        "standalone_sum": 5, "sample_reduction_percent": 40.0}
    assert recovery["metrics"]["total_tokens"]["sample_reduction_percent"] is None


def test_reviewed_retry_uses_independent_failure_evidence_and_keeps_the_original(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema": "liveprobe-comparison-catalog/v1",
                                   "modes": [{"id": "single_astra", "model": "proof", "effort": "xhigh"}],
                                   "basic": {"cases": [{"id": "test-case"}]},
                                   "kubernetes_new": {"cases": []}, "application": {"cases": []}}))
    first = tmp_path / "live-repair-v1" / "test-case" / "single_astra.json"
    first.parent.mkdir(parents=True)
    record = {"case": "test-case", "arm": "single_astra", "model": "proof", "effort": "xhigh",
              "protocol": "live-repair/v1", "input_sha256": "0" * 64, "fixture_sha256": "1" * 64,
              "healthy_before": {"contract_ok": True}, "fault_before": {"contract_ok": False}, "status": "unrecovered",
              "wall_duration_ms": 5000}
    first.write_text(json.dumps(record))
    retry = tmp_path / "retry.json"
    retry.write_text(json.dumps({**record, "wall_duration_ms": 3000,
                                 "independent_probes": [{"contract_ok": False}] * 2}))
    assert list(repair_rows(catalog, tmp_path, reviewed_retries=[retry]))[0]["wall_duration_ms"] == 3000
    assert json.loads(first.read_text())["wall_duration_ms"] == 5000
    with pytest.raises(ValueError, match="independent recovery or failure evidence"):
        list(repair_rows(catalog, tmp_path, retries=[retry]))
    retry.write_text(json.dumps({**record, "independent_probes": [{"contract_ok": False}]}))
    with pytest.raises(ValueError, match="independent recovery or failure evidence"):
        list(repair_rows(catalog, tmp_path, reviewed_retries=[retry]))


def test_application_audit_binds_the_selected_retry_and_rejects_ambiguity(tmp_path):
    from evaluation.application_comparison_audit import MATCH_FIELDS, selected_receipts

    record = {"case": "test-case", "arm": "liveprobe", "protocol": "live-repair/v1", "status": "unrecovered",
              "reason_code": "second", "wall_duration_ms": 1000, "calls": [{}]}
    row = {key: str(record[key]) if key in record else "" for key in MATCH_FIELDS}
    row["model_calls"] = "1"
    directory = tmp_path / "live-repair-v1" / "test-case"
    nested = directory / "retry" / "model.json"
    nested.parent.mkdir(parents=True)
    (directory / "liveprobe.json").write_text(json.dumps({**record, "reason_code": "first"}))
    nested.write_text(json.dumps(record))
    _, selected, identity = next(selected_receipts([row], tmp_path))
    assert selected == record and identity["path"].endswith("/retry/model.json")
    (nested.parent / "duplicate.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="found 2"):
        list(selected_receipts([row], tmp_path))


def test_application_audit_requires_log_hash_accepted_change_and_two_independent_observations():
    from evaluation.application_comparison_audit import receipt_outcome
    from evaluation.live_repair import TASK

    first = "initial error"
    healthy = {"contract_ok": True, "business": 200, "ready": 200}
    record = {"case": "configuration-case", "arm": "single_astra", "protocol": "live-repair/v1",
              "status": "verified_recovered", "initial_error_log": first,
              "input_sha256": sha256((TASK + first).encode()).hexdigest(), "successful_config_writes": 1,
              "investigation": [{"request": {"tool": "configure"}, "result": {"accepted": True}}],
              "verified": [healthy, healthy.copy()]}
    assert receipt_outcome(record)["independent_recovery_verified"]
    for override, error in [({"initial_error_log": "new log"}, "opening input"),
                            ({"successful_config_writes": 2}, "accepted mutations"),
                            ({"verified": [healthy]}, "independent probe")]:
        with pytest.raises(ValueError, match=error):
            receipt_outcome({**record, **override})
