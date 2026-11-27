from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app import db
from app.config import AppConfig, Settings
from app.engine import Engine
from app.flowgraph import _evidence_card, build_flow
from app.hpa_metrics import API_SERVICE, RECOVERY_LABEL, HpaMetricsSignals, Outage
from app.hpa_recovery import HpaRecovery
from app.incidents import HPA_METRICS_BLOCKED, HPA_METRICS_SOURCE, ingest
from app.lifecycle import transition
from app.models import Action, AgentRun, AuditLog, Evidence, Incident, IncidentEvent, RoutingDecision
from app.narrative import stage_of
from app.safety import needs_cluster_authority
from conftest import needs_db


AT = datetime(2026, 11, 24, 11, 33, 38, tzinfo=timezone.utc)


def provider():
    return {"metadata": {"name": API_SERVICE, "uid": "provider-uid"},
            "status": {"conditions": [{"type": "Available", "status": "False", "reason": "MissingEndpoints",
                                       "lastTransitionTime": (AT - timedelta(seconds=3)).isoformat()}]}}


def autoscaler(ns):
    return {"metadata": {"name": "order-hpa", "namespace": ns, "uid": f"{ns}-hpa-uid", "generation": 1},
            "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "order"},
                     "metrics": [{"type": "Resource", "resource": {"name": "cpu"}}]},
            "status": {"observedGeneration": 1, "conditions": [{"type": "ScalingActive", "status": "False",
                       "reason": "FailedGetResourceMetric", "lastTransitionTime": AT.isoformat(),
                       "message": "unable to fetch metrics from resource metrics API: service unavailable"}]}}


def recovered_provider():
    value = provider()
    value["status"]["conditions"][0].update(status="True", reason="Passed",
                                             lastTransitionTime=(AT + timedelta(seconds=20)).isoformat())
    return value


def recovered_autoscaler(ns, utilization=60):
    value = autoscaler(ns)
    value["status"]["conditions"][0].update(status="True", reason="ValidMetricFound",
                                             lastTransitionTime=(AT + timedelta(seconds=24)).isoformat())
    # The controller does not always emit observedGeneration.
    value["status"].pop("observedGeneration")
    value["status"]["currentMetrics"] = [{"type": "Resource", "resource": {"name": "cpu",
        "current": {"averageUtilization": utilization}}}]
    return value


def captured_outage(namespaces):
    kube = SimpleNamespace(get_resource_metrics_api=provider, list_autoscalers=lambda ns: [autoscaler(ns)])
    signals = []
    watcher = HpaMetricsSignals(kube, namespaces, signals.append, max_gap=60)
    watcher.poll_once(AT)
    watcher.poll_once(AT + timedelta(seconds=15))
    return signals[0]


def test_one_outage_combines_active_namespaces_and_requires_repeat():
    kube = SimpleNamespace(get_resource_metrics_api=provider, list_autoscalers=lambda ns: [autoscaler(ns)])
    signals = []
    watcher = HpaMetricsSignals(kube, ["dev", "prd"], signals.append, max_gap=60)
    assert watcher.poll_once(AT)["state"] == "degraded"
    assert not signals
    watcher.poll_once(AT + timedelta(seconds=15))
    watcher.poll_once(AT + timedelta(seconds=30))
    signal, = signals
    assert (signal.namespace, signal.kind, signal.name, signal.reason, signal.source) == (
        "dev", "APIService", API_SERVICE, HPA_METRICS_BLOCKED, HPA_METRICS_SOURCE)
    assert [(r["namespace"], r["target_name"]) for r in signal.detail["affected_hpas"]] == [("dev", "order"), ("prd", "order")]
    assert signal.detail["provider_reason"] == "MissingEndpoints"


@pytest.mark.parametrize("different", ["custom_metric", "stale", "different_hpa", "wrong_namespace", "absent_provider",
                                      "read_failed", "other_metric_error", "gap", "updated_generation", "available"])
