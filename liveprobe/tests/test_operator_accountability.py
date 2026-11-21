from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db, runlog
from app.accountability import AWAITING, REPORT, accountability_router
from app.agent_groups import EvidenceStore
from app.auth import require_user
from app.investigation import EvidenceInquiry
from app.models import Action, AgentRun, AuditLog, Evidence, Incident
from test_premature_handoff import engine, pair, seed


def client():
    app = FastAPI()
    app.include_router(accountability_router())
    app.dependency_overrides[require_user] = lambda: "operator-test"
    return TestClient(app)


def test_observed_recovery_requires_report_and_one_explicit_confirmation(db_url, monkeypatch):
    first, second = pair()
    iid = seed()
    engine(monkeypatch, [first, second]).process(iid)
    api = client()
    path = f"/api/incidents/{iid}/accountability"
    report = api.get(path).json()
    assert report["status"] == "pending_operator" and report["recovery_observed_at"]
    assert report["decision_at"] is None and report["evidence_refs"]
    assert [x["incident_id"] for x in api.get("/api/reviews/unexplained").json()["items"]] == [iid]
    with db.session() as s:
        assert s.get(Incident, iid).status == AWAITING and s.get(Incident, iid).closed_at is None
    confirm = {"note": "조치 없이 두 차례 정상 상태를 확인했습니다.", "revision": report["revision"]}
    decision = api.post(path + "/confirm", json=confirm)
    assert decision.status_code == 200 and decision.json()["decision_by"] == "operator-test"
    assert api.post(path + "/confirm", json=confirm).status_code == 409
    assert api.get("/api/reviews/unexplained").json()["total"] == 0
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == "resolved" and inc.closed_at and inc.handoff["observed_recovery"]
        assert s.scalar(select(func.count()).select_from(Action)) == 0
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event == REPORT)) == 2


def test_historical_reinvestigation_appends_and_never_rewrites_closure(db_url):
    iid = seed("resolved")
    with db.session() as s:
        inc = s.get(Incident, iid)
        inc.closed_at = datetime.now(timezone.utc) - timedelta(days=20)
        old = (inc.status, inc.closed_at, deepcopy(inc.handoff))
    api = client()
    path = f"/api/incidents/{iid}/accountability"
    for _ in range(2):
        assert api.get(path).json()["revision"] == 0
        assert api.get("/api/reviews/unexplained?limit=1").json()["total"] == 1
    assert api.post(path + "/confirm", json={"revision": 0}).status_code == 409
    result = api.post(path + "/reinvestigate", json={"revision": 0}).json()
    assert result["status"] == "pending_operator" and result["revision"] > 0
    confirmation = api.post(path + "/confirm", json={"revision": result["revision"]}).json()
    assert confirmation["status"] == "confirmed"
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert (inc.status, inc.closed_at, inc.handoff) == old


def test_verified_image_retry_explains_recovery_without_ai_write(db_url, monkeypatch):
    first, second = pair()
    iid = seed(signals=["ImagePullBackOff"])
    start = datetime.now(timezone.utc) - timedelta(seconds=20)
    with db.session() as s:
        s.get(Incident, iid).created_at = start
    samples = [
        {"last": (start + timedelta(seconds=2)).isoformat(), "type": "Warning", "reason": "Failed",
         "message": 'Failed to pull image "registry/app:v1": timeout'},
        {"last": (start + timedelta(seconds=4)).isoformat(), "type": "Normal", "reason": "Pulled",
         "message": 'Successfully pulled image "registry/app:v1" in 1s'},
    ]
    for b in (first, second):
        b.events = [{"object": "Pod/delivery-pod", "uid": "stable-pod-uid", **sample} for sample in samples]
        next(e for e in b.items if e.source == "k8s_event").payload = b.events
    engine(monkeypatch, [first, second]).process(iid)
    api = client()
    result = api.get(f"/api/incidents/{iid}/accountability").json()
    assert result["status"] == "explained" and "registry/app:v1" in result["recovery_ko"]
    assert {ref.split()[0] for ref in result["evidence_refs"]} >= {"이미지", "동일"}
    with db.session() as s:
        assert s.get(Incident, iid).status == "resolved"
        assert s.scalar(select(func.count()).select_from(Action)) == 0


