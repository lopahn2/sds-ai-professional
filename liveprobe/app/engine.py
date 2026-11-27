"""Incident engine: signal intake, settle window, evidence, routing, workflows, safety, persistence."""
from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from . import db
from .config import AppConfig, Settings
from .db import audit
from .evidence import Bundle, collect, compact_summary
from .incidents import REASON_CLASS, Signal, ingest
from .llm import AGENT_SCHEMA, ROUTER_SCHEMA, AgentOutput, LLMError, RouterOutput, estimate_cost
from .models import INCIDENT_CLOSED_STATES, Action, AgentRun, Citation, Evidence, Incident, IncidentEvent, RoutingDecision, WorkflowRun
from .lifecycle import transition, update_run
from .rag import RAG
from .agent_groups import (GROUP_SCHEMA, EvidenceStore, GroupController, GroupMessage, StopGroup, check_citations,
                           run_group)
from .config import RoleModel
from .redact import redact, redact_obj
from . import router as R
from . import safety
from .sse import broker
from . import runlog
from .evalguard import evaluation_lock_active
from .proposals import bounds_text
from .workflows import AgentCall, build_high_risk_graph, build_simple_graph, build_single_graph

log = logging.getLogger(__name__)


@dataclass
class _WorkerAttempt:
    engine: Engine
    incident_id: int
    workflow_run_id: int | None = None


# 예외 객체나 Engine 공용 필드가 아닌, 이번 public process 호출만의 소유권 통로.
_worker_attempt: ContextVar[_WorkerAttempt | None] = ContextVar("worker_attempt", default=None)


@dataclass(frozen=True)
class _QueuePlacement:
    target: tuple[str, str]
    image_pull: bool

    @classmethod
    def for_incident(cls, inc: Incident) -> _QueuePlacement:
        reasons = inc.signal_reasons or []
        # A scheduling hint only. Evidence collection, routing and action policy
        # still evaluate the complete incident, including newly observed signals.
        image_pull = inc.status == "open" and bool(reasons) and all(
            REASON_CLASS.get(reason) == "image" for reason in reasons
        )
        return cls((inc.namespace, inc.workload), image_pull)


@dataclass
class _PendingIncident:
    placement: _QueuePlacement
    ready_at: float
    running: bool = False