def test_does_not_turn_unconfirmed_metrics_into_an_incident(different):
    first, second = autoscaler("dev"), autoscaler("dev")
    registry = provider()
    later = AT + timedelta(seconds=15)
    hpa_status = second["status"]["conditions"][0]
    if different == "custom_metric":
        second["spec"]["metrics"] = [{"type": "External"}]
    elif different == "stale":
        hpa_status["lastTransitionTime"] = (AT - timedelta(minutes=2)).isoformat()
    elif different == "different_hpa":
        second["metadata"]["uid"] = "recreated-hpa-uid"
    elif different == "wrong_namespace":
        second["metadata"]["namespace"] = "stg"
    elif different == "other_metric_error":
        hpa_status["message"] = "missing cpu request in container"
    elif different == "gap":
        later = AT + timedelta(seconds=61)
    elif different == "updated_generation":
        second["metadata"]["generation"] = 2
    reads = [[first], [second]]
    if different == "read_failed":
        reads[-1] = PermissionError("unobserved")
    kube = SimpleNamespace(get_resource_metrics_api=Mock(return_value=registry), list_autoscalers=Mock(side_effect=reads))
    signals = []
    watcher = HpaMetricsSignals(kube, ["dev"], signals.append, max_gap=60)
    watcher.poll_once(AT)
    if different == "absent_provider":
        kube.get_resource_metrics_api.return_value = None
    if different == "available":
        registry["status"]["conditions"][0]["status"] = "True"
    observed = watcher.poll_once(later)
    if different == "read_failed":
        assert observed["unobserved_namespaces"] == ["dev"]
    assert not signals


@pytest.mark.parametrize("gap", ["provider_failed_again", "hpa_read_denied", "provider_read_denied", "paused_namespace",
    "active_without_resource_metric", "recreated_hpa", "recreated_provider", "generation_changed", "other_target",
    "stale_recovery", "empty_quantity", "metric_missing", "unseen_hpa", "too_long"])
def test_requires_consecutive_fresh_samples_of_the_original_provider_and_every_hpa(gap):
    signal = captured_outage(["dev", "prd"])
    recovery = Mock()
    recovery.pending.side_effect = lambda uid: Outage(1, 2, signal.detail) if uid == "provider-uid" else None
    kube = SimpleNamespace(get_resource_metrics_api=Mock(return_value=recovered_provider()),
                           list_autoscalers=Mock(side_effect=lambda ns: [recovered_autoscaler(ns)]))
    namespaces = ["dev", "prd"]
    watcher = HpaMetricsSignals(kube, namespaces, Mock(), max_gap=60, recovery=recovery)
    assert watcher.poll_once(AT + timedelta(seconds=30))["hpa_recovery"] == "confirming"
    hpa, api = recovered_autoscaler("prd"), recovered_provider()
    if gap == "provider_failed_again":
        api = provider()
    elif gap == "recreated_provider":
        api["metadata"]["uid"] = "replacement"
    elif gap == "provider_read_denied":
        kube.get_resource_metrics_api.side_effect = PermissionError
    elif gap == "paused_namespace":
        namespaces.remove("prd")
    elif gap == "active_without_resource_metric":
        hpa["status"]["currentMetrics"] = [{"type": "External", "external": {"current": {"value": "1"}}}]
    elif gap == "recreated_hpa":
        hpa["metadata"]["uid"] = "replacement"
    elif gap == "generation_changed":
        hpa["metadata"]["generation"] = 2
    elif gap == "other_target":
        hpa["spec"]["scaleTargetRef"]["name"] = "payment"
    elif gap == "stale_recovery":
        hpa["status"]["conditions"][0]["lastTransitionTime"] = AT.isoformat()
    elif gap == "empty_quantity":
        hpa["status"]["currentMetrics"][0]["resource"]["current"] = {}
    elif gap == "metric_missing":
        hpa["status"]["currentMetrics"][0]["resource"]["name"] = "memory"

    def list_autoscalers(ns):
        if ns == "dev":
            return [recovered_autoscaler(ns)]
        if gap == "hpa_read_denied":
            raise PermissionError
        return [] if gap == "unseen_hpa" else [hpa]

    kube.get_resource_metrics_api.return_value = api
    kube.list_autoscalers.side_effect = list_autoscalers
    watcher.poll_once(AT + timedelta(seconds=91 if gap == "too_long" else 45))
    recovery.record.assert_not_called()
    if gap == "paused_namespace":
        assert kube.list_autoscalers.call_count == 2


