"""P15: the incident story for the war room — Korean headline, raw items with readings, flow graph."""
from __future__ import annotations

from sqlalchemy import select

from .flowgraph import ACTION_KO, build_flow, signal_ko
from .config import get_app_config
from .remediation import remediation_label
from .interpret import DISPLAY_ONLY_KINDS, incident_items, readings
from .models import INCIDENT_CLOSED_STATES, Action, AgentRun, Incident, OperatorCommand, RoutingDecision, RunEvent
from .roles_ko import GROUP_KO, LEVEL_KO, role_info

STAGE_KO = {"routing": "사건 수준 판단 중", "analyzing": "Agent 분석 중", "verifying": "결과 확인 중", "acting": "조치 실행 중",
            "approval": "운영자 승인 대기", "resolved": "자동 복구 완료", "handed_off": "운영자 인계", "failed": "처리 실패",
            "closed": "운영자 종료", "collecting": "근거 수집 중"}


def stage_of(inc: Incident, routing: list, runs: list, events: list) -> dict:
    st = inc.status
    if st in INCIDENT_CLOSED_STATES:
        key = st
    elif st == "action_pending":
        key = "approval"
    elif st == "acting":
        key = "acting"
    elif not routing:
        key = "routing" if any(e.kind == "evidence_collected" for e in events) else "collecting"
    else:
        last_started = next((e.detail.get("role") for e in reversed(events) if e.kind == "agent_started"
                             and isinstance(e.detail, dict)), None)
        key = "verifying" if last_started in ("result_verifier", "group_verifier", "safety_verifier") else "analyzing"
    label = STAGE_KO[key]
    if key == 'resolved' and (inc.handoff or {}).get('observed_recovery'):
        label = '정상 상태 확인 · 원인 미확정'
    if key in ("analyzing", "verifying"):
        role = next((e.detail.get("role") for e in reversed(events) if e.kind == "agent_started" and isinstance(e.detail, dict)), None)
        if role:
            label = f"{label} — {role_info(role)['role_ko']}"
    return {"key": key, "label": label}


def _impact(items: list[dict], inc: Incident) -> str:
    parts = []
    dep = next((i for i in items if i["ref"] == "E1_deployment"), None)
    if dep and dep.get("ko"):
        parts.append(dep["ko"])
    svc = next((i for i in items if i["ref"] == "E3_service"), None)
    if svc and svc.get("ko") and "0개" in svc["ko"]:
        parts.append(svc["ko"])
    if any(i["ref"] == "E7_error_logs" and i.get("ko") and ("503" in i["raw"] or "정상 대상" in i["ko"]) for i in items):
        parts.append("호출하는 서비스에서 요청 실패(5xx)가 함께 관측되었습니다.")
    if not parts:
        parts.append(f"{inc.namespace}/{inc.workload}에서 이상 신호가 관측되었습니다. 영향 범위는 근거 수집 후 표시됩니다.")
    return " ".join(parts[:3])


def _next(inc: Incident, actions: list, commands: list) -> str:
    pending_cmd = next((c for c in commands if c.status in ("awaiting_confirmation", "awaiting_secret")), None)
    if pending_cmd:
        return "운영자 지시의 실행 계획과 부작용을 확인하고 진행 여부를 결정하세요."
    if inc.status == "resolved":
        if (inc.handoff or {}).get('observed_recovery'):
            return '두 관측에서 증상 해소를 확인했습니다. 원인은 미확정이며 AI가 복구 조치를 실행한 것은 아닙니다.'
        a = next((x for x in reversed(actions) if x.status == "succeeded"), None)
        what = ACTION_KO.get(a.type, a.type) if a else "조치"
        return f"AI가 '{what}'을(를) 실행하고 검증까지 마쳤습니다. 재발 여부만 지켜보면 됩니다."
    if inc.status == "closed":
        return "운영자가 종료했습니다. 클러스터는 변경하지 않았고 이 장애에는 더 이상 지시를 받지 않습니다. 같은 증상이 다시 감지되면 새 장애로 접수됩니다."
    if inc.status == "handed_off":
        return "AI가 자동 조치를 멈추고 인계했습니다. '분석 · 운영자 대화'의 단계 카드나 아래 입력란으로 AI에게 작업을 지시할 수 있습니다(실행 전 부작용을 확인합니다)."
    if inc.status == "failed":
        return "처리 중 오류가 발생했습니다. 기록을 확인한 뒤 채팅으로 AI에게 후속 작업을 지시하세요."
    if inc.status == "action_pending":
        return "AI가 제안한 조치를 승인하거나 거절하세요."
    return "AI가 분석 중입니다. 채팅으로 조사 방향을 지시하면 다음 단계부터 반영됩니다."


