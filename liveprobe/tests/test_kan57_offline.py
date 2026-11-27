"""KAN-57 synthetic, offline routing/safety contracts; not live E2E evidence."""

import pytest

from app.evidence import EvidenceItem
from app.router import decide
from test_router_policy import family2, family3
from test_router_workflows import family1, family4


@pytest.mark.parametrize(
    "factory,signals,expected,feature",
    [
        (family1, ["ImagePullBackOff"], "SIMPLE", "image_pull_failing"),
        (family2, ["TargetWorkloadOOMKilled"], "NORMAL", "oom_killed"),
        (family3, ["TargetHighLatency"], "NORMAL", "latency_signal"),
        (family4, ["TargetServiceNoEndpoints"], "HIGH_RISK", "service_selector_matches_zero_pods"),
    ],
)
def test_four_synthetic_families_keep_traceable_policy(factory, signals, expected, feature):
    decision = decide(factory(), 15, signal_reasons=signals)
    assert decision.level == expected
    assert decision.workflow == expected.lower() + "_group"
    assert decision.features[feature] is True
    assert decision.features["signal_reasons"] == signals
    assert decision.rationale.strip()
    assert decision.policy is not None
    assert decision.policy["version"] == "v2"
    assert decision.policy["selected_model"]


@pytest.mark.parametrize("factory", [family1, family2, family3])
def test_classifier_unavailable_retains_rules_floor_after_bounded_retry(factory):
    def unavailable(_prompt):
        raise TimeoutError("synthetic classifier outage")

    decision = decide(factory(), 15, unavailable)
    assert decision.level == 'NORMAL'
    assert decision.decided_by == "rules_fallback"
    assert decision.policy is not None
    assert not decision.policy.get("handoff_only")
    assert len(decision.features['classifier_attempts']) == 2


@pytest.mark.parametrize("factory", [family1, family2, family3])
def test_classifier_without_explanation_retries_then_uses_rules_floor(factory):
    decision = decide(factory(), 15, lambda _: {
        "level": "SIMPLE", "rationale": " \n ", "confidence": 0.95,
    })
    assert decision.level == 'NORMAL'
    assert decision.decided_by == "rules_fallback"
    assert decision.policy is not None
    assert not decision.policy.get("handoff_only")


def test_synthetic_f2_f3_signal_names_without_catalog_truth():
    oom = family2()
    oom.events = []
    oom.pods[0]["containers"][0]["waiting_reason"] = None
    latency = family3()
    latency.events = []
    for bundle, signal, key in [
        (oom, "TargetWorkloadOOMKilled", "oom_killed"),
        (latency, "TargetHighLatency", "latency_signal"),
    ]:
        decision = decide(bundle, 15, signal_reasons=[signal])
        assert decision.level == "NORMAL"
        assert decision.features[key]


def test_synthetic_protected_resource_cannot_be_downgraded():
    bundle = family3()
    bundle.items.append(EvidenceItem("k8s_event", "liveprobe-target/Node/n", {}))
    decision = decide(bundle, 15, lambda _: {
        "level": "SIMPLE", "rationale": "synthetic downgrade", "confidence": 1,
    })
    assert decision.level == "HIGH_RISK"
    assert decision.features["protected_resource"] is True
    assert "protected resource implicated" in decision.rationale
    assert decision.decided_by == "rules"
