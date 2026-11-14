"""P15 W4-W6: LLM interpreter cache, flow graph and the narrative API."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import needs_db

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def seed(level: str = "COMPLEX", status: str = "resolved", *, escalate: bool = True, action: str | None = "set_memory_limit",
         extra_routing: bool = False, message: str | None = None, running_role: str | None = None) -> int:
    from app import db
    from app.models import Action, AgentRun, Evidence, Incident, OperatorMessage, RoutingDecision, RunEvent
    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="liveprobe-target", workload="inventory", status=status,
                       severity_level=level, title="TargetWorkloadOOMKilled on Deployment/inventory",
                       signal_reasons=["TargetWorkloadOOMKilled"], created_at=T0, summary="Inventory OOM at 512Mi.")
        s.add(inc)
        s.flush()
        iid = inc.id
        ev = [("deployment", {"name": "inventory", "replicas": 1, "ready_replicas": 0, "images": ["liveprobe/inventory:dev"]}),
              ("pod_status", [{"name": "inventory-x", "phase": "Running",
                               "containers": [{"name": "inventory", "waiting_reason": "CrashLoopBackOff",
                                               "last_terminated_reason": "OOMKilled", "restart_count": 3}]}]),
              ("service", {"name": "inventory", "ready_endpoints": 0}),
              ("k8s_event", [{"last": "2026-10-11T12:01:00+00:00", "type": "Warning", "count": 3, "object": "Pod/inventory-x",
                              "reason": "BackOff", "message": "Back-off restarting failed container inventory in pod inventory-x"}]),
              ("prometheus", {"http_5xx_rate_per_s": {"order": 2.0}, "http_rate_per_s": {"order": 4.0}, "_query": {"promql": {}}}),
              ("loki", {"lines": ["[order] inventory call failed error=ServiceUnavailable: 503 Service Unavailable: \"no healthy upstream\"",
                                  "[order] mysterious internal widget hiccup code=77"], "_query": {}})]
        for src, payload in ev:
            s.add(Evidence(incident_id=iid, source=src, target=f"liveprobe-target/{src}", payload=payload, collected_at=T0))
        s.add(RoutingDecision(incident_id=iid, level=level, workflow=f"{level.lower()}_group", features={}, rationale="bounded OOM signal",
                              policy={"selected_model": "gpt-6.1-sol"}, confidence=0.9, decided_by="rules+llm",
                              created_at=T0 + timedelta(seconds=5)))
        roles = {"SIMPLE": ["signal_analyst", "result_verifier"],
                 "NORMAL": ["k8s_analyst", "log_summarizer", "coordinator", "group_verifier"],
                 "COMPLEX": ["k8s_log_analyst", "cause_reasoner", "challenger", "recovery_planner", "group_verifier"],
                 "HIGH_RISK": ["evidence_curator", "safety_verifier"]}[level]
        runs = ["router"] + ([roles[0]] if escalate else []) + roles
        if running_role:
            runs = runs[: runs.index(running_role)]
        t = T0 + timedelta(seconds=4)
        seq = 0
        for i, role in enumerate(runs):
            t += timedelta(seconds=3)
            out = {"summary": f"{role} says the inventory container was OOMKilled at 512Mi.", "confidence": 0.9,
                   "evidence": [{"ref": "E2_pods", "quote": "OOMKilled", "supports": "x"}],
                   "root_cause_candidates": [{"cause": "memory limit too low", "confidence": 0.9, "evidence_refs": ["E2_pods", "E1"]}]}
            s.add(AgentRun(incident_id=iid, role=role, provider="codex_cli", model="gpt-6-luna" if role == "router" else "gpt-6.1-sol",
                           reasoning_effort="low", input_tokens=1000, output_tokens=100, estimated_cost_usd=0.001, duration_ms=2000,
                           status="succeeded", attempts=1, output=out, created_at=t))
        if escalate:
            seq += 1
            s.add(RunEvent(incident_id=iid, seq=seq, kind="escalated", summary="escalated",
                           detail={"role": roles[0], "from_model": "gpt-6.1-sol", "to_model": "gpt-6-astra"}, created_at=T0))
        if running_role:
            seq += 1
            s.add(RunEvent(incident_id=iid, seq=seq, kind="agent_started", summary="", detail={"role": running_role}, created_at=t))
        if extra_routing:
            s.add(RoutingDecision(incident_id=iid, level="HIGH_RISK", workflow="high_risk_group", features={}, rationale="operator reroute",
                                  policy={}, confidence=1.0, decided_by="operator", created_at=t + timedelta(seconds=1)))
        if action:
            s.add(Action(incident_id=iid, type=action, target="liveprobe-target/Deployment/inventory", params={"memory_limit_mi": 640},
                         policy_mode="auto", status="succeeded", executed=True, created_at=t + timedelta(seconds=2)))
        if message:
            m = OperatorMessage(incident_id=iid, body=message, applied_at=T0 + timedelta(seconds=9), intent="instruction")
            s.add(m)
            s.flush()
            seq += 1
            s.add(RunEvent(incident_id=iid, seq=seq, kind="operator_applied", summary="", created_at=T0,
                           detail={"message_ids": [m.id], "applied_to_role": roles[0]}))
    return iid


class FakeProvider:
    name = "codex_cli"

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def complete(self, role, prompt, schema, model, effort, timeout):
        from app.llm.base import LLMError, LLMResult, Usage
        self.calls.append({"role": role, "prompt": prompt, "model": model, "effort": effort})
        if self.fail:
            raise LLMError("timeout", "boom")
        ids = json.loads(prompt.split("ITEMS_JSON:", 1)[1])
        items = [{"id": it["id"], "ko": f"한글 해석 {it['id']}"} for it in ids] + [{"id": "invented", "ko": "가짜"},
                                                                                  {"id": ids[0]["id"], "ko": "english only"}]
        return LLMResult(text="", data={"items": items}, usage=Usage(input_tokens=500, output_tokens=200), duration_ms=10)


def _interpreter(provider):
    from app.config import AppConfig
    from app.interpret import Interpreter
    return Interpreter(AppConfig(), provider)


@needs_db
def test_interpreter_reads_unknown_lines_once_and_records_cost(db_url):
    from app import db
    from app.models import AgentRun, Interpretation, OperatorMessage
    iid = seed()
    with db.session() as s:
        s.add(OperatorMessage(incident_id=iid, body="pending"))
    p = FakeProvider()
    n = _interpreter(p).run(iid)
    assert n > 0 and len(p.calls) == 1
    call = p.calls[0]
    assert call["role"] == "interpreter" and call["model"] == "gpt-6-luna" and call["effort"] == "low"
    assert "mysterious internal widget hiccup" in call["prompt"]       # unknown log line goes to the LLM
    assert "no healthy upstream" not in call["prompt"]               # rule-covered lines do not
    with db.session() as s:
        rows = s.query(Interpretation).filter_by(incident_id=iid, source="llm").all()
        assert rows and all(r.ko.startswith("한글 해석") for r in rows)
        assert not any(r.item_key == "invented" for r in rows)
        run = s.query(AgentRun).filter_by(incident_id=iid, role="interpreter").one()
        assert run.estimated_cost_usd and run.estimated_cost_usd > 0
        assert s.query(OperatorMessage).filter_by(incident_id=iid).one().applied_at is None   # never consumes steering
    assert _interpreter(p).run(iid) == 0 and len(p.calls) == 1     # cached


@needs_db
@pytest.mark.parametrize("legacy", [True, False])
def test_finished_incident_interpretation_keeps_one_persistent_budget(db_url, legacy):
    import time
    from sqlalchemy import select
    from app import db
    from app.models import Evidence, Incident, WorkflowRun
    from app.config import AppConfig
    from app.interpret import Interpreter
    iid = seed(escalate=False)
    if not legacy:
        with db.session() as s:
            s.add(WorkflowRun(incident_id=iid, status="succeeded", phase="resolved", snapshot={
                "incident_runtime": {"started_at": time.time() - 100000, "provider_attempts": 6}}))
    cfg = AppConfig()
    cfg.workflows.max_agent_calls = {level: 4 for level in cfg.workflows.max_agent_calls}
    provider = FakeProvider()
    for index in range(3):
        with db.session() as s:
            s.add(Evidence(incident_id=iid, source="loki", payload={"lines": [f"알 수 없는 표시 원문 {index}"]}))
        stored = Interpreter(cfg, provider).run(iid)
        assert (stored > 0) == (index < 2), "종료 후에도 같은 사건 호출 상한을 유지해야 함"
    assert len(provider.calls) == 2
    with db.session() as s:
        rows = list(s.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)))
        assert len(rows) == 1 and rows[0].status == "succeeded"
        assert rows[0].snapshot["incident_runtime"]["provider_attempts"] == 8
        assert s.get(Incident, iid).status == "resolved", "표시 해석으로 분석을 다시 시작하면 안 됨"


@needs_db
@pytest.mark.parametrize("boundary", ["attempts", "cost", "time", "unknown_cost"])
def test_finished_incident_legacy_budget_never_becomes_free(db_url, boundary):
    from app import db
    from app.config import AppConfig
    from app.models import AgentRun, Incident
    from app.interpret import Interpreter
    iid = seed()
    cfg = AppConfig()
    if boundary == "attempts":
        cfg.workflows.max_agent_calls = {level: 1 for level in cfg.workflows.max_agent_calls}
    elif boundary == "cost":
        cfg.workflows.max_cost_usd = {level: 0.000001 for level in cfg.workflows.max_cost_usd}
    else:
        with db.session() as s:
            prior = s.query(AgentRun).filter_by(incident_id=iid).first()
            if boundary == "time":
                prior.duration_ms = int((cfg.workflows.deadline_s + 1) * 1000)
            else:
                prior.estimated_cost_usd = prior.input_tokens = prior.output_tokens = None
    provider = FakeProvider()
    assert Interpreter(cfg, provider).run(iid) == 0 and not provider.calls
    with db.session() as s:
        assert s.get(Incident, iid).status == "resolved"
        failed = s.query(AgentRun).filter_by(incident_id=iid, role="interpreter").one()
        assert failed.attempts == 0 and "budget" in failed.error


@needs_db
def test_interpreter_failure_is_isolated(db_url):
    from app import db
    from app.models import Incident, Interpretation, RunEvent
    iid = seed()
    assert _interpreter(FakeProvider(fail=True)).run(iid) == 0
    with db.session() as s:
        assert s.query(Interpretation).filter_by(incident_id=iid).count() == 0
        assert s.get(Incident, iid).status == "resolved"
        assert s.query(RunEvent).filter_by(incident_id=iid, kind="agent_finished").count() == 0


@needs_db
def test_closed_incident_with_untranslated_conclusions_requests_reading_then_reports_failure_in_korean(db_url):
    """종료 사건도 영문 분석 결론의 한국어가 없으면 해석을 요청하고, 해석 실패 시 반복 대신 한국어 사유를 보인다."""
    from app import db
    from app.narrative import build_narrative
    iid = seed()                                            # resolved, 영문 결론만 있고 해석 이력 없음
    with db.session() as s:
        first = build_narrative(s, iid)
    assert first["interpreting"] is False and first["interpret_state"]["status"] == "not_requested"
    assert _interpreter(FakeProvider(fail=True)).run(iid) == 0
    with db.session() as s:
        after = build_narrative(s, iid)
    assert after["interpreting"] is False
    assert after["interpret_state"]["status"] == "unavailable"
    assert "한글 해석을 만들지 못했습니다" in after["interpret_state"]["reason_ko"]
    assert "제한 시간" in after["interpret_state"]["reason_ko"]   # timeout → 한국어 사유
    # 상태 사유·제목·행 로그·이벤트는 모델 없이도 한국어로 읽힌다.
    assert after["incident"]["title_ko"].startswith("메모리 초과 강제 종료")
    raw = {r["ref"]: r for r in after["raw"] if r["ref"] in ("E5_recent_events", "E7_error_logs") and r.get("ko")}
    assert "E5_recent_events" in raw and "재시작" in raw["E5_recent_events"]["ko"]
    assert any("inventory 호출이 실패" in (r.get("ko") or "") for r in after["raw"] if r["ref"] == "E7_error_logs")


@needs_db
def test_flow_graph_complex_with_escalation_action_and_steer(db_url):
    from app import db
    from app.flowgraph import build_flow
    iid = seed("COMPLEX", message="로그부터 다시 봐")
    with db.session() as s:
        g = build_flow(s, iid)
    nodes = {n["id"]: n for n in g["nodes"]}
    types = [n["type"] for n in g["nodes"]]
    assert types[:3] == ["signal", "evidence", "router"] and "group" in types and "outcome" in types
    agents = [n for n in g["nodes"] if n["type"] in ("agent", "verify")]
    assert [a["role"] for a in agents].count("k8s_log_analyst") == 2
    assert any(a["status"] == "escalated" for a in agents)
    assert any(e["kind"] == "escalate" for e in g["edges"])
    ch, rp = (next(a for a in agents if a["role"] == r) for r in ("challenger", "recovery_planner"))
    assert ch["depth"] == rp["depth"] and ch["lane"] != rp["lane"]           # parallel stage
    v = next(a for a in agents if a["role"] == "group_verifier")
    assert v["type"] == "verify" and v["role_ko"] == "결과 확인" and v["job_ko"]
    assert v["model"] == "gpt-6.1-sol" and v["tokens"] == {"in": 1000, "out": 100} and "E2_pods" in v["inputs"]
    act = next(n for n in g["nodes"] if n["type"] == "action")
    assert act["status"] == "done" and "메모리" in act["label"]
    assert nodes["outcome"]["status"] == "done" and "복구" in nodes["outcome"]["label"]
    steer = next(n for n in g["nodes"] if n["type"] == "steer")
    assert any(e["kind"] == "steer" and e["source"] == steer["id"] for e in g["edges"])
    ids = set(nodes)
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])


@needs_db
def test_flow_graph_marks_running_planned_and_reroute(db_url):
    from app import db
    from app.flowgraph import build_flow
    iid = seed("NORMAL", status="analyzing", escalate=False, action=None, running_role="coordinator")
    with db.session() as s:
        g = build_flow(s, iid)
    st = {n.get("role"): n["status"] for n in g["nodes"] if n["type"] in ("agent", "verify")}
    assert st["coordinator"] == "running" and st["group_verifier"] == "planned"
    assert next(n for n in g["nodes"] if n["id"] == "outcome")["status"] == "planned"
    iid2 = seed("COMPLEX", extra_routing=True, action=None, status="handed_off")
    with db.session() as s:
        g2 = build_flow(s, iid2)
    assert len([n for n in g2["nodes"] if n["type"] == "group"]) == 2
    assert any(e["kind"] == "reroute" for e in g2["edges"])


@needs_db
def test_narrative_api_shape_and_korean_headline(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    from app.main import app
    iid = seed("COMPLEX")
    with TestClient(app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        r = c.get(f"/api/incidents/{iid}/narrative")
        assert r.status_code == 200, r.text
        d = r.json()
        assert c.get("/api/incidents/999999/narrative").status_code == 404
    h = d["headline"]
    assert {"what", "impact", "stage", "next", "source"} <= set(h)
    assert any("\uac00" <= ch <= "\ud7a3" for ch in h["what"] + h["impact"] + h["next"])
    assert h["stage"]["key"] == "resolved"
    raw = d["raw"]
    assert any(r["ref"] == "E5_recent_events" and "Back-off" in r["raw"] and r["ko"] for r in raw)
    pod = next(r for r in raw if r["ref"] == "E2_pods")
    assert "k8s_log_analyst" in pod["cited_by"]
    assert any(r["kind"] == "agent" for r in raw)
    assert d["flow"]["nodes"] and d["flow"]["edges"]
    assert isinstance(d["interpreting"], bool)
    config.get_settings.cache_clear()
