"""P18 W6: stage-targeted operator steering (`target_role`): hold until the target starts, otherwise fall back."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import needs_db


@pytest.fixture()
def iid(db_url):
    from app import db
    from app.models import Incident
    with db.session() as s:
        inc = Incident(fingerprint="w6-steer", namespace="liveprobe-target", workload="inventory", status="analyzing",
                       title="t", signal_reasons=[], severity_level="COMPLEX")
        s.add(inc)
        s.flush()
        return inc.id


def _started(iid, role):
    from app import runlog
    runlog.emit(iid, "agent_started", f"{role} 시작", {"role": role})


def _msg(iid, mid):
    from app import db
    from app.models import OperatorMessage
    with db.session() as s:
        m = s.get(OperatorMessage, mid)
        return {"applied_at": m.applied_at, "intent": m.intent, "detail": dict(m.intent_detail or {})}


@needs_db
def test_post_stores_target_and_reports_hold_with_korean_ack(iid):
    from app import runlog, steering
    out = steering.post(iid, "원인 후보에 DB 연결 풀도 포함해", target_role="challenger", target_node="plan:1:challenger")
    assert out["target_role"] == "challenger" and out["target_node"] == "plan:1:challenger" and out["hold"] is True
    assert out["ack_ko"] == "반론 검토 시작 전에 이 지시를 전달합니다."
    m = _msg(iid, out["message_id"])
    assert m["detail"] == {"target_level": None, "target_role": "challenger", "target_node": "plan:1:challenger"}
    ev = runlog.events_since(iid)
    om = next(e for e in ev if e["kind"] == "operator_message")
    si = next(e for e in ev if e["kind"] == "steering_interpreted")
    for e in (om, si):
        assert e["detail"]["message_id"] == out["message_id"]
        assert e["detail"]["target_role"] == "challenger" and e["detail"]["target_node"] == "plan:1:challenger"


@needs_db
def test_post_for_an_already_started_role_says_it_goes_to_the_next_agent(iid):
    from app import steering
    _started(iid, "k8s_log_analyst")
    out = steering.post(iid, "로그를 더 봐", target_role="k8s_log_analyst")
    assert out["hold"] is False
    assert out["ack_ko"] == "Kubernetes·로그 분석은 이미 실행되어 다음 단계 Agent에게 전달합니다."


@needs_db
def test_post_for_a_passed_stage_does_not_claim_hold(iid):
    from app import steering
    _started(iid, "challenger")                    # stage 2 is running; cause_reasoner (stage 1) was skipped/passed
    out = steering.post(iid, "원인 추론에게", target_role="cause_reasoner")
    assert out["hold"] is False and "이미 실행되어" in out["ack_ko"]


@needs_db
def test_post_without_target_keeps_the_previous_contract(iid):
    from app import steering
    out = steering.post(iid, "로그부터 다시 봐")
    assert out["target_role"] is None and out["target_node"] is None and out["hold"] is False
    assert out["ack_ko"] == steering.ACK["instruction"]
    assert _msg(iid, out["message_id"])["detail"] == {"target_level": None, "target_role": None, "target_node": None}


@needs_db
def test_targeted_instruction_is_held_until_the_target_role_starts(iid):
    from app import runlog, steering
    out = steering.post(iid, "DB 연결 풀도 반박해 봐", target_role="challenger")
    st = steering.take(iid, "k8s_log_analyst", "COMPLEX")
    assert st.texts == [] and _msg(iid, out["message_id"])["applied_at"] is None
    _started(iid, "k8s_log_analyst")
    st = steering.take(iid, "cause_reasoner", "COMPLEX")
    assert st.texts == [] and _msg(iid, out["message_id"])["applied_at"] is None
    st = steering.take(iid, "challenger", "COMPLEX")
    assert st.texts == ["DB 연결 풀도 반박해 봐"] and _msg(iid, out["message_id"])["applied_at"] is not None
    applied = [e for e in runlog.events_since(iid) if e["kind"] == "operator_applied"]
    assert len(applied) == 1
    d = applied[0]["detail"]
    assert d["applied_to_role"] == "challenger" and d["targeted_role"] == "challenger" and d["fallback"] is False


@needs_db
def test_untargeted_messages_still_apply_while_a_targeted_one_waits(iid):
    from app import steering
    steering.post(iid, "반론 검토에게만", target_role="challenger")
    steering.post(iid, "모두에게")
    st = steering.take(iid, "k8s_log_analyst", "COMPLEX")
    assert st.texts == ["모두에게"]


@needs_db
def test_targeted_instruction_falls_back_when_the_target_already_ran(iid):
    from app import runlog, steering
    _started(iid, "cause_reasoner")
    out = steering.post(iid, "원인 추론에 반영해", target_role="cause_reasoner")
    st = steering.take(iid, "challenger", "COMPLEX")
    assert st.texts == ["원인 추론에 반영해"] and _msg(iid, out["message_id"])["applied_at"] is not None
    d = next(e for e in runlog.events_since(iid) if e["kind"] == "operator_applied")["detail"]
    assert d["applied_to_role"] == "challenger" and d["targeted_role"] == "cause_reasoner" and d["fallback"] is True


@needs_db
def test_targeted_instruction_falls_back_when_the_target_is_not_in_the_current_group(iid):
    from app import runlog, steering
    steering.post(iid, "신호 분석에게", target_role="signal_analyst")        # SIMPLE role, group is COMPLEX
    st = steering.take(iid, "k8s_log_analyst", "COMPLEX")
    assert st.texts == ["신호 분석에게"]
    d = next(e for e in runlog.events_since(iid) if e["kind"] == "operator_applied")["detail"]
    assert d["fallback"] is True and d["targeted_role"] == "signal_analyst"


@needs_db
def test_targeted_message_falls_back_when_its_stage_was_skipped(iid):
    from app import db, runlog, steering
    from app.models import Incident
    with db.session() as s:
        s.get(Incident, iid).severity_level = "NORMAL"
    steering.post(iid, "진행 조정에게", target_role="coordinator")
    _started(iid, "k8s_analyst")
    st = steering.take(iid, "group_verifier", "NORMAL")       # coordinator skipped by early stop; never left pending
    assert st.texts == ["진행 조정에게"]
    d = next(e for e in runlog.events_since(iid) if e["kind"] == "operator_applied")["detail"]
    assert d["fallback"] is True and d["targeted_role"] == "coordinator" and d["applied_to_role"] == "group_verifier"


@needs_db
def test_targeted_escalate_is_held_then_applied_to_the_target(iid):
    from app import steering
    steering.post(iid, "더 높은 모델로 분석해", target_role="group_verifier")
    assert steering.take(iid, "k8s_log_analyst", "COMPLEX").escalate is False
    assert steering.take(iid, "group_verifier", "COMPLEX").escalate is True


@needs_db
def test_targeted_message_before_routing_falls_back_rather_than_waiting_forever(iid):
    from app import steering
    steering.post(iid, "결과 확인에서 봐", target_role="group_verifier")
    st = steering.take(iid, "evidence_curator", None)          # no level: target cannot be in a known plan
    assert st.texts == ["결과 확인에서 봐"]


@needs_db
def test_engine_boundary_delivers_held_message_to_the_target_prompt(iid):
    from app.llm import AGENT_SCHEMA, AgentOutput
    from app import steering
    from test_p15_steering import Prov, _engine
    steering.post(iid, "TARGETED-HINT", target_role="cause_reasoner")
    prov = Prov()
    eng = _engine(prov)
    eng._current_level = "COMPLEX"
    from app.config import RoleModel
    rm = RoleModel(model="gpt-6.1-sol", effort="low")
    eng._call(iid, "k8s_log_analyst", "A", AGENT_SCHEMA, AgentOutput, hits=[], role_model=rm)
    eng._call(iid, "cause_reasoner", "B", AGENT_SCHEMA, AgentOutput, hits=[], role_model=rm)
    assert "TARGETED-HINT" not in prov.prompts[0] and "TARGETED-HINT" in prov.prompts[1]


@needs_db
def test_reroute_and_stop_ignore_target_role(iid):
    from app import runlog, steering
    out = steering.post(iid, "분석 중단해", target_role="challenger")
    assert out["intent"] == "stop" and runlog.control_state(iid) == "stopping" and out["hold"] is False
    out2 = steering.post(iid, "보통 그룹으로 바꿔", target_role="challenger")
    assert steering.take(iid, "k8s_log_analyst", "COMPLEX").reroute is not None
    assert out2["hold"] is False


@needs_db
def test_flow_steer_node_links_to_the_targeted_role(db_url, iid):
    from app import db, steering
    from app.flowgraph import build_flow
    from app.models import RoutingDecision
    with db.session() as s:
        s.add(RoutingDecision(incident_id=iid, level="COMPLEX", workflow="complex_group", features={}, rationale="x",
                              policy={}, confidence=0.9, decided_by="rules"))
    out = steering.post(iid, "반론 검토에게만", target_role="challenger", target_node="plan:x:challenger")
    with db.session() as s:
        g = build_flow(s, iid)
    steer = next(n for n in g["nodes"] if n["id"] == f"steer:{out['message_id']}")
    edge = next(e for e in g["edges"] if e["source"] == steer["id"])
    target = next(n for n in g["nodes"] if n["id"] == edge["target"])
    assert target["role"] == "challenger" and steer["target_role"] == "challenger" and steer["at"]


# ------------------------------------------------------------------ API
def _client(db_url, monkeypatch):
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    from app import config
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    from app.main import app
    return TestClient(app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"})


@needs_db
def test_api_accepts_target_role_and_returns_hold(operator_gateway, db_url, monkeypatch, iid):
    with _client(db_url, monkeypatch) as c:
        r = c.post(f"/api/incidents/{iid}/messages", json={"body": "반박 근거를 더 찾아", "target_role": "challenger",
                                                            "target_node": "plan:3:challenger"})
        assert r.status_code == 202, r.text
        b = r.json()
        assert {"message_id", "intent", "target_level", "ack_ko", "applies", "target_role", "target_node", "hold"} <= set(b)
        assert b["target_role"] == "challenger" and b["target_node"] == "plan:3:challenger" and b["hold"] is True
        assert b["applies"] == "next_node_boundary"
        r2 = c.post(f"/api/incidents/{iid}/messages", json={"body": "그냥 봐"})
        assert r2.status_code == 202 and r2.json()["target_role"] is None and r2.json()["hold"] is False
    from app import config
    config.get_settings.cache_clear()


@needs_db
def test_api_rejects_unknown_role_and_long_node(operator_gateway, db_url, monkeypatch, iid):
    with _client(db_url, monkeypatch) as c:
        assert c.post(f"/api/incidents/{iid}/messages", json={"body": "x", "target_role": "hacker"}).status_code == 422
        assert c.post(f"/api/incidents/{iid}/messages", json={"body": "x", "target_role": "challenger",
                                                              "target_node": "n" * 81}).status_code == 422
    from app import config
    config.get_settings.cache_clear()


from test_p15_command_api import api  # fixture uses fake planner/Kubernetes only


@pytest.mark.parametrize("status", ["analyzing", "handed_off", "resolved", "failed"])
def test_targeted_mutation_keeps_command_safety_and_target_metadata(operator_gateway, api, status):
    from app import db, runlog
    from app.models import Incident, OperatorMessage
    from test_p15_commands import operation
    with db.session() as s:
        s.get(Incident, api.iid).status = status
    r = api.client.post(f"/api/incidents/{api.iid}/messages", json={"body": "설정 ConfigMap을 new로 바꿔줘",
                        "target_role": "challenger", "target_node": "run:23"})
    assert r.status_code == 202 and r.json()["applies"] == "command_confirmation_required"
    assert r.json()["target_role"] == "challenger" and r.json()["target_node"] == "run:23" and r.json()["hold"] is False
    assert api.kube.get(operation())["data"]["mode"] == "old"
    with db.session() as s:
        m = s.get(OperatorMessage, r.json()["message_id"])
        assert m.intent_detail["command_id"] and m.intent_detail["target_role"] == "challenger"
        assert m.intent_detail["target_node"] == "run:23" and m.applied_at is not None
    for e in runlog.events_since(api.iid):
        if e["kind"] in ("operator_message", "steering_interpreted"):
            assert e["detail"]["message_id"] == r.json()["message_id"]
            assert e["detail"]["target_role"] == "challenger" and e["detail"]["target_node"] == "run:23"


@needs_db
def test_api_closed_incident_still_409(operator_gateway, db_url, monkeypatch, iid):
    from app import db
    from app.models import Incident
    with db.session() as s:
        s.get(Incident, iid).status = "closed"
    with _client(db_url, monkeypatch) as c:
        assert c.post(f"/api/incidents/{iid}/messages", json={"body": "x", "target_role": "challenger"}).status_code == 409
    from app import config
    config.get_settings.cache_clear()
