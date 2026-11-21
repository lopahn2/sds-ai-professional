"""P18 W6: Korean rule sentences for the recorded routing decision process (display only).

Every sentence here is derived from persisted facts only — RoutingDecision.features / rationale / policy, the router
AgentRun output and workflow audit records. Nothing is inferred beyond what was recorded; an unknown rationale prefix
is reported as "기록된 규칙 문장과 대응되지 않음" with the raw text kept by the caller.
"""
from __future__ import annotations

import re
from typing import Any

from .roles_ko import LEVEL_KO

DECIDED_BY_KO = {"rules": "규칙", "rules+llm": "규칙 + AI 분류", "operator": "운영자", "router_failure": "규칙(분류 실패 인계)"}
SOURCE_KO = {"deployment": "Deployment", "pod_status": "Pod 상태", "service": "Service", "endpoints": "엔드포인트",
             "k8s_event": "Kubernetes 이벤트", "prometheus": "지표(Prometheus)", "loki": "로그(Loki)", "node": "노드",
             "configmap": "ConfigMap", "code_refs": "코드 참조", "investigation": "추가 근거 탐색"}
SOURCE_REF = {"deployment": "E1_deployment", "pod_status": "E2_pods", "service": "E3_service", "endpoints": "E3_service",
              "k8s_event": "E5_recent_events", "prometheus": "E6_metrics", "loki": "E7_error_logs", "node": "E8_node",
              "configmap": "E9_config", "code_refs": None}
VERDICT_KO = {"supported": "근거로 지지", "contradicted": "반박됨", "insufficient": "근거 부족",
              "not_applicable": "해당 없음(검토 역할 아님)"}
NEXT_ACTION_KO = {"handoff": "운영자 인계", "investigate_more": "추가 조사 필요", "none": "조치 없음", "act": "Kubernetes 조치 제안"}
WF_STATUS_KO = {"verified": "검증 통과", "unverified": "검증 미통과", "stopped": "한도 도달로 중단", "failed": "실패"}
STAGE_KO = {"coordinator": "진행 조정", "challenge": "반론 검토", "verification": "독립 검증"}


def _lv(level: str | None) -> str:
    return LEVEL_KO.get(level or "", level or "미정")


def pair_router_runs(routing: list[Any], runs: list[Any]) -> dict[int, dict]:
    """RoutingDecision.id -> router AgentRun.output of the classification that fed it (operator reroutes have none)."""
    routers = [r for r in runs if r.role == "router" and r.status == "succeeded" and r.output]
    used: set[int] = set()
    out: dict[int, dict] = {}
    for rd in routing:
        if rd.decided_by == "operator":
            continue
        cand = [r for r in routers if r.id not in used and (rd.created_at is None or r.created_at is None
                                                            or r.created_at <= rd.created_at)]
        if cand:
            used.add(cand[-1].id)
            out[rd.id] = cand[-1].output
    return out


def topic(word: str) -> str:
    """'은' after a final consonant, otherwise '는' (Latin-ending names get '은')."""
    ch = (word or " ")[-1]
    if "\uac00" <= ch <= "\ud7a3":
        return word + ("은" if (ord(ch) - 0xAC00) % 28 else "는")
    return word + "은"


def first_sentence(text: str | None) -> str | None:
    if not text:
        return None
    m = re.match(r"^(.+?[.!?。])(\s|$)", text.strip(), re.S)
    return (m.group(1) if m else text.strip()) or None


