"""Incident engine: signal intake, settle window, evidence, routing, workflows, safety, persistence."""
from __future__ import annotations

import logging
import queue
import threading
import time
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
from .incidents import Signal, ingest
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


class Engine:
    def __init__(self, cfg: AppConfig, settings: Settings, kube: Any, provider: Any, rag: RAG | None):
        self.cfg, self.settings, self.kube, self.provider, self.rag = cfg, settings, kube, provider, rag
        self.q: "queue.Queue[int]" = queue.Queue()
        self._scheduled: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._cleanup_pending: int | None = None
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
        if outcome == "created" and iid:
            broker.publish(iid, "created", "open")
            self.schedule(iid, delay=self.cfg.settle_seconds)
        elif outcome == "merged" and iid:
            broker.publish(iid, "updated")

    def schedule(self, incident_id: int, delay: float = 0) -> None:
        with self._lock:
            if incident_id in self._scheduled:
                return
            self._scheduled.add(incident_id)
        if delay > 0:
            t = threading.Timer(delay, self.q.put, args=(incident_id,))
            t.daemon = True
            t.start()
        else:
            self.q.put(incident_id)

    def start(self) -> None:
        with self._lock:
            # 한 Engine의 시작은 한 번뿐이다. 죽은 작업자를 조용히 재시작하지 않는다.
            if self._worker_thread is not None or self._stop.is_set():
                return
            self._worker_thread = threading.Thread(target=self._worker, name="incident-worker", daemon=True)
            self._worker_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def worker_health(self) -> dict[str, bool]:
        with self._lock:
            alive = self._worker_thread is not None and self._worker_thread.is_alive()
            pending = self._cleanup_pending is not None
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
        resume_ids: list[int] = []
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
                    resume_ids.append(inc.id)
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
            open_ids = list(s.scalars(select(Incident.id).where(Incident.status == "open")))
        for iid in dict.fromkeys(failed_ids):
            broker.publish(iid, "failed", "failed")
        # Interrupted actions/runs whose incident was not itself failed above (e.g. action_pending).
        for iid in dict.fromkeys(action_ids):
            if iid not in failed_ids:
                broker.publish(iid, "action")
        for iid in dict.fromkeys(run_ids):
            if iid not in failed_ids and iid not in action_ids:
                broker.publish(iid, "updated")
        for iid in dict.fromkeys([*open_ids, *resume_ids]):
            self.schedule(iid, delay=0 if iid in resume_ids else self.cfg.settle_seconds)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                iid = self.q.get(timeout=1)
            except queue.Empty:
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
                with self._lock:
                    if self._cleanup_pending != iid:
                        self._scheduled.discard(iid)

    def _finish_worker(self, iid: int, error: Exception, *, worker_run_id: int | None = None) -> None:
        # 처리 자체는 재실행하지 않는다. 종료 기록만 새 트랜잭션에서 재확인한다.
        with self._lock:
            self._cleanup_pending = iid
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
                with self._lock:
                    self._cleanup_pending = None
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
            if arm is None: auto_create_for_incident(runner, iid)
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

    def _observe_recovery(self, iid: int, first: Bundle, incident_view: dict, *, phase: str) -> tuple[bool, Bundle]:
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
                if (inc.status != 'analyzing' or list(inc.signal_reasons) != incident_view['signals']
                        or s.scalar(select(Action.id).where(Action.incident_id == iid).limit(1)) is not None
                        or s.scalar(select(OperatorCommand.id).where(OperatorCommand.incident_id == iid,
                            OperatorCommand.status.in_(('planning', 'awaiting_confirmation', 'awaiting_secret', 'executing', 'rolling_back'))).limit(1)) is not None
                        or s.scalar(select(OperatorMessage.id).where(OperatorMessage.incident_id == iid,
                            OperatorMessage.applied_at.is_(None)).limit(1)) is not None
                        or (s.get(RunControl, iid) and s.get(RunControl, iid).state != 'running')):
                    return False, fresh
                transition(inc, 'resolved')
                inc.status_reason = '정상 상태 확인 · 원인 미확정 · AI 조치 없음'
                retained = {k: v for k, v in (inc.handoff or {}).items() if k != 'handoff_classification'}
                inc.handoff = {**retained, 'observed_recovery': proof, 'summary': inc.status_reason,
                               'summary_ko': inc.status_reason}
                update_run(s, iid, 'succeeded', 'resolved', inc.status_reason)
                audit(s, 'liveprobe', 'observed_recovery', iid, **proof)
            broker.publish(iid, 'resolved', 'resolved')
            runlog.emit(iid, 'completed', '정상 상태 확인 · 원인 미확정 · AI 조치 없음', {'outcome': 'resolved', **proof})
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
            if self._workflow_run_id is not None:
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
        self._save_remediation(iid, bundle, final, verified=out.get('outcome') == 'propose_action'
                               or (out.get('handoff') or {}).get('workflow_status') == 'verified')

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
        if self._propose_and_run(iid, decision.level, out["action"], summary):
            return
        self._set_status(iid, "handed_off", status_reason="action was not executable; operator handoff",
                         handoff={"summary": summary, "notes": ["action was not executable; see audit"]})

    def _save_remediation(self, iid: int, bundle: Bundle, output=None, *, verified=False):
        from .remediation import select_remediation
        remediation = select_remediation(bundle, output, verified=verified)
        with db.session() as s:
            inc = s.get(Incident, iid)
            inc.handoff = {**(inc.handoff or {}), "remediation": remediation}
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

    def _propose_and_run(self, iid: int, level: str, action: dict, summary: str | None) -> bool:
        """One agent-proposed Kubernetes action through the validated executor. True when the incident was finished
        or moved to approval; False when the proposal was denied (caller hands off)."""
        from . import actions as typed
        if self._network_handoff(iid):
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
        runlog.emit(iid, "action_proposed", f"조치 #{aid} 실행 시작", {"action_id": aid, "actor": actor, "level": level})
        self._set_status(iid, "acting")
        res = safety.execute(db.session, self.cfg, self._kube_for(aid), aid, actor, level=level)
        runlog.emit(iid, "action_executed", f"조치 #{aid} 결과: {res.status}", {"action_id": aid, "status": res.status,
                                                                          "reason": redact(str(res.reason or ""))[:300]})
        runlog.emit(iid, "verification_result", "롤아웃 검증 " + ("통과" if res.status == "succeeded" else "미통과"),
                    {"action_id": aid, "ok": res.status == "succeeded"})
        if res.status == "succeeded":
            with db.session() as s:
                atype = (s.get(Action, aid).type if s.get(Action, aid) else None) or "action"
            self._set_status(iid, "resolved", status_reason=f"{atype} succeeded and verified")
        else:
            self._set_status(iid, "handed_off", status_reason=f"action {res.status}: {res.reason}",
                             handoff={"notes": [f"automatic action {res.status}: {res.reason}"]})
        return res

    # ------------------------------------------------------ NORMAL / COMPLEX (KAN-53)
    def _run_group(self, iid: int, decision: R.Decision, bundle: Bundle, incident_view: dict) -> None:
        """Run the NORMAL/COMPLEX group through the existing safety executor or bounded read-only follow-up."""
        hits = self._rag(self._rag_query(bundle, decision))
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
                   "budget": res.get("budget"), "confidence": res.get("confidence")}
        with db.session() as s:
            s.get(Incident, iid).summary = res.get("summary")
        proposal = res.get("action") or {}
        if proposal.get("propose") and incident_view.get("workload"):
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
        # node boundary: honour pause/stop, then apply pending operator chat (P15 steering). The router never
        # consumes chat: a reroute request waits for the first agent after routing.
        runlog.gate(iid, f"에이전트 {role}")
        if role != "router":
            from . import steering
            st = steering.take(iid, role, getattr(self, "_current_level", None))
            if st.reroute is not None:
                raise st.reroute
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
        from uuid import uuid4
        call_id = uuid4().hex
        runlog.emit(iid, "agent_started", f"{role} 시작", {"role": role, "model": model, "effort": effort, "call_id": call_id})
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
        if self.interpret_worker is not None:
            try:
                self.interpret_worker.request(iid)
            except Exception:  # noqa: BLE001 - display helper never affects the analysis
                pass
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
        while True:
            try:
                res = complete(runtime, self.provider, role, prompt, schema, model, effort, self.cfg.llm.timeout_s)
                actual_attempts += res.provider_attempts or 0
                res.provider_attempts = actual_attempts
                res.usage = aggregate_usage([*prior_usage, res.usage]) if prior_usage else res.usage
                res.duration_ms += prior_duration
                if announced:
                    runlog.emit(iid, "resumed", "Codex 사용 한도가 풀려 분석을 이어갑니다.", {"after_s": round(waited)})
                self._relay_pool_events(iid)
                return res
            except LLMError as e:
                self._relay_pool_events(iid)
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

    def _relay_pool_events(self, iid: int) -> None:
        """Turn account switches reported by the pool into feed events (once each)."""
        pool = self.provider
        if not hasattr(pool, "drain_events"):
            return
        for kind, detail in pool.drain_events():
            if kind == "account_switched":
                runlog.emit(iid, "account_switched", f"Codex 계정을 {detail.get('from')} → {detail.get('to')}로 전환했습니다.", detail)
            elif kind == "account_limited":
                runlog.emit(iid, "note", f"Codex 계정 {detail.get('account')} 사용 한도 도달 (약 {detail.get('blocked_for_s')}초)", detail)
            elif kind == "account_auth_failed":
                runlog.emit(iid, "note", f"Codex 계정 {detail.get('account')} 인증 실패 — 재로그인이 필요합니다.", detail)

    # ----------------------------------------------------------------- RAG
    def _rag_query(self, b: Bundle, d: R.Decision) -> str:
        f = d.features
        parts = [f"{b.workload} incident"]
        if f.get("waiting_reasons"):
            parts.append("pods " + " ".join(f["waiting_reasons"]) + " image pull failure after rollout")
        if f.get("service_selector_matches_zero_pods") or f.get("service_has_no_endpoints"):
            parts.append("service has no endpoints selector mismatch")
        if f.get("dependent_callers_failing"):
            parts.append("callers return 5xx " + " ".join(f["dependent_callers_failing"]))
        if f.get("oom_killed"):
            parts.append("container OOMKilled exit code 137 memory limit")
        if f.get("latency_signal"):
            parts.append("high latency connection pool exhaustion slow responses")
        return "; ".join(parts)

    def _rag(self, query: str) -> list[dict]:
        from copy import deepcopy
        from .runtime_resume import fingerprint
        key = fingerprint({'query': query, 'kinds': self.rag_kinds, 'references': self.cfg.use_reference_guide})
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
                kinds = self.rag_kinds or (None if self.cfg.use_reference_guide else
                                           ["runbook", "public_doc", "past_incident"])
                hits = self.rag.search(s, query, k=3, **({"kinds": kinds} if kinds else {}))
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
