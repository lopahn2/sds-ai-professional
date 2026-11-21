"""대기 사건 중단과 실행 소유권 인계 회귀. 전용 시험 DB·가짜 외부 전송만 사용한다."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select

from app import db, main, runlog
from app.config import AppConfig, Settings
from app.engine import Engine
from app.evidence import Bundle, EvidenceItem
from app.llm.base import LLMError
from app.models import Incident, RunControl, WorkflowRun


def incident(workload, status="open"):
    with db.session() as s:
        row = Incident(fingerprint="queued-stop-" + workload, namespace="offline-only", workload=workload,
                       title="대기 사건 중단 시험", status=status, signal_reasons=[])
        s.add(row)
        s.flush()
        return row.id


def snapshot(iid):
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc is not None
        ctl = s.get(RunControl, iid)
        runs = list(s.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)))
        return {"status": inc.status, "handoff": inc.handoff, "control": ctl.state if ctl else "running",
                "runs": [(r.status, r.phase) for r in runs]}


def engine(provider=None):
    return Engine(AppConfig(change_auto_create=False),
                  Settings.model_construct(quota_wait_max_s=600, quota_recheck_s=300, background=False),
                  kube=object(), provider=provider, rag=None)


def bundle(kube, cfg, settings, ns, workload, **kwargs):
    return Bundle(namespace=ns, workload=workload,
                  deployment={"current_revision": 2, "images": ["fake:2"],
                              "recent_rollout_at": datetime.now(timezone.utc).isoformat()},
                  pods=[{"name": "fake-pod", "containers": [{"ready": False,
                                                          "waiting_reason": "ImagePullBackOff"}]}],
                  items=[EvidenceItem("deployment", ns + "/Deployment/" + workload, {"fake": True}),
                         EvidenceItem("pod_status", ns + "/pods", {"fake": True})])


def test_queued_http_stop_finishes_without_stopping_front_quota_wait(db_url, monkeypatch):
    from app import engine as module

    class QuotaProvider:
        name = "offline-fake"
        calls = 0

        def complete(self, *args):
            self.calls += 1
            raise LLMError("quota", "가짜 할당량 대기")

        def next_available_in(self):
            return 300

    a, b = incident("front"), incident("queued")
    provider = QuotaProvider()
    eng = engine(provider)
    sleeping, release = threading.Event(), threading.Event()

    def wait(seconds):
        assert seconds <= 1
        sleeping.set()
        assert release.wait(8), "가짜 대기 해제 기한 초과"

    eng._sleep = wait
    monkeypatch.setattr(module, "collect", bundle)
    eng.schedule(a)
    eng.schedule(b)
    worker = threading.Thread(target=eng._worker, daemon=True)
    worker.start()
    try:
        assert sleeping.wait(5), "앞 사건이 실제 quota 루프에 들어가지 않음"
        before = snapshot(a)
        assert main.control(b, main.ControlIn(action="stop"), user="test-operator") == {"state": "stopping"}
        stopped = snapshot(b)
        assert stopped["status"] == "handed_off", "대기 사건의 종료가 앞 사건의 worker 해제를 기다림"
        assert stopped["control"] == "stopping" and stopped["runs"] == []
        assert snapshot(a) == before and provider.calls == 1
        assert not [e for e in runlog.events_since(a) if e["kind"] == "stopped"]
        completed = [e for e in runlog.events_since(b) if e["kind"] == "completed"]
        assert len(completed) == 1 and completed[0]["detail"]["outcome"] == "stopped_before_start"
        # 실제 캠페인 drain 조정기를 DB 조회/실제 종료 endpoint에 연결한다. 외부 API는 사용하지 않는다.
        import importlib.util
        from pathlib import Path
        from types import SimpleNamespace
        path = Path(__file__).resolve().parents[2] / "scripts/p18-campaign-run.py"
        spec = importlib.util.spec_from_file_location("queued_stop_runner", path)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        h = runner.Harness.__new__(runner.Harness)
        h.args = SimpleNamespace(drain_timeout=2)
        h.read_incident = lambda iid: {"incident": {"id": iid, "status": snapshot(iid)["status"]},
                                       "actions": [], "agent_runs": []}

        def api(path, method, body):
            assert path == f"/api/incidents/{b}/close" and method == "POST"
            return main.close_incident(b, main.CloseIn(**body), user="test-operator")

        h.api = api
        drained = h.close_records([h.read_incident(b)])
        assert drained[0]["incident"]["status"] == "closed" and snapshot(a) == before
        assert provider.calls == 1
    finally:
        eng.stop()
        runlog.set_control(a, "stop")
        release.set()
        worker.join(6)
        assert not worker.is_alive(), "시험 worker 정리 실패"


@pytest.mark.parametrize("kind,status", [
    ("run", "running"),
    ("action", "proposed"), ("action", "pending_approval"), ("action", "approved"), ("action", "executing"), ("action", "indeterminate"),
    ("command", "planning"), ("command", "awaiting_confirmation"), ("command", "awaiting_secret"),
    ("command", "executing"), ("command", "rolling_back"),
])
def test_open_with_live_execution_evidence_is_not_falsely_finished(db_url, kind, status):
    from app.models import Action, OperatorCommand
    iid = incident("live-evidence")
    with db.session() as s:
        if kind == "run":
            s.add(WorkflowRun(incident_id=iid, status=status, phase="analysis", snapshot={}))
        elif kind == "action":
            s.add(Action(incident_id=iid, type="rollout_restart", target="offline-only/Deployment/fake",
                         params={}, policy_mode="assessment", status=status))
        else:
            s.add(OperatorCommand(incident_id=iid, actor="test", instruction="가짜 조사", status=status))
    runlog.set_control(iid, "stop")
    assert snapshot(iid)["status"] == "open", "실행·조치 소멸 확인 없이 대기 종료를 확정함"
    assert snapshot(iid)["control"] == "stopping"
    eng = engine()
    eng.schedule(iid)
    worker = threading.Thread(target=eng._worker, daemon=True)
    worker.start()
    try:
        wait_until(lambda: iid not in eng._scheduled)
        assert snapshot(iid)["status"] == "open", "오래된 큐 작업이 미종료 조치를 건너뛰어 종료를 확정함"
    finally:
        eng.stop()
        worker.join(6)
        assert not worker.is_alive()
    assert not [e for e in runlog.events_since(iid) if e["kind"] == "completed"]


@pytest.mark.parametrize("action_status", ["executing", "indeterminate"])
def test_stopped_queue_yields_worker_until_external_effect_is_confirmed(db_url, action_status):
    from app.models import Action
    iid, other = incident("미확정-조치"), incident("뒤-대기")
    with db.session() as s:
        action = Action(incident_id=iid, type="rollout_restart", target="offline-only/Deployment/fake",
                        params={}, policy_mode="assessment", status=action_status)
        s.add(action)
        s.flush()
        aid = action.id
    runlog.set_control(iid, "stop")
    runlog.set_control(other, "stop")
    eng = engine()
    eng.schedule(iid)
    eng.schedule(other)
    worker = threading.Thread(target=eng._worker, daemon=True)
    worker.start()
    try:
        wait_until(lambda: other not in eng._scheduled)
        assert iid not in eng._scheduled and not eng._cleanup_pending
        assert snapshot(iid)["status"] == "open"
        assert snapshot(other)["status"] == "handed_off"
        assert not [e for e in runlog.events_since(iid) if e["kind"] == "completed"]
        # 실제 효과가 확정된 이후 callback만 사건을 종료한다.
        with db.session() as s:
            s.get(Action, aid).status = "succeeded"
        assert runlog.finalize_stop(iid)
        assert not runlog.finalize_stop(iid)
        assert snapshot(iid)["status"] == "handed_off"
        assert len([e for e in runlog.events_since(iid) if e["kind"] == "completed"]) == 1
    finally:
        eng.stop()
        worker.join(6)
        assert not worker.is_alive()


def wait_until(predicate):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("실제 DB 경쟁 조건 관측 기한 초과")


def test_stop_wins_claim_race_without_collection_or_followup(db_url, monkeypatch):
    from app import changes, engine as module
    iid = incident("stop-wins")
    eng = engine()
    collected, followups, errors = [], [], []
    holding, release = threading.Event(), threading.Event()
    original_session = db.session

    @contextmanager
    def sessions():
        with original_session() as s:
            yield s
            if threading.current_thread().name == "queued-stopper":
                holding.set()
                assert release.wait(8), "중단 트랜잭션 해제 기한 초과"

    def stop():
        try:
            runlog.set_control(iid, "stop")
        except BaseException as e:
            errors.append(e)

    def collect(*args, **kwargs):
        collected.append(True)
        return bundle(*args, **kwargs)

    pid = {}

    def cursor_started(conn, cursor, statement, params, context, many):
        if threading.current_thread().name == "claim-worker" and "incidents" in statement.lower():
            pid["worker"] = cursor.connection.info.backend_pid

    def worker_blocked():
        if "worker" not in pid:
            return False
        with db.engine().connect() as c:
            return c.exec_driver_sql("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                                     (pid["worker"],)).scalar() == "Lock"

    monkeypatch.setattr(db, "session", sessions)
    monkeypatch.setattr(module, "collect", collect)
    monkeypatch.setattr(changes, "auto_create_for_incident", lambda *args: followups.append(True))
    event.listen(db.engine(), "before_cursor_execute", cursor_started)
    stopper = threading.Thread(target=stop, name="queued-stopper")
    worker = threading.Thread(target=eng._worker, name="claim-worker", daemon=True)
    try:
        stopper.start()
        assert holding.wait(5)
        eng.schedule(iid)
        worker.start()
        wait_until(worker_blocked)
        release.set()
        stopper.join(5)
        wait_until(lambda: iid not in eng._scheduled)
        assert not errors
        assert snapshot(iid)["status"] == "handed_off"
        assert snapshot(iid)["runs"] == []
        assert collected == [] and followups == [], "중단이 먼저 확정됐는데 오래된 큐 작업이 실행됨"
    finally:
        release.set()
        eng.stop()
        stopper.join(6)
        if worker.ident is not None:
            worker.join(6)
        event.remove(db.engine(), "before_cursor_execute", cursor_started)
        assert not stopper.is_alive() and not worker.is_alive()


def test_worker_claim_wins_stop_stays_nonterminal_until_boundary_and_skips_collection(db_url, monkeypatch):
    from sqlalchemy.orm import Session
    from app import engine as module
    iid = incident("worker-wins")
    eng = engine()
    claimed, release = threading.Event(), threading.Event()
    held, collected = [], []

    def after_commit(s):
        if threading.current_thread().name == "claim-worker" and not held and any(
                isinstance(r, Incident) and r.id == iid and r.status == "analyzing"
                for r in s.identity_map.values()):
            held.append(True)
            claimed.set()
            assert release.wait(8), "실행 소유권 인계 해제 기한 초과"

    monkeypatch.setattr(module, "collect", lambda *args, **kwargs: (collected.append(True), bundle(*args, **kwargs))[1])
    event.listen(Session, "after_commit", after_commit)
    eng.schedule(iid)
    worker = threading.Thread(target=eng._worker, name="claim-worker", daemon=True)
    worker.start()
    try:
        assert claimed.wait(5)
        runlog.set_control(iid, "stop")
        assert snapshot(iid)["status"] == "analyzing"
        assert snapshot(iid)["runs"] == [("running", "analysis")]
        release.set()
        wait_until(lambda: iid not in eng._scheduled)
        assert snapshot(iid)["status"] == "handed_off"
        assert collected == [], "실행 소유권 인계 직후 중단됐는데 수집을 시작함"
    finally:
        release.set()
        eng.stop()
        worker.join(6)
        event.remove(Session, "after_commit", after_commit)
        assert not worker.is_alive()


def test_router_propagates_operator_stop_instead_of_classifier_failure():
    from app import router

    def stop(prompt):
        raise runlog.RunStopped("quota 대기 중 운영자 중단")

    b = bundle(None, None, None, "offline-only", "fake")
    with pytest.raises(runlog.RunStopped):
        router.decide(b, 15, llm_classify=stop)


@pytest.mark.parametrize("channel", ["http", "chat"])
def test_queued_stop_is_committed_before_publish_and_idempotent(db_url, monkeypatch, channel):
    from app import steering
    from app.models import AuditLog
    iid = incident("commit-and-idempotence")
    published = []
    original_publish = runlog.broker.publish

    def publish(*args, **kwargs):
        if args[0] == iid:
            published.append(snapshot(iid)["status"])
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(runlog.broker, "publish", publish)
    if channel == "chat":
        reply = steering.post(iid, "분석을 중단해 주세요.", actor="test-operator")
        assert reply["intent"] == "stop"
    else:
        main.control(iid, main.ControlIn(action="stop"), user="test-operator")
    assert snapshot(iid)["status"] == "handed_off"
    # 채팅 접수/해석 이벤트는 중단 트랜잭션 이전에 발행될 수 있다.
    assert published[-1] == "handed_off"
    events = runlog.events_since(iid)
    runlog.set_control(iid, "stop")
    assert runlog.set_control(iid, "resume") == "stopping"
    assert runlog.set_control(iid, "pause") == "stopping"
    assert runlog.events_since(iid) == events
    completed = [e for e in events if e["kind"] == "completed"]
    assert len(completed) == 1 and completed[0]["detail"]["cluster_changed"] is False
    assert snapshot(iid)["runs"] == []
    with db.session() as s:
        audits = list(s.scalars(select(AuditLog).where(AuditLog.incident_id == iid,
                           AuditLog.event == "incident_stopped_before_start")))
        assert len(audits) == 1 and audits[0].detail["cluster_changed"] is False


def test_settle_window_and_stale_queue_cannot_restart_a_stopped_incident(db_url, monkeypatch):
    from app import changes, engine as module
    iid = incident("delayed")
    collected, followups = [], []
    eng = engine()
    monkeypatch.setattr(module, "collect", lambda *a, **kw: collected.append(True))
    monkeypatch.setattr(changes, "auto_create_for_incident", lambda *a: followups.append(True))
    eng.schedule(iid, delay=0.05)
    runlog.set_control(iid, "stop")
    eng.schedule(iid, delay=0.05)
    assert list(eng._scheduled) == [iid]
    eng.start()
    try:
        wait_until(lambda: iid not in eng._scheduled)
        eng.process(iid)
        assert snapshot(iid)["status"] == "handed_off" and snapshot(iid)["runs"] == []
        assert collected == [] and followups == []
        assert len([e for e in runlog.events_since(iid) if e["kind"] == "completed"]) == 1
    finally:
        eng.stop()
        for worker in (eng._worker_thread, eng._image_worker):
            worker.join(6)
            assert not worker.is_alive()