# ------------------------------------------------------------------ 1. features
def features_ko(f: dict | None) -> list[str]:
    """Recorded features as Korean facts. A missing key is omitted; a key present with null is '미관측'."""
    f = f or {}
    out: list[str] = []

    def has(k):
        return k in f

    if has("ready_pods"):
        out.append(f"준비된 Pod {f['ready_pods']}개" if isinstance(f["ready_pods"], int) else "준비된 Pod 수 미관측")
    for key, yes, no in (("crashloop", "반복 재시작 관측", "반복 재시작 없음"),
                         ("oom_killed", "메모리 초과 종료 관측", "메모리 초과 종료 없음"),
                         ("image_pull_failing", "이미지 내려받기 실패 관측", "이미지 내려받기 실패 없음")):
        if has(key):
            out.append(yes if f[key] else (no if f[key] is False else f"{no.rsplit(' ', 1)[0]} 미관측"))
    if has("recent_rollout"):
        rev, age = f.get("current_revision"), f.get("current_revision_age_min")
        tail = []
        if rev is not None:
            tail.append(f"현재 버전 {rev}")
        if age is not None:
            tail.append(f"{age}분 전")
        info = f"({', '.join(tail)})" if tail else ""
        out.append(("최근 배포 있음" if f["recent_rollout"] else "최근 배포 없음") + info if f["recent_rollout"] is not None
                   else "최근 배포 여부 미관측")
    if has("waiting_reasons") and f["waiting_reasons"]:
        out.append("대기 사유: " + ", ".join(map(str, f["waiting_reasons"])))
    if has("service_has_no_endpoints"):
        state = f.get("service_backend_state")
        if state in ("scaled_down", "manual", "external_name"):
            out.append({"scaled_down": "연결 대상 목표 Pod 0개", "manual": "연결 대상 수동 관리",
                        "external_name": "외부 DNS 이름으로 연결"}[state])
        elif f["service_has_no_endpoints"] is None:
            out.append("Service 연결 대상 미관측")
        else:
            out.append("Service 연결 대상 없음" if f["service_has_no_endpoints"] else "Service 연결 대상 있음")
    if f.get("service_selector_matches_zero_pods"):
        out.append("Service 선택자가 Pod 0개와 일치")
    if f.get("endpoints_empty_because_workload_down"):
        out.append("워크로드가 내려가 연결 대상이 비어 있음")
    if has("dependent_callers_failing"):
        callers = f["dependent_callers_failing"]
        out.append("호출 서비스 실패 미관측" if callers is None else
                   "호출 서비스 실패: " + ", ".join(map(str, callers)) if callers else "호출 서비스 실패 없음")
    if has("latency_signal"):
        out.append("지연 신호 미관측" if f["latency_signal"] is None else "지연 신호 관측" if f["latency_signal"] else "지연 신호 없음")
    if f.get("network_drop_signal"):
        out.append("네트워크 차단 신호 관측")
    if f.get("protected_resource"):
        out.append("보호 자원(Node·Secret·Namespace) 관련")
    if has("affected_workload_count") and isinstance(f["affected_workload_count"], int) and f["affected_workload_count"] != 1:
        out.append(f"영향받은 워크로드 {f['affected_workload_count']}개")
    if has("event_reasons") and f["event_reasons"]:
        out.append("이벤트 사유: " + ", ".join(map(str, f["event_reasons"][:6])))
    if has("signal_reasons") and f["signal_reasons"]:
        out.append("감지 신호: " + ", ".join(map(str, f["signal_reasons"])))
    if has("evidence_source_count"):
        errs = f.get("evidence_errors") or []
        err = ("수집 오류: " + ", ".join(str(SOURCE_KO.get(e, e)) for e in errs)) if errs else (
            "수집 오류 없음" if f.get("evidence_errors") is not None else "수집 오류 미관측")
        out.append(f"수집 출처 {f['evidence_source_count']}종, {err}")
    if "evidence_error" in f:
        out.append(f"특징 추출 실패({f['evidence_error']})")
    return out


