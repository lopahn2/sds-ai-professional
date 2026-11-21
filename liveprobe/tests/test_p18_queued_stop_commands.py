"""QSTOP P1: 실제 PostgreSQL 잠금과 worker/명령 종료 장벽. 외부 전송만 대역."""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import event

from app import db, main, runlog
from app.commands import CommandService
from app.config import AppConfig
from test_p15_commands import MemoryKube, Planner, cm, operation
from test_p18_queued_stop import incident, snapshot, wait_until


def prepared(iid, kube):
    service = CommandService(AppConfig(), Planner([operation()]), kube)
    row = service.create(iid, '설정 mode를 new로 변경', actor='p1-operator')
    row = service.plan(row['id'])
    row = service.confirm(row['id'], row['plan_sha256'])
    return service, row


def stop(iid, channel):
    if channel == 'http':
        return main.control(iid, main.ControlIn(action='stop'), user='p1-operator')
    from fastapi import BackgroundTasks
    return main.post_message(iid, main.MessageIn(body='분석을 중단해 주세요.'), BackgroundTasks(), user='p1-operator')


def completed(iid):
    return [e for e in runlog.events_since(iid) if e['kind'] == 'completed']


def invoke(errors, fn):
    try:
        fn()
    except BaseException as exc:
        errors.append(exc)


@pytest.mark.parametrize('channel', ['http', 'chat'])
def test_stop_lock_serializes_rollback_claim_and_allows_explicit_later_rollback(db_url, channel):
    iid = incident('stop-before-rollback')
    checked, stop_release = threading.Event(), threading.Event()
    transport, transport_release = threading.Event(), threading.Event()
    errors, pids = [], {}

    class HeldKube(MemoryKube):
        def mutate(self, op, before, after, *, dry_run=False):
            if not dry_run and threading.current_thread().name == 'p1-rollback':
                transport.set()
                assert transport_release.wait(12), '원복 전송 장벽 기한 초과'
            return super().mutate(op, before, after, dry_run=dry_run)

    service, command = prepared(iid, HeldKube(cm()))
    assert service.execute(command['id'])['status'] == 'succeeded'

    def after_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-stop' and not checked.is_set() and 'operator_commands.id' in statement and 'operator_commands.status IN' in statement:
            checked.set()
            assert stop_release.wait(12), '중단 busy 조회 장벽 기한 초과'

    def before_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-rollback':
            pids['rollback'] = cursor.connection.info.backend_pid

    def locked():
        if 'rollback' not in pids:
            return False
        with db.engine().connect() as c:
            return c.exec_driver_sql('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', (pids['rollback'],)).scalar() == 'Lock'

    event.listen(db.engine(), 'after_cursor_execute', after_cursor)
    event.listen(db.engine(), 'before_cursor_execute', before_cursor)
    stopper = threading.Thread(target=lambda: invoke(errors, lambda: stop(iid, channel)), name='p1-stop')
    inverse = threading.Thread(target=lambda: invoke(errors, lambda: service.rollback(command['id'], command['plan_sha256'])), name='p1-rollback')
    try:
        stopper.start()
        assert checked.wait(6)
        inverse.start()
        wait_until(lambda: locked() or transport.is_set())
        assert locked() and not transport.is_set(), '중단이 Incident 잠금을 보유했는데 원복 claim/전송이 끼어듦'
        stop_release.set()
        stopper.join(6)
        assert not stopper.is_alive()
        assert snapshot(iid)['status'] == 'handed_off'
        assert transport.wait(6), '중단 확정 뒤 명시적 원복 기능은 유지해야 함'
        assert service.get(command['id'])['status'] == 'rolling_back'
        assert len(completed(iid)) == 1
    finally:
        stop_release.set()
        transport_release.set()
        stopper.join(6)
        if inverse.ident is not None:
            inverse.join(6)
        event.remove(db.engine(), 'after_cursor_execute', after_cursor)
        event.remove(db.engine(), 'before_cursor_execute', before_cursor)
        assert not stopper.is_alive() and not inverse.is_alive()
    assert errors == []
    assert service.get(command['id'])['status'] == 'rolled_back'
    assert len(completed(iid)) == 1


