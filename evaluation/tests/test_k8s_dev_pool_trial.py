from datetime import datetime, timezone
import hashlib
import json

import pytest

from evaluation.comparison_summary import dev_pool_record, repair_rows
from evaluation.dev_pool_case import (
    POOL_CASES, POOL_FIXTURES, POOL_PROTOCOL, ROOT, load_pool_case, operation_window, pool_log_evidence,
)
from evaluation.live_repair import TASK


OP = {"mode": "pool_exhaust", "pool_max": 2, "held_connections": 2,
      "operation_id": "17a69ee9-d49b-4b52-a6e7-c2e8ea6fef72",
      "accepted_at": "2026-11-24T00:00:00Z", "expires_at": "2026-11-24T00:00:30Z"}
ERROR = "stock lookup db_error item=a error=CannotGetJdbcConnectionException"


def test_pool_catalog_and_operation_pins():
    assert [(load_pool_case(cid).application, load_pool_case(cid).connections) for cid in POOL_CASES] == [
        ("inventory", 2), ("order", 8), ("inventory", 6),
    ]
    case = load_pool_case("F3-02")
    assert (operation_window(case, OP)[1] - operation_window(case, OP)[0]).total_seconds() == 30
    with pytest.raises(ValueError, match="approved fixture"):
        operation_window(case, {**OP, "held_connections": 3})


def test_pool_logs_keep_only_actual_stock_error_and_matching_terminal_event():
    scoped = {"mode": "pool_exhaust", "chaos_event": "expired", "operation_id": OP["operation_id"]}
    fixture_logs = [
        {"@timestamp": "2026-11-23T23:59:59Z", "message": ERROR},
        {"@timestamp": "2026-11-24T00:00:02Z", "message": "unrelated exception"},
        {"@timestamp": "2026-11-24T00:00:04Z", "message": ERROR},
        {"@timestamp": "2026-11-24T00:00:30.010Z", **scoped, "operation_id": "other"},
        {"@timestamp": "2026-11-24T00:00:30.020Z", **scoped},
    ]
    start, end = pool_log_evidence(load_pool_case("F3-02"), OP, "\n".join(map(json.dumps, fixture_logs)))
    assert start == {"message": ERROR, "timestamp": "2026-11-24T00:00:04+00:00"}
    assert end == [{"event": "expired", "timestamp": "2026-11-24T00:00:30.020000+00:00"}]


def pool_receipt():
    probes = [{"ready": 200, "business": 200, "order_dependency": 200,
               "contract_ok": True, "endpoint_uid": "inventory-pod"}] * 2
    return {"case": "F3-02", "arm": "liveprobe", "protocol": POOL_PROTOCOL, "status": "pool_observed",
            "target": "dev/Service/lp-eval-inventory-pulse", "case_sha256": load_pool_case("F3-02").case_sha256,
            "fixture_sha256": "a" * 64, "input_sha256": hashlib.sha256((TASK + ERROR).encode()).hexdigest(),
            "duration_seconds": 30, "operation": OP, "healthy_before": {"contract_ok": True, "probes": probes},
            "fault_before": {"contract_ok": False, "error": ERROR, "http_status": 503, "order_dependency": 500,
                             "endpoint_uid": "inventory-pod", "held_connections": 2,
                             "first_error_at": "2026-11-24T00:00:02Z", "observed_at": "2026-11-24T00:00:03Z"},
            "restored": probes, "recovered_at": "2026-11-24T00:00:33Z", "incidents": [],
            "observation_seconds_after_recovery": 75,
            "termination_events": [{"event": "expired", "timestamp": "2026-11-24T00:00:30.010Z"}],
            "pool_active_samples": [{"value": 2, "timestamp": datetime(2026, 11, 24, 0, 0, 5, tzinfo=timezone.utc).timestamp()}]}


def test_pool_product_without_metrics_is_observation_only():
    actual = pool_receipt()
    accepted = dev_pool_record(actual, "liveprobe")
    assert accepted["status"] == "verified_natural_recovery"
    assert accepted["first_error_to_operation_end_ms"] == 28010
    assert accepted["wall_duration_ms"] is None
    assert dev_pool_record({**actual, "pool_active_samples": []}, "liveprobe")["status"] == "product_observation_only"
    with pytest.raises(ValueError, match="unrelated incident"):
        dev_pool_record({**actual, "incidents": [{"signal_reasons": ["NoEndpoints"], "executed_actions": 0}]}, "liveprobe")


def test_pool_event_counts_calls_without_attributing_natural_expiry_to_product():
    actual = pool_receipt()
    actual.update(incidents=[{"status": "handoff", "signal_reasons": ["TargetConnectionPoolExhausted"],
                             "executed_actions": 0, "calls": [{"input_tokens": 900, "output_tokens": 20,
                                                                "duration_ms": 510, "estimated_usd": 0.1}]}],
                  product_terminal_observed_at="2026-11-24T00:00:40Z")
    accepted = dev_pool_record(actual, "liveprobe")
    assert accepted["status"] == "verified_natural_recovery"
    assert accepted["wall_duration_ms"] == 38000 and accepted["usage"] == {"input_tokens": 900, "output_tokens": 20}
    assert not accepted.get("verified_dev_action")