def incident_origin(fingerprint: str | None) -> str:
    """정돈-04: provenance from an explicit marker only. `eval/` fingerprints are written by evalrun; anything else is
    'unconfirmed' — never guessed from titles, dates or service names."""
    return "evaluation" if isinstance(fingerprint, str) and fingerprint.startswith("eval/") else "unconfirmed"


def build_narrative(s, iid: int, interpret_job: str | None = None) -> dict | None:
    """Read-only story of one incident. 정돈-02: never schedules interpretation work.

    `interpret_job` is the in-memory worker state for this incident ('queued'|'running'|None), supplied by the API
    so the response can say whether a reading is actually being prepared instead of promising one."""
    inc = s.get(Incident, iid)
    if inc is None:
        return None
    routing = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid).order_by(RoutingDecision.id)))
    runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
    events = list(s.scalars(select(RunEvent).where(RunEvent.incident_id == iid).order_by(RunEvent.seq)))
    actions = list(s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id)))
    commands = list(s.scalars(select(OperatorCommand).where(OperatorCommand.incident_id == iid).order_by(OperatorCommand.id)))

    cited: dict[str, list[str]] = {}
    from .flowgraph import refs_of
    for r in runs:
        for ref in refs_of(r.output):
            cited.setdefault(ref, [])
            if r.role not in cited[ref]:
                cited[ref].append(r.role)
    items = readings(s, iid, incident_items(s, iid))
    # P18 W6: routing/hand-off readings feed the cards and handoff_view only, never the evidence list (`raw`).
    display_only = [i for i in items if i["kind"] in DISPLAY_ONLY_KINDS]
    row_items = [i for i in items if i.get("evidence_id") is not None]
    items = [i for i in items if i["kind"] not in DISPLAY_ONLY_KINDS and i.get("evidence_id") is None]
    for it in items:
        it["cited_by"] = list(cited.get(it["ref"], [])) if it["kind"] != "agent" else []
    order = {"error": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: (i["kind"] == "agent", order.get(i["severity"], 3)))

    level = inc.severity_level
    lvl = f"{LEVEL_KO.get(level, level)} 수준 · {GROUP_KO.get(level, '')}" if level else "수준 판단 전"
    remediation = (inc.handoff or {}).get("remediation")
    what = f"{inc.namespace}/{inc.workload}: {signal_ko(inc.signal_reasons)} ({lvl})"
    label = remediation_label(remediation)
    if label:
        what = label + " · " + what
    pending = [i for i in items + display_only if i.get("ko") is None and i["kind"] in ("event", "log", "agent", *DISPLAY_ONLY_KINDS)]
    last_work = max((r.id for r in runs if r.role != "interpreter"), default=0)
    last_interp = max((r.id for r in runs if r.role == "interpreter"), default=0)
    last_interp_run = next((r for r in reversed(runs) if r.role == "interpreter"), None)
    # 분석 결론·판단·인계 문장은 운영자가 가장 먼저 읽는다. 종료된 사건이어도 이 항목의 한국어가 없으면 다시 해석한다.
    # 직전 해석 호출이 실패(예산·한도 등)했다면 화면을 열 때마다 반복하지 않고 사유를 한국어로 알린다.
    key_pending = any(i["kind"] in ("agent", *DISPLAY_ONLY_KINDS) for i in pending)
    failed_last = bool(last_interp_run is not None and last_interp_run.status == "failed" and last_interp > last_work)
    # Unread items the interpreter would process. This is a fact about stored readings, not a promise of work.
    needs_reading = (any(i.get("ko") is None for i in display_only) or key_pending
                     or bool(pending) and (inc.status not in INCIDENT_CLOSED_STATES or last_interp < last_work))
    active = inc.status not in INCIDENT_CLOSED_STATES
    # 정돈-02: "interpreting" means a reading job is actually queued or running in this process. Viewing never
    # creates one; a finished incident needs an explicit, cost-visible request (POST .../interpretations).
    interpreting = interpret_job in ("queued", "running")
    interpret_state = None
    from .readings_ko import error_ko
    if interpreting:
        interpret_state = {"status": "pending", "reason_ko": "한글 해석을 만드는 중입니다. 원문은 상세보기에서 그대로 볼 수 있습니다."}
    elif pending and failed_last and last_interp_run is not None:
        interpret_state = {"status": "unavailable",
                           "reason_ko": f"한글 해석을 만들지 못했습니다: {error_ko(last_interp_run.error) or '사유 미기록'} "
                                        "원문은 상세보기에서 확인할 수 있습니다. 다시 요청하면 모델 호출과 비용이 발생합니다."}
    elif needs_reading and not get_app_config().assist.interpret_enabled:
        interpret_state = {"status": "disabled", "reason_ko": "한글 해석 기능이 꺼져 있습니다. 원문은 상세보기에서 확인할 수 있습니다."}
    elif needs_reading and active:
        interpret_state = {"status": "pipeline", "reason_ko": "분석 단계가 끝날 때 한글 해석을 함께 만듭니다. 지금 바로 만들려면 "
                                                              "'한글 해석 요청'을 누르세요(모델 호출·비용 발생)."}
    elif needs_reading:
        interpret_state = {"status": "not_requested",
                           "reason_ko": f"저장된 한글 해석이 없는 항목이 {len(pending)}개 있습니다. 기록 열람은 새 해석을 만들지 않습니다. "
                                        "필요하면 '한글 해석 요청'을 누르세요(모델 호출·비용 발생)."}
    # 정돈-07: one cost rule for every API — the shared accounting contract (full total only when every row is observed).
    from .accounting import cost_of
    cost = cost_of([r.estimated_cost_usd for r in runs])
    from .readings_ko import status_reason_ko, title_ko
    incident = {"id": inc.id, "title": inc.title, "title_ko": title_ko(inc.title, inc.signal_reasons),
                "status_reason_ko": status_reason_ko(inc.status_reason),
                "namespace": inc.namespace, "workload": inc.workload, "status": inc.status,
                "severity_level": level, "signal_count": inc.signal_count,
                "system": inc.system, "environment": get_app_config().environment(inc.namespace),
                "created_at": inc.created_at.isoformat() if inc.created_at else None,
                "updated_at": inc.updated_at.isoformat() if inc.updated_at else None,
                "closed_at": inc.closed_at.isoformat() if inc.closed_at else None,
                **cost, "agent_run_count": len(runs), "origin": incident_origin(inc.fingerprint),
                "recovery_kind": 'observed' if (inc.handoff or {}).get('observed_recovery') else None}
    flow = build_flow(s, iid, items + display_only + row_items)
    outcome = next((n for n in flow["nodes"] if n["id"] == "outcome"), None)
    return {
        "incident": incident,
        "headline": {"what": what, "impact": _impact(items, inc), "stage": stage_of(inc, routing, runs, events),
                     "next": _next(inc, actions, commands), "remediation": remediation, "source": "rules"},
        "raw": items,
        "flow": flow,
        "handoff_view": ((outcome or {}).get("card") or {}).get("handoff"),
        "interpreting": interpreting,
        "interpret_state": interpret_state,
        "interpret_pending_count": len(pending),
    }
