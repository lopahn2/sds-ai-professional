"""P18 사건 작업자 복구 계약: 외부 자원 없는 독립 unittest.

실행: liveprobe/.venv/bin/python scripts/test_p18_incident_worker_recovery.py -v
실제 작업자·상태 전이·감사 함수를 사용하고 저장소와 전송 경계만 대역으로 바꾼다.
"""
from __future__ import annotations

import copy
import io
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "liveprobe"))


def _offline_guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system",
                 "os.posix_spawn", "sqlite3.connect"}:
        raise AssertionError("오프라인 시험에서 외부 실행·연결은 금지됩니다.")
    if event == "open" and isinstance(args[0], (str, bytes)):
        name = str(args[0])
        if any(token in name for token in ("conftest.py", "/.kube/", "/.codex/", "/.claude/", "/var/run/secrets/")):
            raise AssertionError("금지된 파일 접근입니다.")


sys.addaudithook(_offline_guard)
from app import db, engine as engine_module, runlog  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.config import AppConfig  # noqa: E402
from app.models import AuditLog, Incident, IncidentEvent, WorkflowRun  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402


class MemoryStore:
    """트랜잭션별 복사·롤백을 갖는 메모리 저장소. 실제 DB는 초기화하지 않는다."""
    def __init__(self):
        self.incidents = {11: SimpleNamespace(id=11, status="analyzing", status_reason=None, closed_at=None,
                                              handoff={}),
                          12: SimpleNamespace(id=12, status="open", status_reason=None, closed_at=None,
                                              handoff={})}
        self.runs = {71: SimpleNamespace(id=71, incident_id=11, status="running", phase="analysis", reason=None)}
        self.audits = []
        self.events = []
        self.fail_reads = 0
        self.fail_commits = 0
        self.commit_then_raise = 0
        self.reads = 0
        self.commits = 0
        self.fail_seen = threading.Event()
        self.trace = []
        self.on_snapshot = lambda: None
        self.commands = []
        self.locked_reads = []

    @contextmanager
    def session(self):
        store = self
        incidents, runs = copy.deepcopy(self.incidents), copy.deepcopy(self.runs)
        added = []

        class Session:
            def get(self, model, iid):
                if model.__name__ == "Setting":
                    store.on_snapshot()
                    return None
                if model.__name__ == "RunControl":
                    return SimpleNamespace(state="stopping")
                if model is Incident:
                    store.reads += 1
                    if store.fail_reads:
                        store.fail_reads -= 1
                        store.fail_seen.set()
                        raise OperationalError(None, None, RuntimeError("오프라인 DB 장애"))
                    return incidents.get(iid)
                if model is WorkflowRun:
                    return runs.get(iid)
                return None

            def scalar(self, stmt):
                model = stmt.column_descriptions[0].get("entity")
                params = stmt.compile().params
                iid = params.get("id_1", params.get("incident_id_1", 11))
                if model is Incident:
                    store.locked_reads.append(stmt._for_update_arg is not None)
                    return self.get(Incident, iid)
                if model is WorkflowRun or getattr(model, "__name__", "") == "OperatorCommand":
                    rows = list(runs.values()) if model is WorkflowRun else store.commands
                    rows = [r for r in rows if r.incident_id == params.get("incident_id_1", r.incident_id)]
                    if "id_1" in params:
                        rows = [r for r in rows if r.id == params["id_1"]]
                    if "status_1" in params:
                        statuses = params["status_1"]
                        rows = [r for r in rows if r.status in (statuses if isinstance(statuses, list) else [statuses])]
                    row = rows[-1] if rows else None
                    selected = stmt.column_descriptions[0]["expr"]
                    return row if selected is model else row.id if row is not None else None
                return None

            def scalars(self, stmt):
                model = stmt.column_descriptions[0].get("entity")
                if getattr(model, "__name__", "") == "OperatorCommand":
                    return [r for r in store.commands if r.status == "failed"]
                return []

            def add(self, row):
                added.append(row)

            def flush(self):
                pass

        yield Session()
        if self.fail_commits:
            self.fail_commits -= 1
            self.fail_seen.set()
            raise OperationalError(None, None, RuntimeError("오프라인 commit 장애"))
        self.incidents, self.runs = incidents, runs
        self.audits.extend(row for row in added if isinstance(row, AuditLog))
        self.events.extend(row for row in added if isinstance(row, IncidentEvent))
        self.commits += 1
        self.trace.append("commit")
        if self.commit_then_raise:
            self.commit_then_raise -= 1
            raise OperationalError(None, None, RuntimeError("오프라인 확정 응답 유실"))


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.engine = Engine(AppConfig(), SimpleNamespace(), None, None, None)
        self.calls = []
        self.published = []
        self.thread_errors = []
        self.threads = []
        self.next_seen = threading.Event()
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        engine_module.log.addHandler(handler)
        self.addCleanup(engine_module.log.removeHandler, handler)
        self.addCleanup(self.cleanup_worker)
        for target in ("init_db", "run_migrations", "engine"):
            self.enterContext(patch.object(db, target, side_effect=AssertionError("실제 DB 접근 금지")))
        self.enterContext(patch.object(db, "session", self.store.session))
        self.enterContext(patch.object(engine_module.broker, "publish", self.publish))
        loop = SimpleNamespace(is_closed=lambda: False, call_soon_threadsafe=lambda callback, msg: callback(msg))
        self.enterContext(patch.object(engine_module.broker, "_loop", loop))
        self.enterContext(patch.object(engine_module.broker, "_deliver",
                                       lambda msg: self.publish(msg["incident_id"], msg["event"], msg["status"])))
        self.enterContext(patch.object(threading, "excepthook", lambda args: self.thread_errors.append(args.exc_type.__name__)))

    def publish(self, iid, event="updated", status=None):
        self.published.append((iid, event, status))
        self.store.trace.append("publish")

    def cleanup_worker(self):
        self.engine.stop()
        for thread in self.threads:
            thread.join(2)
        self.assertFalse(any(t.is_alive() for t in self.threads), "시험 작업자 종료 실패")

    def launch(self, error=None, *, stop_after_next=True):
        # 실제 process()의 설정 스냅샷 읽기에서 장애를 주입한다. 처리 함수를 바꾸지 않는다.
        # 다음 사건은 이미 terminal인 대조군으로, 외부 근거·모델 경로에 진입하지 않는다.
        self.store.incidents[12].status = "closed"
        def on_snapshot():
            # 저장소 대역에서 실제 호출자의 사건 ID를 관측해 재실행도 검출한다.
            caller = sys._getframe(3)
            self.assertIs(caller.f_code, Engine.process.__code__)
            iid = caller.f_locals["iid"]
            self.calls.append(iid)
            if iid == 11 and error is not None:
                raise error
            if iid == 12:
                self.next_seen.set()
                if stop_after_next:
                    self.engine.stop()
        self.store.on_snapshot = on_snapshot
        self.engine.schedule(11)
        self.engine.schedule(12)
        created = []
        real_thread = threading.Thread
        def record_worker(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if kwargs.get("name") == "incident-worker":
                created.append(thread)
            return thread
        with patch.object(engine_module.threading, "Thread", record_worker):
            self.engine.start()
        thread = getattr(self.engine, "_worker_thread", None) or created[0]
        self.threads.append(thread)
        return thread

    def test_secondary_db_failure_keeps_worker_and_never_replays_process(self):
        self.store.fail_reads = 1
        self.launch(OperationalError(None, None, RuntimeError("오프라인 최초 처리 장애")))
        self.assertTrue(self.next_seen.wait(2), "이중 예외 뒤 작업자가 다음 사건에 도달하지 못했습니다.")
        self.assertEqual(self.thread_errors, [])
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(self.store.incidents[11].status, "failed")
        self.assertEqual(len(self.store.audits), 1)
        self.assertEqual(self.published, [(11, "failed", "failed")])

    def test_ambiguous_commit_keeps_one_atomic_failure_event_without_replay(self):
        self.store.commit_then_raise = 1
        self.launch(RuntimeError("오프라인 최초 장애"))
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(self.store.incidents[11].status, "failed")
        self.assertEqual(len(self.store.audits), 1)
        self.assertEqual(len(self.store.events), 1, "실패 확정과 실패 이벤트가 같은 트랜잭션이어야 합니다.")
        self.assertEqual(self.store.events[0].event, "failed")
        self.assertLessEqual(len(self.published), 1)

    def test_readyz_without_background_preserves_uninitialized_api_compatibility(self):
        from app import main
        for engine in (None, object()):
            with self.subTest(engine_present=engine is not None), \
                    patch.dict(main.STATE, {"ready": False, "engine": engine, "background_requested": False}), \
                    patch.object(db, "db_ok", return_value=True):
                response = main.readyz()
                self.assertEqual(response.status_code, 200)
                self.assertFalse(json.loads(response.body)["background"])

    def test_readyz_rejects_dead_worker_when_background_was_requested(self):
        from app import main
        thread = self.launch(SystemExit("오프라인 작업자 종료"))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        with patch.dict(main.STATE, {"ready": True, "engine": self.engine, "background_requested": True}), \
                patch.object(db, "db_ok", return_value=True):
            response = main.readyz()
        self.assertEqual(response.status_code, 503, "DB만 복구된 dead worker를 ready로 응답했습니다.")
        body = json.loads(response.body)
        self.assertFalse(body["background"])
        self.assertFalse(body["incident_worker"]["alive"])

    def test_pending_cleanup_blocks_next_case_and_stop_interrupts_wait(self):
        self.store.fail_reads = 100
        waiting = threading.Event()
        intervals = []
        original_wait = self.engine._stop.wait

        def observe_wait(seconds):
            intervals.append(seconds)
            waiting.set()
            return original_wait(seconds)

        self.enterContext(patch.object(self.engine._stop, "wait", observe_wait))
        thread = self.launch(RuntimeError("오프라인 최초 장애"))
        self.assertTrue(waiting.wait(1))
        health = getattr(self.engine, "worker_health", lambda: {})()
        self.assertEqual(health.get("cleanup_pending"), True, "미확정 종료 정리가 건강 상태에 드러나지 않습니다.")
        self.assertEqual(health.get("alive"), True)
        self.assertEqual(health.get("ready"), False)
        self.assertEqual(self.calls, [11])
        self.assertTrue(all(0.1 <= interval <= 5 for interval in intervals))
        self.assertEqual(len(intervals), 1)
        self.engine.stop()
        thread.join(0.3)
        self.assertFalse(thread.is_alive(), "stop이 복구 대기를 즉시 해제하지 못했습니다.")
        self.assertEqual(self.calls, [11])
        self.assertIn(11, self.engine._scheduled, "확정 전 사건을 정리 완료로 취급하면 안 됩니다.")
        self.assertTrue(self.engine.worker_health()["cleanup_pending"])

    def test_concurrent_start_creates_exactly_one_identifiable_worker(self):
        created = []
        real_thread = threading.Thread

        def thread_factory(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if kwargs.get("name") == "incident-worker":
                created.append(thread)
                self.threads.append(thread)
            return thread

        gate = threading.Barrier(8)
        def start_together():
            gate.wait(timeout=2)
            self.engine.start()

        with patch.object(engine_module.threading, "Thread", thread_factory):
            callers = [real_thread(target=start_together) for _ in range(8)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(2)
        self.assertEqual(len(created), 1, "start 중복 호출이 여러 사건 작업자를 만들었습니다.")
        self.assertIs(getattr(self.engine, "_worker_thread", None), created[0])
        self.assertTrue(created[0].is_alive())

    def test_raw_exception_and_untrusted_type_name_never_leave_worker(self):
        sentinel = "비공개원문표식_7F3A"
        error_type = type(sentinel, (RuntimeError,), {})
        self.launch(error_type(sentinel))
        self.assertTrue(self.next_seen.wait(2))
        self.assertTrue(sentinel not in self.logs.getvalue(), "작업자 로그에 원문 예외가 노출됐습니다.")
        self.assertTrue(sentinel not in self.store.incidents[11].status_reason, "실패 사유에 원문이 노출됐습니다.")
        self.assertTrue(sentinel not in str([r.detail for r in self.store.audits]), "감사에 원문이 노출됐습니다.")
        self.assertEqual(self.store.incidents[11].status_reason, "내부 처리 오류 (Exception)")

    def test_terminal_race_preserves_outcome_without_false_failure_event(self):
        self.store.fail_reads = 1
        original_wait = self.engine._stop.wait

        def terminal_wins(seconds):
            self.assertGreaterEqual(seconds, 0.1)
            self.store.incidents[11].status = "resolved"
            self.store.incidents[11].status_reason = "별도 확정 결과"
            return original_wait(0)

        self.enterContext(patch.object(self.engine._stop, "wait", terminal_wins))
        self.launch(RuntimeError("오프라인 최초 장애"))
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.store.incidents[11].status, "resolved")
        self.assertEqual(self.store.incidents[11].status_reason, "별도 확정 결과")
        self.assertEqual(self.store.audits, [])
        self.assertEqual(self.published, [], "terminal 승자의 결과에 실패 이벤트를 덧붙이면 안 됩니다.")

    def test_stop_finalizer_db_failure_retries_only_owned_cleanup(self):
        self.store.fail_reads = 1
        stopped = runlog.RunStopped("오프라인 중단 경계")
        stopped.workflow_run_id = 71
        self.launch(stopped)
        self.assertTrue(self.next_seen.wait(2), "중단 정리 DB 예외가 작업자를 종료했습니다.")
        self.assertEqual(self.thread_errors, [])
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(self.store.incidents[11].status, "handed_off")
        self.assertEqual(self.store.runs[71].status, "failed")
        self.assertEqual(self.store.runs[71].phase, "stopped")
        self.assertEqual([row.event for row in self.store.audits], ["incident_stopped"])


    def test_normal_terminal_noop_is_processed_once_without_failure_records(self):
        self.store.incidents[11].status = "closed"
        self.launch()
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(self.store.audits, [])
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.thread_errors, [])


    def test_several_db_read_failures_never_release_next_case_early(self):
        self.store.fail_reads = 3
        waits = []
        def observe_retry(seconds):
            waits.append(seconds)
            self.assertEqual(self.calls, [11])
            self.assertTrue(self.engine.worker_health()["cleanup_pending"])
            self.assertEqual(self.store.incidents[11].status, "analyzing")
            self.assertEqual(self.store.audits, [])
            self.assertEqual(self.store.events, [])
            self.assertEqual(self.published, [])
            return False
        self.enterContext(patch.object(self.engine._stop, "wait", observe_retry))
        self.launch(RuntimeError("오프라인 처리 장애"))
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(waits, [1.0, 1.0, 1.0])
        self.assertEqual(len(self.store.audits), 1)
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.published, [(11, "failed", "failed")])


    def test_commit_failures_rollback_audit_and_event_until_confirmed(self):
        self.store.fail_commits = 2
        waits = []
        def before_retry(seconds):
            waits.append(seconds)
            self.assertEqual(self.calls, [11])
            self.assertEqual(self.store.incidents[11].status, "analyzing")
            self.assertEqual(self.store.runs[71].status, "running")
            self.assertEqual(self.store.audits, [])
            self.assertEqual(self.store.events, [])
            self.assertEqual(self.published, [])
            return False
        self.enterContext(patch.object(self.engine._stop, "wait", before_retry))
        self.launch(RuntimeError("오프라인 처리 장애"))
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(waits, [1.0, 1.0])
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(len(self.store.audits), 1)
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.published, [(11, "failed", "failed")])
        self.assertLess(self.store.trace.index("commit"), self.store.trace.index("publish"))


    def test_stop_finalizer_commit_failure_keeps_owned_run_until_confirmed(self):
        self.store.fail_commits = 1
        observed = []
        def before_retry(seconds):
            observed.append(seconds)
            self.assertEqual(self.calls, [11])
            self.assertEqual(self.store.runs[71].status, "running")
            self.assertEqual(self.store.incidents[11].status, "analyzing")
            self.assertEqual(self.store.audits, [])
            self.assertEqual(self.published, [])
            return False
        self.enterContext(patch.object(self.engine._stop, "wait", before_retry))
        stopped = runlog.RunStopped("비공개중단원문표식")
        stopped.workflow_run_id = 71
        self.launch(stopped)
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(observed, [1.0])
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(self.store.runs[71].status, "failed")
        self.assertEqual(self.store.runs[71].reason, "운영자 중단")
        self.assertEqual(self.store.incidents[11].status, "handed_off")
        self.assertEqual([a.event for a in self.store.audits], ["incident_stopped"])
        self.assertTrue("비공개중단원문표식" not in self.logs.getvalue())


    def test_stop_finalizer_never_terminates_another_running_attempt(self):
        self.store.runs[72] = SimpleNamespace(id=72, incident_id=11, status="running", phase="analysis", reason=None)
        stopped = runlog.RunStopped("오프라인 중단")
        stopped.workflow_run_id = 71
        self.launch(stopped)
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.store.runs[71].status, "failed")
        self.assertEqual(self.store.runs[72].status, "running")
        self.assertEqual(self.store.incidents[11].status, "analyzing")
        self.assertEqual(self.store.audits, [])
        self.assertEqual(self.published, [])


    def test_stop_finalizer_never_turns_inflight_command_into_success(self):
        command = SimpleNamespace(id=91, incident_id=11, status="failed", result={"inflight": {"step": 1}},
                                  rollback={}, error="process_restarted_outcome_requires_inspection")
        self.store.commands = [command]
        stopped = runlog.RunStopped("오프라인 중단")
        stopped.workflow_run_id = 71
        self.launch(stopped)
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.store.runs[71].status, "failed")
        self.assertEqual(self.store.incidents[11].status, "analyzing")
        self.assertIs(self.store.commands[0], command)
        self.assertEqual(command.status, "failed")
        self.assertEqual(command.result, {"inflight": {"step": 1}})
        self.assertEqual(self.published, [])


    def test_readyz_pending_is_503_and_confirmed_recovery_is_200(self):
        from app import main
        self.store.fail_reads = 1
        waiting = threading.Event()
        original_wait = self.engine._stop.wait
        def observe_wait(seconds):
            waiting.set()
            return original_wait(seconds)
        self.enterContext(patch.object(self.engine._stop, "wait", observe_wait))
        self.launch(RuntimeError("오프라인 처리 장애"), stop_after_next=False)
        self.assertTrue(waiting.wait(1))
        with patch.dict(main.STATE, {"ready": True, "engine": self.engine, "background_requested": True}),                 patch.object(db, "db_ok", return_value=True):
            response = main.readyz()
            self.assertEqual(response.status_code, 503)
            self.assertFalse(json.loads(response.body)["background"])
            self.assertTrue(json.loads(response.body)["incident_worker"]["alive"])
            self.assertTrue(json.loads(response.body)["incident_worker"]["cleanup_pending"])
            self.assertTrue(self.next_seen.wait(2))
            response = main.readyz()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(json.loads(response.body)["background"])
            self.assertFalse(json.loads(response.body)["incident_worker"]["cleanup_pending"])


    def test_all_terminal_states_and_missing_incident_are_not_overwritten(self):
        from app.models import INCIDENT_CLOSED_STATES
        for status in INCIDENT_CLOSED_STATES:
            with self.subTest(status=status):
                self.store.incidents[11].status = status
                self.store.incidents[11].status_reason = "기존 결과"
                self.engine._record_worker_failure(11, RuntimeError("오프라인 장애"))
                self.assertEqual(self.store.incidents[11].status, status)
                self.assertEqual(self.store.incidents[11].status_reason, "기존 결과")
        del self.store.incidents[11]
        self.engine._record_worker_failure(11, RuntimeError("오프라인 장애"))
        self.assertTrue(all(self.store.locked_reads))
        self.assertEqual(self.store.audits, [])
        self.assertEqual(self.store.events, [])
        self.assertEqual(self.published, [])

    def test_stop_after_dequeue_never_starts_another_process(self):
        get = self.engine.q.get
        def stop_at_dequeue(*args, **kwargs):
            iid = get(*args, **kwargs)
            self.engine.stop()
            return iid
        self.enterContext(patch.object(self.engine.q, "get", stop_at_dequeue))
        self.store.on_snapshot = lambda: self.calls.append(11)
        self.store.incidents[11].status = "closed"
        self.engine.schedule(11)
        self.engine.start()
        thread = self.engine._worker_thread
        self.threads.append(thread)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.calls, [], "stop 확인 뒤 새 사건 처리를 시작했습니다.")
        self.assertIn(11, self.engine._scheduled)


    def test_notification_loop_close_never_duplicates_durable_failure(self):
        class ClosedDuringDelivery:
            def is_closed(self):
                return False
            def call_soon_threadsafe(self, callback, msg):
                raise RuntimeError("비공개알림원문표식")
        self.enterContext(patch.object(engine_module.broker, "_loop", ClosedDuringDelivery()))
        self.launch(RuntimeError("오프라인 최초 장애"))
        self.assertTrue(self.next_seen.wait(2))
        self.assertEqual(self.calls, [11, 12])
        self.assertEqual(self.thread_errors, [])
        self.assertEqual(len(self.store.audits), 1)
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.published, [])
        self.assertTrue("비공개알림원문표식" not in self.logs.getvalue())


    def test_readyz_requires_initialized_worker_and_database_for_background(self):
        from app import main
        for engine, ready, database_ok in ((None, False, True), (self.engine, True, True), (None, True, False)):
            with self.subTest(ready=ready, database_ok=database_ok),                     patch.dict(main.STATE, {"ready": ready, "engine": engine, "background_requested": True}),                     patch.object(db, "db_ok", return_value=database_ok):
                response = main.readyz()
                self.assertEqual(response.status_code, 503)
                self.assertFalse(json.loads(response.body)["background"])
        with patch.dict(main.STATE, {"ready": False, "engine": None, "background_requested": False}),                 patch.object(db, "db_ok", return_value=False):
            self.assertEqual(main.readyz().status_code, 503)
        self.assertEqual(main.healthz(), {"status": "ok"})


    def test_offline_guard_rejects_external_execution_and_forbidden_reads(self):
        for event in ("socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system",
                      "os.posix_spawn", "sqlite3.connect"):
            with self.subTest(event=event), self.assertRaises(AssertionError):
                _offline_guard(event, ())
        for name in ("/차단/conftest.py", "/차단/.kube/config", "/var/run/secrets/차단"):
            with self.subTest(name=name), self.assertRaises(AssertionError):
                _offline_guard("open", (name,))


if __name__ == "__main__":
    unittest.main()
