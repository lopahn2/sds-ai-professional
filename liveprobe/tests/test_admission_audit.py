import json
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.admission_audit import AdmissionAuditWatcher, failure_signal
from app.config import AppConfig
from app.incidents import ADMISSION_WEBHOOK_FAILURE, CNI_EVENT_SOURCE, CNI_VERSION_MISMATCH, RETIRED_INGRESS_API, Signal
from app.flowgraph import _evidence_card
from app.models import Evidence, Incident
from app.safety import needs_cluster_authority
from conftest import needs_db


def audit_entry(audit_id=1, namespace="dev"):
    return {
        "apiVersion": "audit.k8s.io/v1", "kind": "Event", "stage": "ResponseComplete", "verb": "create",
        "auditID": str(UUID(int=audit_id)), "objectRef": {"namespace": namespace, "resource": "configmaps",
                                                        "name": "release-config"},
        "requestURI": f"/api/v1/namespaces/{namespace}/configmaps?timeout=5s",
        "stageTimestamp": "2026-11-24T10:00:00.123456Z",
        "responseStatus": {"code": 500, "message": 'Internal error occurred: failed calling webhook '
                                                   '"release.verify.invalid": internal backend detail'},
        "user": {"username": "not for incident evidence"}, "requestObject": {"data": {"hidden": "not retained"}},
    }


def append(path, event):
    with path.open("ab") as stream:
        stream.write(json.dumps(event).encode() + b"\n")


def ingress_audit_entry(group="networking.k8s.io", namespace="dev", audit_id=2):
    return {
        "apiVersion": "audit.k8s.io/v1", "kind": "Event", "stage": "ResponseComplete", "level": "Metadata",
        "verb": "create", "auditID": str(UUID(int=audit_id)), "stageTimestamp": "2026-11-24T10:00:00Z",
        "objectRef": {"namespace": namespace, "apiGroup": group, "apiVersion": "v1beta1", "resource": "ingresses"},
        "requestURI": f"/apis/{group}/v1beta1/namespaces/{namespace}/ingresses?fieldValidation=Strict",
        "responseStatus": {"code": 404},
    }


def served_ingress_versions():
    return {"networking.k8s.io/v1", "apps/v1"}


@pytest.fixture
def cfg():
    return AppConfig(target_namespaces=["dev"], historical_namespaces=["prd", "stg"])


def test_audit_extracts_metadata_and_targets_cluster_authority(cfg):
    sig = failure_signal(audit_entry(), cfg)
    assert sig.namespace == "dev" and sig.kind == "AdmissionWebhook"
    assert sig.name == "admission:release.verify.invalid" and sig.reason == ADMISSION_WEBHOOK_FAILURE
    assert sig.detail == {"audit_id": str(UUID(int=1)), "http_status": 500, "object_name": "release-config",
                          "resource": "configmaps", "verb": "create", "webhook": "release.verify.invalid",
                          "observed_at": "2026-11-24T10:00:00.123456+00:00"}
    assert needs_cluster_authority(Incident(signal_reasons=[sig.reason]))


def test_incident_card_explains_the_request_failure(cfg):
    sig = failure_signal(audit_entry(), cfg)
    evidence = Evidence(id=1, source=sig.source, target="dev/configmaps", payload=sig.detail,
                        collected_at=datetime(2026, 11, 24, 10, tzinfo=timezone.utc))
    row, = _evidence_card([evidence], [])["items"]
    assert row["source_ko"] == "Kubernetes API 감사"
    assert "release-config" in row["summary_ko"] and "release.verify.invalid" in row["summary_ko"]
    assert "HTTP 500" in row["raw_items"][0]["ko"]
    assert "requestObject" not in row["raw_items"][0]["raw"]


@pytest.mark.parametrize("group", ["networking.k8s.io", "extensions"])
def test_retired_ingress_requires_resource_404_and_a_served_replacement(group, cfg):
    entry = ingress_audit_entry(group)
    entry["requestObject"] = {"private": "never retain"}
    sig = failure_signal(entry, cfg, served_ingress_versions)
    assert (sig.namespace, sig.kind, sig.reason) == ("dev", "Ingress", RETIRED_INGRESS_API)
    assert sig.detail == {
        "audit_id": entry["auditID"], "resource": "ingresses", "verb": "create", "http_status": 404,
        "api_group": group, "requested_api_version": f"{group}/v1beta1", "current_api_version": "networking.k8s.io/v1",
        "observed_at": "2026-11-24T10:00:00+00:00",
    }
    assert failure_signal(entry, cfg, lambda: {*served_ingress_versions(), f"{group}/v1beta1"}) is None
    assert failure_signal(entry, cfg, set) is None
    assert failure_signal(entry, cfg) is None