def test_new_evidence_is_bound_to_its_inquiry_and_reviewer(db_url, monkeypatch):
    from app.evidence import compact_summary
    from app.flowgraph import build_flow
    from app.config import AppConfig
    from app import investigation
    from app.engine import Engine
    from app.config import Settings
    from app.agent_groups import GroupController

    first, second = pair()
    iid = seed("analyzing")
    first.service["ready_endpoints"] = 0
    control = GroupController("COMPLEX", AppConfig(), lambda *args: None)
    e = Engine(AppConfig(), Settings(), None, SimpleNamespace(name="test"), None)
    store = EvidenceStore(compact_summary(first), [])
    inquiry = EvidenceInquiry(e, iid, first, {}, store, control)
    with db.session() as s:
        s.add(AgentRun(incident_id=iid, role="group_verifier", provider="test", model="test",
                       status="succeeded", attempts=1, output={}))
    monkeypatch.setattr(investigation, "collect", lambda *a, **k: second)
    state = {"primary_role": "cause_reasoner", "msgs": {"cause_reasoner": {
        "root_cause_candidates": [{"evidence_refs": ["E3_service"]}]}}}
    assert inquiry(state, "group_verifier")
    assert not inquiry(state, "group_verifier")
    assert "E10_investigation" in store.view(("E3",))
    with db.session() as s:
        ix = s.scalar(select(Evidence).where(Evidence.source == "investigation"))
        assert ix.payload["findings"]["E3_service"]["ready_endpoints"] == 1
        nodes = build_flow(s, iid)["nodes"]
        assert any(n["type"] == "investigation" and n["status"] == "done" for n in nodes)
    assert any(i["kind"] == "investigation_finished" for i in runlog.events_since(iid))


def test_legacy_explanations_require_matching_symptom_version_target_and_time(db_url):
    from app.accountability import action_explanation
    iid = seed("resolved", signals=["OOMKilled"])
    api = client()
    with db.session() as s:
        inc = s.get(Incident, iid)
        inc.closed_at = inc.created_at + timedelta(minutes=5)
        action = Action(incident_id=iid, target="dev/Deployment/delivery", type="set_memory_limit",
                        before_snapshot={"memory_limit": "512Mi"}, params={"applied": {"memory_limit": "1Gi"}},
                        after_state={"checks": {"limit_applied": True, "rollout_healthy": True}},
                        policy_mode="auto", executed=True, status="succeeded",
                        created_at=inc.created_at + timedelta(seconds=20), updated_at=inc.created_at + timedelta(seconds=30))
        s.add(action)
        s.flush()
        assert action_explanation(inc, action)
        for attr, value in [("target", "dev/Deployment/unrelated"), ("executed", False), ("type", "rollout_restart"),
                            ("params", {"applied": {"memory_limit": "64Mi"}}),
                            ("updated_at", inc.closed_at + timedelta(seconds=10))]:
            old = getattr(action, attr)
            setattr(action, attr, value)
            assert not action_explanation(inc, action)
            setattr(action, attr, old)
        for reasons in ([], ["TargetHigh5xxRate"], ["OOMKilled", "Unhealthy"]):
            inc.signal_reasons = reasons
            assert not action_explanation(inc, action)
        inc.signal_reasons = ["OOMKilled"]
    assert api.get(f"/api/incidents/{iid}/accountability").json()["status"] == "explained"
    assert api.get("/api/reviews/unexplained").json()["total"] == 0
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event == REPORT)) == 0