@pytest.mark.parametrize('channel', ['http', 'chat'])
def test_rollback_claim_first_defers_stop_until_verified(db_url, channel):
    iid = incident('rollback-before-stop')
    transport, release = threading.Event(), threading.Event()
    errors = []

    class HeldKube(MemoryKube):
        def mutate(self, op, before, after, *, dry_run=False):
            if not dry_run and threading.current_thread().name == 'p1-rollback':
                transport.set()
                assert release.wait(12)
            return super().mutate(op, before, after, dry_run=dry_run)

    service, command = prepared(iid, HeldKube(cm()))
    assert service.execute(command['id'])['status'] == 'succeeded'
    inverse = threading.Thread(target=lambda: invoke(errors, lambda: service.rollback(command['id'], command['plan_sha256'])), name='p1-rollback')
    try:
        inverse.start()
        assert transport.wait(6)
        stop(iid, channel)
        assert snapshot(iid)['status'] == 'open' and completed(iid) == []
        assert service.get(command['id'])['status'] == 'rolling_back'
    finally:
        release.set()
        inverse.join(6)
        assert not inverse.is_alive()
    assert errors == []
    assert service.get(command['id'])['rollback']['verified'] is True
    assert snapshot(iid)['status'] == 'handed_off', '원복 검증 종료 뒤 공통 중단 확정이 누락됨'
    assert len(completed(iid)) == 1
    runlog.set_control(iid, 'stop')
    runlog.finalize_stop(iid)
    assert len(completed(iid)) == 1


@pytest.mark.parametrize('channel', ['http', 'chat'])
@pytest.mark.parametrize('order', ['worker_first', 'command_first'])
@pytest.mark.parametrize('outcome', ['succeeded', 'verification_failed'])
def test_claimed_worker_and_command_both_exit_before_single_stop_completion(db_url, monkeypatch, channel, order, outcome):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from test_p18_queued_stop import engine
    from app.models import AuditLog, Incident
    iid = incident('claim-and-command')
    publications = []
    original_deliver = runlog.broker._deliver
    def deliver(message):
        if message['incident_id'] == iid and message['event'] == 'handed_off':
            assert snapshot(iid)['status'] == 'handed_off', 'commit 전에 terminal SSE를 발행함'
            publications.append(message)
        return original_deliver(message)
    # 중단 기록은 이미 원자적으로 저장되어 publish(재쓰기)가 아니라 전송을 관측한다.
    class DeliveryLoop:
        def is_closed(self):
            return False
        def call_soon_threadsafe(self, callback, *args):
            callback(*args)
    monkeypatch.setattr(runlog.broker, '_loop', DeliveryLoop())
    monkeypatch.setattr(runlog.broker, '_deliver', deliver)
    verifying, verify_release = threading.Event(), threading.Event()
    claimed, claim_release = threading.Event(), threading.Event()
    errors, held = [], []

    class HeldKube(MemoryKube):
        written = False
        def mutate(self, op, before, after, *, dry_run=False):
            out = super().mutate(op, before, after, dry_run=dry_run)
            if not dry_run:
                self.written = True
            return out
        def get(self, op):
            if self.written and threading.current_thread().name == 'p1-verifier':
                verifying.set()
                assert verify_release.wait(12)
                observed = super().get(op)
                if outcome == 'verification_failed':
                    observed['data']['mode'] = 'synthetic-unverified-value'
                return observed
            return super().get(op)

    service, command = prepared(iid, HeldKube(cm()))
    eng = engine()
    def after_commit(s):
        if threading.current_thread().name == 'p1-worker' and not held and any(isinstance(r, Incident) and r.id == iid and r.status == 'analyzing' for r in s.identity_map.values()):
            held.append(True)
            claimed.set()
            assert claim_release.wait(12)

    event.listen(Session, 'after_commit', after_commit)
    executor = threading.Thread(target=lambda: invoke(errors, lambda: service.execute(command['id'])), name='p1-verifier')
    worker = threading.Thread(target=eng._worker, name='p1-worker', daemon=True)
    try:
        executor.start()
        assert verifying.wait(6)
        eng.schedule(iid)
        worker.start()
        assert claimed.wait(6)
        stop(iid, channel)
        assert snapshot(iid)['status'] == 'analyzing' and completed(iid) == []
        if order == 'worker_first':
            claim_release.set()
            wait_until(lambda: iid not in eng._scheduled)
            observed = snapshot(iid)
            assert observed['status'] == 'analyzing' and completed(iid) == [], '명령 검증 진행 중인데 worker 종료가 사건을 완료로 꾸밈'
            assert observed['runs'] == [('failed', 'stopped')], '중단한 WorkflowRun을 성공으로 기록함'
            # 단일 worker를 명령 대기 루프에 붙잡지 않는다.
            other = incident('other-queued')
            runlog.set_control(other, 'stop')
            eng.schedule(other)
            wait_until(lambda: other not in eng._scheduled)
            verify_release.set()
            executor.join(6)
        else:
            verify_release.set()
            executor.join(6)
            assert not executor.is_alive()
            assert snapshot(iid)['status'] == 'analyzing' and completed(iid) == []
            assert snapshot(iid)['runs'] == [('running', 'analysis')], '명령 종료 callback이 아직 실행 중인 worker를 종료시킴'
            claim_release.set()
            wait_until(lambda: iid not in eng._scheduled)
        assert snapshot(iid)['status'] == 'handed_off'
        assert snapshot(iid)['runs'] == [('failed', 'stopped')]
        assert len(completed(iid)) == 1, '중단 완료가 중복 기록됨'
        runlog.set_control(iid, 'stop')
        runlog.finalize_stop(iid)
        assert len(completed(iid)) == 1
        assert len(publications) == 1
        with db.session() as s:
            assert len(list(s.scalars(select(AuditLog).where(AuditLog.incident_id == iid,
                AuditLog.event == 'incident_stopped')))) == 1
        actual = service.get(command['id'])
        assert actual['status'] == ('succeeded' if outcome == 'succeeded' else 'failed')
        if outcome == 'verification_failed':
            assert actual['error'] == 'execution_verification_failed'
    finally:
        claim_release.set()
        verify_release.set()
        eng.stop()
        if worker.ident is not None:
            worker.join(6)
        executor.join(6)
        event.remove(Session, 'after_commit', after_commit)
        assert not executor.is_alive() and not worker.is_alive()
    assert errors == []