# ------------------------------------------------------------------ 2. rule verdict
_RULE_PREFIX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^NetworkDrop:", re.I), "네트워크 차단(NetworkDrop)이 관측되어 고위험으로 판정(자동 조치 없음, 변경 불가)"),
    (re.compile(r"^(protected resource implicated|Service selector matches 0 pods|Service has 0 ready endpoints|"
                r"dependent callers failing)", re.I),
     "보호 자원 또는 트래픽 라우팅 문제(여러 서비스 영향)로 고위험으로 판정(변경 불가)"),
    (re.compile(r"^Deployment with .* after a rollout", re.I), "최근 배포 직후 이미지 내려받기 실패·반복 재시작이 있어 단순으로 판정"),
    (re.compile(r"^bounded OOM/latency signal", re.I), "메모리 초과 또는 지연 신호가 있어 보통으로 판정(자동 조치 없이 다중 근거 분석)"),
    (re.compile(r"^no deterministic SIMPLE/NORMAL/HIGH_RISK pattern matched", re.I),
     "단순·보통·고위험 규칙에 해당하지 않아 기본값 복잡으로 판정"),
    (re.compile(r"^critical evidence unavailable", re.I), "핵심 근거(Deployment·Pod·Service·엔드포인트) 수집 실패로 고위험 인계"),
    (re.compile(r"^deployment/pod evidence missing", re.I), "Deployment·Pod 근거가 없어 고위험 인계"),
    (re.compile(r"^no routing evidence", re.I), "판단할 근거가 없어 고위험 인계"),
    (re.compile(r"^feature extraction failed", re.I), "근거에서 특징을 추출하지 못해 고위험 인계"),
    (re.compile(r"^LLM classifier unavailable", re.I), "AI 분류기를 사용할 수 없어 고위험 인계"),
    (re.compile(r"^LLM classifier confidence below threshold", re.I), "규칙 판정은 기록에 없음 — AI 분류 신뢰도 미달로 고위험 인계"),
    (re.compile(r"^model policy unavailable", re.I), "분석 모델 정책을 적용할 수 없어 고위험 인계"),
]


def rule_step_ko(rationale: str | None, decided_by: str | None) -> str:
    r = (rationale or "").strip()
    if decided_by == "operator":
        return "운영자 재지정으로 규칙 판정을 다시 하지 않음(이전 판정의 관측 특징을 그대로 사용)"
    for pat, ko in _RULE_PREFIX:
        if pat.search(r):
            return ko
    return "기록된 규칙 문장과 대응되지 않음(원문 참조)"


# ------------------------------------------------------------------ 4. final decision
_RAISED = re.compile(r"LLM raised level (\w+)->(\w+)")


def final_step_ko(rationale: str | None, decided_by: str | None, level: str | None) -> str:
    r = rationale or ""
    if decided_by == "operator":
        return "운영자가 Group을 바꿔 다시 분석"
    if "confidence below threshold" in r:
        return "AI 분류 신뢰도가 기준보다 낮아 자동 처리 없이 고위험으로 운영자 인계"
    if "operator handoff" in r and decided_by == "router_failure":
        return "규칙에 따라 자동 처리 없이 고위험으로 운영자 인계"
    if "rules win for HIGH_RISK" in r:
        return "AI 의견과 달라도 고위험 규칙이 우선해 고위험 유지"
    if "rules keep COMPLEX" in r or "HIGH_RISK is reserved for the rules" in r:
        m = _RAISED.search(r)
        if m:
            return f"AI가 고위험을 제안했지만 고위험은 규칙만 정할 수 있어 {_lv(m.group(1))}보다 높은 {_lv(m.group(2))}으로 결정"
        return "AI가 고위험을 제안했지만 고위험은 규칙만 정할 수 있어 복잡 유지"
    m = _RAISED.search(r)
    if m:
        return f"AI 분류가 더 높은 수준을 제시해 {_lv(m.group(1))}보다 높은 {_lv(m.group(2))}으로 결정"
    if "conservative rule level kept" in r:
        return f"AI가 더 낮은 수준을 제안했지만 보수적인 규칙 판정 {_lv(level)} 유지"
    if "LLM agrees" in r:
        return f"AI 분류가 규칙 판정에 동의해 {_lv(level)}으로 결정"
    if decided_by == "rules":
        return f"AI 분류 없이 규칙 판정 {_lv(level)}으로 결정"
    return f"{_lv(level)}으로 결정(결정 문장 미기록)"


