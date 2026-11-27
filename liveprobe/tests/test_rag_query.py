"""Retrieval query construction: the historical template is unchanged; `findings` only adds structured phrases."""
import pytest

from app import rag_query as Q

FEATURES = {"waiting_reasons": ["ImagePullBackOff"], "service_has_no_endpoints": True,
            "dependent_callers_failing": ["shop-order"], "oom_killed": False, "latency_signal": True,
            "signal_reasons": ["TargetConnectionPoolExhausted"]}
FINDINGS = [{"kind": "db_pool_state", "active": 6, "max": 6, "active_trend": {"resets_to_minimum": 1}},
            {"kind": "recent_restart", "liveness_failures": 3}]


def test_template_is_the_previous_engine_query():
    assert Q.template_query("shop-stock", FEATURES, FINDINGS, []) == (
        "shop-stock incident; pods ImagePullBackOff image pull failure after rollout; "
        "service has no endpoints selector mismatch; callers return 5xx shop-order; "
        "high latency connection pool exhaustion slow responses; "
        "signature liveness_restart pool_resets pool_saturated recent_restart")
    assert Q.template_query("w", {}, [], []) == "w incident"


def test_findings_mode_adds_plain_phrases_and_signal_words_only():
    q = Q.findings_query("shop-stock", FEATURES, FINDINGS, [])
    assert q.startswith(Q.template_query("shop-stock", FEATURES, FINDINGS, []))
    assert "observed liveness probe failures restart, connection pool resets after restart, connection pool " \
           "exhausted saturated, container restarted recently" in q
    assert q.endswith("signals target connection pool exhausted")
    assert Q.findings_query("w", {"family_hint": "image_pull"}, [], []) == "w incident"   # label keys are ignored


def test_signal_words_are_bounded_to_safe_names():
    assert Q.signal_words(["OrderHigh5xxRate", "OOMKilled", "x; DROP TABLE", 7, "OrderHigh5xxRate"]) == [
        "order high 5 xx rate", "oom killed"]
    assert len(Q.signal_words([f"Alert{i}" for i in range(20)])) == 8


def test_unknown_mode_is_rejected():
    assert Q.build("template", "w", {}, [], []) == "w incident"
    with pytest.raises(ValueError):
        Q.build("hyde", "w", {}, [], [])