@pytest.mark.parametrize("verb", ["patch", "update"])
def test_retired_ingress_matches_named_mutations(verb, cfg):
    entry = ingress_audit_entry()
    entry["verb"] = verb
    entry["objectRef"]["name"] = "payment-ingress"
    entry["requestURI"] = entry["requestURI"].split("?")[0] + "/payment-ingress"
    sig = failure_signal(entry, cfg, served_ingress_versions)
    assert sig.detail["object_name"] == "payment-ingress"
    assert sig.detail["verb"] == verb


@pytest.mark.parametrize("filt", ["prd", "dry-run", "v1", "mismatched-path", "delete", "get", "subresource",
                                  "forbidden", "different-kind", "fabricated-uri", "different-response"])
def test_retired_ingress_ignores_other_failures_before_discovery(filt, cfg):
    entry = ingress_audit_entry(namespace="prd" if filt == "prd" else "dev")
    match filt:
        case "dry-run":
            entry["requestURI"] += "&%64ryRun=All"
        case "v1":
            entry["objectRef"]["apiVersion"] = "v1"
            entry["requestURI"] = entry["requestURI"].replace("v1beta1", "v1")
        case "mismatched-path":
            entry["requestURI"] = entry["requestURI"].replace("/dev/", "/prd/")
        case "delete" | "get":
            entry["verb"] = filt
        case "subresource":
            entry["objectRef"]["subresource"] = "status"
        case "forbidden":
            entry["responseStatus"]["code"] = 403
        case "different-kind":
            entry["objectRef"]["resource"] = "deployments"
        case "fabricated-uri":
            entry["requestURI"] = "https://example.invalid" + entry["requestURI"]
        case "different-response":
            entry["responseStatus"]["reason"] = "Forbidden"
    assert failure_signal(entry, cfg, lambda: pytest.fail("unrelated audits do not read discovery")) is None


def test_discovery_failure_leaves_webhook_collection_running(tmp_path, cfg):
    from unittest.mock import Mock

    path = tmp_path / "audit.jsonl"
    path.touch()
    signals = []
    discovery = Mock(side_effect=[TimeoutError(), served_ingress_versions()])
    watcher = AdmissionAuditWatcher(path, cfg, signals.append, discovery)
    try:
        assert watcher.poll_once() == 0
        append(path, ingress_audit_entry(audit_id=2))
        append(path, ingress_audit_entry(audit_id=3))
        append(path, audit_entry(4))
        assert watcher.poll_once() == 1
        assert [s.reason for s in signals] == [ADMISSION_WEBHOOK_FAILURE]
        assert discovery.call_count == 1
        assert watcher.status["state"] == "degraded" and watcher.status["source"] == "api_discovery"
        append(path, ingress_audit_entry(audit_id=5))
        assert watcher.poll_once() == 1
        assert signals[-1].reason == RETIRED_INGRESS_API
        assert watcher.status["state"] == "running"
    finally:
        watcher.stop()


@needs_db
def test_retired_ingress_flows_from_audit_file_to_durable_incident(tmp_path, db_url, cfg, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import select

    from app import db
    from app.config import Settings
    from app.engine import Engine
    from app.models import Action, AgentRun, RoutingDecision

    runner = Engine(cfg, Settings(), SimpleNamespace(), SimpleNamespace(name="test"), None)
    monkeypatch.setattr(runner, "_collect_or_reuse", lambda *_: pytest.fail("audited failure needs no LLM collection"))
    path = tmp_path / "audit.jsonl"
    path.touch()
    watcher = AdmissionAuditWatcher(path, cfg, runner.on_signal, served_ingress_versions)
    try:
        assert watcher.poll_once() == 0
        append(path, ingress_audit_entry())
        append(path, ingress_audit_entry(namespace="prd", audit_id=3))
        assert watcher.poll_once() == 1
        with db.session() as s:
            inc = s.scalars(select(Incident)).one()
            assert inc.status == "open" and inc.signal_reasons == [RETIRED_INGRESS_API]
            iid = inc.id
        runner.process(iid)
        append(path, ingress_audit_entry(audit_id=4))
        assert watcher.poll_once() == 1
        with db.session() as s:
            inc = s.scalars(select(Incident)).one()
            assert inc.status == "handed_off" and needs_cluster_authority(inc)
            route = s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).one()
            assert route.features["reason_code"] == "retired_ingress_manifest_authority"
            assert route.policy["handoff_only"] is True
            evidence = s.scalars(select(Evidence).where(Evidence.incident_id == iid)).one()
            assert evidence.target == "dev/Ingress/networking.k8s.io/v1beta1"
            row, = _evidence_card([evidence], [])["items"]
            assert "HTTP 404" in row["summary_ko"] and "networking.k8s.io/v1 제공" in row["summary_ko"]
            assert s.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid)) is None
            assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
    finally:
        watcher.stop()