# ------------------------------------------------------------------ 5. model policy
_CAND_REASON = {"below quality floor": "품질 기준 미달", "unknown price or throughput": "가격·처리량 정보 없음"}


def model_policy_ko(policy: dict | None) -> dict | None:
    if not policy:
        return None
    sel = policy.get("selected_model")
    w = policy.get("weights") or {}
    if policy.get("handoff_only") or not sel:
        reason = "운영자 인계 경로라 분석 모델을 선택하지 않음"
    elif w:
        reason = (f"적격 후보 중 가중 점수(품질 {w.get('quality')}·비용 {w.get('cost')}·처리량 {w.get('throughput')})가 "
                  f"가장 높은 {sel} 선택")
    else:
        reason = f"{sel} 선택(가중치 미기록)"
    cands = []
    for c in policy.get("candidate_scores") or []:
        if not isinstance(c, dict):
            continue
        if c.get("eligible"):
            ko = f"적격 · 점수 {c.get('score')}" + (" · 선택" if c.get("model") == sel else "")
        else:
            ko = _CAND_REASON.get(str(c.get("reason")), f"부적격({c.get('reason') or '사유 미기록'})")
        cands.append({"model": c.get("model"), "eligible": bool(c.get("eligible")), "score": c.get("score"), "reason_ko": ko})
    return {"selected_model": sel if not policy.get("handoff_only") else None, "reason_ko": reason, "candidates": cands}


# ------------------------------------------------------------------ steps
def routing_card(rd, router_output: dict | None, llm_ko: str | None) -> dict:
    """Card for one RoutingDecision. `router_output` is the router AgentRun.output when it belongs to this decision."""
    feats = features_ko(rd.features)
    rule = rule_step_ko(rd.rationale, rd.decided_by)
    final = final_step_ko(rd.rationale, rd.decided_by, rd.level)
    mp = model_policy_ko(rd.policy)
    llm = None
    out = router_output or {}
    from .readings_ko import is_korean
    hint = out.get("rationale_ko")
    if not llm_ko and isinstance(hint, str) and is_korean(hint):
        llm_ko = hint.strip()
    if not llm_ko and is_korean(str(out.get("rationale") or "")):
        llm_ko = str(out.get("rationale")).strip()
    if rd.decided_by != "operator" and out.get("level"):
        llm = {"level": out.get("level"), "confidence": out.get("confidence"), "rationale_raw": str(out.get("rationale") or ""),
               "rationale_ko": llm_ko}
    if llm:
        llm_detail = f"AI 분류 의견: {_lv(llm['level'])} (신뢰도 {llm['confidence']})" + (
            f" — {llm_ko}" if llm_ko else " — 한글 해석 준비 중")
    elif rd.decided_by == "operator":
        llm_detail = "운영자 재지정이라 AI 분류를 다시 하지 않음"
    else:
        llm_detail = "AI 분류 의견 미기록"
    steps = [
        {"key": "features", "title_ko": "관측 특징 확인", "detail_ko": f"관측 특징 {len(feats)}개" if feats else "관측 특징 미기록",
         "items_ko": feats, "source": "rules"},
        {"key": "rule", "title_ko": "규칙 판정", "detail_ko": rule, "items_ko": [], "source": "rules"},
        {"key": "llm", "title_ko": "AI 분류 의견", "detail_ko": llm_detail, "items_ko": [], "source": "llm"},
        {"key": "final", "title_ko": "최종 결정", "detail_ko": final, "items_ko": [], "source": "rules"},
        {"key": "model", "title_ko": "분석 모델 선택", "detail_ko": mp["reason_ko"] if mp else "모델 선택 기록 없음",
         "items_ko": [f"{c['model']}: {c['reason_ko']}" for c in (mp or {}).get("candidates", [])], "source": "policy"},
    ]
    return {"kind": "routing", "routing_id": rd.id, "level": rd.level, "level_ko": _lv(rd.level), "decided_by": rd.decided_by,
            "decided_by_ko": DECIDED_BY_KO.get(rd.decided_by, rd.decided_by), "confidence": rd.confidence, "steps": steps,
            "final_reason_ko": final, "reason_raw": rd.rationale or "", "llm": llm, "model_policy": mp,
            "interpreting": bool(llm and llm["rationale_raw"].strip() and not llm_ko)}