@needs_db
def test_persisted_metrics_failure_locks_out_writes_and_deduplicates_across_worker_restart(db_url, monkeypatch):
    cfg = AppConfig(target_namespaces=["dev"], historical_namespaces=["prd", "stg"])
    kube = SimpleNamespace(get_resource_metrics_api=provider, list_autoscalers=lambda ns: [autoscaler(ns)])
    signals = []
    watcher = HpaMetricsSignals(kube, ["dev"], signals.append, max_gap=60)
    watcher.poll_once(AT)
    watcher.poll_once(AT + timedelta(seconds=15))
    signal, = signals
    with db.session() as session:
        incident, outcome = ingest(session, cfg, signal, AT, kube=kube)
        assert outcome == "created"
        incident_id = incident.id
    engine = Engine(cfg, Settings(), kube, SimpleNamespace(name="unit-test"), None)
    monkeypatch.setattr(engine, "_collect_or_reuse", lambda *args: pytest.fail("cluster boundary uses captured conditions"))
    engine.process(incident_id)
    with db.session() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == "handed_off" and needs_cluster_authority(incident)
        route = session.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == incident_id)).one()
        assert route.features["reason_code"] == "resource_metrics_cluster_authority"
        assert route.policy["handoff_only"] is True
        evidence = session.scalars(select(Evidence).where(Evidence.incident_id == incident_id)).one()
        assert evidence.source == HPA_METRICS_SOURCE and evidence.target == f"APIService/{API_SERVICE}"
        assert evidence.payload["affected_hpas"] == signal.detail["affected_hpas"]
        card, = _evidence_card([evidence], [])["items"]
        assert "dev/order-hpa" in card["summary_ko"] and "업무 요청 영향은 미관측" in card["summary_ko"]
        assert session.scalar(select(AgentRun.id).where(AgentRun.incident_id == incident_id)) is None
        assert session.scalar(select(Action.id).where(Action.incident_id == incident_id)) is None
    earlier = HpaRecovery(cfg).pending("provider-uid")
    with db.session() as session:
        same, outcome = ingest(session, cfg, signal, AT + timedelta(days=1), kube=kube)
        assert (same.id, outcome) == (incident_id, "suppressed")
        later_signal = replace(signal, detail={**signal.detail, "provider_since": (AT + timedelta(days=1)).isoformat()})
        new, outcome = ingest(session, cfg, later_signal, AT + timedelta(days=1), kube=kube)
        assert outcome == "created" and new.id != incident_id
        newer_id = new.id
    engine.process(newer_id)
    recovery = HpaRecovery(cfg)
    assert recovery.pending("provider-uid").incident_id == newer_id
    assert not recovery.record(earlier, [])