@pytest.mark.parametrize('method', ['create', 'confirm', 'execute', 'secret_values'])
def test_every_command_start_locks_incident_before_command_without_network_lock(db_url, method):
    from sqlalchemy import select
    from app.models import Incident, OperatorCommand
    iid = incident('start-' + method)
    service = CommandService(AppConfig(), Planner([operation()]), MemoryKube(cm()))
    row = service.create(iid, '설정 변경')
    row = service.plan(row['id'])
    if method == 'execute':
        row = service.confirm(row['id'], row['plan_sha256'])
    if method == 'secret_values':
        with db.session() as s:
            command = s.get(OperatorCommand, row['id'])
            command.status = 'awaiting_secret'
            command.plan = {'operations': [{'op': 'secret_update', 'namespace': 'sandbox', 'name': 'test-only', 'keys': ['key']}]}
    calls, errors, pids = [], [], {}
    class Writer:
        def write(self, *args, **kwargs):
            calls.append('write')
    def action():
        if method == 'create':
            service.create(iid, '새 명시적 변경')
        elif method == 'secret_values':
            service.secret_values(row['id'], row['plan_sha256'], {'key': 'synthetic-test-input'}, Writer())
        elif method == 'confirm':
            service.confirm(row['id'], row['plan_sha256'])
        else:
            service.execute(row['id'])
    done = threading.Event()
    def run():
        invoke(errors, action)
        done.set()
    def before_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-start':
            pids['start'] = cursor.connection.info.backend_pid
    def waiting_query():
        if 'start' not in pids:
            return None
        with db.engine().connect() as c:
            return c.exec_driver_sql("SELECT query FROM pg_stat_activity WHERE pid=%s AND wait_event_type='Lock'", (pids['start'],)).scalar()
    event.listen(db.engine(), 'before_cursor_execute', before_cursor)
    worker = threading.Thread(target=run, name='p1-start')
    try:
        with db.session() as s:
            s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            worker.start()
            wait_until(lambda: waiting_query() is not None or done.is_set())
            query = waiting_query() or ''
            assert 'FROM incidents' in query and 'FOR UPDATE' in query, '명령 시작이 공통 Incident 잠금을 우회함'
            assert calls == [] and service.kube.calls.count(('patch', False)) == 0
            # Inc를 기다리며 Command를 먼저 잠그면 반대순서 교착을 만들 수 있다.
            with db.session() as other:
                other.scalar(select(OperatorCommand).where(OperatorCommand.id == row['id']).with_for_update(nowait=True))
    finally:
        if worker.ident is not None:
            worker.join(6)
        event.remove(db.engine(), 'before_cursor_execute', before_cursor)
        assert not worker.is_alive()
    assert errors == []


