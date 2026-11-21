"""P5: run events (chat feed), operator messages and pause/stop control.

Contract (see docs/plans/liveprobe-revamp-plan.md 5.4):
* every pipeline step appends a `run_event` (monotonic `seq` per incident); the dashboard renders them as a feed
  and can replay it after a reload;
* an operator message is applied at the NEXT node boundary (before the next agent call) and is shown to the
  agent as an instruction, never as evidence;
* pause/stop take effect at node boundaries and right before an action is executed. A write that has already
  started is never interrupted: it finishes, is verified, then the run stops.
No hidden reasoning is recorded: only evidence references, steps, tool work, judgement summaries and results.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import func, select

from . import db
from .models import Base  # noqa: F401  (models register tables)
from .redact import redact, redact_obj
from .sse import broker

KINDS = ("evidence_collected", "routing_decided", "group_composed", "agent_started", "agent_finished",
         "escalated", "action_proposed", "action_executed", "verification_result", "review_recommended",
         "operator_message", "operator_applied", "paused", "resumed", "stopped", "account_switched",
         "waiting_quota", "status", "completed", "note", "steering_interpreted",
         "command_planned", "command_confirmed", "command_executed", "command_failed", "command_rolled_back",
         "command_cancelled", "agent_progress", "investigation_started", "investigation_progress", "investigation_finished")
CONTROL_ACTIONS = ("pause", "resume", "stop")
POLL_S = 1.0


class CleanupResult(str, Enum):
    COMPLETED = "completed"
    ALREADY_TERMINAL = "already_terminal"
    MISSING = "missing"
    PENDING = "pending"
    INSPECTION_REQUIRED = "inspection_required"
    NOT_REQUESTED = "not_requested"

    def __bool__(self) -> bool:
        # 기존 callback의 성공 여부와 정리 재시도 여부는 다른 계약이다.
        return self is CleanupResult.COMPLETED

    @property
    def pending(self) -> bool:
        return self in (CleanupResult.PENDING, CleanupResult.INSPECTION_REQUIRED, CleanupResult.NOT_REQUESTED)


class RunStopped(Exception):
    """Raised at a node boundary when the operator stopped the run."""
    workflow_run_id: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def emit(incident_id: int, kind: str, summary: str, detail: dict | None = None, actor: str = "liveprobe") -> int:
    """Append one event; returns its per-incident sequence number. Never raises into the pipeline."""
    from .models import Incident, RunEvent
    if kind not in KINDS:
        kind = "note"
    try:
        with db.session() as s:
            # Serialize sequence allocation, including delayed stop-completion callbacks.
            s.scalar(select(Incident.id).where(Incident.id == incident_id).with_for_update())
            seq = (s.scalar(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.incident_id == incident_id)) or 0) + 1
            s.add(RunEvent(incident_id=incident_id, seq=seq, kind=kind, actor=actor, summary=redact(summary)[:600],
                           detail=redact_obj(detail or {})))
        if kind != "agent_progress":
            broker.publish(incident_id, "updated")
        return seq
    except Exception:  # noqa: BLE001 - the feed must never break an incident
        return -1


def events_since(incident_id: int, since: int = 0, limit: int = 500) -> list[dict]:
    from .models import RunEvent
    with db.session() as s:
        rows = s.scalars(select(RunEvent).where(RunEvent.incident_id == incident_id, RunEvent.seq > since)
                         .order_by(RunEvent.seq).limit(limit)).all()
        return [{"seq": r.seq, "kind": r.kind, "actor": r.actor, "summary": r.summary, "detail": r.detail,
                 "at": r.created_at.isoformat().replace("+00:00", "Z")} for r in rows]


# ------------------------------------------------------------------ control
def control_state(incident_id: int) -> str:
    from .models import RunControl
    with db.session() as s:
        row = s.get(RunControl, incident_id)
        return row.state if row else "running"


def cleanup_blocker_locked(s, iid: int) -> CleanupResult | None:
    """사건 잠금 아래 미확정 효과를 읽기만 한다. 하위 네트워크 작업을 기다리지 않는다."""
    from .models import Action, OperatorCommand, WorkflowRun
    for row in s.scalars(select(OperatorCommand).where(OperatorCommand.incident_id == iid)):
        if ((row.result or {}).get("inflight") is not None
                or (row.rollback or {}).get("inflight") is not None
                or row.error == "process_restarted_outcome_requires_inspection"):
            return CleanupResult.INSPECTION_REQUIRED
    if s.scalar(select(Action.id).where(Action.incident_id == iid, Action.status == "failed",
                                        Action.reason.contains("indeterminate")).limit(1)) is not None:
        return CleanupResult.INSPECTION_REQUIRED
    if s.scalar(select(Action.id).where(Action.incident_id == iid,
                                        Action.status == "indeterminate").limit(1)) is not None:
        return CleanupResult.INSPECTION_REQUIRED
    busy = any(s.scalar(select(model.id).where(model.incident_id == iid,
                    model.status.in_(statuses)).limit(1)) is not None for model, statuses in (
        (WorkflowRun, ("running", "resume_pending")),
        (Action, ("proposed", "pending_approval", "approved", "executing")),
        (OperatorCommand, ("planning", "awaiting_confirmation", "awaiting_secret", "executing", "rolling_back")),
    ))
    return CleanupResult.PENDING if busy else None


_STOP_AUDITS = {"incident_stopped": "stopped", "incident_stopped_before_start": "stopped_before_start"}


@dataclass(frozen=True)
class PastStopProof:
    """종결 사건의 과거 stop 근거 검증 결과. 검증 실패 사유는 `inspection`에 남긴다."""
    outcome: str | None
    verified: bool
    inspection: str | None = None
    stop_audit_at: str | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str):
        raise ValueError("시각 형식 아님")
    return _aware(datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value))


def _verify_past_stop_locked(s, inc) -> PastStopProof | None:
    """사건 잠금 아래, 같은 사건의 감사·상태·close 이력·completed 기원·시각 순서를 한 번에 대조한다.

    반환값: None은 stop 근거 자체가 없음(일반 인계·종료, 다른 사건 감사)이다. 근거가 있으나
    불충분하거나 서로 모순되면 verified=False와 사유를 돌려주며 호출자는 아무 이력도 확정하지 않는다.
    필수 근거의 부재(*_missing)와 명시적 불일치(*_mismatch)를 다른 사유로 구분한다. 보조 근거
    (closed completed, handoff.closed_at, close 사유, 감사 시각)는 값이 없으면 대조하지 않고,
    값이 있으면 서로 일치하고 실제 close 전이 순서를 지켜야 한다.
    """
    from .models import AuditLog, RunControl, RunEvent, WorkflowRun

    def fail(reason: str) -> PastStopProof:
        return PastStopProof(None, False, reason)

    audits = list(s.scalars(select(AuditLog).where(AuditLog.incident_id == inc.id,
                        AuditLog.event.in_((*_STOP_AUDITS, "incident_closed")))))
    stop_audits = [row for row in audits if row.event in _STOP_AUDITS]
    evidence = {_STOP_AUDITS[row.event] for row in stop_audits}
    marker = inc.status_reason if inc.status == "handed_off" and inc.status_reason in _STOP_AUDITS.values() else None
    completed = list(s.scalars(select(RunEvent).where(RunEvent.incident_id == inc.id, RunEvent.kind == "completed")))
    if not evidence and marker is None:
        if any((row.detail or {}).get("outcome") in _STOP_AUDITS.values() for row in completed):
            return fail("stop_audit_missing")  # completed만으로 stop을 확정하거나 감사를 발명하지 않는다.
        return None  # 일반 인계/종료 또는 다른 사건 감사로 stop을 만들지 않는다.
    if marker is not None:
        evidence.add(marker)
    if len(evidence) != 1:
        return fail("conflicting_stop_kinds")
    outcome = next(iter(evidence))
    control = s.get(RunControl, inc.id)
    if control is None or control.state != "stopping":
        return fail("control_not_stopping")
    if any((row.detail or {}).get("outcome") in _STOP_AUDITS.values()
           and row.detail["outcome"] != outcome for row in completed):
        return fail("conflicting_stop_completed")
    if outcome == "stopped_before_start" and s.scalar(select(WorkflowRun.id).where(
            WorkflowRun.incident_id == inc.id).limit(1)) is not None:
        return fail("before_start_has_workflow_run")
    stop_times = [t for t in (_aware(row.ts) for row in stop_audits) if t is not None]
    if inc.status == "closed":
        closed_at = _aware(inc.closed_at)
        handoff = inc.handoff or {}
        if closed_at is None:
            return fail("closed_at_missing")
        if handoff.get("closed_from") is None:
            return fail("closed_from_missing")
        if handoff["closed_from"] != "handed_off":
            return fail("closed_from_mismatch")
        close_audits = [row for row in audits if row.event == "incident_closed"]
        if not close_audits:
            return fail("close_audit_missing")
        if any((row.detail or {}).get("previous_status") is None for row in close_audits):
            return fail("close_audit_previous_status_missing")
        if any(row.detail["previous_status"] != "handed_off" for row in close_audits):
            return fail("close_audit_previous_status_mismatch")
        close_audit_times = [_aware(row.ts) for row in close_audits if row.ts is not None]
        if any(at is not None and at < closed_at for at in close_audit_times):
            return fail("close_audit_before_closed_at")
        close_reasons = [(row.detail or {}).get("reason") for row in close_audits
                         if (row.detail or {}).get("reason") is not None]
        if handoff.get("close_reason") is not None:
            # 실제 db.audit와 같은 기존 저장 표현으로 현재 사유만 대조한다.
            close_reasons.append(redact_obj(handoff["close_reason"]))
        if close_reasons and any(reason != close_reasons[0] for reason in close_reasons[1:]):
            return fail("close_audit_reason_mismatch")
        if handoff.get("closed_at") is not None:
            try:
                recorded = _parse_time(handoff["closed_at"])
            except ValueError:
                return fail("handoff_closed_at_invalid")
            if recorded != closed_at:
                return fail("handoff_closed_at_mismatch")
        if any((row.detail or {}).get("outcome") == "closed"
               and row.detail.get("previous_status") is not None
               and row.detail["previous_status"] != "handed_off" for row in completed):
            return fail("closed_completed_previous_status_mismatch")
        close_times = [closed_at, *close_audit_times]
        if any(stop > close for stop in stop_times for close in close_times):
            return fail("stop_audit_after_close")
    elif inc.status != "handed_off" or marker is None:
        return fail("terminal_status_not_stop")
    return PastStopProof(outcome, True, None, _iso(min(stop_times)) if stop_times else None)


def _finish_stop_locked(s, inc, actor: str) -> str | CleanupResult | None:
    """Incident lock is held; active starts must take it before their Command lock."""
    from .lifecycle import transition
    from .models import INCIDENT_CLOSED_STATES, WorkflowRun
    if inc is None or cleanup_blocker_locked(s, inc.id) is not None:
        return None
    if inc.status in INCIDENT_CLOSED_STATES:
        proof = _verify_past_stop_locked(s, inc)
        if proof is None:
            return None
        if not proof.verified or proof.outcome is None:
            return CleanupResult.INSPECTION_REQUIRED
        # 검증과 같은 트랜잭션·같은 사건 잠금 아래에서만 누락 이력을 보완한다.
        _record_stop_locked(s, inc.id, proof.outcome, actor, reconciled_status=inc.status,
                            stop_audit_at=proof.stop_audit_at)
        return None
    iid = inc.id
    before_start = inc.status == "open" and s.scalar(select(WorkflowRun.id).where(
        WorkflowRun.incident_id == iid).limit(1)) is None
    outcome = "stopped_before_start" if before_start else "stopped"
    transition(inc, "handed_off")
    inc.status_reason = outcome
    inc.handoff = {**(inc.handoff or {}),
                   "summary": "운영자가 분석 시작 전에 중단했습니다." if before_start else "운영자가 실행을 중단했습니다.",
                   "root_cause_candidates": [],
                   "recommended_steps": ["기록을 검토한 뒤 필요하면 수동으로 처리하세요."],
                   "notes": ["분석·조치 실행 없이 대기 중 중단됨" if before_start else "실행 worker 및 활성 작업 종료 확인 후 중단됨"]}
    _record_stop_locked(s, iid, outcome, actor)
    return outcome


def _record_stop_locked(s, incident_id: int, outcome: str, actor: str, *,
                        reconciled_status: str | None = None, stop_audit_at: str | None = None) -> None:
    """사건·Broker 잠금 아래 감사와 두 재생 로그를 원자적·멱등 기록한다.

    RunEvent는 과거 사실(outcome)과 소급 보완 사실(reconciled, 보완 시각, 보완 당시 현재 상태)을 함께 남긴다.
    IncidentEvent는 commit 당시 현재 상태 알림이다. 현재가 handed_off이면 누락된 handed_off 알림을 한 번
    보완하고, 그 밖의 종결 상태(closed)이면 이번 트랜잭션이 실제로 보완한 것이 있을 때만
    updated/<현재 상태> 한 건을 알린다. 과거 상태 알림을 새 cursor에 만들지 않고 기존 행은 고치지 않는다.
    """
    from .models import AuditLog, IncidentEvent, RunEvent
    current = reconciled_status or "handed_off"
    wrote = False
    event = "incident_stopped_before_start" if outcome == "stopped_before_start" else "incident_stopped"
    if s.scalar(select(AuditLog.id).where(AuditLog.incident_id == incident_id,
                                         AuditLog.event == event).limit(1)) is None:
        db.audit(s, actor, event, incident_id,
                 **({"cluster_changed": False} if outcome == "stopped_before_start" else {}))
        wrote = True
    completed = s.scalars(select(RunEvent).where(RunEvent.incident_id == incident_id,
                                                RunEvent.kind == "completed"))
    if not any((row.detail or {}).get("outcome") == outcome for row in completed):
        seq = (s.scalar(select(func.coalesce(func.max(RunEvent.seq), 0)).where(
            RunEvent.incident_id == incident_id)) or 0) + 1
        s.add(RunEvent(incident_id=incident_id, seq=seq, kind="completed", actor=actor,
                       summary="과거 운영자 중단의 누락 기록을 보완했습니다." if reconciled_status else "운영자 중단으로 실행을 종료했습니다.",
                       detail={"outcome": outcome,
                               **({"reconciled": True, "status_at_reconciliation": reconciled_status,
                                   "reconciled_at": _iso(_now()), "stop_audit_at": stop_audit_at}
                                  if reconciled_status else {}),
                               **({"cluster_changed": False} if outcome == "stopped_before_start" else {})}))
        wrote = True
    if current == "handed_off":
        if s.scalar(select(IncidentEvent.id).where(IncidentEvent.incident_id == incident_id,
                        IncidentEvent.event == "handed_off", IncidentEvent.status == "handed_off").limit(1)) is None:
            s.add(IncidentEvent(incident_id=incident_id, event="handed_off", status="handed_off"))
    elif wrote:
        s.add(IncidentEvent(incident_id=incident_id, event="updated", status=current))


def _publish_stop(incident_id: int, outcome: str | CleanupResult | None, actor: str) -> None:
    # 영속 쓰기는 이미 확정됐다. 전송 실패는 재생 조회로 복원하며 DB에 중복 쓰지 않는다.
    if outcome in ("stopped", "stopped_before_start"):
        loop = broker._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(broker._deliver,
                    {"incident_id": incident_id, "event": "handed_off", "status": "handed_off"})
            except RuntimeError:
                pass


def finalize_stop(incident_id: int, *, worker_run_id: int | None = None,
                  reason: str | None = None, actor: str = "liveprobe") -> bool:
    """기존 callback의 bool 반환 계약은 유지한다. worker는 상세 결과를 사용한다."""
    return bool(finalize_stop_result(incident_id, worker_run_id=worker_run_id, reason=reason, actor=actor))


def finalize_stop_result(incident_id: int, *, worker_run_id: int | None = None,
                         reason: str | None = None, actor: str = "liveprobe") -> CleanupResult:
    """Recheck after a terminal commit, never while holding a Command lock.

    Only an exiting worker may terminate its own attempt. Command callbacks cannot
    clear running attempts and must not wait here for network work to finish.
    """
    from .models import Incident, RunControl, WorkflowRun
    with broker._lock, db.session() as s:
        inc = s.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if worker_run_id is not None:
            run = s.scalar(select(WorkflowRun).where(WorkflowRun.id == worker_run_id).with_for_update())
            if run is not None and run.incident_id != incident_id:
                blocker = cleanup_blocker_locked(s, incident_id)
                return blocker if blocker is not None else CleanupResult.INSPECTION_REQUIRED
            if run is not None and run.status in ("running", "resume_pending"):
                run.status, run.phase, run.reason = "failed", "stopped", redact(reason or "operator_stop")[:500]
        control = s.get(RunControl, incident_id)
        from .models import INCIDENT_CLOSED_STATES
        outcome = (_finish_stop_locked(s, inc, actor) if inc is not None and
                   (inc.status in INCIDENT_CLOSED_STATES or control and control.state == "stopping") else None)
        blocker = cleanup_blocker_locked(s, incident_id) if inc is not None else None
        if inc is None:
            result = CleanupResult.MISSING
        elif blocker is not None:
            result = blocker
        elif isinstance(outcome, CleanupResult):
            result = outcome
        elif outcome is not None:
            result = CleanupResult.COMPLETED
        elif inc.status in INCIDENT_CLOSED_STATES:
            result = CleanupResult.ALREADY_TERMINAL
        elif control and control.state == "stopping":
            result = CleanupResult.PENDING
        else:
            result = CleanupResult.NOT_REQUESTED
    _publish_stop(incident_id, outcome, actor)
    return result


def set_control(incident_id: int, action: str, actor: str = "local-operator") -> str:
    from .models import Incident, RunControl
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"unknown control action {action!r}")
    new = {"pause": "paused", "resume": "running", "stop": "stopping"}[action]
    outcome = None
    with broker._lock, db.session() as s:
        inc = s.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        row = s.get(RunControl, incident_id)
        if row is None:
            row = RunControl(incident_id=incident_id, state="running")
            s.add(row)
        if row.state == "stopping" and action != "stop":
            return row.state          # a stop request is final
        changed = row.state != new
        row.state, row.updated_at = new, _now()
        if action == "stop":
            outcome = _finish_stop_locked(s, inc, actor)
    if changed:
        emit(incident_id, {"pause": "paused", "resume": "resumed", "stop": "stopped"}[action],
             {"pause": "운영자가 일시정지를 요청했습니다. 다음 단계 시작 전에 멈춥니다.",
              "resume": "운영자가 재개했습니다.",
              "stop": "운영자가 중단을 요청했습니다. 실행 중인 조치는 검증까지 마친 뒤 멈춥니다."}[action], actor=actor)
    _publish_stop(incident_id, outcome, actor)
    return new


def gate(incident_id: int, where: str, sleep=time.sleep, max_wait_s: float | None = None) -> None:
    """Node boundary. Blocks while paused, raises RunStopped when stopping. `where` names the next step."""
    announced = False
    waited = 0.0
    while True:
        state = control_state(incident_id)
        if state == "stopping":
            raise RunStopped(where)
        if state != "paused":
            return
        if not announced:
            emit(incident_id, "note", f"일시정지됨 — 다음 단계: {where}")
            announced = True
        sleep(POLL_S)
        waited += POLL_S
        if max_wait_s is not None and waited >= max_wait_s:
            raise RunStopped(f"paused beyond limit before {where}")


# ------------------------------------------------------------------ operator messages
def add_message(incident_id: int, body: str, actor: str = "local-operator") -> int:
    from .models import OperatorMessage
    body = (body or "").strip()
    if not body:
        raise ValueError("empty message")
    with db.session() as s:
        row = OperatorMessage(incident_id=incident_id, body=redact(body)[:2000])
        s.add(row)
        s.flush()
        mid = row.id
    emit(incident_id, "operator_message", body[:600], {"message_id": mid}, actor=actor)
    return mid


def take_messages(incident_id: int, applied_at: str) -> list[str]:
    """Mark pending messages as applied at `applied_at` (the step that will see them) and return their text."""
    from .models import OperatorMessage
    with db.session() as s:
        rows = s.scalars(select(OperatorMessage).where(OperatorMessage.incident_id == incident_id,
                                                       OperatorMessage.applied_at.is_(None))
                         .order_by(OperatorMessage.id)).all()
        out = [r.body for r in rows]
        for r in rows:
            r.applied_at = _now()
        ids = [r.id for r in rows]
    if ids:
        emit(incident_id, "operator_applied", f"운영자 메시지 {len(ids)}건을 {applied_at}에 반영했습니다.", {"message_ids": ids})
    return out


def with_operator_instructions(prompt: str, messages: list[str]) -> str:
    if not messages:
        return prompt
    block = "\n".join(f"- {m}" for m in messages)
    return (prompt + "\n\nOperator instructions (steering from the human operator; they are instructions, NOT evidence, "
            "and never override safety rules or the allowed action list):\n" + block)