def progress_ko(audits: list[Any]) -> list[str]:
    """Workflow audit records (wf_early_stop / wf_escalated / wf_budget_exceeded / wf_group_finished) in Korean."""
    from .roles_ko import role_info
    out: list[str] = []
    for a in audits:
        d = a.detail or {}
        if a.event == "wf_early_stop":
            stage = d.get("stage")
            reason = str(d.get("reason") or "")
            if stage == "challenge":
                out.append("반론 검토가 결론을 지지해 재추론을 생략" if "supported" in reason
                           else "반론 검토 결과 재추론 회차를 생략")
            elif stage == "verification":
                out.append("독립 검증이 결론을 지지해 추가 회차 없음")
            elif stage == "coordinator":
                out.append("Kubernetes 분석 결론이 근거로 확인되고 더 강한 로그 가설이 없어 진행 조정을 생략")
            else:
                out.append(f"{STAGE_KO.get(stage, stage)} 단계 조기 종료")
        elif a.event == "wf_escalated":
            role = role_info(str(d.get("role") or ""))["role_ko"]
            out.append(f"{role}을(를) 상위 모델로 승격({d.get('from_model')} → {d.get('to_model')})")
        elif a.event == "wf_budget_exceeded":
            kind = {"loop": "Agent 호출 한도", "cost": "비용 한도", "deadline": "시간 한도"}.get(d.get("limit"), "한도")
            out.append(f"{kind}에 도달해 Group을 멈춤")
        elif a.event == "wf_contradiction":
            out.append(f"{role_info(str(d.get('by') or ''))['role_ko']}가 {role_info(str(d.get('target') or ''))['role_ko']} 결론을 반박")
        elif a.event == "wf_group_finished":
            b = d.get("budget") or {}
            parts = [f"Group 종료: {WF_STATUS_KO.get(d.get('status'), d.get('status'))}"]
            if b.get("agent_calls") is not None:
                parts.append(f"Agent 호출 {b['agent_calls']}회")
            if b.get("estimated_cost_usd") is not None:
                parts.append(f"추정 {b['estimated_cost_usd']} USD")
            out.append(" · ".join(parts))
    return out


# ------------------------------------------------------------------ hand-off view
OUTCOME_KO = {"handed_off": "운영자 인계 · 자동 복구 아님", "closed": "운영자 종료 · 서비스 복구 판정 아님",
              "resolved": "자동 복구 완료", "failed": "처리 실패 · 자동 복구 아님"}
ACTION_STATUS_KO = {"cancelled": "증상 해소로 조치 취소", "succeeded": "성공", "failed": "실패", "denied": "정책상 거부", "rejected": "운영자 거절",
                    "pending_approval": "승인 대기", "approved": "승인됨", "executing": "실행 중", "proposed": "제안됨",
                    "rolled_back": "원복됨", "rolling_back": "원복 중"}
PENDING_KO = "한글 해석 준비 중"


def _operator_ko(status: str, h: dict) -> str:
    if status == "closed":
        who = h.get("closed_by") or "운영자"
        reason = f" — 사유: {h['close_reason']}" if h.get("close_reason") else ""
        return f"{who}님이 종료했습니다{reason}. 클러스터는 변경하지 않았습니다."
    if status == "handed_off":
        base = "원인을 확인한 뒤 대화로 조치를 지시하거나 장애를 종료하세요."
        return f"{h['claimed_by']}님이 인수함 · {base}" if h.get("claimed_by") else base
    if status == "resolved":
        if h.get('observed_recovery'):
            return '정상 상태를 관측했습니다. 원인은 미확정이며 AI 복구 조치는 없었습니다.'
        return "자동 조치와 검증이 끝났습니다. 재발 여부만 지켜보면 됩니다."
    return "기록을 확인한 뒤 대화로 후속 작업을 지시하거나 장애를 종료하세요."


