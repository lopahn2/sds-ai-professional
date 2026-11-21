"""P15: operator chat steering while an analysis runs.

A chat message is classified once (deterministic rules, no LLM: stop/reroute must act immediately and predictably)
into one intent and applied at the next agent boundary:

* instruction     -> appended to the next agent's prompt as an operator instruction (never evidence)
* escalate        -> the next agent runs one step higher on the model/effort ladder
* reroute         -> the current group stops at the next boundary and the requested group re-analyses the same
                     evidence (HIGH_RISK is never lowered; same level is refused)
* stop            -> the run stops at the next boundary (same as the stop control)
* cluster_change  -> during analysis it is passed to agents as an instruction; after the incident is finished the
                     operator issues it as a command (plan -> dry-run -> side effects -> confirm)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from . import db, runlog
from .roles_ko import GROUP_KO, role_info

LEVEL_ORDER = {"SIMPLE": 0, "NORMAL": 1, "COMPLEX": 2, "HIGH_RISK": 3}


class RerouteRequested(Exception):
    """Raised at an agent boundary when the operator asked for another group."""

    def __init__(self, level: str, message_id: int, body: str):
        super().__init__(level)
        self.level, self.message_id, self.body = level, message_id, body


_NEG = re.compile(r"하지\s*(마|말)|말고|금지|않게|don'?t|do not|never", re.I)
_STOP = re.compile(r"중단|멈춰|멈추|정지|그만|\bstop\b|\babort\b", re.I)
_LEVELS = [(re.compile(r"고위험|high[\s_-]?risk", re.I), "HIGH_RISK"), (re.compile(r"복잡|complex", re.I), "COMPLEX"),
           (re.compile(r"보통|normal", re.I), "NORMAL"), (re.compile(r"단순|simple", re.I), "SIMPLE")]
_GROUPISH = re.compile(r"그룹|group|수준|등급|레벨|level|(으로|로)\s*(바꿔|전환|변경|다시|재분석|분석)|전환|재분류", re.I)
_ESCALATE = re.compile(r"(높은|상위|강한|좋은|고성능|상급|큰)\s*(모델|model)|모델\S*\s*(올려|높여|승격|바꿔)|escalat|"
                       r"upgrade|추론\S*\s*(높여|올려|강도)", re.I)
_K8S = re.compile(r"롤백|rollback|되돌려|재시작|restart|재배포|redeploy|scale|스케일|레플리카|replica|메모리|memory|\bcpu\b|"
                  r"limit|한도|삭제|지워|delete|생성|create|배포|deploy|패치|patch|apply|cordon|drain|configmap|"
                  r"secret|시크릿|이미지\s*(바꿔|변경)|image", re.I)
_IMPERATIVE = re.compile(r"(해|해줘|해라|하세요|하라|하자|올려|늘려|줄여|내려|바꿔|변경|설정|적용|만들어|지워|돌려|줘)\s*[.!]?\s*$|"
                         r"(해|해줘|올려|늘려|줄여|내려|바꿔)\b", re.I)

ACK = {
    "instruction": "다음 단계 Agent에게 운영자 지시로 전달합니다. 근거가 아니라 지시로 취급하며 안전 규칙보다 우선하지 않습니다.",
    "escalate": "다음에 실행되는 Agent를 한 단계 높은 모델(또는 추론 강도)로 실행합니다.",
    "stop": "다음 단계가 시작되기 전에 분석을 멈추고 운영자에게 인계합니다.",
    "cluster_change": ("클러스터 변경 지시입니다. 분석 중에는 Agent에게 지시로 전달합니다. 분석이 끝난 뒤 같은 지시를 보내면 실행 계획과 "
                       "부작용을 먼저 보여 드리고 확인을 받은 다음 실행합니다."),
}


def classify(text: str) -> dict:
    t = (text or "").strip()
    if _NEG.search(t):
        return {"intent": "instruction", "target_level": None, "ack_ko": ACK["instruction"]}
    if _STOP.search(t):
        return {"intent": "stop", "target_level": None, "ack_ko": ACK["stop"]}
    if _GROUPISH.search(t):
        for pat, level in _LEVELS:
            if pat.search(t):
                return {"intent": "reroute", "target_level": level,
                        "ack_ko": f"다음 단계부터 {GROUP_KO[level]}으로 바꿔 같은 근거를 다시 분석합니다. 고위험 수준은 낮출 수 없습니다."}
    if _ESCALATE.search(t):
        return {"intent": "escalate", "target_level": None, "ack_ko": ACK["escalate"]}
    if _K8S.search(t) and _IMPERATIVE.search(t):
        return {"intent": "cluster_change", "target_level": None, "ack_ko": ACK["cluster_change"]}
    return {"intent": "instruction", "target_level": None, "ack_ko": ACK["instruction"]}


def reroute_allowed(current: str | None, target: str) -> bool:
    if current is None or current == target or target not in LEVEL_ORDER:
        return False
    return current != "HIGH_RISK"


def escalate_model(cfg, model: str, effort: str | None) -> tuple[str, str | None] | None:
    vendor = "anthropic" if cfg.llm.provider in ("claude_cli", "anthropic_api") else "openai"
    ladder = cfg.workflows.model_ladder.get(vendor) or []
    if model in ladder and ladder.index(model) + 1 < len(ladder):
        return ladder[ladder.index(model) + 1], effort
    efforts = cfg.workflows.effort_ladder
    if effort in efforts and efforts.index(effort) + 1 < len(efforts):
        return model, efforts[efforts.index(effort) + 1]
    return None


TARGETABLE = ("instruction", "escalate")


def planned_roles(level: str | None) -> set[str]:
    from .flowgraph import STAGES
    return {r for stage in STAGES.get(level or "", []) for r in stage}


def _started_roles(s, iid: int) -> set:
    from .models import RunEvent
    return {e.detail.get("role") for e in s.scalars(select(RunEvent).where(RunEvent.incident_id == iid,
                                                                          RunEvent.kind == "agent_started"))
            if isinstance(e.detail, dict)}


def _holds(s, iid: int, target_role: str | None, level: str | None) -> bool:
    """Same rule as `take`, judged from the last started role: planned, not started and not already passed."""
    from .models import RunEvent
    if not target_role:
        return False
    last = next((e.detail.get("role") for e in s.scalars(select(RunEvent).where(
        RunEvent.incident_id == iid, RunEvent.kind == "agent_started").order_by(RunEvent.seq.desc()).limit(1))
        if isinstance(e.detail, dict)), None)
    return _waits(target_role, last or "", level, _started_roles(s, iid))


def _add_message(iid: int, body: str, actor: str, target_role: str | None, target_node: str | None) -> int:
    """Same as runlog.add_message, but the `operator_message` event also names the targeted stage."""
    from .models import OperatorMessage
    from .redact import redact
    body = (body or "").strip()
    if not body:
        raise ValueError("empty message")
    with db.session() as s:
        row = OperatorMessage(incident_id=iid, body=redact(body)[:2000])
        s.add(row)
        s.flush()
        mid = row.id
    runlog.emit(iid, "operator_message", body[:600], {"message_id": mid, "target_role": target_role, "target_node": target_node},
                actor=actor)
    return mid


def _stage_index(level: str | None, role: str | None) -> int | None:
    from .flowgraph import STAGES
    return next((i for i, roles in enumerate(STAGES.get(level or "", [])) if role in roles), None)


def _waits(want: str, role: str, level: str | None, started: set) -> bool:
    """Hold while the target is planned, not started and not already passed (an earlier stage that was skipped)."""
    if want not in planned_roles(level) or want in started:
        return False
    here, there = _stage_index(level, role), _stage_index(level, want)
    return here is None or there is None or there >= here


def post(iid: int, body: str, actor: str = "local-operator", target_role: str | None = None,
         target_node: str | None = None) -> dict:
    """Store the chat message, classify it, record the reading; a stop request takes effect immediately.

    `target_role` (P18 W6) only changes *when* an instruction/escalate is applied (hold until that role starts);
    reroute/stop keep their meaning and safety boundaries."""
    from .models import Incident, OperatorMessage
    mid = _add_message(iid, body, actor, target_role, target_node)
    c = classify(body)
    hold = False
    with db.session() as s:
        row = s.get(OperatorMessage, mid)
        row.intent = c["intent"]
        row.intent_detail = {"target_level": c["target_level"], "target_role": target_role, "target_node": target_node}
        if target_role and c["intent"] in TARGETABLE:
            inc = s.get(Incident, iid)
            hold = _holds(s, iid, target_role, inc.severity_level if inc else None)
            if c["intent"] == "instruction":
                from .routing_ko import topic
                name = role_info(target_role)["role_ko"]
                c["ack_ko"] = (f"{name} 시작 전에 이 지시를 전달합니다." if hold
                               else f"{topic(name)} 이미 실행되어 다음 단계 Agent에게 전달합니다.")
    out = {"message_id": mid, **c, "target_role": target_role, "target_node": target_node, "hold": hold}
    runlog.emit(iid, "steering_interpreted", c["ack_ko"], out, actor="liveprobe")
    if c["intent"] == "stop":
        runlog.set_control(iid, "stop", actor=actor)
    return out


@dataclass
class Steer:
    texts: list[str] = field(default_factory=list)
    escalate: bool = False
    reroute: RerouteRequested | None = None
    # C3 dispatch/v1: ids of the instruction messages whose text is appended to this call's prompt (same order as
    # `texts`). Escalate requests change the model only and are listed separately; they never enter the prompt.
    message_ids: list[int] = field(default_factory=list)
    escalate_message_ids: list[int] = field(default_factory=list)


def take(iid: int, role: str, level: str | None, call_id: str | None = None) -> Steer:
    """Consume pending chat at the boundary before `role`. A reroute stops consumption: later messages wait for the
    first agent of the new group. Before routing (level None) a reroute message stays pending.

    `call_id` (C3) is the agent call about to start; it is written on `operator_applied` so a delivered message can be
    joined to that call's `agent_started.detail.dispatch`. A reroute means that call never starts, so it is not linked."""
    from .models import OperatorMessage
    out, consumed, refused = Steer(), [], []
    targeted: list[str] = []
    with db.session() as s:
        rows = s.scalars(select(OperatorMessage).where(OperatorMessage.incident_id == iid, OperatorMessage.applied_at.is_(None))
                         .order_by(OperatorMessage.id)).all()
        started = None
        for m in rows:
            intent = m.intent or "instruction"
            want = (m.intent_detail or {}).get("target_role") if intent in TARGETABLE else None
            if want and want != role:
                if started is None:
                    started = _started_roles(s, iid)
                if _waits(want, role, level, started):
                    continue                                   # held for its target; later messages still apply
            if want:
                targeted.append(want)
            if intent == "reroute":
                target = (m.intent_detail or {}).get("target_level")
                if level is None:
                    break
                m.applied_at = datetime.now(timezone.utc)
                consumed.append(m.id)
                if reroute_allowed(level, target):
                    out.reroute = RerouteRequested(target, m.id, m.body)
                    break
                refused.append((m.id, target))
                continue
            m.applied_at = datetime.now(timezone.utc)
            consumed.append(m.id)
            if intent == "escalate":
                out.escalate = True
                out.escalate_message_ids.append(m.id)
            elif intent == "instruction":
                out.texts.append(m.body)
                out.message_ids.append(m.id)
            # cluster_change must use dry-run / side-effects / explicit confirmation, never the autonomous path.
    info = role_info(role)
    for mid, target in refused:
        why = ("고위험 수준은 자동으로 낮출 수 없어" if level == "HIGH_RISK" else f"이미 {GROUP_KO.get(level, level)}로 분석 중이라")
        runlog.emit(iid, "steering_interpreted", f"Group 변경 요청을 따르지 않았습니다: {why} 현재 {GROUP_KO.get(level, level)}를 유지합니다.",
                    {"message_id": mid, "intent": "reroute", "target_level": target, "refused": True})
    if consumed:
        detail = {"message_ids": consumed, "applied_to_role": role, "escalate": out.escalate,
                  "reroute": out.reroute.level if out.reroute else None}
        if targeted:
            detail["targeted_role"] = targeted[0] if len(set(targeted)) == 1 else targeted
            detail["fallback"] = any(t != role for t in targeted)
        if call_id and out.reroute is None:
            detail["call_id"] = call_id
            detail["prompt_message_ids"] = list(out.message_ids)
            if out.escalate_message_ids:
                detail["escalate_message_ids"] = list(out.escalate_message_ids)
        runlog.emit(iid, "operator_applied", f"운영자 메시지 {len(consumed)}건을 {info['role_ko']}({role}) 시작 전에 반영했습니다.",
                    detail)
    return out