@pytest.mark.parametrize('outcome', ['cancelled', 'rejected', 'secret_succeeded', 'execution_failed'])
def test_command_terminal_commits_recheck_stop(outcome, db_url, monkeypatch):
    from sqlalchemy import select
    from app.models import Incident, OperatorCommand
    iid = incident('terminal-' + outcome)
    planner = Planner([operation()])
    service = CommandService(AppConfig(), planner, MemoryKube(cm()))
    row = service.create(iid, '계획만 작성')
    original_finalize = runlog.finalize_stop
    def finalize(iid, **kwargs):
        # 다른 연결의 NOWAIT 성공으로 callback 진입 시 두 잠금의 해제를 확인한다.
        with db.session() as other:
            other.scalar(select(Incident).where(Incident.id == iid).with_for_update(nowait=True))
            other.scalar(select(OperatorCommand).where(OperatorCommand.id == row['id']).with_for_update(nowait=True))
        return original_finalize(iid, **kwargs)
    monkeypatch.setattr(runlog, 'finalize_stop', finalize)
    if outcome.startswith('secret_'):
        row = service.plan(row['id'])
        with db.session() as s:
            command = s.get(OperatorCommand, row['id'])
            command.status = 'awaiting_secret'
            command.plan = {'operations': [{'op': 'secret_update', 'namespace': 'sandbox', 'name': 'test-only', 'keys': ['key']}]}
    if outcome == 'execution_failed':
        row = service.plan(row['id'])
        service.confirm(row['id'], row['plan_sha256'])
        service.kube.fail_dry = True
    runlog.set_control(iid, 'stop')
    assert snapshot(iid)['status'] == 'open' and completed(iid) == []
    if outcome == 'cancelled':
        service.cancel(row['id'])
    elif outcome == 'rejected':
        planner.operations = []
        assert service.plan(row['id'])['status'] == 'rejected'
    elif outcome == 'execution_failed':
        assert service.execute(row['id'])['status'] == 'failed'
    else:
        class Writer:
            def write(self, *args, **kwargs):
                return None
        service.secret_values(row['id'], row['plan_sha256'], {'key': 'synthetic-test-input'}, Writer())
    assert snapshot(iid)['status'] == 'handed_off', '명령 terminal commit 뒤 중단 확정 callback 누락'
    assert len(completed(iid)) == 1
    assert runlog.finalize_stop(iid) is False
    assert len(completed(iid)) == 1


def test_duplicate_background_execution_error_does_not_steal_live_claim(db_url):
    from app.command_api import perform
    from app.models import OperatorCommand
    iid = incident('duplicate-background')
    service, command = prepared(iid, MemoryKube(cm()))
    with db.session() as s:
        row = s.get(OperatorCommand, command['id'])
        row.result = {'claimed': True, 'operations': [], 'inflight': None}
    runlog.set_control(iid, 'stop')
    perform(service, 'execute', command['id'])
    assert service.get(command['id'])['status'] == 'executing', '중복 실행 오류가 다른 실행자의 활성 claim을 실패로 덮어씀'
    assert snapshot(iid)['status'] == 'open' and completed(iid) == []


def test_failed_secret_transport_keeps_durable_uncertainty(db_url):
    from app.models import OperatorCommand
    iid = incident('secret-uncertainty')
    service, row = prepared(iid, MemoryKube(cm()))
    with db.session() as s:
        command = s.get(OperatorCommand, row['id'])
        command.status = 'awaiting_secret'
        command.plan = {'operations': [{'op': 'secret_update', 'namespace': 'sandbox', 'name': 'test-only', 'keys': ['key']}]}
    runlog.set_control(iid, 'stop')
    class Writer:
        def write(self, *args, **kwargs):
            raise RuntimeError('synthetic-unknown-response')
    assert service.secret_values(row['id'], row['plan_sha256'], {'key': 'synthetic-test-input'}, Writer())['status'] == 'failed'
    assert service.get(row['id'])['result'].get('inflight') == 0, 'Secret 전송 실패의 불확실성이 유실됨'
    runlog.finalize_stop(iid)
    assert snapshot(iid)['status'] == 'open' and completed(iid) == []


