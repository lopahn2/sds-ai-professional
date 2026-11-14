"""P18 campaign: latency_signal must come from observed values, not from the mere presence of metric keys."""
from app.evidence import Bundle
from app.router import extract_features, rules_decide


def _bundle(metrics: dict) -> Bundle:
    b = Bundle(namespace="prd", workload="payment")
    b.deployment = {"name": "payment", "revisions": [], "images": ["x"], "current_revision": "1"}
    b.pods = [{"name": "payment-1", "containers": [{"ready": True, "restart_count": 0}]}]
    b.service = {"selector": {"app": "payment"}, "matching_pods": 1, "ready_endpoints": 1}
    base = {"http_5xx_rate_per_s": {"payment": 0.4}, "http_rate_per_s": {"payment": 1.0}, "restarts": {},
            "mean_latency_ms_1m": {}, "db_pool_active": {}, "db_pool_max": {}, "db_pool_pending_threads": {},
            "db_pool_acquire_timeouts_5m": {}}
    base.update(metrics)
    b.metrics = base
    return b


def test_fast_responses_are_not_a_latency_signal():
    f = extract_features(_bundle({"mean_latency_ms_1m": {"payment": 12.0}}))
    assert f["latency_signal"] is False
    assert rules_decide(f)[0] == "COMPLEX"


def test_slow_mean_latency_is_a_latency_signal():
    f = extract_features(_bundle({"mean_latency_ms_1m": {"payment": 1800.0}}))
    assert f["latency_signal"] is True
    assert rules_decide(f)[0] == "NORMAL"


def test_pool_pressure_is_a_latency_signal():
    for extra in ({"db_pool_pending_threads": {"payment": 3}},
                  {"db_pool_acquire_timeouts_5m": {"payment": 2.0}},
                  {"db_pool_active": {"payment": 5}, "db_pool_max": {"payment": 5}}):
        assert extract_features(_bundle(extra))["latency_signal"] is True, extra


def test_idle_pool_is_not_a_latency_signal():
    f = extract_features(_bundle({"db_pool_active": {"payment": 1}, "db_pool_max": {"payment": 5},
                                  "db_pool_pending_threads": {"payment": 0}, "db_pool_acquire_timeouts_5m": {"payment": 0.0}}))
    assert f["latency_signal"] is False


def test_legacy_metric_keys_still_count_when_present():
    # Older bundles keyed latency/pool facts directly; those keys stay a signal (backward compatible).
    f = extract_features(_bundle({"inventory_latency_p99": {"inventory": 2.0}}))
    assert f["latency_signal"] is True