def test_six_slot_pool_receipt_requires_six_observed_connections_and_its_own_target():
    case = load_pool_case("F3-14")
    actual = pool_receipt()
    actual.update(case=case.case_id, case_sha256=case.case_sha256,
                  target=f"dev/Service/{POOL_FIXTURES[case.case_id]}",
                  operation={**OP, "pool_max": 6, "held_connections": 6})
    with pytest.raises(ValueError, match="pool hold"):
        dev_pool_record(actual, "liveprobe")
    actual["fault_before"]["held_connections"] = 6
    assert dev_pool_record(actual, "liveprobe")["status"] == "product_observation_only"
    actual["pool_active_samples"][0]["value"] = 6
    assert dev_pool_record(actual, "liveprobe")["status"] == "verified_natural_recovery"
    actual["incidents"] = [{"status": "recovery_review", "signal_reasons": ["TargetConnectionPoolExhausted"],
                            "executed_actions": 0, "first_pool_signal_at": "2026-11-23T23:59:59Z"}]
    actual["product_terminal_observed_at"] = "2026-11-24T00:00:40Z"
    with pytest.raises(ValueError, match="signal preceded"):
        dev_pool_record(actual, "liveprobe")
    actual["incidents"][0]["first_pool_signal_at"] = "2026-11-24T00:00:12Z"
    assert dev_pool_record(actual, "liveprobe")["status"] == "verified_natural_recovery"
    with pytest.raises(ValueError, match="pool hold"):
        dev_pool_record({**actual, "target": "dev/Service/inventory"}, "liveprobe")
    with pytest.raises(ValueError, match="pool hold"):
        dev_pool_record({**actual, "case_sha256": load_pool_case("F3-02").case_sha256}, "liveprobe")


def test_pool_standalone_action_must_match_the_logged_reset_and_probe():
    actual = pool_receipt()
    actual.update(arm="single_sol", lease_released=True, model_evaluation_duration_ms=8500,
                  calls=[{"status": "ok", "model": "gpt-6.2-sol", "effort": "high"}],
                  investigation=[{"request": {"tool": "configure"}, "result": {"accepted": True}}],
                  standalone_action={"accepted": True, "action": "scoped_operation_reset", "operation_id": OP["operation_id"]},
                  verified_before_cleanup=actual["restored"],
                  termination_events=[{"event": "reset", "timestamp": "2026-11-24T00:00:10Z"}])
    assert dev_pool_record(actual, "single_sol")["verified_dev_action"] is True
    with pytest.raises(ValueError, match="scoped completion"):
        dev_pool_record({**actual, "standalone_action": {**actual["standalone_action"], "operation_id": "different"}},
                        "single_sol")


def test_order_pool_receipt_requires_the_order_log_and_healthy_inventory():
    case = load_pool_case("F3-08")
    actual = pool_receipt()
    actual.update(case=case.case_id, case_sha256=case.case_sha256,
                  target=f"dev/Service/{POOL_FIXTURES[case.case_id]}",
                  operation={**OP, "pool_max": 8, "held_connections": 8})
    actual["fault_before"].update(held_connections=8, order_lookup=200, inventory_lookup=200)
    with pytest.raises(ValueError, match="pool hold"):
        dev_pool_record(actual, "liveprobe")
    error = "order db lookup failed item=a error=CannotGetJdbcConnectionException"
    actual["fault_before"]["error"] = error
    actual["input_sha256"] = hashlib.sha256((TASK + error).encode()).hexdigest()
    actual["pool_active_samples"][0]["value"] = 8
    assert dev_pool_record(actual, "liveprobe")["status"] == "verified_natural_recovery"
    actual["fault_before"]["inventory_lookup"] = 503
    with pytest.raises(ValueError, match="independent observations"):
        dev_pool_record(actual, "liveprobe")


def test_pool_protocol_is_counted_by_the_actual_comparison_reader(tmp_path):
    case = tmp_path / POOL_PROTOCOL.replace("/", "-") / "F3-02"
    case.mkdir(parents=True)
    (case / "liveprobe.json").write_text(json.dumps(pool_receipt()), encoding="utf-8")
    ledger = repair_rows(ROOT / "evaluation/catalogs/comparative-2026-11-23.json", tmp_path)
    rows = {r["arm"]: r for r in ledger if r["case"] == "F3-02"}
    assert rows["liveprobe"]["status"] == "verified_natural_recovery"
    assert rows["liveprobe"]["first_error_to_operation_end_ms"] == 28010
    assert rows["single_astra"]["status"] == "pending"