@pytest.mark.parametrize('inflight', ['execution', 'rollback', 'restart'])
def test_indeterminate_command_does_not_become_stopped_completion(db_url, inflight):
    from app.models import OperatorCommand
    iid = incident('indeterminate-' + inflight)
    with db.session() as s:
        s.add(OperatorCommand(incident_id=iid, actor='test', instruction='불확실한 결과 보존', status='failed',
            result={'inflight': 0} if inflight == 'execution' else {},
            rollback={'inflight': 0} if inflight == 'rollback' else {},
            error='process_restarted_outcome_requires_inspection' if inflight == 'restart' else 'operator_error:RuntimeError'))
    runlog.set_control(iid, 'stop')
    runlog.finalize_stop(iid)
    assert snapshot(iid)['status'] == 'open' and completed(iid) == []


def test_duplicate_live_claim_does_not_hold_incident_while_waiting_for_network(db_url):
    from app.command_api import perform
    iid = incident('duplicate-lock-ack')
    verifying, release = threading.Event(), threading.Event()
    duplicate_query, duplicate_done, stop_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    class HeldKube(MemoryKube):
        written = False
        def mutate(self, op, before, after, *, dry_run=False):
            out = super().mutate(op, before, after, dry_run=dry_run)
            if not dry_run:
                self.written = True
            return out
        def get(self, op):
            if self.written and threading.current_thread().name == 'p1-live':
                verifying.set()
                assert release.wait(12)
            return super().get(op)
    service, command = prepared(iid, HeldKube(cm()))
    def duplicate():
        invoke(errors, lambda: perform(service, 'execute', command['id']))
        duplicate_done.set()
    def stopping():
        invoke(errors, lambda: runlog.set_control(iid, 'stop'))
        stop_done.set()
    def before_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-duplicate' and 'FROM operator_commands' in statement and 'FOR UPDATE' in statement:
            duplicate_query.set()
    event.listen(db.engine(), 'before_cursor_execute', before_cursor)
    live = threading.Thread(target=lambda: invoke(errors, lambda: service.execute(command['id'])), name='p1-live')
    duplicate_thread = threading.Thread(target=duplicate, name='p1-duplicate')
    stopper = threading.Thread(target=stopping)
    try:
        live.start()
        assert verifying.wait(6)
        duplicate_thread.start()
        assert duplicate_query.wait(6)
        stopper.start()
        assert stop_done.wait(2), '중복 claim이 Command 네트워크 잠금을 기다리며 Incident를 붙잡아 중단 ACK를 막음'
        assert duplicate_done.wait(2)
        assert snapshot(iid)['status'] == 'open' and completed(iid) == []
        assert service.get(command['id'])['status'] == 'executing'
    finally:
        release.set()
        for thread in (live, duplicate_thread, stopper):
            if thread.ident is not None:
                thread.join(6)
            assert not thread.is_alive()
        event.remove(db.engine(), 'before_cursor_execute', before_cursor)
    assert errors == []
    assert snapshot(iid)['status'] == 'handed_off' and len(completed(iid)) == 1


def test_stop_completion_event_cannot_be_lost_to_concurrent_feed_append(db_url):
    from app.models import Incident, RunControl
    iid = incident('completion-event-race')
    with db.session() as s:
        s.get(Incident, iid).status = 'analyzing'
        s.add(RunControl(incident_id=iid, state='stopping'))
    selected, release, appended = threading.Event(), threading.Event(), threading.Event()
    errors, pids = [], {}
    def after_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-completion' and 'max(run_events.seq)' in statement:
            selected.set()
            assert release.wait(12)
    def before_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-feed':
            pids['feed'] = cursor.connection.info.backend_pid
    def blocked():
        if 'feed' not in pids:
            return False
        with db.engine().connect() as c:
            return c.exec_driver_sql('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', (pids['feed'],)).scalar() == 'Lock'
    def append():
        runlog.emit(iid, 'note', '동시 기록')
        appended.set()
    event.listen(db.engine(), 'after_cursor_execute', after_cursor)
    event.listen(db.engine(), 'before_cursor_execute', before_cursor)
    finishing = threading.Thread(target=lambda: invoke(errors, lambda: runlog.finalize_stop(iid)), name='p1-completion')
    feed = threading.Thread(target=append, name='p1-feed')
    try:
        finishing.start()
        assert selected.wait(6)
        feed.start()
        wait_until(lambda: blocked() or appended.is_set())
    finally:
        release.set()
        finishing.join(6)
        if feed.ident is not None:
            feed.join(6)
        event.remove(db.engine(), 'after_cursor_execute', after_cursor)
        event.remove(db.engine(), 'before_cursor_execute', before_cursor)
        assert not finishing.is_alive() and not feed.is_alive()
    assert errors == []
    assert len(completed(iid)) == 1, '동시 feed seq 경합으로 중단 완료 이벤트가 유실됨'
    assert len([e for e in runlog.events_since(iid) if e['kind'] == 'note']) == 1