def test_reviewer_receives_distinguishing_new_observation_before_action(db_url, monkeypatch):
    from app.config import AppConfig, Settings
    from app import investigation
    from app.agent_groups import GroupController, run_group
    from app.engine import Engine
    from app.evidence import compact_summary
    from test_agent_groups import Fake, good, verifier

    iid = seed("analyzing", signals=["OOMKilled"])
    first, second = pair()
    first.pods[0]["containers"][0]["last_terminated_reason"] = "OOMKilled"
    second.pods[0]["containers"][0].update(last_terminated_reason="OOMKilled", last_exit_code=137)
    first.pods[0]["containers"][0]["last_exit_code"] = 1
    store = EvidenceStore(compact_summary(first))
    original = good(evidence=[{"ref": "E2_pods", "quote": '"last_terminated_reason": "OOMKilled"', "supports": "OOMKilled"}],
                    root_cause_candidates=[{"cause": "OOMKilled", "confidence": 0.95, "evidence_refs": ["E2_pods"]}])
    update = good(evidence=[{"ref": "E10_investigation", "quote": '"last_terminated_reason": "OOMKilled"', "supports": "OOMKilled"}],
                  root_cause_candidates=[{"cause": "OOMKilled", "confidence": 0.95, "evidence_refs": ["E10_investigation"]}])
    suggestion = {"type": "set_memory_limit", "target": "delivery", "memory_limit_mi": 512, "why": "verified OOMKilled"}
    update.update(next_action="act", action=suggestion)
    checked = verifier()
    checked.update(evidence=update["evidence"], next_action="act", action=suggestion)
    uncertain = verifier("insufficient", "insufficient")
    uncertain["evidence"] = original["evidence"]
    fake = Fake({"k8s_analyst": original, "log_summarizer": original, "coordinator": update,
                 "group_verifier": [uncertain, checked]})
    cfg = AppConfig()
    ctl = GroupController("NORMAL", cfg, fake)
    engine = Engine(cfg, Settings(), SimpleNamespace(), SimpleNamespace(name="fake"), None)
    monkeypatch.setattr(investigation, "collect", lambda *a, **kw: second)
    inquiry = EvidenceInquiry(engine, iid, first, {}, store, ctl)
    ctl.inquire = inquiry
    result = run_group("NORMAL", ctl, store, {"id": iid, "workload": "delivery"}, {"oom_killed": True})
    assert result["status"] == "verified", result
    assert result["action"]["propose"], result["action"]
    assert [a for a, _, _ in fake.calls].count("group_verifier") == 2
    assert result["root_cause_candidates"][0]["evidence_refs"] == ["E10_investigation"]
    assert inquiry.attempts[0]["evidence_refs"] == ["E10_investigation"]


def test_same_service_and_unchanged_pod_prove_endpoint_selection_repair(db_url, monkeypatch):
    first, second = pair()
    iid = seed(signals=["NoEndpoints"])
    before = first.items[0].collected_at - timedelta(seconds=10)
    for b in (first, second):
        b.service["uid"] = "stable-service"
        b.pods[0]["labels"] = {"app": "delivery"}
    with db.session() as s:
        s.get(Incident, iid).created_at = before - timedelta(seconds=2)
        s.add(Evidence(incident_id=iid, source="service", target="dev/Service/delivery", collected_at=before,
                       payload={"uid": "stable-service", "selector": {"app": "old"}, "matching_pods": [],
                                "ready_endpoints": 0}))
        s.add(Evidence(incident_id=iid, source="pod_status", target="dev/pods(app=delivery)", collected_at=before,
                       payload=[{"uid": "stable-pod-uid", "labels": {"app": "delivery"}}]))
    engine(monkeypatch, [first, second]).process(iid)
    report = client().get(f"/api/incidents/{iid}/accountability").json()
    assert report["status"] == "explained" and "선택자" in report["recovery_ko"]
    with db.session() as s:
        from app.accountability import investigate
        inc = s.get(Incident, iid)
        record = s.scalar(select(Evidence).where(Evidence.incident_id == iid, Evidence.source == "pod_status",
                                                 Evidence.collected_at == before))
        record.payload = [{"uid": "replacement", "labels": {"app": "delivery"}}]
        s.flush()
        assert investigate(s, inc)["status"] == "pending_operator"


def test_recovery_during_agent_work_stops_new_write_and_requests_accountability(db_url, monkeypatch):
    first, second = pair()
    iid = seed("analyzing", signals=["NoEndpoints"])
    worker = engine(monkeypatch, [first, second])
    view = {"signals": ["NoEndpoints"]}
    assert worker._guard_new_change(iid, first, view, "COMPLEX")
    with db.session() as s:
        assert s.get(Incident, iid).status == AWAITING
        assert s.scalar(select(func.count()).select_from(Action)) == 0
        assert s.scalar(select(AuditLog).where(AuditLog.event == REPORT)).detail["status"] == "pending_operator"


def test_approval_after_recovery_withdraws_only_the_unexecuted_proposal(db_url, monkeypatch):
    first, second = pair()
    iid = seed("action_pending", signals=["NoEndpoints"])
    with db.session() as s:
        proposal = Action(incident_id=iid, target="dev/Deployment/delivery", type="rollout_restart",
                          status="pending_approval", policy_mode="approval", executed=False)
        s.add(proposal)
        s.flush()
        aid = proposal.id
    actual = engine(monkeypatch, [first, second]).run_action(iid, aid, "operator-test", "COMPLEX")
    assert actual.status == "cancelled"
    with db.session() as s:
        assert s.get(Incident, iid).status == AWAITING
        action = s.get(Action, aid)
        assert action.status == "cancelled" and not action.executed