def handoff_view(inc, ko: dict[str, str | None], actions: list | None = None) -> dict | None:
    """Korean summary + decision table for a finished incident. Texts come from the interpreter cache (keys
    `handoff:*`) or fixed rule sentences; untranslated items are reported as pending, never filled in."""
    from .models import INCIDENT_CLOSED_STATES
    if inc is None or inc.status not in INCIDENT_CLOSED_STATES or not inc.handoff:
        return None
    from .flowgraph import ACTION_KO
    from .interpret import handoff_items
    h = inc.handoff if isinstance(inc.handoff, dict) else {}
    keys = [it["id"] for it in handoff_items(h)]
    decisions = [{"key": "outcome", "label_ko": "결과", "value_ko": OUTCOME_KO.get(inc.status, inc.status)}]
    if inc.status == 'resolved' and h.get('observed_recovery'):
        decisions[0]['value_ko'] = '정상 상태 확인 · 원인 미확정 · AI 조치 없음'
    cands = [c for c in (h.get("root_cause_candidates") or []) if isinstance(c, dict)]
    if cands:
        c0 = cands[0]
        verdict = {"supported": "근거로 지지", "contradicted": "반박됨", "unconfirmed": "미확인",
                   "insufficient": "근거 부족"}.get(str(c0.get("verifier")), "기록 없음")
        text = ko.get("handoff:cause#0") or PENDING_KO
        decisions.append({"key": "cause", "label_ko": "원인 판단",
                          "value_ko": f"{text} (신뢰도 {c0.get('confidence')}, 독립 검증: {verdict})"})
    else:
        decisions.append({"key": "cause", "label_ko": "원인 판단", "value_ko": "근거로 확인된 원인 후보 없음"})
    ran = [a for a in (actions or []) if a.executed or a.status in ("succeeded", "failed", "executing", "rolled_back")]
    if ran:
        a = ran[-1]
        auto = f"{ACTION_KO.get(a.type, a.type)} — {ACTION_STATUS_KO.get(a.status, a.status)}"
    else:
        why = (h.get("remediation") or {}).get("why") if isinstance(h.get("remediation"), dict) else None
        auto = "실행하지 않음" + (f" — {ko.get('handoff:why') or PENDING_KO}" if why else "")
    decisions.append({"key": "auto_action", "label_ko": "자동 조치", "value_ko": auto})
    decisions.append({"key": "operator", "label_ko": "운영자 결정", "value_ko": _operator_ko(inc.status, h)})
    steps_raw = [str(x) for x in (h.get("recommended_steps") or [])]
    steps_ko = [first_sentence(ko.get(f"handoff:step#{i}")) for i in range(min(3, len(steps_raw)))]
    summary_ko, summary_source = ko.get("handoff:summary"), "interpret"
    if not summary_ko and isinstance(h.get("summary_ko"), str) and h["summary_ko"].strip():
        summary_ko, summary_source = h["summary_ko"].strip(), "model_ko"
    if not h.get("summary"):
        # 요약 원문이 없으면 해석할 대상이 없다. '준비 중' 대신 기록된 사실만 고정 문장으로 말한다.
        notes = [str(n) for n in (h.get("notes") or [])]
        failed = h.get("workflow_status") == "failed" or any("no valid output" in n for n in notes)
        summary_ko = ("분석 Agent가 유효한 결과를 만들지 못해 원인을 확정하지 못한 채 운영자에게 인계했습니다."
                      if failed else "AI 분석 요약이 기록되지 않았습니다.")
        summary_source = "rules"
    return {"summary_ko": summary_ko, "summary_raw": h.get("summary"), "summary_source": summary_source,
            "decisions": decisions,
            "next_steps_ko": [x for x in steps_ko if x], "next_steps_raw": steps_raw,
            "interpreting": any(not ko.get(k) for k in keys)}