@pytest.mark.parametrize('method', ['confirm', 'rollback'])
def test_preflight_network_does_not_hold_incident_or_block_stop_ack(db_url, method):
    iid = incident('preflight-' + method)
    preflight, release, ack = threading.Event(), threading.Event(), threading.Event()
    errors = []
    class HeldKube(MemoryKube):
        def get(self, op):
            if threading.current_thread().name == 'p1-preflight':
                preflight.set()
                assert release.wait(12)
            return super().get(op)
    kube = HeldKube(cm())
    service = CommandService(AppConfig(), Planner([operation()]), kube)
    command = service.plan(service.create(iid, '명시적 변경')['id'])
    if method == 'rollback':
        service.confirm(command['id'], command['plan_sha256'])
        service.execute(command['id'])
    writes_before = kube.calls.count(('patch', False))
    attempt = threading.Thread(target=lambda: invoke(errors, lambda: getattr(service, method)(command['id'], command['plan_sha256'])), name='p1-preflight')
    def stopping():
        invoke(errors, lambda: runlog.set_control(iid, 'stop'))
        ack.set()
    stopper = threading.Thread(target=stopping)
    try:
        attempt.start()
        assert preflight.wait(6)
        stopper.start()
        assert ack.wait(2), '사전 대상 조회 network가 Incident 잠금을 보유해 stop ACK를 막음'
        assert kube.calls.count(('patch', False)) == writes_before
        assert snapshot(iid)['status'] == ('handed_off' if method == 'rollback' else 'open')
    finally:
        release.set()
        for thread in (attempt, stopper):
            if thread.ident is not None:
                thread.join(6)
            assert not thread.is_alive()
    assert errors == []
    if method == 'confirm':
        assert service.get(command['id'])['status'] == 'executing'
        assert snapshot(iid)['status'] == 'open' and completed(iid) == []
        service.cancel(command['id'])
    assert snapshot(iid)['status'] == 'handed_off' and len(completed(iid)) == 1


@pytest.mark.parametrize('method', ['confirm', 'rollback'])
@pytest.mark.parametrize('changed', ['hash', 'status'])
def test_start_claim_revalidates_after_preflight_transaction(db_url, method, changed):
    from app.commands import CommandError
    from app.models import OperatorCommand
    iid = incident('revalidate-' + method + '-' + changed)
    service = CommandService(AppConfig(), Planner([operation()]), MemoryKube(cm()))
    command = service.plan(service.create(iid, '명시적 변경')['id'])
    if method == 'rollback':
        service.confirm(command['id'], command['plan_sha256'])
        service.execute(command['id'])
    start, release = threading.Event(), threading.Event()
    errors = []
    writes_before = service.kube.calls.count(('patch', False))
    def before_cursor(conn, cursor, statement, params, ctx, many):
        if threading.current_thread().name == 'p1-revalidate' and 'FROM incidents' in statement and 'FOR UPDATE' in statement:
            start.set()
            assert release.wait(12)
    event.listen(db.engine(), 'before_cursor_execute', before_cursor)
    thread = threading.Thread(target=lambda: invoke(errors, lambda: getattr(service, method)(command['id'], command['plan_sha256'])), name='p1-revalidate')
    try:
        thread.start()
        assert start.wait(6)
        with db.session() as s:
            row = s.get(OperatorCommand, command['id'])
            if changed == 'hash':
                row.plan_sha256 = '0' * 64
            else:
                row.status = 'cancelled'
    finally:
        release.set()
        thread.join(6)
        event.remove(db.engine(), 'before_cursor_execute', before_cursor)
        assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], CommandError)
    assert errors[0].code == ('plan_hash_mismatch' if changed == 'hash' else 'command_not_confirmable' if method == 'confirm' else 'command_not_rollbackable')
    assert service.kube.calls.count(('patch', False)) == writes_before