class Engine:
    def __init__(self, cfg: AppConfig, settings: Settings, kube: Any, provider: Any, rag: RAG | None):
        self.cfg, self.settings, self.kube, self.provider, self.rag = cfg, settings, kube, provider, rag
        self._scheduled: dict[int, _PendingIncident] = {}
        self._work_changed = threading.Condition()
        self._stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._image_worker: threading.Thread | None = None
        self._cleanup_pending: set[int] = set()
        # Evaluation harness only (app/evalrun.py): restrict RAG kinds, capture the evidence store of the last run.
        self.exec_kube = None    # SafeActionKube (separate write identity) or None => typed actions are denied
        self.rag_kinds: list[str] | None = None
        self.capture: dict | None = None
        self.model_snapshot: dict | None = None
        self._workflow_run_id: int | None = None
        self.interpret_worker = None   # P15 display-only Korean readings (InterpretWorker); None in evaluation

    # ------------------------------------------------------------------ intake
    def on_signal(self, sig: Signal) -> None:
        if evaluation_lock_active():     # evaluation campaign owns the fault namespace; signals are its own
            return
        with db.session() as s:
            inc, outcome = ingest(s, self.cfg, sig, kube=self.kube)
            iid = inc.id if inc else None
            placement = _QueuePlacement.for_incident(inc) if inc else None
        if outcome == "created" and iid and placement:
            broker.publish(iid, "created", "open")
            self.schedule(iid, delay=self.cfg.settle_seconds, placement=placement)
            # A concurrent merge may have committed before this creator registered.
            if placement.image_pull and not self._image_signal_current(iid):
                self._demote(iid)
        elif outcome == "merged" and iid and placement:
            if not placement.image_pull:
                self._demote(iid)
            broker.publish(iid, "updated")

    def schedule(self, incident_id: int, delay: float = 0, *, placement: _QueuePlacement | None = None) -> None:
        if placement is None:
            with db.session() as s:
                inc = s.get(Incident, incident_id)
                if inc is None:
                    return
                placement = _QueuePlacement.for_incident(inc)
        with self._work_changed:
            if self._stop.is_set() or incident_id in self._scheduled:
                return
            self._scheduled[incident_id] = _PendingIncident(placement, time.monotonic() + delay)
            self._work_changed.notify_all()

    def _image_signal_current(self, iid: int) -> bool:
        try:
            with db.session() as s:
                inc = s.get(Incident, iid)
                return inc is not None and _QueuePlacement.for_incident(inc).image_pull
        except Exception:
            log.warning("사건 %s의 이미지 신호 재확인을 일반 작업자로 넘깁니다.", iid)
            return False

    def _demote(self, iid: int) -> None:
        with self._work_changed:
            task = self._scheduled.get(iid)
            if task and task.placement.image_pull:
                task.placement = _QueuePlacement(task.placement.target, False)
                self._work_changed.notify_all()

    def _claim(self, image_lane: bool) -> int | None:
        with self._work_changed:
            while not self._stop.is_set():
                now = time.monotonic()
                next_deadline = None
                seen_targets = set()
                # Insertion order is FIFO per workload, even across the two lanes.
                # A claimed or settling head blocks only its own target.
                for iid, task in self._scheduled.items():
                    target = task.placement.target
                    if target in seen_targets:
                        continue
                    seen_targets.add(target)
                    if task.running or task.placement.image_pull != image_lane:
                        continue
                    if task.ready_at <= now:
                        task.running = True
                        return iid
                    if next_deadline is None or task.ready_at < next_deadline:
                        next_deadline = task.ready_at
                self._work_changed.wait(None if next_deadline is None else max(0, next_deadline - time.monotonic()))
        return None

    def start(self) -> None:
        with self._work_changed:
            # 한 Engine의 시작은 한 번뿐이다. 죽은 작업자를 조용히 재시작하지 않는다.
            if self._worker_thread is not None or self._stop.is_set():
                return
            self._worker_thread = threading.Thread(target=self._worker, name="incident-worker", daemon=True)
            self._image_worker = threading.Thread(target=self._worker, args=(True,),
                                                  name="incident-image-pull", daemon=True)
            self._worker_thread.start()
            self._image_worker.start()

    def stop(self) -> None:
        with self._work_changed:
            self._stop.set()
            self._work_changed.notify_all()

    def worker_health(self) -> dict[str, bool]:
        with self._work_changed:
            alive = all(t is not None and t.is_alive()
                        for t in (self._worker_thread, self._image_worker))
            pending = bool(self._cleanup_pending)
            return {"alive": alive, "cleanup_pending": pending,
                    "ready": alive and not pending and not self._stop.is_set()}

    def recover_on_startup(self) -> None:
        """Explicit handling of incidents interrupted by a restart (never silent, never replay actions).

        Every recovery transition is also published as a durable SSE event (after the recovery
        commit) so connected dashboards learn of it and cursor replay includes it.
        """
        failed_ids: list[int] = []
        action_ids: list[int] = []
        run_ids: list[int] = []
        pending: dict[int, tuple[float, _QueuePlacement]] = {}
        with db.session() as s:
            for inc in s.scalars(select(Incident).where(Incident.status.in_(("analyzing", "acting")))):
                prev = inc.status
                read_run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == inc.id,
                    WorkflowRun.status.in_(("running", "resume_pending"))).order_by(WorkflowRun.id.desc()).limit(1))
                if (prev == 'analyzing' and read_run and 'collection' in (read_run.snapshot or {})
                        and (read_run.snapshot or {}).get('execution')
                        and s.scalar(select(Action.id).where(Action.incident_id == inc.id).limit(1)) is None):
                    read_run.status, read_run.phase = 'resume_pending', 'analysis'
                    read_run.reason = '완료된 읽기 자료 재사용 대기; 쓰기 재실행 없음'
                    pending[inc.id] = (0, _QueuePlacement.for_incident(inc))
                    audit(s, 'liveprobe', 'analysis_resume_pending', inc.id, workflow_run_id=read_run.id)
                    continue
                transition(inc, "failed")
                inc.status_reason = f"LiveProbe restarted while incident was '{prev}'; marked failed (no replay)"
                update_run(s, inc.id, "failed", prev, inc.status_reason)
                audit(s, "liveprobe", "incident_interrupted", inc.id, previous_status=prev)
                failed_ids.append(inc.id)
            for act in s.scalars(select(Action).where(Action.status.in_(("executing", "approved")))):
                act.status = "failed"
                act.reason = "LiveProbe restarted during action; outcome indeterminate, inspect target manually"
                act.after_state = {**(act.after_state or {}), 'reconciliation': {
                    'status': 'required', 'write_outcome': 'unknown'}}
                audit(s, "liveprobe", "action_interrupted", act.incident_id, action_id=act.id,
                      reconciliation='required', write_outcome='unknown')
                run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == act.incident_id)
                               .order_by(WorkflowRun.id.desc()).limit(1))
                if run:
                    previous = (run.snapshot or {}).get('write_reconciliation') or {}
                    run.snapshot = {**(run.snapshot or {}), 'write_reconciliation': {
                        'action_ids': sorted(set([*previous.get('action_ids', []), act.id])), 'status': 'required'}}
                action_ids.append(act.incident_id)
            for run in s.scalars(select(WorkflowRun).where(WorkflowRun.status == "running")):
                run.status, run.reason = "failed", "LiveProbe restarted; work not replayed"
                run_ids.append(run.incident_id)
            for inc in s.scalars(select(Incident).where(Incident.status == "open").order_by(Incident.id)):
                pending[inc.id] = (self.cfg.settle_seconds, _QueuePlacement.for_incident(inc))
        for iid in dict.fromkeys(failed_ids):
            broker.publish(iid, "failed", "failed")
        # Interrupted actions/runs whose incident was not itself failed above (e.g. action_pending).
        for iid in dict.fromkeys(action_ids):
            if iid not in failed_ids:
                broker.publish(iid, "action")
        for iid in dict.fromkeys(run_ids):
            if iid not in failed_ids and iid not in action_ids:
                broker.publish(iid, "updated")
        for iid, (delay, placement) in pending.items():
            self.schedule(iid, delay=delay, placement=placement)

    def _worker(self, image_lane: bool = False) -> None:
        while not self._stop.is_set():
            iid = self._claim(image_lane)
            if iid is None or self._stop.is_set():
                return
            if image_lane:
                image_only = self._image_signal_current(iid)
                with self._work_changed:
                    task = self._scheduled[iid]
                    if not image_only or not task.placement.image_pull:
                        task.placement = _QueuePlacement(task.placement.target, False)
                        task.running = False
                        self._work_changed.notify_all()
                        continue
                    if self._stop.is_set():
                        return
            attempt = _WorkerAttempt(self, iid)
            token = _worker_attempt.set(attempt)
            try:
                self.process(iid)
            except runlog.RunStopped as stop:
                self._finish_worker(iid, stop, worker_run_id=attempt.workflow_run_id)
            except Exception as e:  # noqa: BLE001
                log.error("사건 %s 처리 실패; 종료 기록을 확인합니다.", iid)
                self._finish_worker(iid, e, worker_run_id=attempt.workflow_run_id)
            finally:
                _worker_attempt.reset(token)
                with self._work_changed:
                    if iid not in self._cleanup_pending:
                        del self._scheduled[iid]
                        self._work_changed.notify_all()

    def _finish_worker(self, iid: int, error: Exception, *, worker_run_id: int | None = None) -> None:
        # 처리 자체는 재실행하지 않는다. 종료 기록만 새 트랜잭션에서 재확인한다.
        with self._work_changed:
            self._cleanup_pending.add(iid)
        while not self._stop.is_set():
            try:
                if isinstance(error, runlog.RunStopped):
                    result = runlog.finalize_stop_result(iid, worker_run_id=worker_run_id,
                                                  reason="운영자 중단")
                else:
                    result = self._record_worker_failure(iid, error,
                        worker_run_id=worker_run_id)
                if (isinstance(result, runlog.CleanupResult) and result.pending
                        and not isinstance(error, runlog.RunStopped)):
                    if self._stop.wait(1.0):
                        return
                    continue
                # 중단 소유 실행의 종료는 확정됐다. 남은 외부 조치/다른 실행은
                # callback·수동 조정이 재확인하며 사건은 비종결로 둔다.
                # DB 예외(아래 except)는 여전히 정리 확정 전 다음 사건을 막는다.
                with self._work_changed:
                    self._cleanup_pending.discard(iid)
                return
            except Exception:  # noqa: BLE001
                log.warning("사건 %s 종료 기록 보류; 저장소 재확인을 기다립니다.", iid)
                if self._stop.wait(1.0):
                    return

    def _record_worker_failure(self, iid: int, error: Exception, *,
                               worker_run_id: int | None = None) -> runlog.CleanupResult:
        # Broker와 같은 순서로 직렬화하고 사건·감사·재생 이벤트를 함께 확정한다.
        # commit 응답이 유실돼도 다음 조회는 terminal을 보므로 중복 기록하지 않는다.
        with broker._lock, db.session() as s:
            inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            if inc is None:
                return runlog.CleanupResult.MISSING
            if worker_run_id is not None:
                run = s.scalar(select(WorkflowRun).where(WorkflowRun.id == worker_run_id).with_for_update())
                # rollback으로 사라진 ID와 취득 후 사건 연결 불일치는 다르다.
                if run is not None and run.incident_id != iid:
                    blocker = runlog.cleanup_blocker_locked(s, iid)
                    return blocker if blocker is not None else runlog.CleanupResult.INSPECTION_REQUIRED
                if run is not None and run.status in ("running", "resume_pending"):
                    run.status, run.phase, run.reason = "failed", "error", "내부 처리 오류"
            blocker = runlog.cleanup_blocker_locked(s, iid)
            if blocker is not None:
                return blocker
            if inc.status in INCIDENT_CLOSED_STATES:
                return runlog.CleanupResult.ALREADY_TERMINAL
            transition(inc, "failed")
            kind = type(error).__name__
            if kind not in {"OperationalError", "InterfaceError", "RuntimeError", "ValueError", "TimeoutError", "LLMError"}:
                kind = "Exception"
            inc.status_reason = f"내부 처리 오류 ({kind})"
            audit(s, "liveprobe", "incident_failed", iid, error=inc.status_reason)
            s.add(IncidentEvent(incident_id=iid, event="failed", status="failed"))
        # publish()는 별도 DB 쓰기이므로 재호출하지 않는다. 확정된 이벤트만 전달한다.
        loop = broker._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(broker._deliver, {"incident_id": iid, "event": "failed", "status": "failed"})
            except RuntimeError:
                log.warning("사건 %s 실패 기록 확정; 실시간 알림은 재생 조회로 확인합니다.", iid)
        return runlog.CleanupResult.COMPLETED

    # ------------------------------------------------------------ processing
    def _request_readings(self, iid: int) -> None:
        if self.interpret_worker is not None:
            try:
                self.interpret_worker.request(iid)
            except Exception:
                log.warning("display reading queue unavailable for incident %s", iid)

    def _set_status(self, iid: int, status: str, **fields) -> None:
        with db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None:
                raise ValueError("incident not found")
            transition(inc, status)
            if status == 'handed_off':
                from .handoff_classes import record, RECOVERY_EXHAUSTED
                handoff = dict(fields.get('handoff') or inc.handoff or {})
                diagnostic = handoff.get('workflow_status') in ('unverified', 'stopped', 'failed', 'verified')
                code = 'group_diagnostic_exhausted' if diagnostic else 'operator_authority_required'
                if 'handoff_classification' not in handoff:
                    handoff['handoff_classification'] = record(code, {'group_diagnostic_exhausted': RECOVERY_EXHAUSTED},
                        authorization={'level': inc.severity_level}, budget=handoff.get('budget'),
                        attempts=[{'kind': 'group_diagnosis', 'outcome': handoff['workflow_status']}] if diagnostic else [])
                fields['handoff'] = handoff
            for k, v in fields.items():
                if k == "handoff" and isinstance(v, dict):
                    v = {**(inc.handoff or {}), **v}
                setattr(inc, k, v)
            update_run(s, iid, "failed" if status == "failed" else "succeeded" if status in ("resolved", "handed_off") else "running", status, fields.get("status_reason"))
            audit(s, "liveprobe", "incident_status", iid, status=status, reason=fields.get("status_reason"))
        self._request_readings(iid)
        broker.publish(iid, status if status in ("resolved", "handed_off", "failed") else "updated", status)
        if status in ("resolved", "handed_off", "failed"):
            runlog.emit(iid, "completed", {"resolved": "해결됨", "handed_off": "운영자 인계", "failed": "실패"}[status] +
                        (f": {redact(str(fields.get('status_reason') or ''))[:300]}" if fields.get("status_reason") else ""),
                        {"outcome": status})
        elif status == "action_pending":
            runlog.emit(iid, "status", "운영자 승인을 기다립니다.", {"status": status})

    def process(self, iid: int, *, arm: dict | None = None) -> None:
        # Never mutate the engine-wide config: active calls retain their original placement.
        from copy import copy
        from .model_settings import snapshot_config
        runner = copy(self)
        runner.model_snapshot = None
        runner._workflow_run_id = None
        attempt = _worker_attempt.get()
        # 호출 중 중첩 process는 바깥 worker의 carrier를 상속하지 않는다.
        token = _worker_attempt.set(None)
        try:
            if arm is None:
                with db.session() as s:
                    runner.cfg, runner.model_snapshot = snapshot_config(s, self.cfg)
            if runner._process(iid, arm=arm) is False:
                return
            from .changes import auto_create_for_incident
            if arm is None:
                auto_create_for_incident(runner, iid)
                from .unified import auto_continue
                auto_continue(iid)
        finally:
            try:
                if attempt is not None and attempt.engine is self and attempt.incident_id == iid:
                    attempt.workflow_run_id = runner._workflow_run_id
            finally:
                _worker_attempt.reset(token)

    def _collect_or_reuse(self, ns: str, workload: str, since: datetime | None) -> Bundle:
        import json
        from copy import deepcopy
        from dataclasses import asdict
        from .evidence import EvidenceItem

        def restore(value):
            value = deepcopy(value)
            if value.get('namespace') != ns or value.get('workload') != workload:
                raise ValueError('봉인된 수집 자료의 대상 불일치')
            for item in value.get('items', []):
                stamp = item.get('collected_at')
                if isinstance(stamp, str):
                    item['collected_at'] = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            value['items'] = [EvidenceItem(**item) for item in value.get('items', [])]
            return Bundle(**value)

        if self._workflow_run_id is not None:
            with db.session() as s:
                row = s.get(WorkflowRun, self._workflow_run_id)
                if row and 'collection' in (row.snapshot or {}):
                    self._collection_attempts = [restore(v) for v in row.snapshot.get('collection_attempts', [row.snapshot['collection']])]
                    return restore(row.snapshot['collection'])
        bundle = collect(self.kube, self.cfg, self.settings, ns, workload, since=since)
        self._collection_attempts = [bundle]
        core = [item for item in bundle.items if item.source in R.CRITICAL_SOURCES]
        if self.cfg.recovery.evidence_recollect and (any(item.error for item in core) or not bundle.deployment or not bundle.pods):
            if self._workflow_run_id is not None:
                with db.session() as session:
                    row = session.get(WorkflowRun, self._workflow_run_id)
                    iid = row.incident_id
                runlog.gate(iid, '핵심 근거 재수집')
                runlog.emit(iid, 'analysis_retry', '핵심 조회 근거가 부족해 한 번 다시 수집합니다.',
                            {'attempt': 2, 'sources': sorted({i.source for i in core if i.error})})
            second = collect(self.kube, self.cfg, self.settings, ns, workload, since=since)
            # Preserve history separately, never mix stale errors into current features.
            self._collection_attempts.append(second)
            bundle = second
        if self._workflow_run_id is not None:
            frozen = json.loads(json.dumps(redact_obj(asdict(bundle)), default=lambda v: v.isoformat()))
            with db.session() as s:
                row = s.scalar(select(WorkflowRun).where(WorkflowRun.id == self._workflow_run_id).with_for_update())
                if row is None:
                    raise RuntimeError('수집 결과를 저장할 실행 원장 없음')
                attempts = json.loads(json.dumps(redact_obj([asdict(b) for b in self._collection_attempts]), default=lambda v: v.isoformat()))
                row.snapshot = {**(row.snapshot or {}), 'collection': (row.snapshot or {}).get('collection', frozen),
                                'collection_attempts': attempts}
                return restore(row.snapshot['collection'])
        return bundle

    def _process(self, iid: int, *, arm: dict | None = None) -> bool | None:
        """Run one incident. `arm` is set only by the evaluation harness (liveprobe/app/evalrun.py):
        {"strategy": "single"|"fixed_group"|"adaptive", "role": RoleModel (single arm), "level": "COMPLEX" (fixed)}."""
        arm = arm or {}
        stop_pending = False
        with db.session() as s:
            # 실행 시작과 대기 중 중단은 같은 사건 행 잠금으로 직렬화한다.
            from .models import RunControl
            inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            resume = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == iid,
                WorkflowRun.status == 'resume_pending').order_by(WorkflowRun.id.desc()).limit(1).with_for_update())
            if inc is None or (inc.status != "open" and not (inc.status == 'analyzing' and resume)):
                return False
            if resume:
                self._workflow_run_id = resume.id
                from .runtime_resume import source_fingerprint
                execution = (resume.snapshot or {}).get('execution') or {}
                if (execution.get('code_sha256') != source_fingerprint()
                        or s.scalar(select(Action.id).where(Action.incident_id == iid).limit(1)) is not None):
                    raise RuntimeError('봉인된 코드 지문 충돌 또는 쓰기 이력: 재개 중단')
                self.cfg = AppConfig.model_validate(execution['config'])
                self.model_snapshot = execution.get('model_snapshot')
            ns, workload, created = inc.namespace, inc.workload, inc.created_at
            incident_view = {"id": inc.id, "namespace": ns, "workload": workload, "title": inc.title,
                             "signals": inc.signal_reasons, "created_at": inc.created_at.isoformat(),
                             "system": inc.system, "environment": self.cfg.environment(ns)}
            control = s.get(RunControl, iid)
            if control is not None and control.state == "stopping":
                stop_pending = True
            else:
                if s.scalar(select(WorkflowRun.id).where(WorkflowRun.incident_id == iid,
                        WorkflowRun.status == "running").limit(1)) is not None:
                    raise RuntimeError("다른 활성 실행 확인 필요")
                if resume:
                    run = resume
                    run.status, run.reason = 'running', None
                else:
                    from .runtime_resume import source_fingerprint
                    transition(inc, "analyzing")
                    run = WorkflowRun(incident_id=iid, status="running", phase="analysis", snapshot={
                        'execution': {'config': self.cfg.model_dump(mode='json'),
                            'code_sha256': source_fingerprint(), 'model_snapshot': self.model_snapshot}})
                    s.add(run)
                    s.flush()
                self._workflow_run_id = run.id
                audit(s, "liveprobe", "analysis_started", iid)
        from .runtime_budget import for_incident
        self._incident_runtime = for_incident(self.cfg, iid)
        if stop_pending:
            # 잠금 해제 후 공통 경계에서 재확인한다. 살아 있는 조치는 완료로 꾸미지 않는다.
            runlog.set_control(iid, "stop")
            if runlog.finalize_stop_result(iid).pending:
                raise runlog.RunStopped("대기 중 운영자 중단")
            return False
        broker.publish(iid, "updated", "analyzing")

        # evidence -----------------------------------------------------------
        runlog.gate(iid, "근거 수집")
        bundle = self._collect_or_reuse(ns, workload, created - timedelta(seconds=60))
        with db.session() as s:
            for attempt in self._collection_attempts:
                for it in attempt.items:
                    s.add(Evidence(incident_id=iid, source=it.source, target=it.target, payload=it.payload, error=it.error,
                                   collected_at=it.collected_at))
        broker.publish(iid, "updated", "analyzing")
        runlog.emit(iid, "evidence_collected", f"근거 {len(bundle.items)}건 수집", {
            "sources": sorted({it.source for it in bundle.items}),
            "errors": [{"source": it.source, "error": str(it.error)[:160]} for it in bundle.items if it.error][:5]})

        self._request_readings(iid)
        self._save_remediation(iid, bundle)
        # routing ------------------------------------------------------------
        strategy = arm.get("strategy", "adaptive")
        # Collection/LLM calls take time: incorporate signals merged since analysis began.
        with db.session() as s:
            current = s.get(Incident, iid)
            incident_view["signals"] = list(current.signal_reasons) if current else incident_view["signals"]
        decision = R.decide(bundle, self.cfg.recent_rollout_minutes,
                            llm_classify=(lambda p: self._classify(iid, p)) if strategy == "adaptive" else None,
                            cfg=self.cfg, signal_reasons=incident_view["signals"])
        if strategy == "fixed_group" and decision.decided_by != "router_failure":
            # Fixed Multi-Agent arm: the same COMPLEX group for every incident (no routing).
            decision = R.Decision(arm["level"], R.WORKFLOW[arm["level"]], decision.features,
                                  "fixed multi-agent arm: group fixed regardless of incident", 1.0, "fixed",
                                  llm=None, policy=R.score_models(arm["level"], self.cfg))
        base_cfg = self.cfg
        self._record_decision(iid, decision)

        if decision.decided_by == "router_failure" or (decision.policy or {}).get('handoff_only'):
            from .handoff_classes import record, RECOVERY_EXHAUSTED
            classification = record(decision.features.get('reason_code'), {c: RECOVERY_EXHAUSTED for c in R.RECOLLECTABLE_CODES},
                attempts=[{'kind': 'evidence_collection', 'attempt': n + 1,
                           'errors': sorted({i.source for i in b.items if i.error})}
                          for n, b in enumerate(self._collection_attempts)],
                budget={'evidence_recollect': self.cfg.recovery.evidence_recollect})
            self._set_status(iid, "handed_off", status_reason=decision.rationale,
                             handoff={"summary": decision.rationale, "root_cause_candidates": [],
                                      "handoff_classification": classification,
                                      "recommended_steps": ["inspect evidence and router failure"],
                                      "notes": ["router failure; no automatic action"]})
            return
        if strategy == 'adaptive' and decision.level in ('NORMAL', 'COMPLEX'):
            recovered, bundle = self._observe_recovery(iid, bundle, incident_view, phase='pre_group')
            if recovered:
                return
            latest = R.decide(bundle, self.cfg.recent_rollout_minutes, cfg=self.cfg, signal_reasons=incident_view['signals'])
            if R.LEVEL_ORDER[latest.level] > R.LEVEL_ORDER[decision.level] or latest.decided_by == 'router_failure':
                decision = latest
                self._record_decision(iid, decision)
                if latest.decided_by == 'router_failure':
                    from .handoff_classes import record, RECOVERY_EXHAUSTED
                    self._set_status(iid, 'handed_off', status_reason=latest.rationale,
                        handoff={'summary': latest.rationale, 'handoff_classification': record(
                            'fresh_evidence_unavailable', {'fresh_evidence_unavailable': RECOVERY_EXHAUSTED},
                            attempts=[{'kind': 'evidence_collection'}, {'kind': 'fresh_health_recheck'}],
                            budget={'fresh_health_recheck': 1})})
                    return
            else:
                decision.features = latest.features
        from . import steering
        for _ in range(3):        # P15: an operator may move the analysis to another group mid-run (bounded)
            try:
                self._run_decision(iid, decision, bundle, incident_view, strategy, arm)
                return
            except steering.RerouteRequested as rr:
                self.cfg = base_cfg
                decision = R.Decision(rr.level, R.WORKFLOW[rr.level], decision.features,
                                      f"운영자 채팅으로 {rr.level} Group 재지정: {redact(rr.body)[:200]}", 1.0, "operator",
                                      llm=None, policy=R.score_models(rr.level, self.cfg))
                self._record_decision(iid, decision)
        self._set_status(iid, "handed_off", status_reason="운영자 Group 재지정이 반복되어 분석을 멈췄습니다.",
                         handoff={"summary": "Group 재지정이 3회를 넘었습니다.", "root_cause_candidates": [],
                                  "recommended_steps": ["분석 기록을 검토한 뒤 채팅으로 지시하세요."], "notes": ["reroute limit"]})

    def _observe_recovery(self, iid: int, first: Bundle, incident_view: dict, *, phase: str,
                          pending_action_id: int | None = None) -> tuple[bool, Bundle]:
        from .observed_recovery import observed_at, recovery_proof
        runlog.gate(iid, '읽기 전용 건강 재확인')
        start = observed_at(first)
        if phase == 'group_no_action' or start is None or (datetime.now(timezone.utc) - start).total_seconds() > 90:
            # Recovery may have happened during the group: both confirmations must be post-group reads.
            first = collect(self.kube, self.cfg, self.settings, first.namespace, first.workload,
                            since=datetime.now(timezone.utc) - timedelta(seconds=60))
            self._save_observation(iid, first, phase + '_baseline')
        self._sleep(self.cfg.recovery.observation_interval_s)
        runlog.gate(iid, '새 상태 수집')
        # Restrict log collection to this observation interval, not the original incident window.
        fresh = collect(self.kube, self.cfg, self.settings, first.namespace, first.workload,
                        since=observed_at(first) or datetime.now(timezone.utc))
        self._save_observation(iid, fresh, phase)
        with db.session() as s:
            inc = s.get(Incident, iid)
            incident_view['signals'] = list(inc.signal_reasons)
        proof = recovery_proof(first, fresh, incident_view['signals'],
                               min_interval=self.cfg.recovery.observation_interval_s)
        runlog.emit(iid, 'health_recheck', '정상 상태 재확인' if proof else '추가 관측 후 진단 계속',
                    {'phase': phase, 'recovered': bool(proof), 'cause_confirmed': False, 'ai_remediation': False})
        if proof:
            runlog.gate(iid, '관측 회복 기록')
            with db.session() as s:
                inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
                # A merged unknown signal, operator instruction or any action invalidates a read-only proof.
                from .models import OperatorCommand, OperatorMessage, RunControl
                other_actions = select(Action.id).where(Action.incident_id == iid)
                pending = None
                if pending_action_id is not None:
                    pending = s.scalar(select(Action).where(Action.id == pending_action_id,
                        Action.incident_id == iid).with_for_update())
                    if not pending or pending.executed or pending.status not in ('approved', 'pending_approval'):
                        return False, fresh
                    other_actions = other_actions.where(Action.id != pending_action_id)
                expected_state = 'action_pending' if pending else 'analyzing'
                if (inc.status != expected_state or list(inc.signal_reasons) != incident_view['signals']
                        or s.scalar(other_actions.limit(1)) is not None
                        or s.scalar(select(OperatorCommand.id).where(OperatorCommand.incident_id == iid,
                            OperatorCommand.status.in_(('planning', 'awaiting_confirmation', 'awaiting_secret', 'executing', 'rolling_back'))).limit(1)) is not None
                        or s.scalar(select(OperatorMessage.id).where(OperatorMessage.incident_id == iid,
                            OperatorMessage.applied_at.is_(None)).limit(1)) is not None
                        or (s.get(RunControl, iid) and s.get(RunControl, iid).state != 'running')):
                    return False, fresh
                if pending:
                    pending.status, pending.reason = "cancelled", "두 관측에서 원래 증상의 해소를 확인해 적용하지 않았습니다."
                    audit(s, "liveprobe", "action_cancelled_after_recovery", iid, action_id=pending.id)
                from .accountability import AWAITING, WAITING_LABEL, append, investigate
                retained = {k: v for k, v in (inc.handoff or {}).items() if k != 'handoff_classification'}
                inc.handoff = {**retained, 'observed_recovery': proof}
                report = investigate(s, inc)
                if report['status'] == 'explained':
                    proof = {**proof, 'cause_confirmed': True}
                    inc.handoff = {**inc.handoff, 'observed_recovery': proof}
                status = 'resolved' if report['status'] == 'explained' else AWAITING
                transition(inc, status)
                inc.status_reason = '정상 · 복구 원인 확인' if status == 'resolved' else WAITING_LABEL
                inc.handoff = {**inc.handoff, 'summary': inc.status_reason, 'summary_ko': inc.status_reason}
                append(s, iid, report, 'liveprobe')
                update_run(s, iid, 'succeeded', status, inc.status_reason)
                audit(s, 'liveprobe', 'observed_recovery', iid, **proof)
            self._request_readings(iid)
            broker.publish(iid, 'updated', status)
            runlog.emit(iid, 'completed', inc.status_reason, {'outcome': status, **proof})
            return True, fresh
        return False, fresh

    def _save_observation(self, iid: int, bundle: Bundle, phase: str) -> None:
        import json
        from dataclasses import asdict
        frozen = json.loads(json.dumps(redact_obj(asdict(bundle)), default=lambda v: v.isoformat()))
        with db.session() as s:
            for it in bundle.items:
                s.add(Evidence(incident_id=iid, source=it.source, target=it.target, payload=it.payload,
                               error=it.error, collected_at=it.collected_at))
            if getattr(self, '_workflow_run_id', None) is not None:
                row = s.scalar(select(WorkflowRun).where(WorkflowRun.id == self._workflow_run_id).with_for_update())
                row.snapshot = {**(row.snapshot or {}), 'observations': [
                    *((row.snapshot or {}).get('observations') or []), {'phase': phase, 'collection': frozen}]}

    def reassess_read_only(self, iid: int, run_id: int) -> None:
        """A separate, write-incapable lane: no Group, action, change request or authority replay."""
        from copy import copy
        from .handoff_classes import record, RECOVERY_EXHAUSTED
        runner = copy(self)
        runner._workflow_run_id, runner.model_snapshot = run_id, None
        try:
            with db.session() as s:
                inc = s.get(Incident, iid)
                view = {'id': iid, 'signals': list(inc.signal_reasons)}
                ns, workload = inc.namespace, inc.workload
            first = runner._collect_or_reuse(ns, workload, datetime.now(timezone.utc) - timedelta(seconds=60))
            with db.session() as s:
                for b in runner._collection_attempts:
                    for it in b.items:
                        s.add(Evidence(incident_id=iid, source=it.source, target=it.target, payload=it.payload,
                                       error=it.error, collected_at=it.collected_at))
            decision = R.decide(first, runner.cfg.recent_rollout_minutes, cfg=runner.cfg, signal_reasons=view['signals'])
            runner._record_decision(iid, decision)
            if decision.level != 'HIGH_RISK':
                recovered, latest = runner._observe_recovery(iid, first, view, phase='reassessment')
                if recovered:
                    return
                decision = R.decide(latest, runner.cfg.recent_rollout_minutes, cfg=runner.cfg, signal_reasons=view['signals'])
                runner._record_decision(iid, decision)
            boundary = decision.level == 'HIGH_RISK' and decision.decided_by != 'router_failure'
            runner._set_status(iid, 'handed_off', status_reason='읽기 전용 재평가: 진단 완료, 정상 상태 미증명. 자동 변경 없음.',
                handoff={'reassessment': {'outcome': 'diagnosed', 'read_only': True, 'run_id': run_id},
                    'handoff_classification': record('rules_high_risk' if boundary else 'reassessment_health_unproven', {'reassessment_health_unproven': RECOVERY_EXHAUSTED},
                        attempts=[{'kind': 'read_only_reassessment', 'run_id': run_id}],
                        authorization={'level': decision.level, 'read_only': True})})
        except Exception as exc:
            with db.session() as s:
                inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
                if inc.status != 'analyzing':
                    # A concurrent stop may already have finalized the incident. Never reopen it.
                    audit(s, 'liveprobe', 'reassessment_interrupted', iid, run_id=run_id,
                          outcome=inc.status, error_kind=type(exc).__name__, read_only=True)
                    return
            runner._set_status(iid, 'handed_off', status_reason='읽기 전용 재평가 실패 · 자동 변경 없음',
                handoff={'reassessment': {'outcome': 'diagnosed', 'read_only': True, 'error': type(exc).__name__, 'run_id': run_id},
                    'handoff_classification': record('reassessment_failed', {'reassessment_failed': RECOVERY_EXHAUSTED},
                        attempts=[{'kind': 'read_only_reassessment', 'outcome': type(exc).__name__}])})

    def _record_decision(self, iid: int, decision: R.Decision) -> None:
        from .model_settings import apply_group
        self.cfg, decision.policy = apply_group(self.cfg, getattr(self, 'model_snapshot', None),
                                                decision.level, decision.policy)
        self._current_level = decision.level
        with db.session() as s:
            s.add(RoutingDecision(incident_id=iid, level=decision.level, workflow=decision.workflow,
                                  features=redact_obj(decision.features), rationale=redact(decision.rationale),
                                  policy=redact_obj(decision.policy),
                                  confidence=decision.confidence, decided_by=decision.decided_by))
            inc = s.get(Incident, iid)
            inc.severity_level = decision.level
            audit(s, "router", "routed", iid, level=decision.level, workflow=decision.workflow,
                  decided_by=decision.decided_by)
        broker.publish(iid, "updated", "analyzing")
        self._request_readings(iid)
        runlog.emit(iid, "routing_decided", f"사건 수준 {decision.level} → {decision.workflow}",
                    {"level": decision.level, "workflow": decision.workflow, "decided_by": decision.decided_by,
                     "rationale": redact(decision.rationale)[:400], "confidence": decision.confidence})

    def _run_decision(self, iid: int, decision: R.Decision, bundle: Bundle, incident_view: dict, strategy: str,
                      arm: dict) -> None:
        workload = incident_view.get("workload")
        if strategy != "single" and decision.level in ("NORMAL", "COMPLEX"):
            self._run_group(iid, decision, bundle, incident_view)
            return

        # workflow -----------------------------------------------------------
        query = self._rag_query(bundle, decision)
        hits = self._rag(query)
        state = {
            "incident": incident_view, "features": decision.features, "evidence": compact_summary(bundle),
            "rag": hits, "has_previous_revision": decision.features.get("has_previous_revision", False),
            "min_confidence": max(self.cfg.min_confidence, self.cfg.workflows.min_agent_confidence or 0),
            "allowlist": list(self.cfg.allowlist),
            "bounds": bounds_text(self.cfg),
        }
        if strategy == "single":
            # Single arm (evaluation only): ONE model/effort performs the whole analysis, verification and safety review.
            rm = arm["role"]
            run_agent = lambda role, prompt: self._call(iid, "signal_analyst", prompt, AGENT_SCHEMA, AgentOutput, hits,  # noqa: E731
                                                        model_override=rm.model, effort_override=rm.effort, role_model=rm)
        else:
            run_agent = self._agent_runner(iid, hits, selected_model=(decision.policy or {})["selected_model"])
        if strategy == "single":
            out = build_single_graph(run_agent).invoke(state)
        elif decision.level == "SIMPLE":
            out = build_simple_graph(run_agent).invoke(state)
        else:
            out = build_high_risk_graph(run_agent).invoke(state)

        final = out.get("verifier") or out.get("safety") or out.get("analyst") or out.get("curator") or {}
        summary = (final or {}).get("summary")
        selected_remediation = self._save_remediation(iid, bundle, final, verified=out.get('outcome') == 'propose_action'
                               or (out.get('handoff') or {}).get('workflow_status') == 'verified')
        if isinstance(out.get('handoff'), dict):
            # Keep deterministic provenance when the workflow's raw handoff is persisted.
            out['handoff']['remediation'] = selected_remediation

        # KAN-53: every cited evidence/runbook ref must resolve to a source that was actually shown.
        # (SIMPLE/HIGH_RISK keep the MVP-0 message without verbatim quotes, so only ref resolution applies.)
        store = EvidenceStore(state["evidence"], hits)
        if self.capture is not None:
            self.capture["store"] = store
        cite = {role: check_citations(store, out.get(role) or {}).as_dict()
                for role in ("analyst", "verifier", "curator", "safety") if out.get(role)}
        unknown = sorted({r for c in cite.values() for r in c["unknown_refs"]})
        audit_detail = {"level": decision.level, "roles_checked": sorted(cite), "unknown_refs": unknown}
        with db.session() as s:
            audit(s, "workflow", "wf_citation_check", iid, **audit_detail)
        if isinstance(out.get("handoff"), dict):
            out["handoff"]["citation_check"] = audit_detail
            if unknown and out.get("outcome") == "handoff":
                out["reason"] = "출처·원문 인용 검증 실패 · 수집되지 않은 근거 번호를 인용했습니다."
        if unknown and out.get("outcome") == "propose_action":
            out = {**out, "outcome": "handoff", "reason": f"cited evidence refs do not exist: {unknown[:5]}",
                   "handoff": {"summary": summary, "notes": ["citation check failed; automatic action withheld"],
                               "citation_check": audit_detail}}
        if decision.level == "HIGH_RISK":
            from . import policy as _pol
            _pol.record(iid, "high_risk_level", profile=safety.active_profile(), level="HIGH_RISK", workload=workload,
                        proposed={"outcome": out.get("outcome")}, rationale=str(out.get("reason") or decision.rationale or ""),
                        impact="traffic-routing / cross-service blast radius",
                        outcome="held_for_operator" if not safety.high_risk_may_execute() else "proposed_for_approval")
        if decision.level == "HIGH_RISK" and not safety.high_risk_may_execute():
            # Assessment profile: record the suggestion (if any) as a denied action for auditability; never execute.
            with db.session() as s:
                inc = s.get(Incident, iid)
                inc.summary = summary
            self._set_status(iid, "handed_off", status_reason=out.get("reason") or "HIGH_RISK policy: handoff",
                             handoff=redact_obj(out.get("handoff")))
            return

        reanalysis = self.cfg.recovery.simple_reanalysis_level
        evidence_only = (out.get('handoff') or {}).get('workflow_status') == 'unverified'
        suggested_change = (final or {}).get('remediation') or {}
        if (decision.level == 'SIMPLE' and strategy == 'adaptive' and reanalysis and evidence_only
            and suggested_change.get('kind') in (None, 'none') and not out.get('action')):
            runlog.gate(iid, '추가 독립 분석')
            runlog.emit(iid, 'analysis_retry', '확정되지 않은 분석을 추가 역할이 다시 검증합니다.',
                        {'from': 'SIMPLE', 'to': reanalysis, 'attempt': 2})
            escalated = R.Decision(reanalysis, R.WORKFLOW[reanalysis], decision.features,
                                   '근거 검증 실패에 따른 1회 재분석', decision.confidence, 'bounded_reanalysis',
                                   llm=None, policy=R.score_models(reanalysis, self.cfg))
            self._record_decision(iid, escalated)
            return self._run_decision(iid, escalated, bundle, incident_view, strategy, arm)
        if out.get("outcome") != "propose_action":
            with db.session() as s:
                s.get(Incident, iid).summary = summary
            self._set_status(iid, "handed_off", status_reason=out.get("reason"),
                             handoff=redact_obj(out.get("handoff") or {"summary": summary, "notes": [out.get("reason")]}))
            return

        # safety -------------------------------------------------------------
        if self._guard_new_change(iid, bundle, incident_view, decision.level):
            return
        if self._propose_and_run(iid, decision.level, out["action"], summary):
            return
        self._set_status(iid, "handed_off", status_reason="action was not executable; operator handoff",
                         handoff={"summary": summary, "notes": ["action was not executable; see audit"]})

    def _save_remediation(self, iid: int, bundle: Bundle, output=None, *, verified=False):
        from .remediation import select_remediation
        remediation = select_remediation(bundle, output, verified=verified)
        with db.session() as s:
            inc = s.get(Incident, iid)
            latest = None
            if verified and remediation.get('source') == 'llm' and remediation.get('kind') in ('code_fix', 'both'):
                query = select(AgentRun).where(AgentRun.incident_id == iid,
                    AgentRun.role.in_(['result_verifier', 'group_verifier']))
                workflow_id = getattr(self, '_workflow_run_id', None)
                if workflow_id is not None:
                    workflow = s.get(WorkflowRun, workflow_id)
                    query = query.where(AgentRun.created_at >= workflow.created_at) if workflow else query.where(False)
                latest = s.scalar(query.order_by(AgentRun.id.desc()).limit(1))
            review = (latest.output or {}) if latest is not None else {}
            review_remediation = review.get('remediation') or {}
            code_verified = bool(verified and remediation.get('source') == 'llm'
                and remediation.get('kind') in ('code_fix', 'both') and latest is not None
                and latest.status == 'succeeded' and review.get('verdict') == 'supported'
                and not review.get('_acceptance_rejected')
                and review_remediation.get('kind') == remediation.get('kind')
                and review_remediation.get('code_refs') == remediation.get('code_refs'))
            marker = {'verified': code_verified,
                'verifier_run_id': latest.id if code_verified else None,
                'workflow_run_id': getattr(self, '_workflow_run_id', None),
                'basis': 'independent_workflow' if code_verified else 'unverified_or_not_code'}
            # This also invalidates a previous diagnosis before any new analysis.
            # It survives a successful K8s mitigation that returns before handoff assembly.
            inc.handoff = {**(inc.handoff or {}), 'remediation': remediation, 'code_cause_verification': marker}
            audit(s, "workflow", "remediation_decided", iid, **remediation)
        return remediation

    def _kube_for(self, aid: int):
        """rollout_undo goes through the rollback controller (observe client + signed grant); only the five typed
        actions use the separate write identity. Mixing them up fails (the executor client has no request_rollback)."""
        from . import actions as typed
        with db.session() as s:
            a = s.get(Action, aid)
            is_typed = bool(a) and a.type in typed.TYPED
        return (self.exec_kube or self.kube) if is_typed else self.kube

    def attach_executor(self, token_file: str) -> bool:
        """Wire the dedicated write identity. Without it typed actions are denied (fail closed). Shared by the API
        startup and the evaluation runner so both exercise the same execution path."""
        try:
            self.exec_kube = self.kube.executor(token_file, cfg=self.cfg)
        except Exception as e:  # noqa: BLE001 - never block startup
            log.warning("safe-action executor unavailable: %s", type(e).__name__)
            self.exec_kube = None
        return self.exec_kube is not None

    def _network_handoff(self, iid: int, aid: int | None = None) -> bool:
        """Recheck late network risk before proposal or execution; never replay an action."""
        reason = 'NetworkDrop: 트래픽 경로 이상 신호가 추가되어 자동 조치를 중단하고 운영자에게 인계합니다.'
        with db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None or not safety.has_network_risk(inc) or inc.status not in ('open', 'analyzing', 'action_pending', 'acting'):
                return False
            if inc.severity_level != 'HIGH_RISK':
                inc.severity_level = 'HIGH_RISK'
                s.add(RoutingDecision(incident_id=iid, level='HIGH_RISK', workflow=R.WORKFLOW['HIGH_RISK'],
                                      features={'network_drop_signal': True, 'signal_reasons': inc.signal_reasons},
                                      rationale=reason, policy={**R.score_models('HIGH_RISK', self.cfg),
                                                                'late_network_drop': True},
                                      confidence=1.0, decided_by='rules'))
            if aid is not None:
                action = s.get(Action, aid)
                if action and action.status in ('proposed', 'pending_approval', 'approved'):
                    action.status, action.reason = 'denied', reason
                    audit(s, 'liveprobe', 'action_denied', iid, action_id=aid, reason=reason)
            audit(s, 'liveprobe', 'network_risk_rechecked', iid, action_id=aid, reason=reason)
        self._set_status(iid, 'handed_off', status_reason=reason,
                         handoff={'summary': reason, 'root_cause_candidates': [],
                                  'recommended_steps': ['네트워크 차단 경로와 정책을 확인하세요.'],
                                  'notes': ['late NetworkDrop; no automatic action']})
        return True

    def _guard_new_change(self, iid: int, bundle: Bundle, incident_view: dict, level: str, handoff=None) -> bool:
        """An incident can recover during an agent call. Re-read the target before creating a write."""
        if self.capture is not None:
            return False
        runlog.gate(iid, "조치 직전 서비스 상태 확인")
        fresh = collect(self.kube, self.cfg, self.settings, bundle.namespace, bundle.workload,
                        since=datetime.now(timezone.utc) - timedelta(seconds=60))
        self._save_observation(iid, fresh, "before_action")
        latest = R.decide(fresh, self.cfg.recent_rollout_minutes, cfg=self.cfg,
                          signal_reasons=incident_view.get("signals"))
        if latest.level == "HIGH_RISK" and latest.decided_by != "router_failure" and level != "HIGH_RISK":
            from .handoff_classes import record
            self._record_decision(iid, latest)
            detail = {**(handoff or {}), "handoff_classification": record("rules_high_risk", {}, authorization={"level": latest.level})}
            self._set_status(iid, "handed_off", status_reason=latest.rationale, handoff=detail)
            return True
        # A previously terminated Pod can retain its old OOM reason. Let the two-snapshot
        # proof compare actual restart counters and symptom metrics, independently of that label.
        if fresh.service and (fresh.service.get("ready_endpoints") or 0) > 0 and fresh.pods and all(
                c.get("ready") is True for pod in fresh.pods for c in pod.get("containers") or []):
            recovered, _ = self._observe_recovery(iid, fresh, incident_view, phase="before_verified_action")
            return recovered
        return False

    def _propose_and_run(self, iid: int, level: str, action: dict, summary: str | None) -> bool:
        """One agent-proposed Kubernetes action through the validated executor. True when the incident was finished
        or moved to approval; False when the proposal was denied (caller hands off)."""
        from . import actions as typed
        if self._network_handoff(iid):
            return True
        from .unified import defer_mixed
        if defer_mixed(iid):
            self._set_status(iid, "handed_off", status_reason="복합 원인을 확인했습니다. 코드 수정의 dev 검증 뒤 인프라 조치를 확인합니다.")
            return True
        with db.session() as s:
            inc = s.get(Incident, iid)
            inc.summary = summary
            if action["type"] == "rollout_undo":
                act = safety.propose(s, self.cfg, inc, level, "rollout_undo", "Deployment", action["name"])
            else:
                act = typed.propose_typed(s, self.cfg, self.exec_kube or self.kube, inc, level, action["type"], action["kind"],
                                          action["name"], action["params"])
            aid, astatus, reason = act.id, act.status, act.reason
        broker.publish(iid, "action")
        runlog.emit(iid, "action_proposed", f"조치 제안: {action['type']} → {action['kind']}/{action['name']}",
                    {"action_id": aid, "type": action["type"], "params": action["params"], "status": astatus,
                     "why": action.get("why")})
        if astatus == "pending_approval":
            self._set_status(iid, "action_pending", status_reason="waiting for operator approval")
            return True
        if astatus != "proposed":
            runlog.emit(iid, "note", f"조치가 거부되었습니다: {reason}", {"action_id": aid})
            return False
        self.run_action(iid, aid, actor="liveprobe", level=level)
        return True

    def run_action(self, iid: int, aid: int, actor: str, level: str | None = None) -> safety.ExecResult:
        runlog.gate(iid, "조치 실행")        # last chance to pause/stop; once executing, the action runs to verification
        if self._network_handoff(iid, aid):
            return safety.ExecResult(aid, 'denied', 'NetworkDrop: operator handoff')
        # Human approval may arrive long after proposal. Only a not-yet-executed, pending
        # change can be withdrawn; an action that has started always reaches verification.
        from .observed_recovery import SUPPORTED
        with db.session() as s:
            inc, action = s.get(Incident, iid), s.get(Action, aid)
            signals = list(inc.signal_reasons or [])
            pending = (inc.status == 'action_pending' and action and not action.executed and
                       action.status in ('approved', 'pending_approval') and signals and set(signals) <= SUPPORTED)
            ns, workload = inc.namespace, inc.workload
        if pending:
            snapshot = collect(self.kube, self.cfg, self.settings, ns, workload, since=datetime.now(timezone.utc)-timedelta(seconds=60))
            self._save_observation(iid, snapshot, "approval_recheck")
            if snapshot.service and (snapshot.service.get("ready_endpoints") or 0) > 0 and snapshot.pods:
                recovered, _ = self._observe_recovery(iid, snapshot, {"signals": signals},
                    phase="before_approved_action", pending_action_id=aid)
                if recovered:
                    return safety.ExecResult(aid, "cancelled", "서비스가 회복되어 대기 중인 조치를 취소했습니다.")
        runlog.emit(iid, "action_proposed", f"조치 #{aid} 실행 시작", {"action_id": aid, "actor": actor, "level": level})
        self._set_status(iid, "acting")
        res = safety.execute(db.session, self.cfg, self._kube_for(aid), aid, actor, level=level)
        runlog.emit(iid, "action_executed", f"조치 #{aid} 결과: {res.status}", {"action_id": aid, "status": res.status,
                                                                          "reason": redact(str(res.reason or ""))[:300]})
        runlog.emit(iid, "verification_result", "롤아웃 검증 " + ("통과" if res.status == "succeeded" else "미통과"),
                    {"action_id": aid, "ok": res.status == "succeeded"})
        if res.status == "succeeded":
            from .accountability import AWAITING, WAITING_LABEL, append, investigate
            from .accountability import action_explanation
            from .observed_recovery import SUPPORTED, recovery_proof
            with db.session() as s:
                inc, act = s.get(Incident, iid), s.get(Action, aid)
                signals = list(inc.signal_reasons or [])
                ns, workload = inc.namespace, inc.workload
                direct_proof = action_explanation(inc, act)
            observed = None
            if not direct_proof and signals and set(signals) <= SUPPORTED:
                # Release the database connection while collecting original-symptom observations.
                baseline = collect(self.kube, self.cfg, self.settings, ns, workload, since=datetime.now(timezone.utc))
                self._save_observation(iid, baseline, "post_action")
                self._sleep(self.cfg.recovery.observation_interval_s)
                final = collect(self.kube, self.cfg, self.settings, ns, workload, since=datetime.now(timezone.utc))
                self._save_observation(iid, final, "post_action")
                observed = recovery_proof(baseline, final, signals, min_interval=self.cfg.recovery.observation_interval_s)
            with db.session() as s:
                inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
                act = s.get(Action, aid)
                if observed and list(inc.signal_reasons or []) == signals:
                    inc.handoff = {**(inc.handoff or {}), "observed_recovery": {**observed, "ai_remediation": None, "applied_action_id": aid}}
                report = investigate(s, inc)
                append(s, iid, report, actor)
                if report["status"] == "explained":
                    status, explanation = "resolved", report["recovery_ko"]
                elif report["recovery_observed_at"]:
                    status, explanation = AWAITING, WAITING_LABEL
                else:
                    status, explanation = "handed_off", "조치는 적용했지만 원래 장애의 해소를 확인할 근거가 부족합니다."
                    from .handoff_classes import record, RECOVERY_EXHAUSTED
                    inc.handoff = {**(inc.handoff or {}), "summary_ko": explanation,
                        "handoff_classification": record("post_action_observation_exhausted",
                            {"post_action_observation_exhausted": RECOVERY_EXHAUSTED},
                            attempts=[{"kind": "post_action_observation", "action_id": aid}])}
                transition(inc, status)
                inc.status_reason = explanation
                update_run(s, iid, "succeeded", status, explanation)
                audit(s, actor, "incident_status", iid, status=status, reason=explanation)
            self._request_readings(iid)
            broker.publish(iid, "updated", status)
            runlog.emit(iid, "completed", report["recovery_ko"], {"outcome": status, "action_id": aid})
        else:
            self._set_status(iid, "handed_off", status_reason=f"action {res.status}: {res.reason}",
                             handoff={"notes": [f"automatic action {res.status}: {res.reason}"]})
        return res

    # ------------------------------------------------------ NORMAL / COMPLEX (KAN-53)
    def _run_group(self, iid: int, decision: R.Decision, bundle: Bundle, incident_view: dict) -> None:
        """Run the NORMAL/COMPLEX group through the existing safety executor or bounded read-only follow-up."""
        hits = self._rag(self._rag_query(bundle, decision))
        from .resolution_memory import precedent as _precedent, signature as _signature
        signature = _signature(bundle.findings, bundle.dependencies, incident_view.get("signals"))
        self._memory_precedent(signature, hits)
        match = _precedent(hits, signature)
        if match is None:
            match = self._inherit_precedent(iid, incident_view, signature, hits)
            if match:
                runlog.emit(iid, "note", f"앞 사건 #{match.get('from_incident')}의 선례 {match['ref']}를 이어받음 "
                            f"(유사도 {match['similarity']}, 그룹은 유지)", {"precedent": match, "signature": signature})
        from .resolution_memory import precedent_checks as _checks
        checks = _checks(match, hits)
        if checks:
            # Shown to every role through the incident block; a hypothesis to check, never an instruction.
            incident_view = {**incident_view, "precedent_checks": checks}
        if match and decision.level == "COMPLEX" and match.get("verified") and not match.get("inherited"):
            # A verified precedent turns discovery into confirmation: the lighter group checks it against the
            # current evidence. It is cited like any reference and never authorizes a change by itself.
            from dataclasses import replace as _replace
            decision = _replace(decision, level="NORMAL", workflow=R.WORKFLOW["NORMAL"],
                                rationale=(f"{decision.rationale} · 검증된 선례 {match['ref']} 일치"
                                           f"(유사도 {match['similarity']}) → NORMAL 그룹으로 확인"),
                                policy={**R.score_models("NORMAL", self.cfg), "precedent": match})
            self._record_decision(iid, decision)
        if match and not match.get("inherited"):
            runlog.emit(iid, "note", f"{'검증된' if match.get('verified') else '관측된'} 선례 {match['ref']} 일치 "
                        f"(유사도 {match['similarity']})", {"precedent": match, "signature": signature})
        store = EvidenceStore(compact_summary(bundle), hits)
        if self.capture is not None:
            self.capture["store"] = store

        def call(role: str, prompt: str, rm: RoleModel) -> AgentCall:
            return self._call(iid, role, prompt, GROUP_SCHEMA, GroupMessage, hits, model_override=rm.model,
                              effort_override=rm.effort, role_model=rm)

        def on_event(kind: str, ev: dict) -> None:
            with db.session() as s:
                audit(s, "workflow", f"wf_{kind}", iid, **redact_obj(ev))
            broker.publish(iid, "updated", "analyzing")
            ek = {"group_started": "group_composed", "escalated": "escalated"}.get(kind)
            if ek:
                runlog.emit(iid, ek, f"{kind}: " + ", ".join(f"{k}={v}" for k, v in list(ev.items())[:4])[:300],
                            {k: v for k, v in ev.items() if isinstance(v, (str, int, float, bool, list))})

        ctl = GroupController(decision.level, self.cfg, call, on_event,
                              primary_model=(decision.policy or {}).get("selected_model"))
        from .investigation import EvidenceInquiry
        inquiry = EvidenceInquiry(self, iid, bundle, incident_view, store, ctl)
        if self.capture is None:
            ctl.inquire = inquiry
        try:
            res = run_group(decision.level, ctl, store, incident_view, decision.features)
        except StopGroup as e:  # configuration problems before the first node
            res = {"status": "stopped", "reason": f"{e.kind}: {e.detail}", "root_cause_candidates": [],
                   "recommended_steps": [], "notes": [f"{decision.level}: workflow could not start"],
                   "budget": ctl.budget()}
        self._save_remediation(iid, bundle, res, verified=res.get("status") == "verified")
        handoff = {"summary": res.get("summary"), "summary_ko": res.get("summary_ko"),
                   "root_cause_candidates": res.get("root_cause_candidates", [])[:3],
                   "recommended_steps": res.get("recommended_steps", []), "notes": res.get("notes", []),
                   "group": res.get("group"), "workflow_status": res.get("status"),
                   "counter_evidence": res.get("counter_evidence", []), "verification": res.get("verification"),
                   "budget": res.get("budget"), "confidence": res.get("confidence"),
                   "investigations": inquiry.attempts, "signature": signature,
                   "retrieval": getattr(self, "_retrieval_key", None),
                   **({"precedent": match} if match else {}), **({"precedent_checks": checks} if checks else {})}
        with db.session() as s:
            s.get(Incident, iid).summary = res.get("summary")
        proposal = res.get("action") or {}
        if proposal.get("propose") and incident_view.get("workload"):
            if self._guard_new_change(iid, bundle, incident_view, decision.level, handoff):
                return
            if self._propose_and_run(iid, decision.level, proposal, res.get("summary")):
                return
            handoff["notes"] = [*handoff.get("notes", []), "proposed action was not executable"]
        else:
            recovered, fresh = self._observe_recovery(iid, bundle, incident_view, phase='group_no_action')
            if recovered:
                return
            latest = R.decide(fresh, self.cfg.recent_rollout_minutes, cfg=self.cfg, signal_reasons=incident_view['signals'])
            if latest.level == 'HIGH_RISK' and latest.decided_by != 'router_failure':
                from .handoff_classes import record
                self._record_decision(iid, latest)
                handoff['handoff_classification'] = record('rules_high_risk', {}, authorization={'level': latest.level})
        if proposal.get('propose'):
            from .handoff_classes import record
            handoff['handoff_classification'] = record('operator_authority_required', {}, authorization={'level': decision.level})
        elif (' is not allowlisted' in str(proposal.get('why') or '')
              or str(proposal.get('why') or '').startswith('action targets ')):
            from .handoff_classes import record
            handoff['handoff_classification'] = record('proposal_authority_boundary', {},
                authorization={'level': decision.level, 'reason': proposal['why']})
        self._set_status(iid, "handed_off",
                         status_reason=f"{decision.level} group {res.get('status')}: {res.get('reason')}"[:500],
                         handoff=redact_obj(handoff))

    # ----------------------------------------------------------------- LLM
    def _classify(self, iid: int, prompt: str) -> dict:
        r = self._call(iid, "router", prompt, ROUTER_SCHEMA, RouterOutput, hits=[])
        if not r.ok:
            raise R.ClassifierRecoveryExhausted(r.error or "router failed")
        return r.output or {}

    def _agent_runner(self, iid: int, hits: list[dict], selected_model: str):
        def run(role: str, prompt: str) -> AgentCall:
            # Route the primary analysis/curation role; independent verifiers keep their
            # separately configured safety models and effort settings.
            model = selected_model if role in ("signal_analyst", "evidence_curator") else None
            return self._call(iid, role, prompt, AGENT_SCHEMA, AgentOutput, hits, model_override=model)
        return run

    def _call(self, iid: int, role: str, prompt: str, schema: dict, model_cls, hits: list[dict],
              model_override: str | None = None, effort_override: str | None = None,
              role_model: RoleModel | None = None) -> AgentCall:
        rm = role_model or self.cfg.llm.roles.get(role)
        if rm is None:
            return AgentCall(False, None, None, f"no model mapping for role {role}", error_kind="config")
        model = model_override or rm.model
        effort = effort_override if role_model is not None else rm.effort
        from uuid import uuid4
        from . import dispatch as D
        # C3: one call identity per logical call, created before chat is consumed so `operator_applied` names it.
        call_id = uuid4().hex
        delivered: list[int] = []
        instructions_at = None
        # node boundary: honour pause/stop, then apply pending operator chat (P15 steering). The router never
        # consumes chat: a reroute request waits for the first agent after routing.
        runlog.gate(iid, f"에이전트 {role}")
        if role != "router":
            from . import steering
            st = steering.take(iid, role, getattr(self, "_current_level", None), call_id=call_id)
            if st.reroute is not None:
                raise st.reroute
            if st.texts:
                # Only instruction messages enter the prompt; escalate requests change the model, not the prompt.
                instructions_at = len(prompt)
                delivered = list(st.message_ids)
            prompt = runlog.with_operator_instructions(prompt, st.texts)
            if st.escalate:
                up = steering.escalate_model(self.cfg, model, effort)
                if up is None:
                    runlog.emit(iid, "steering_interpreted", f"{role}은(는) 이미 가장 높은 모델·추론 강도라 올릴 수 없습니다.",
                                {"intent": "escalate", "refused": True, "role": role})
                else:
                    runlog.emit(iid, "escalated", f"운영자 요청으로 {role}을(를) {up[0]}·{up[1]}로 올립니다.",
                                {"role": role, "by": "operator", "from_model": model, "from_effort": effort,
                                 "to_model": up[0], "to_effort": up[1]})
                    model, effort = up
        # dispatch/v1: public facts of THIS call, computed from the final prompt; the prompt itself is not stored.
        runlog.emit(iid, "agent_started", f"{role} 시작", {"role": role, "model": model, "effort": effort, "call_id": call_id,
                                                         "investigation_seq": getattr(self, "_investigation_seq", None),
                                                         "dispatch": D.build(role, prompt, hits, delivered,
                                                                             instructions_at=instructions_at)})
        from .runtime_resume import fingerprint, source_fingerprint
        from .analysis_context import rag_usage
        input_sha = fingerprint({'role': role, 'prompt': prompt, 'schema': schema, 'model': model, 'effort': effort,
                                 'code': source_fingerprint(), 'settings': self.cfg.model_dump(mode='json')})
        if self._workflow_run_id is not None:
            with db.session() as s:
                workflow = s.get(WorkflowRun, self._workflow_run_id)
                cached = ((workflow.snapshot or {}).get('read_calls') or {}).get(input_sha) if workflow else None
                if cached:
                    model_cls.model_validate(cached['output'])
                    runlog.emit(iid, "agent_finished", "저장된 동일 입력의 분석 결과를 재사용했습니다.",
                                {"role": role, "call_id": call_id, "model": model, "status": "succeeded",
                                 "agent_run_id": cached['agent_run_id'], "cached": True})
                    return AgentCall(True, cached['output'], cached['agent_run_id'], model=model, effort=effort)
        attempts, err, result = 0, None, None
        provider_attempts = 0
        err_kind = None
        attempt_usages = []
        dur = 0
        for attempt in (1, 2):  # invalid output -> one retry
            attempts = attempt
            try:
                from .runtime_budget import call_trace
                from .llm.progress import observe
                def progress(summary, detail):
                    runlog.emit(iid, "agent_progress", summary,
                                {**detail, "call_id": call_id, "validation_round": attempt})
                with call_trace(input_sha, attempt), observe(progress):
                    result = self._complete_waiting_for_quota(iid, role, prompt, schema, model, effort)
                provider_attempts += result.provider_attempts if result.provider_attempts is not None else 1
                attempt_usages.append(result.usage)
                dur += result.duration_ms
                model_cls.model_validate(result.data)
                err = err_kind = None
                break
            except LLMError as e:
                actual = e.provider_attempts if e.provider_attempts is not None else 1
                provider_attempts += actual
                if actual:
                    attempt_usages.append(e.usage)
                dur += e.duration_ms or 0
                err, err_kind = f"{e.kind}: {e.message}", e.kind
                result = None
                if e.kind != "invalid_output":
                    break
            except ValidationError as e:
                err, err_kind = f"invalid_output: schema validation failed ({e.error_count()} errors)", "invalid_output"
                result = None
        from .llm.base import Usage
        # A known retry cannot fill a gap in an earlier attempt. Keep each counter
        # independent; zero is observed usage, not a missing value.
        in_tok = (sum(u.input_tokens for u in attempt_usages)
                  if all(u.input_tokens is not None for u in attempt_usages) else None)
        out_tok = (sum(u.output_tokens for u in attempt_usages)
                   if all(u.output_tokens is not None for u in attempt_usages) else None)
        cost = estimate_cost(self.cfg, model, Usage(input_tokens=in_tok, output_tokens=out_tok))
        with db.session() as s:
            run = AgentRun(incident_id=iid, role=role, provider=self.provider.name, model=model,
                           reasoning_effort=effort, input_tokens=in_tok, output_tokens=out_tok,
                           estimated_cost_usd=cost, duration_ms=dur, status="succeeded" if err is None else "failed",
                           attempts=provider_attempts, output=redact_obj(result.data) if result else None,
                           error=redact(err)[:800] if err else None)
            s.add(run)
            s.flush()
            rag_tracking = rag_usage(hits, prompt, result.data if result else {})
            for h in hits:
                if h['ref'] not in rag_tracking['cited']:
                    continue
                s.add(Citation(agent_run_id=run.id, chunk_id=h["chunk_id"], score=h["score"]))
            # Existing JSON audit payload carries observed subtotals without changing
            # AgentRun's nullable complete-total columns or model output schema.
            audit(s, role, "agent_run", iid, agent_run_id=run.id, status=run.status, model=model,
                  input_sha256=input_sha, rag_usage=rag_tracking, role_calls=1,
                  provider_attempts=provider_attempts, validation_rounds=attempts,
                  usage_summary={
                      "known_input_tokens": sum(u.input_tokens for u in attempt_usages if u.input_tokens is not None),
                      "known_output_tokens": sum(u.output_tokens for u in attempt_usages if u.output_tokens is not None),
                      "missing_usage_attempts": sum(u.input_tokens is None or u.output_tokens is None
                                                    for u in attempt_usages)})
            rid = run.id
            if result is not None and self._workflow_run_id is not None:
                workflow = s.scalar(select(WorkflowRun).where(WorkflowRun.id == self._workflow_run_id).with_for_update())
                if workflow:
                    workflow.snapshot = {**(workflow.snapshot or {}), 'read_calls': {
                        **((workflow.snapshot or {}).get('read_calls') or {}), input_sha: {
                            'output': redact_obj(result.data), 'agent_run_id': rid}}}
        broker.publish(iid, "agent_run")
        _out = result.data if result else {}
        runlog.emit(iid, "agent_finished",
                    (str(_out.get("summary") or "")[:400] if err is None else f"{role} 실패: {err}"[:400]) or f"{role} 완료",
                    {"role": role, "call_id": call_id, "model": model, "effort": effort, "status": "succeeded" if err is None else "failed",
                     "agent_run_id": rid, "input_tokens": in_tok, "output_tokens": out_tok, "duration_ms": dur,
                     "confidence": _out.get("confidence"),
                     "root_cause_candidates": [{"cause": c.get("cause"), "confidence": c.get("confidence"),
                                                "evidence_refs": c.get("evidence_refs")}
                                               for c in (_out.get("root_cause_candidates") or [])[:3]]})
        usage = dict(cost_usd=cost, model=model, effort=effort, input_tokens=in_tok, output_tokens=out_tok)
        self._request_readings(iid)
        if err is None:
            return AgentCall(True, result.data, rid, **usage)
        return AgentCall(False, None, rid, err, error_kind=err_kind, **usage)

    def _complete_waiting_for_quota(self, iid: int, role: str, prompt: str, schema: dict, model: str, effort: str | None):
        """Call the provider; while every Codex account is at its usage limit, save state, wait and retry.

        Never switches to another provider (no API key, no Claude). The wait honours pause/stop, so the operator can
        still hand the incident off. After `quota_wait_max_s` the original quota error is raised and the incident is
        handed off with the analysis so far."""
        waited, announced = 0.0, False
        from .runtime_budget import complete, aggregate_usage
        runtime = getattr(self, '_incident_runtime', None)
        prior_usage, prior_duration, actual_attempts = [], 0, 0
        capture = getattr(self.provider, "capture_events", None)
        while True:
            try:
                with capture() if capture else nullcontext([]) as events:
                    try:
                        res = complete(runtime, self.provider, role, prompt, schema, model, effort, self.cfg.llm.timeout_s)
                    finally:
                        if events:
                            self._relay_pool_events(iid, events)
                actual_attempts += res.provider_attempts or 0
                res.provider_attempts = actual_attempts
                res.usage = aggregate_usage([*prior_usage, res.usage]) if prior_usage else res.usage
                res.duration_ms += prior_duration
                if announced:
                    runlog.emit(iid, "resumed", "Codex 사용 한도가 풀려 분석을 이어갑니다.", {"after_s": round(waited)})
                return res
            except LLMError as e:
                actual_attempts += e.provider_attempts or 0
                if e.provider_attempts:
                    prior_usage.append(e.usage)
                prior_duration += e.duration_ms or 0
                e.usage, e.duration_ms, e.provider_attempts = aggregate_usage(prior_usage), prior_duration, actual_attempts
                if e.kind != "quota":
                    raise
                limit = self.settings.quota_wait_max_s
                if waited >= limit:
                    raise
                step = float(max(1, min(self.settings.quota_recheck_s, getattr(self.provider, "next_available_in", lambda: 60)() or 60)))
                if not announced:
                    runlog.emit(iid, "waiting_quota", "두 Codex 계정 모두 사용 한도입니다. 상태를 저장하고 대기합니다(API·Claude로 전환하지 않음).",
                                {"retry_in_s": round(step), "max_wait_s": limit,
                                 "pool": getattr(self.provider, "status", lambda: {})()})
                    announced = True
                runlog.gate(iid, f"에이전트 {role} 재시도")
                # 긴 quota backoff도 중단 확인 기한보다 짧게 나눈다. 중단 후에는
                # 공급자를 다시 호출하지 않고 다음 경계로 종료 신호를 전달한다.
                remaining = min(step, max(0, limit - waited))
                while remaining > 0:
                    chunk = min(1.0, remaining)
                    if runtime:
                        try:
                            runtime.wait(chunk)
                        except LLMError as stop:
                            stop.usage = aggregate_usage(prior_usage)
                            stop.duration_ms, stop.provider_attempts = prior_duration, actual_attempts
                            raise
                    self._sleep(chunk)
                    waited += chunk
                    remaining -= chunk
                    runlog.gate(iid, f"에이전트 {role} 재시도")

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop.is_set():
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def _relay_pool_events(self, iid: int, events: list[tuple[str, dict]]) -> None:
        """Publish the account events captured by this specific incident call."""
        for kind, detail in events:
            if kind == "account_switched":
                runlog.emit(iid, "account_switched", f"Codex 계정을 {detail.get('from')} → {detail.get('to')}로 전환했습니다.", detail)
            elif kind == "account_limited":
                runlog.emit(iid, "note", f"Codex 계정 {detail.get('account')} 사용 한도 도달 (약 {detail.get('blocked_for_s')}초)", detail)
            elif kind == "account_auth_failed":
                runlog.emit(iid, "note", f"Codex 계정 {detail.get('account')} 인증 실패 — 재로그인이 필요합니다.", detail)

    # ----------------------------------------------------------------- RAG
    def _rag_query(self, b: Bundle, d: R.Decision) -> str:
        # Query construction is a node of the retrieval configuration that rag_tuning evaluates and applies.
        from .rag_query import build
        from .rag_tuning import active_config
        return build(active_config().query_mode, b.workload, d.features, b.findings, b.dependencies)

    def _rag_kinds_allowed(self) -> list[str]:
        from .rag_tuning import production_kinds
        return list(self.rag_kinds) if self.rag_kinds else production_kinds(self.cfg)

    @staticmethod
    def _memory_reference(s, doc: str, n: int, *, inherited: bool = False) -> dict | None:
        """A resolution memory document as one citable reference: all active chunks in order (steps included)."""
        from .models import RagChunk
        chunks = list(s.scalars(select(RagChunk).where(RagChunk.doc == doc, RagChunk.retired_at.is_(None))
                                .order_by(RagChunk.chunk_ix)))
        if not chunks:
            return None
        first = chunks[0]
        ref = {"ref": f"R{n}", "chunk_id": first.id, "doc": first.doc, "source": first.source, "chunk_ix": first.chunk_ix,
               "score": 0.0, "hybrid_score": 0.0, "source_uri": None, "locator": first.locator, "section": first.section,
               "license": "internal", "attribution": None, "content": "\n".join(c.content for c in chunks),
               "channel": "memory"}
        if inherited:
            ref["inherited"] = True
        return ref

    def _memory_precedent(self, signature: list[str], hits: list[dict]) -> None:
        """Memory channel: append the best signature-matching resolution memory to `hits` as a citable reference."""
        from .models import RagChunk, RagDocument
        from .resolution_memory import MEMORY_KINDS, best_memory
        kinds = [k for k in self._rag_kinds_allowed() if k in MEMORY_KINDS]
        if not kinds or not [t for t in signature if not t.startswith("signal:")]:
            return
        try:
            with db.session() as s:
                docs = [{"doc": d.doc_key, "kind": d.kind, "source": d.source,
                         "signature": (d.meta or {}).get("signature") or []}
                        for d in s.scalars(select(RagDocument).where(RagDocument.kind.in_(kinds)))]
                best = best_memory(docs, signature)
                if best is None or any(h.get("doc") == best["doc"] for h in hits):
                    return
                ref = self._memory_reference(s, best["doc"], len(hits) + 1)
                if ref is not None:
                    hits.append(ref)
        except Exception as e:  # noqa: BLE001
            log.warning("memory precedent lookup failed: %s", type(e).__name__)

    def _inherit_precedent(self, iid: int, incident_view: dict, signature: list[str], hits: list[dict]) -> dict | None:
        """A precedent used by a recent incident on the same workload, appended to `hits` as a citable reference."""
        from .resolution_memory import CONTINUITY_MINUTES, inherited_precedent
        from .models import RagChunk, RagDocument
        try:
            created = datetime.fromisoformat(str(incident_view.get("created_at")))
        except ValueError:
            return None
        previous = []
        with db.session() as s:
            for prev in s.scalars(select(Incident).where(
                    Incident.namespace == incident_view.get("namespace"), Incident.workload == incident_view.get("workload"),
                    Incident.id < iid, Incident.created_at >= created - timedelta(minutes=CONTINUITY_MINUTES))
                    .order_by(Incident.id.desc()).limit(5)):
                p = (prev.handoff or {}).get("precedent") or {}
                if p.get("doc"):
                    kind = s.scalar(select(RagDocument.kind).where(RagDocument.doc_key == p["doc"]))
                    previous.append({"incident_id": prev.id, "precedent": p, "kind": kind})
            match = inherited_precedent(previous, signature, self._rag_kinds_allowed())
            if match is None:
                return None
            if not any(h.get("doc") == match["doc"] for h in hits):
                ref = self._memory_reference(s, match["doc"], len(hits) + 1, inherited=True)
                if ref is None:
                    return None
                hits.append(ref)
        ref = next(h["ref"] for h in hits if h.get("doc") == match["doc"])
        return {**match, "ref": ref}

    def _rag(self, query: str) -> list[dict]:
        from copy import deepcopy
        from .runtime_resume import fingerprint
        from .rag_tuning import active_config
        retrieval = active_config()
        self._retrieval_key = retrieval.key()
        key = fingerprint({'query': query, 'kinds': self.rag_kinds, 'references': self.cfg.use_reference_guide,
                           'retrieval': retrieval.key()})
        if self._workflow_run_id is not None:
            with db.session() as s:
                row = s.get(WorkflowRun, self._workflow_run_id)
                cached = ((row.snapshot or {}).get('rag_searches') or {}) if row else {}
                if key in cached:
                    return deepcopy(cached[key])
        if self.rag is None:
            return []
        try:
            with db.session() as s:
                # Document channel only (never None: an unrestricted search would also see evaluation memory).
                # Resolution memory enters through the signature-matched memory channel (_memory_precedent).
                from .resolution_memory import MEMORY_KINDS
                hits = self.rag.search(s, query, kinds=[k for k in self._rag_kinds_allowed() if k not in MEMORY_KINDS],
                                       **retrieval.search_kwargs())
        except Exception as e:  # noqa: BLE001
            log.warning("RAG search failed: %s", type(e).__name__)
            return []
        result = [{"ref": f"R{i + 1}", "chunk_id": h.chunk_id, "doc": h.doc, "source": h.source,
                   "chunk_ix": h.chunk_ix, "score": h.score, "hybrid_score": h.hybrid_score,
                   "source_uri": h.source_uri, "locator": h.locator, "section": h.section,
                   "license": h.license, "attribution": h.attribution,
                   "content": h.content} for i, h in enumerate(hits)]
        if self._workflow_run_id is not None:
            with db.session() as s:
                row = s.scalar(select(WorkflowRun).where(WorkflowRun.id == self._workflow_run_id).with_for_update())
                if row is None:
                    raise RuntimeError('검색 결과를 저장할 실행 원장 없음')
                searches = dict((row.snapshot or {}).get('rag_searches') or {})
                searches.setdefault(key, redact_obj(result))
                row.snapshot = {**(row.snapshot or {}), 'rag_searches': searches}
                return deepcopy(searches[key])
        return result
