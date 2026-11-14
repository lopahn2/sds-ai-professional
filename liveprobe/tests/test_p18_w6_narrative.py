"""P18 W6: narrative stage cards (signal/evidence/routing process/group/agent/action/outcome), handoff_view and
Korean interpreter items for routing decisions and hand-offs (display only, no rule readings)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import needs_db

T0 = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)

FEATURES = {
    "crashloop": False, "oom_killed": False, "ready_pods": 1, "event_reasons": [], "latency_signal": False,
    "recent_rollout": False, "signal_reasons": ["TargetHigh5xxRate"], "evidence_errors": [], "waiting_reasons": [],
    "current_revision": "2", "evidence_sources": ["configmap", "deployment", "endpoints", "k8s_event", "loki", "node",
                                                  "pod_status", "prometheus", "service", "code_refs"],
    "image_pull_failing": False, "protected_resource": False, "network_drop_signal": False, "evidence_source_count": 10,
    "has_previous_revision": True, "affected_workload_count": 1, "current_revision_age_min": 296.8,
    "service_has_no_endpoints": False, "dependent_callers_failing": [], "service_selector_matches_zero_pods": False,
    "endpoints_empty_because_workload_down": False,
}
POLICY = {
    "version": "v2", "weights": {"quality": 0.6, "cost": 0.25, "throughput": 0.15}, "selected_model": "gpt-6.1-sol",
    "selected_reason": "highest eligible weighted score; lexical tie-break", "workflow": "complex_group",
    "candidate_scores": [
        {"model": "gpt-6-astra", "eligible": True, "score": 0.71, "quality": 0.95, "throughput": 0.4, "estimated_cost_usd": 0.02},
        {"model": "gpt-6.1-sol", "eligible": True, "score": 0.82, "quality": 0.85, "throughput": 0.7, "estimated_cost_usd": 0.004},
        {"model": "gpt-6-luna", "eligible": False, "reason": "below quality floor"},
        {"model": "gpt-x", "eligible": False, "reason": "unknown price or throughput"},
    ],
    "price_basis_tokens": {"input": 1000, "output": 500},
}
HANDOFF = {
    "summary": "order 5xx comes from inventory DB pool exhaustion after a slow query.",
    "root_cause_candidates": [{"cause": "inventory Hikari pool exhausted by slow query", "confidence": 0.65,
                               "evidence_refs": ["E7"], "verifier": "supported"}],
    "recommended_steps": ["Check slow query log on inventory DB.", "Raise pool size temporarily.",
                          "Add index on orders.sku.", "Re-run load test."],
    "notes": ["COMPLEX: operator handoff (no typed action fits)"], "group": "complex_group", "workflow_status": "verified",
    "counter_evidence": [], "verification": {"verdict": "supported", "candidate_verdicts": [{"index": 0, "verdict": "supported"}]},
    "budget": {"agent_calls": 5, "estimated_cost_usd": 0.4427}, "confidence": 0.65,
    "remediation": {"kind": "none", "why": "OOMKilled was not observed; pool exhaustion is not fixable by an allowed action."},
}
EVIDENCE = [
    ("deployment", {"name": "inventory", "replicas": 1, "ready_replicas": 1}, None),
    ("pod_status", [{"name": "inventory-a", "phase": "Running", "containers": [{"name": "inventory", "restart_count": 0}]}], None),
    ("service", {"name": "inventory", "ready_endpoints": 1}, None),
    ("endpoints", {"name": "inventory", "addresses": 1}, None),
    ("k8s_event", [], None),
    ("prometheus", {"http_5xx_rate_per_s": {"order": 2.0}}, None),
    ("loki", {"lines": ["HikariPool-1 - Connection is not available, request timed out after 3000ms"]}, None),
    ("node", {"name": "n1", "conditions": {"Ready": {"status": "True"}}}, None),
    ("configmap", {"name": "inventory-config", "keys": ["a"], "sha256": "x"}, None),
    ("code_refs", None, "repository unavailable"),
]
COMPLEX_ROLES = ["k8s_log_analyst", "cause_reasoner", "challenger", "recovery_planner", "group_verifier"]


def seed(status: str = "handed_off", *, running: str | None = None, reroute: bool = False, action: bool = False) -> tuple[int, int]:
    """A COMPLEX incident with 10 evidence rows (one failed), the router run, group audits and a hand-off."""
    from app import db
    from app.models import Action, AgentRun, AuditLog, Evidence, Incident, RoutingDecision, RunEvent
    with db.session() as s:
        inc = Incident(fingerprint="w6", namespace="liveprobe-target", workload="inventory", status=status, system="shop",
                       severity_level="COMPLEX", title="TargetHigh5xxRate on Deployment/inventory", signal_count=3,
                       signal_reasons=["TargetHigh5xxRate"], created_at=T0, updated_at=T0 + timedelta(minutes=5),
                       handoff=HANDOFF if status in ("handed_off", "closed") else None)
        s.add(inc)
        s.flush()
        iid = inc.id
        s.add(AuditLog(actor="watcher", event="incident_created", incident_id=iid, ts=T0,
                       detail={"source": "alertmanager", "reason": "TargetHigh5xxRate", "object": "Deployment/inventory",
                               "fingerprint": "w6"}))
        for i, (src, payload, err) in enumerate(EVIDENCE):
            s.add(Evidence(incident_id=iid, source=src, target=f"liveprobe-target/{src}", payload=payload, error=err,
                           collected_at=T0 + timedelta(seconds=2 + i)))
        s.add(AgentRun(incident_id=iid, role="router", provider="codex_cli", model="gpt-6-luna", reasoning_effort="low",
                       status="succeeded", attempts=1, created_at=T0 + timedelta(seconds=15),
                       output={"level": "COMPLEX", "rationale": "Several weak signals across services; no single clear change.",
                               "confidence": 0.8}))
        rd = RoutingDecision(incident_id=iid, level="COMPLEX", workflow="complex_group", features=FEATURES, policy=POLICY,
                             rationale=("no deterministic SIMPLE/NORMAL/HIGH_RISK pattern matched. LLM agrees: "
                                        "Several weak signals across services; no single clear change."),
                             confidence=0.8, decided_by="rules+llm", created_at=T0 + timedelta(seconds=20))
        s.add(rd)
        s.flush()
        rid = rd.id
        s.add(AuditLog(actor="workflow", event="wf_group_started", incident_id=iid, ts=T0 + timedelta(seconds=21), detail={
            "group": "complex_group", "roles": {r: {"model": "gpt-6.1-sol" if r == "cause_reasoner" else "gpt-6-astra", "effort": "medium"}
                                                for r in COMPLEX_ROLES},
            "budget": {"max_agent_calls": 12, "max_cost_usd": 1.5, "deadline_s": 900, "agent_calls": 0}}))
        roles = COMPLEX_ROLES[: COMPLEX_ROLES.index(running)] if running else COMPLEX_ROLES
        t = T0 + timedelta(seconds=30)
        for role in roles:
            t += timedelta(seconds=10)
            out = {"summary": f"{role}: inventory DB pool exhausted.", "confidence": 0.7, "counter_evidence": ["x"],
                   "root_cause_candidates": [{"cause": "pool exhausted", "confidence": 0.65, "evidence_refs": ["E7"],
                                              "verifier": "supported"}], "next_action": "handoff"}
            if role in ("challenger", "group_verifier"):
                out["verdict"] = "supported"
            s.add(AgentRun(incident_id=iid, role=role, provider="codex_cli", model="gpt-6-astra", reasoning_effort="medium",
                           status="succeeded", attempts=1, output=out, created_at=t))
        if running:
            s.add(RunEvent(incident_id=iid, seq=1, kind="agent_started", summary="", detail={"role": running}, created_at=t))
        else:
            for k, (ev, det) in enumerate([
                ("wf_early_stop", {"stage": "challenge", "reason": "challenger verdict supported; re-reasoning round skipped"}),
                ("wf_early_stop", {"stage": "verification", "reason": "independent verifier supports the conclusion; no further rounds"}),
                ("wf_group_finished", {"status": "verified", "candidates": 1,
                                       "budget": {"agent_calls": 5, "estimated_cost_usd": 0.4427}})]):
                s.add(AuditLog(actor="workflow", event=ev, incident_id=iid, ts=t + timedelta(seconds=1 + k), detail=det))
        if reroute:
            s.add(RoutingDecision(incident_id=iid, level="HIGH_RISK", workflow="high_risk_group", features=FEATURES, policy=POLICY,
                                  rationale="운영자 채팅으로 HIGH_RISK Group 재지정: 고위험으로 바꿔", confidence=1.0,
                                  decided_by="operator", created_at=t + timedelta(seconds=5)))
        if action:
            s.add(Action(incident_id=iid, type="set_memory_limit", target="liveprobe-target/Deployment/inventory",
                         params={"memory_limit_mi": 640}, policy_mode="auto", status="succeeded", executed=True,
                         created_at=t + timedelta(seconds=8)))
    return iid, rid


def _narr(iid):
    from app import db
    from app.narrative import build_narrative
    with db.session() as s:
        return build_narrative(s, iid)


def _node(d, nid):
    return next(n for n in d["flow"]["nodes"] if n["id"] == nid)


# ------------------------------------------------------------------ pure rules (routing_ko)
def test_features_become_korean_facts_and_distinguish_none_from_unobserved():
    from app.routing_ko import features_ko
    facts = features_ko(FEATURES)
    for want in ("준비된 Pod 1개", "반복 재시작 없음", "메모리 초과 종료 없음", "최근 배포 없음(현재 버전 2, 296.8분 전)",
                 "Service 연결 대상 있음", "호출 서비스 실패 없음", "감지 신호: TargetHigh5xxRate", "수집 출처 10종, 수집 오류 없음"):
        assert want in facts, (want, facts)
    partial = features_ko({"ready_pods": None, "evidence_source_count": 3, "evidence_errors": ["loki"]})
    assert "준비된 Pod 수 미관측" in partial and any("수집 오류" in f and "로그" in f for f in partial)
    assert not any("반복 재시작" in f for f in partial)            # absent key is omitted, not "없음"


@pytest.mark.parametrize("rationale,decided_by,rule_frag,final_frag", [
    ("no deterministic SIMPLE/NORMAL/HIGH_RISK pattern matched. LLM agrees: weak signals", "rules+llm",
     "단순·보통·고위험 규칙에 해당하지 않아 기본값 복잡으로 판정", "동의"),
    ("bounded OOM/latency signal; requires multi-source analysis without automatic action. LLM raised level NORMAL->COMPLEX: x",
     "rules+llm", "보통", "보통보다 높은 복잡"),
    ("Deployment with ImagePullBackOff after a rollout 3.2 min ago (revision 4). LLM suggested lower level SIMPLE; "
     "conservative rule level kept.", "rules+llm", "단순", "보수적"),
    ("NetworkDrop: observed dropped/error traffic on a workload path; operator handoff, no automatic action "
     "(non-overridable). LLM suggested COMPLEX; rules win for HIGH_RISK.", "rules+llm", "고위험", "고위험 규칙이 우선"),
    ("no deterministic SIMPLE/NORMAL/HIGH_RISK pattern matched. LLM suggested HIGH_RISK; rules keep COMPLEX: y", "rules+llm",
     "복잡", "규칙만"),
    ("bounded OOM/latency signal; requires multi-source analysis without automatic action. LLM raised level NORMAL->COMPLEX "
     "(HIGH_RISK is reserved for the rules): z", "rules+llm", "보통", "규칙만"),
    ("LLM classifier confidence below threshold; operator handoff", "router_failure", "기록", "신뢰도"),
    ("critical evidence unavailable; operator handoff", "router_failure", "핵심 근거", "인계"),
    ("운영자 채팅으로 HIGH_RISK Group 재지정: 고위험으로", "operator", "운영자", "운영자가 Group을 바꿔 다시 분석"),
])
def test_rule_and_final_steps_follow_the_recorded_rationale(rationale, decided_by, rule_frag, final_frag):
    from app.routing_ko import rule_step_ko, final_step_ko
    assert rule_frag in rule_step_ko(rationale, decided_by)
    assert final_frag in final_step_ko(rationale, decided_by, "COMPLEX")


def test_model_policy_reason_and_candidates_in_korean():
    from app.routing_ko import model_policy_ko
    mp = model_policy_ko(POLICY)
    assert mp["selected_model"] == "gpt-6.1-sol"
    assert mp["reason_ko"] == "적격 후보 중 가중 점수(품질 0.6·비용 0.25·처리량 0.15)가 가장 높은 gpt-6.1-sol 선택"
    reasons = {c["model"]: c for c in mp["candidates"]}
    assert reasons["gpt-6-luna"]["reason_ko"] == "품질 기준 미달" and reasons["gpt-6-luna"]["eligible"] is False
    assert reasons["gpt-x"]["reason_ko"] == "가격·처리량 정보 없음"
    assert reasons["gpt-6.1-sol"]["score"] == 0.82
    assert model_policy_ko({"handoff_only": True, "selected_reason": "x"})["selected_model"] is None
    assert model_policy_ko(None) is None


def test_missing_observations_never_become_negative_facts():
    from app.routing_ko import features_ko
    facts = features_ko({"dependent_callers_failing": None, "latency_signal": None, "evidence_source_count": 2})
    assert "호출 서비스 실패 미관측" in facts and "지연 신호 미관측" in facts
    assert "수집 출처 2종, 수집 오류 미관측" in facts


@needs_db
def test_no_handoff_record_returns_null_and_outcome_uses_updated_at(db_url):
    from app import db
    from app.models import Incident
    iid, _ = seed()
    with db.session() as s:
        i = s.get(Incident, iid)
        i.handoff = None
        i.closed_at = T0 + timedelta(minutes=4)
        i.updated_at = T0 + timedelta(minutes=6)  # change the persisted value, avoiding the SQLAlchemy onupdate default
        i.status = "closed"
    d = _narr(iid)
    assert d["handoff_view"] is None
    assert _node(d, "outcome")["at"] == (T0 + timedelta(minutes=6)).isoformat()


# ------------------------------------------------------------------ DB: cards
@needs_db
def test_signal_and_evidence_cards_count_the_same_set_as_the_label(db_url):
    iid, _ = seed()
    d = _narr(iid)
    sig = _node(d, "signal")
    assert sig["at"] == T0.isoformat()
    c = sig["card"]
    assert c["kind"] == "signal" and c["source"] == "alertmanager" and c["reason"] == "TargetHigh5xxRate"
    assert c["signal_count"] == 3 and c["first_at"] == T0.isoformat() and "inventory" in c["place_ko"] and c["what_ko"]
    ev = _node(d, "evidence")
    e = ev["card"]
    assert e["kind"] == "evidence" and e["total"] == 10 == len(e["items"]) and ev["label"] == "근거 수집 10건"
    assert e["ok"] == 9 and e["failed"] == 1
    assert ev["at"] == (T0 + timedelta(seconds=2)).isoformat()
    by = {i["source"]: i for i in e["items"]}
    assert by["code_refs"]["ok"] is False and by["code_refs"]["error"] == "repository unavailable" and by["code_refs"]["ref"] is None
    assert by["endpoints"]["ref"] == "E3_service" and by["deployment"]["ref"] == "E1_deployment"
    assert by["deployment"]["summary_ko"].startswith("Deployment 목표 Pod 1개") and by["deployment"]["source_ko"]
    assert by["loki"]["summary_ko"] and "DB 연결 풀" in by["loki"]["summary_ko"]       # rule reading of the log line
    assert all(i["collected_at"] for i in e["items"])


@needs_db
def test_routing_card_shows_the_five_step_decision_process(db_url):
    from app import db
    from app.models import Interpretation
    from app.interpret import sha
    iid, rid = seed()
    d = _narr(iid)
    r = _node(d, "router")
    assert r["at"] == (T0 + timedelta(seconds=20)).isoformat()
    c = r["card"]
    assert c["kind"] == "routing" and c["routing_id"] == rid and c["level"] == "COMPLEX" and c["level_ko"] == "복잡"
    assert c["decided_by"] == "rules+llm" and c["decided_by_ko"] and c["confidence"] == 0.8
    assert [st["key"] for st in c["steps"]] == ["features", "rule", "llm", "final", "model"]
    assert [st["source"] for st in c["steps"]] == ["rules", "rules", "llm", "rules", "policy"]
    assert "준비된 Pod 1개" in c["steps"][0]["items_ko"]
    assert "기본값 복잡" in c["steps"][1]["detail_ko"]
    assert c["llm"]["level"] == "COMPLEX" and c["llm"]["confidence"] == 0.8 and c["llm"]["rationale_ko"] is None
    assert c["interpreting"] is True and c["reason_raw"].startswith("no deterministic")
    assert c["final_reason_ko"] and "동의" in c["final_reason_ko"]
    assert c["model_policy"]["selected_model"] == "gpt-6.1-sol"
    assert all(st["title_ko"] and isinstance(st["detail_ko"], str) for st in c["steps"])
    with db.session() as s:
        raw = "Several weak signals across services; no single clear change."
        s.add(Interpretation(incident_id=iid, item_key=f"routing:{rid}:llm", raw_sha256=sha(raw), ko="여러 서비스에 약한 신호가 흩어져 있습니다.",
                             source="llm"))
    c2 = _node(_narr(iid), "router")["card"]
    assert c2["llm"]["rationale_ko"] == "여러 서비스에 약한 신호가 흩어져 있습니다." and c2["interpreting"] is False
    assert "여러 서비스" in c2["steps"][2]["detail_ko"]


@needs_db
def test_group_card_has_decision_roles_budget_and_progress(db_url):
    iid, rid = seed()
    d = _narr(iid)
    g = _node(d, f"group:{rid}")
    assert g["at"] == (T0 + timedelta(seconds=20)).isoformat()
    c = g["card"]
    assert c["kind"] == "group" and c["routing_id"] == rid and c["group_ko"] == "복잡 Group"
    assert [st["key"] for st in c["decision"]["steps"]] == ["features", "rule", "llm", "final", "model"]
    assert c["decision"]["level_ko"] == "복잡" and c["decision"]["final_reason_ko"]
    assert [x["role"] for x in c["roles"]] == COMPLEX_ROLES
    cr = next(x for x in c["roles"] if x["role"] == "cause_reasoner")
    assert cr["model"] == "gpt-6.1-sol" and cr["effort"] == "medium" and cr["role_ko"] == "원인 추론" and cr["job_ko"]
    assert all(x["status"] == "done" for x in c["roles"])
    assert c["budget"] == {"max_agent_calls": 12, "max_cost_usd": 1.5, "deadline_s": 900}
    assert "반론 검토가 결론을 지지해 재추론을 생략" in c["progress_ko"]
    assert "독립 검증이 결론을 지지해 추가 회차 없음" in c["progress_ko"]
    assert any(p.startswith("Group 종료: 검증 통과") and "Agent 호출 5회" in p and "0.4427 USD" in p for p in c["progress_ko"])
    assert c["status_ko"]


@needs_db
def test_rerouted_group_has_its_own_operator_decision(db_url):
    iid, rid = seed(reroute=True)
    d = _narr(iid)
    groups = [n for n in d["flow"]["nodes"] if n["type"] == "group"]
    assert len(groups) == 2
    second = groups[1]["card"]
    assert second["routing_id"] != rid and second["level"] == "HIGH_RISK"
    assert second["decision"]["final_reason_ko"] == "운영자가 Group을 바꿔 다시 분석"
    assert second["decision"]["decided_by_ko"] == "운영자"


@needs_db
def test_agent_and_verify_cards_and_planned_nodes_accept_steering(db_url):
    iid, _ = seed()
    d = _narr(iid)
    v = next(n for n in d["flow"]["nodes"] if n.get("role") == "group_verifier")
    c = v["card"]
    assert c["kind"] == "agent" and c["agent_run_id"] == v["agent_run_id"] and v["at"]
    assert c["summary_raw"].startswith("group_verifier:") and c["summary_ko"] is None
    # 영문 원인 후보에 모델 한국어 표기가 없으면 cause_ko는 None이며 영문 원문은 그대로 보존한다.
    assert c["candidates"] == [{"cause_raw": "pool exhausted", "cause_ko": None, "confidence": 0.65, "verifier": "supported"}]
    assert c["verdict"] == "supported" and c["verdict_ko"] == "근거로 지지"
    assert c["next_action"] == "handoff" and c["next_action_ko"] == "운영자 인계"
    assert c["counter_evidence_count"] == 1 and c["error"] is None
    assert c["accepts_steering"] is False and c["steer_target_role"] == "group_verifier"     # handed off
    live, _ = seed("analyzing", running="challenger")
    d2 = _narr(live)
    plan = next(n for n in d2["flow"]["nodes"] if n["id"].startswith("plan:") and n["role"] == "group_verifier")
    assert plan["card"]["agent_run_id"] is None and plan["card"]["summary_raw"] is None
    assert plan["card"]["accepts_steering"] is True and plan["at"] is None
    done = next(n for n in d2["flow"]["nodes"] if n.get("role") == "cause_reasoner")
    assert done["card"]["accepts_steering"] is True                      # later stages of the group remain
    running = next(n for n in d2["flow"]["nodes"] if n.get("role") == "challenger")
    assert running["status"] == "running" and running["card"]["accepts_steering"] is True


@needs_db
def test_action_and_outcome_cards(db_url):
    iid, _ = seed(action=True)
    d = _narr(iid)
    a = next(n for n in d["flow"]["nodes"] if n["type"] == "action")
    assert a["at"] and a["card"]["kind"] == "action" and a["card"]["type_ko"] == "메모리 한도 조정"
    assert a["card"]["status"] == "succeeded" and a["card"]["status_ko"] == "성공"
    assert a["card"]["changes"] == ["메모리 한도 → 640Mi"] and a["card"]["target"].endswith("inventory")
    o = _node(d, "outcome")
    assert o["at"] == (T0 + timedelta(minutes=5)).isoformat()
    assert o["card"]["kind"] == "outcome" and o["card"]["status"] == "handed_off" and o["card"]["status_ko"] == "운영자 인계"
    assert o["card"]["handoff"] == d["handoff_view"]


# ------------------------------------------------------------------ DB: handoff view and interpreter items
@needs_db
def test_handoff_view_summary_and_decisions_before_and_after_interpretation(db_url):
    from app import db
    from app.interpret import sha
    from app.models import Interpretation
    iid, _ = seed()
    hv = _narr(iid)["handoff_view"]
    assert hv["summary_ko"] is None and hv["summary_raw"] == HANDOFF["summary"] and hv["interpreting"] is True
    dec = {x["key"]: x for x in hv["decisions"]}
    assert list(dec) == ["outcome", "cause", "auto_action", "operator"]
    assert dec["outcome"]["label_ko"] == "결과" and dec["outcome"]["value_ko"] == "운영자 인계 · 자동 복구 아님"
    assert dec["cause"]["value_ko"].startswith("한글 해석 준비 중") and "(신뢰도 0.65, 독립 검증: 근거로 지지)" in dec["cause"]["value_ko"]
    assert dec["auto_action"]["value_ko"].startswith("실행하지 않음")
    assert dec["operator"]["value_ko"] == "원인을 확인한 뒤 대화로 조치를 지시하거나 장애를 종료하세요."
    assert hv["next_steps_raw"] == HANDOFF["recommended_steps"] and hv["next_steps_ko"] == []
    texts = {"handoff:summary": (HANDOFF["summary"], "inventory DB 연결 풀 고갈로 order 5xx가 발생했습니다."),
             "handoff:cause#0": (HANDOFF["root_cause_candidates"][0]["cause"], "느린 쿼리로 inventory 연결 풀이 고갈됨"),
             "handoff:why": (HANDOFF["remediation"]["why"], "허용된 조치로 고칠 수 없는 연결 풀 고갈입니다."),
             **{f"handoff:step#{i}": (HANDOFF["recommended_steps"][i], f"{i + 1}단계를 확인하세요. 추가 설명입니다.") for i in range(3)}}
    with db.session() as s:
        for key, (raw, ko) in texts.items():
            s.add(Interpretation(incident_id=iid, item_key=key, raw_sha256=sha(raw), ko=ko, source="llm"))
    hv2 = _narr(iid)["handoff_view"]
    assert hv2["summary_ko"].startswith("inventory DB 연결 풀") and hv2["interpreting"] is False
    dec2 = {x["key"]: x for x in hv2["decisions"]}
    assert dec2["cause"]["value_ko"] == "느린 쿼리로 inventory 연결 풀이 고갈됨 (신뢰도 0.65, 독립 검증: 근거로 지지)"
    assert dec2["auto_action"]["value_ko"] == "실행하지 않음 — 허용된 조치로 고칠 수 없는 연결 풀 고갈입니다."
    assert hv2["next_steps_ko"] == ["1단계를 확인하세요.", "2단계를 확인하세요.", "3단계를 확인하세요."]   # max 3, one sentence


@needs_db
def test_handoff_view_is_null_while_analysing_and_closed_wording(db_url):
    from app import db
    from app.models import Incident
    live, _ = seed("analyzing", running="challenger")
    assert _narr(live)["handoff_view"] is None
    iid, _ = seed("closed")
    with db.session() as s:
        inc = s.get(Incident, iid)
        inc.handoff = {**HANDOFF, "closed_by": "kim", "close_reason": "수동 복구 확인"}
    dec = {x["key"]: x for x in _narr(iid)["handoff_view"]["decisions"]}
    assert dec["outcome"]["value_ko"] == "운영자 종료 · 서비스 복구 판정 아님"
    assert "kim" in dec["operator"]["value_ko"]


@needs_db
def test_decision_and_handoff_items_never_use_rule_readings_and_stay_out_of_raw(db_url):
    from app import db
    from app.interpret import incident_items, rule_ko
    from app.models import Incident
    iid, rid = seed()
    with db.session() as s:
        inc = s.get(Incident, iid)
        inc.handoff = {**HANDOFF, "summary": "container OOMKilled with CrashLoopBackOff"}   # would match a rule pattern
        items = incident_items(s, iid)
    keys = {i["id"]: i for i in items}
    assert keys[f"routing:{rid}:llm"]["kind"] == "decision"
    assert keys[f"routing:{rid}:llm"]["raw"] == "Several weak signals across services; no single clear change."
    for k in ("handoff:summary", "handoff:cause#0", "handoff:why", "handoff:step#0", "handoff:step#1", "handoff:step#2"):
        assert keys[k]["kind"] == "handoff", k
    assert "handoff:step#3" not in keys
    assert rule_ko(keys["handoff:summary"]) is None and rule_ko(keys[f"routing:{rid}:llm"]) is None
    d = _narr(iid)
    assert not any(r["kind"] in ("decision", "handoff") for r in d["raw"])
    assert d["interpreting"] is False and d["interpret_state"]["status"] == "not_requested"


@needs_db
def test_pending_handoff_keeps_interpreting_even_after_an_earlier_interpreter_run(db_url):
    from app import db
    from app.models import AgentRun
    iid, _ = seed()
    with db.session() as s:
        s.add(AgentRun(incident_id=iid, role="interpreter", provider="fake", model="fake", attempts=1,
                       status="succeeded", output={"items": 0}))
    assert _narr(iid)["interpret_state"]["status"] == "not_requested"


class _Prov:
    name = "codex_cli"

    def __init__(self):
        self.prompts = []

    def complete(self, role, prompt, schema, model, effort, timeout):
        from app.llm.base import LLMResult, Usage
        self.prompts.append(prompt)
        ids = json.loads(prompt.split("ITEMS_JSON:", 1)[1])
        return LLMResult(text="", data={"items": [{"id": it["id"], "ko": f"한글 결정 {it['id']}"} for it in ids]},
                         usage=Usage(input_tokens=10, output_tokens=10), duration_ms=1)


@needs_db
def test_interpreter_reads_decision_and_handoff_items_once(db_url):
    from app.config import AppConfig
    from app.interpret import Interpreter
    iid, rid = seed()
    cfg = AppConfig()
    # 화면 전용 fixture도 사건 예산으로 이관할 과거 합성 사용량을 명시한다.
    from app import db
    from app.models import AgentRun
    from app.llm import estimate_cost
    from app.llm.base import Usage
    with db.session() as s:
        for prior in s.query(AgentRun).filter_by(incident_id=iid):
            prior.input_tokens, prior.output_tokens, prior.duration_ms = 10, 10, 1
            prior.estimated_cost_usd = estimate_cost(cfg, prior.model, Usage(10, 10))
    cfg.assist.interpret_max_items = 60
    p = _Prov()
    assert Interpreter(cfg, p).run(iid) > 0
    payload = {it["id"] for it in json.loads(p.prompts[0].split("ITEMS_JSON:", 1)[1])}
    assert {f"routing:{rid}:llm", "handoff:summary", "handoff:cause#0", "handoff:why", "handoff:step#0"} <= payload
    d = _narr(iid)
    assert _node(d, "router")["card"]["llm"]["rationale_ko"] == f"한글 결정 routing:{rid}:llm"
    assert d["handoff_view"]["summary_ko"] == "한글 결정 handoff:summary" and d["handoff_view"]["interpreting"] is False
    Interpreter(cfg, p).run(iid)
    assert len(p.prompts) == 1                                           # cached by raw hash