def test_started_action_still_verifies_then_resolves(db_url, monkeypatch):
    from app import safety
    from app.models import Action, Incident, WorkflowRun
    from test_p18_queued_stop import engine
    iid = incident('running-action', status='analyzing')
    with db.session() as s:
        s.get(Incident, iid).signal_reasons = ['ImagePullBackOff']
        action = Action(incident_id=iid, type='rollout_undo', target='offline-only/Deployment/running-action', params={}, status='approved', policy_mode='assessment')
        s.add_all([action, WorkflowRun(incident_id=iid, status='running', phase='analysis', snapshot={})])
        s.flush()
        aid = action.id
    entered, release = threading.Event(), threading.Event()
    errors = []
    def execute(session, cfg, kube, action_id, actor, **kwargs):
        with db.session() as s:
            row = s.get(Action, action_id)
            row.status, row.executed = 'executing', True
        entered.set()
        assert release.wait(12)
        with db.session() as s:
            row = s.get(Action, action_id)
            row.before_snapshot = {'revision': 4, 'images': ['offline/bad']}
            row.params = {**row.params, 'to_revision': 3, 'to_images': ['offline/healthy']}
            row.after_state = {'images': ['offline/healthy'], 'waiting_reasons': [], 'replicas': 1, 'ready_pods': 1}
            row.status = 'succeeded'
        return safety.ExecResult(aid, 'succeeded', '합성 실행 및 검증 완료')
    monkeypatch.setattr(safety, 'execute', execute)
    eng = engine()
    monkeypatch.setattr(eng, '_kube_for', lambda *args: object())
    thread = threading.Thread(target=lambda: invoke(errors, lambda: eng.run_action(iid, aid, actor='test')))
    try:
        thread.start()
        assert entered.wait(6)
        runlog.set_control(iid, 'stop')
        runlog.finalize_stop(iid)
        assert snapshot(iid)['status'] == 'acting' and completed(iid) == []
        assert snapshot(iid)['runs'] == [('running', 'acting')]
    finally:
        release.set()
        thread.join(6)
        assert not thread.is_alive()
    assert errors == []
    assert snapshot(iid)['status'] == 'resolved'
    assert snapshot(iid)['runs'] == [('succeeded', 'resolved')]
    assert len(completed(iid)) == 1


@pytest.mark.parametrize('status', ['planning', 'awaiting_confirmation', 'awaiting_secret'])
def test_worker_exit_preserves_unconfirmed_command_until_explicit_cancel(db_url, status):
    from app.models import OperatorCommand, WorkflowRun
    iid = incident('preserve-' + status, status='analyzing')
    service = CommandService(AppConfig(), Planner([operation()]), MemoryKube(cm()))
    command = service.create(iid, '명시적 지시 보존')
    with db.session() as s:
        s.get(OperatorCommand, command['id']).status = status
        run = WorkflowRun(incident_id=iid, status='running', phase='analysis', snapshot={})
        s.add(run)
        s.flush()
        rid = run.id
    runlog.set_control(iid, 'stop')
    runlog.finalize_stop(iid, worker_run_id=rid, reason='운영자 중단')
    assert snapshot(iid)['status'] == 'analyzing' and completed(iid) == []
    assert service.get(command['id'])['status'] == status
    assert snapshot(iid)['runs'] == [('failed', 'stopped')]
    service.cancel(command['id'])
    assert snapshot(iid)['status'] == 'handed_off' and len(completed(iid)) == 1


@pytest.mark.parametrize('claimed', [False, True])
def test_background_unexpected_error_only_finalizes_unclaimed_execution(db_url, claimed):
    from app.command_api import perform
    from app.models import OperatorCommand
    iid = incident('background-owner-' + str(claimed))
    service, command = prepared(iid, MemoryKube(cm()))
    with db.session() as s:
        s.get(OperatorCommand, command['id']).result = {'claimed': claimed, 'inflight': None}
    runlog.set_control(iid, 'stop')
    class BrokenInvoker:
        def execute(self, cid):
            raise RuntimeError('synthetic-invocation-error')
    perform(BrokenInvoker(), 'execute', command['id'])
    assert service.get(command['id'])['status'] == ('executing' if claimed else 'failed')
    assert snapshot(iid)['status'] == ('open' if claimed else 'handed_off')
    assert len(completed(iid)) == (0 if claimed else 1)