@needs_db
def test_recovery_after_restart_persists_both_observations_and_waits_for_one_operator_decision(db_url):
    from test_operator_accountability import client

    cfg = AppConfig(target_namespaces=["dev"], historical_namespaces=["prd"])
    kube = SimpleNamespace(get_resource_metrics_api=recovered_provider,
                           list_autoscalers=Mock(side_effect=[[recovered_autoscaler("dev", 70)],
                                                           [recovered_autoscaler("dev", 55)]]))
    signal = captured_outage(["dev"])
    with db.session() as session:
        incident, _ = ingest(session, cfg, signal, AT, kube=kube)
        iid = incident.id
    engine = Engine(cfg, Settings(), kube, SimpleNamespace(name="unit-test"), None)
    engine.process(iid)
    with db.session() as session:
        handoff_at = session.get(Incident, iid).closed_at
        outage_row, = session.scalars(select(Evidence).where(Evidence.incident_id == iid))
    recovery = HpaRecovery(cfg)
    outage = recovery.pending("provider-uid")
    watcher = HpaMetricsSignals(kube, ["dev"], Mock(), max_gap=60, recovery=recovery)
    assert watcher.poll_once(AT + timedelta(seconds=30))["hpa_recovery"] == "confirming"
    with db.session() as session:
        assert session.get(Incident, iid).status == "handed_off"
    assert watcher.poll_once(AT + timedelta(seconds=45))["hpa_recovery"] == "observed"
    assert "hpa_recovery" not in watcher.poll_once(AT + timedelta(seconds=50))
    assert kube.list_autoscalers.call_count == 2
    with db.session() as session:
        incident = session.get(Incident, iid)
        assert incident.status == "recovery_review" and incident.closed_at is None
        proof = incident.handoff["observed_recovery"]
        assert proof["initial_handoff_at"] == handoff_at.isoformat()
        assert proof["outage_evidence_id"] == outage_row.id
        assert proof["ai_remediation"] is False and proof["workload_impact_observed"] is False
        assert incident.handoff["handoff_classification"]["reason_code"] == "resource_metrics_cluster_authority"
        cards = _evidence_card(list(incident.evidence), [])["items"]
        assert len(cards) == 3 and "계산 재개" in cards[-1]["summary_ko"]
        assert incident.evidence[0].payload == outage_row.payload
        assert [s["hpas"][0]["current_values"][0]["average_utilization"] for s in proof["observations"]] == [70, 55]
        assert stage_of(incident, True, [], [])["label"] == RECOVERY_LABEL
        assert next(n for n in build_flow(session, iid)["nodes"] if n["id"] == "outcome")["label"] == RECOVERY_LABEL
        assert not list(session.scalars(select(AgentRun).where(AgentRun.incident_id == iid)))
        assert not list(session.scalars(select(Action).where(Action.incident_id == iid)))
        assert len(list(session.scalars(select(AuditLog.id).where(AuditLog.incident_id == iid,
                                                               AuditLog.event == "observed_recovery")))) == 1
        assert session.scalar(select(IncidentEvent.id).where(IncidentEvent.incident_id == iid,
                                                             IncidentEvent.status == "recovery_review"))
    assert not recovery.record(outage, proof["observations"])
    api = client()
    path = f"/api/incidents/{iid}/accountability"
    report = api.get(path).json()
    assert report["provider_recovered_at"] < report["hpa_recovered_at"] < report["recovery_observed_at"]
    assert report["status"] == "pending_operator" and "업무 요청 영향은 미관측" in report["limits_ko"]
    body = {"revision": report["revision"], "note": "지표 계산 재개 근거를 확인했습니다."}
    decision = api.post(path + "/confirm", json=body)
    assert decision.status_code == 200 and decision.json()["awaiting_closure"] is False
    assert api.post(path + "/confirm", json=body).status_code == 409


@needs_db
def test_operator_closed_incident_rejects_late_recovery_observations(db_url):
    cfg = AppConfig(target_namespaces=["dev"])
    signal = captured_outage(["dev"])
    with db.session() as session:
        incident, _ = ingest(session, cfg, signal, AT)
        iid = incident.id
    kube = SimpleNamespace(get_resource_metrics_api=recovered_provider,
                           list_autoscalers=lambda ns: [recovered_autoscaler(ns)])
    Engine(cfg, Settings(), kube, SimpleNamespace(name="unit-test"), None).process(iid)
    recovery = HpaRecovery(cfg)
    prior = recovery.pending("provider-uid")
    watcher = HpaMetricsSignals(kube, ["dev"], Mock(), max_gap=60, recovery=recovery)
    assert watcher.poll_once(AT + timedelta(seconds=30))["hpa_recovery"] == "confirming"
    with db.session() as session:
        incident = session.get(Incident, iid)
        transition(incident, "closed")
        closed_at = incident.closed_at
    assert not recovery.record(prior, [])
    assert "hpa_recovery" not in watcher.poll_once(AT + timedelta(seconds=45))
    with db.session() as session:
        incident = session.get(Incident, iid)
        assert incident.status == "closed" and incident.closed_at == closed_at
        assert "observed_recovery" not in incident.handoff and len(incident.evidence) == 1