@pytest.mark.parametrize("kind", ["admission", "cni"])
def test_confirmed_observation_goes_to_authority_handoff_with_evidence(kind, cfg, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app import engine

    at = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
    if kind == "admission":
        sig = failure_signal(audit_entry(), cfg)
        target, reason_code = "dev/configmaps", "admission_webhook_cluster_authority"
        source_label = "Kubernetes API 감사"
    else:
        sig = Signal("dev", "Node", "node-a", CNI_VERSION_MISMATCH, CNI_EVENT_SOURCE, {
            "node": "node-a", "configured_version": "1.0.0", "supported_versions": ["0.4.0"],
            "observed_at": at.isoformat(),
        })
        target, reason_code = "Node/node-a", "node_cni_cluster_authority"
        source_label = "kubelet CNI 경고"
    session = Mock()
    session.scalar.return_value = SimpleNamespace(detail={"signal": {"detail": sig.detail}}, ts=at)
    monkeypatch.setattr(engine.db, "session", lambda: nullcontext(session))
    monkeypatch.setattr(engine.runlog, "gate", Mock())
    monkeypatch.setattr(engine.runlog, "emit", Mock())
    runner = engine.Engine.__new__(engine.Engine)
    runner._record_decision = Mock()
    runner._set_status = Mock()
    runner._handoff_observed_authority(42, "dev", sig.reason)
    evidence, = session.add.call_args.args
    assert (evidence.source, evidence.target, evidence.payload) == (sig.source, target, sig.detail)
    evidence.id = 1
    row, = _evidence_card([evidence], [])["items"]
    assert row["source_ko"] == source_label and row["summary_ko"]
    if kind == "cni":
        assert "1.0.0" in row["summary_ko"] and "0.4.0" in row["summary_ko"]
    _, decision = runner._record_decision.call_args.args
    assert decision.level == "HIGH_RISK" and decision.policy["handoff_only"]
    assert decision.features["reason_code"] == reason_code
    assert runner._set_status.call_args.args == (42, "handed_off")


@needs_db
def test_cni_incident_routes_from_persisted_signal_without_model_or_action(db_url, cfg, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import select

    from app import db
    from app.config import Settings
    from app.engine import Engine
    from app.incidents import ingest
    from app.models import Action, AgentRun, RoutingDecision

    runner = Engine(cfg, Settings(), SimpleNamespace(), SimpleNamespace(name="test"), None)
    monkeypatch.setattr(runner, "_collect_or_reuse", lambda *_: pytest.fail("authority handoff must use captured evidence"))
    observed_at = datetime.now(timezone.utc)
    detail = {"node": "worker-a", "configured_version": "1.0.0", "supported_versions": ["0.4.0"],
              "observed_at": observed_at.isoformat()}
    sig = Signal("dev", "Node", "worker-a", CNI_VERSION_MISMATCH, CNI_EVENT_SOURCE, detail)
    with db.session() as s:
        incident, outcome = ingest(s, cfg, sig, observed_at)
        assert outcome == "created"
        iid = incident.id
    runner.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == "handed_off" and needs_cluster_authority(inc)
        route = s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).one()
        assert route.features["reason_code"] == "node_cni_cluster_authority"
        assert route.policy["handoff_only"] is True
        evidence = s.scalars(select(Evidence).where(Evidence.incident_id == iid)).one()
        assert evidence.target == "Node/worker-a" and evidence.payload == detail
        assert s.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid)) is None
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None


@pytest.mark.parametrize("filt", ["prd", "dry-run", "success", "policy-denial", "malformed", "secret", "partial"])
def test_unrelated_requests_do_not_emit(filt, cfg):
    event = audit_entry(namespace="prd" if filt == "prd" else "dev")
    if filt == "dry-run":
        event["requestURI"] += "&%64ryRun=All"
    if filt == "success":
        event["responseStatus"]["code"] = 201
    if filt == "policy-denial":
        event["responseStatus"]["message"] = 'admission webhook "valid.verify.invalid" denied the request'
    if filt == "malformed":
        event["auditID"] = "not-a-uuid"
    if filt == "secret":
        event["objectRef"]["resource"] = "secrets"
    if filt == "partial":
        event["stage"] = "RequestReceived"
    assert failure_signal(event, cfg) is None


def test_file_reader_skips_history_and_reads_complete_events_through_rotation(tmp_path, cfg):
    path = tmp_path / "audit.jsonl"
    append(path, audit_entry(1))
    signals = []
    watcher = AdmissionAuditWatcher(path, cfg, signals.append)
    try:
        assert watcher.poll_once() == 0
        line = json.dumps(audit_entry(2)).encode()
        with path.open("ab") as stream:
            stream.write(line[:40])
        assert watcher.poll_once() == 0
        with path.open("ab") as stream:
            stream.write(line[40:] + b"\n")
        assert watcher.poll_once() == 1
        assert watcher.poll_once() == 0
        append(path, audit_entry(3))
        path.rename(tmp_path / "old.jsonl")
        append(path, audit_entry(4, namespace="prd"))
        append(path, audit_entry(5))
        assert watcher.poll_once() == 2
        assert [item.detail["audit_id"] for item in signals] == [str(UUID(int=i)) for i in (2, 3, 5)]
    finally:
        watcher.stop()
