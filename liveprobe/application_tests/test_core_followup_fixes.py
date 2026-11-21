"""CORE-01 잔여·CORE-04·신규 DB 초기화·역할별 모델 기본값의 시험 우선 회귀.

실제 API·worker·폐기 SQLite와 합성 인증만 사용한다. 실제 모델·K8s·Docker·네트워크·운영 DB는 호출하지 않는다.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from test_core_support import HUMAN, MACHINE, PREFIX, NEW_IMAGE
# 모듈로만 가져와 상위 시험이 이 모듈에서 중복 수집되지 않게 한다.
import test_core_release
from app.application.storage import Incident, Job

RAW = 'raw-material-never-persist'
SECRET = 'synthetic-secret-never-persist'
TABLES = {'application_incidents', 'application_jobs', 'application_events',
          'application_mutex', 'application_runner_observation', 'application_source_inquiries', 'application_display_readings', 'application_runner_contacts'}


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.flow = test_core_release.CoreReleaseTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.h = self.flow.h

    def heartbeat(self, job):
        return self.h.client.post(PREFIX + f'/runner/jobs/{job["id"]}/heartbeat', headers=MACHINE,
                                  json={k: job[k] for k in ('lease_token', 'fencing_token')})

    def job_status(self, job_id):
        with self.h.app.state.store.transaction() as session:
            return session.get(Job, job_id).status

    def release_response(self, incident):
        response = self.h.client.get(PREFIX + f'/incidents/{incident}/release', headers=HUMAN)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def everything(self, incident):
        events = self.h.client.get(PREFIX + f'/incidents/{incident}/events', headers=HUMAN).json()['events']
        return json.dumps({'detail': self.h.detail(incident), 'release': self.release_response(incident),
                           'events': events}, ensure_ascii=False)

    def written(self, job, **extra):
        return {'ok': True, 'write_outcome': 'written', 'verification': [], 'error': None,
                'targets': [dict(t, image=NEW_IMAGE, resource_version='11', write_outcome='written', **extra)
                            for t in job['payload']['targets']]}

    def approved_prd_claim(self, at=None):
        incident = self.flow.awaiting()
        body = self.flow.approval_body(incident)
        response = self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body)
        self.assertEqual(response.status_code, 200)
        expires = self.flow.release(incident)['approval']['expires_at']
        if at is not None:
            self.h.now = expires + at
        job = self.h.claim(['deploy'])
        self.assertIsNotNone(job)
        self.assertEqual(job['payload']['stage'], 'prd')
        return incident, job, expires

    def revoke(self, scope):
        identity = Path(self.h.settings.identity_file)
        values = json.loads(identity.read_text())
        values['principals'][0]['scopes'].remove(scope)
        identity.write_text(json.dumps(values))

    def change_prd_target(self, incident):
        with self.h.app.state.store.transaction() as session:
            row = session.get(Incident, incident)
            document = copy.deepcopy(row.document)
            document['data']['release']['targets']['prd'][0]['resource_version'] = '999'
            row.document = document

    def assert_preserved_for_reconcile(self, incident, job):
        """원래 deploy와 대상 잠금은 reconcile 전까지 유지되고 reconcile만 배정된다."""
        self.assertEqual(self.job_status(job['id']), 'reconciliation_required')
        self.assertEqual(self.h.detail(incident)['state'], 'reconciliation_required')
        # 이미 발생했을 수 있는 쓰기를 미실행/실패로 바꾸지 않는다.
        self.assertFalse('prd' in self.flow.release(incident)['stage_results'])
        self.assertIsNone(self.h.claim(['deploy']))
        reconcile = self.h.claim(['reconcile'])
        self.assertIsNotNone(reconcile)
        self.assertEqual(reconcile['payload']['original_job_id'], job['id'])
        self.assertEqual(reconcile['payload']['original_kind'], 'deploy')
        self.assertEqual(reconcile['payload']['original_payload'], job['payload'])
        self.assertEqual(reconcile['payload']['idempotency_key'], job['idempotency_key'])
        return reconcile


class CorePrdLeaseAuthorizationTests(FlowCase):
    """CORE-01 잔여: claim 이후 승인·시험·권한·지문 무효화가 heartbeat/결과 보고에 반영된다."""

    def test_heartbeat_never_extends_prd_lease_past_approval(self):
        incident, job, expires = self.approved_prd_claim(at=-15)
        self.assertLessEqual(job['lease_expires_at'], expires)
        self.h.now = expires - 10
        response = self.heartbeat(job)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.json()['lease_expires_at'], expires)

    def test_expired_approval_rejects_heartbeat_and_preserves_original_job(self):
        incident, job, expires = self.approved_prd_claim(at=-15)
        self.h.now = expires + 1
        self.assertEqual(self.heartbeat(job).status_code, 409)
        self.assertEqual(self.job_status(job['id']), 'reconciliation_required')
        self.assertEqual(self.h.report(job, self.written(job)).status_code, 409)
        self.assert_preserved_for_reconcile(incident, job)

    def test_revocation_or_fingerprint_change_rejects_heartbeat_and_report(self):
        for case in ('tester_scope_revoked', 'approver_scope_revoked', 'identity_file_removed', 'target_fingerprint_changed'):
            with self.subTest(case=case):
                self.setUp()
                incident, job, _ = self.approved_prd_claim()
                if case == 'tester_scope_revoked':
                    self.revoke('tester')
                elif case == 'approver_scope_revoked':
                    self.revoke('prd-approver')
                elif case == 'identity_file_removed':
                    Path(self.h.settings.identity_file).unlink()
                else:
                    self.change_prd_target(incident)
                response = self.heartbeat(job)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'job_authorization_revoked')
                self.assertEqual(self.h.report(job, self.written(job)).status_code, 409)
                if case == 'identity_file_removed':
                    # 조회 경로도 사람 인증을 쓰므로 합성 인증 파일을 복구한 뒤 상태를 확인한다.
                    Path(self.h.settings.identity_file).write_text(json.dumps({'principals': [{
                        'subject': 'synthetic-tester', 'token': 'synthetic-human-only',
                        'scopes': ['incident:read', 'incident:create']}]}))
                self.assert_preserved_for_reconcile(incident, job)

    def test_report_without_heartbeat_after_revocation_is_rejected_not_completed(self):
        incident, job, _ = self.approved_prd_claim()
        self.revoke('prd-approver')
        response = self.h.report(job, self.written(job))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'job_authorization_revoked')
        # 거부 뒤 같은 결과 재보고도 완료로 바뀌지 않는다.
        self.assertEqual(self.h.report(job, self.written(job)).status_code, 409)
        self.assert_preserved_for_reconcile(incident, job)

    def test_reconcile_after_revocation_records_observed_write_without_promotion(self):
        incident, job, _ = self.approved_prd_claim()
        self.revoke('prd-approver')
        self.assertEqual(self.heartbeat(job).status_code, 409)
        reconcile = self.assert_preserved_for_reconcile(incident, job)
        self.assertEqual(self.h.report(reconcile, self.written(job)).status_code, 200)
        detail = self.h.detail(incident)
        self.assertEqual(detail['state'], 'handoff')
        self.assertEqual(detail['error']['code'], 'release_invalidated_during_write')
        prd = self.flow.release(incident)['stage_results']['prd']
        self.assertEqual(prd['write_outcome'], 'written')
        self.assertTrue(prd['deployed'])
        self.assertFalse(prd['verified'])
        self.assertEqual([t['write_outcome'] for t in prd['targets']], ['written'])
        self.assertEqual([t['image'] for t in prd['targets']], [NEW_IMAGE])
        self.assertEqual(self.job_status(job['id']), 'reconciled')
        self.assertIsNone(self.h.claim(['deploy', 'verify', 'reconcile']))

    def test_unresolved_reconcile_after_revocation_keeps_target_ownership(self):
        incident, job, _ = self.approved_prd_claim()
        self.revoke('prd-approver')
        self.assertEqual(self.heartbeat(job).status_code, 409)
        reconcile = self.assert_preserved_for_reconcile(incident, job)
        unknown = {'ok': False, 'write_outcome': 'unknown', 'targets': [], 'verification': [],
                   'error': 'write_outcome_unresolved'}
        self.assertEqual(self.h.report(reconcile, unknown).status_code, 200)
        detail = self.h.detail(incident)
        self.assertEqual(detail['state'], 'handoff')
        self.assertEqual(detail['error']['code'], 'release_invalidated_during_write')
        prd = self.flow.release(incident)['stage_results']['prd']
        self.assertEqual(prd['write_outcome'], 'unknown')
        self.assertEqual(prd['error_code'], 'write_outcome_unresolved')
        self.assertEqual(self.job_status(job['id']), 'reconciliation_required')
        self.assertIsNone(self.h.claim(['deploy', 'verify', 'reconcile']))

    def test_user_test_expiry_after_claim_rejects_report_and_preserves_ownership(self):
        incident, job, _ = self.approved_prd_claim()
        with self.h.app.state.store.transaction() as session:
            row = session.get(Incident, incident)
            document = copy.deepcopy(row.document)
            document['data']['release']['user_test']['expires_at'] = self.h.now
            row.document = document
        self.assertEqual(self.h.report(job, self.written(job)).status_code, 409)
        self.assert_preserved_for_reconcile(incident, job)

    def test_invalidated_deploy_keeps_target_locked_against_other_release(self):
        incident, job, _ = self.approved_prd_claim()
        self.revoke('prd-approver')
        self.assertEqual(self.heartbeat(job).status_code, 409)
        identity = Path(self.h.settings.identity_file)
        values = json.loads(identity.read_text())
        values['principals'][0]['scopes'].append('prd-approver')
        identity.write_text(json.dumps(values))
        # 원래 릴리스는 이미 무효화됐다. 권한 복구로 이를 되살리지 않으면서 새 사건의 정상 승인 경로를 시험한다.
        def create_other(service='order'):
            response = self.h.client.post(PREFIX + '/incidents', headers=HUMAN,
                json={'environment': 'dev', 'service': service, 'reason': '다른 prd 승격의 독립 장애'})
            self.assertEqual(response.status_code, 201)
            return response.json()['id']
        self.h.create = create_other
        other = self.flow.awaiting()
        self.assertNotEqual(incident, other)
        body = self.flow.approval_body(other)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{other}/approve-prd',
            headers=HUMAN, json=body).status_code, 200)
        detail = self.h.detail(other)
        self.assertEqual(detail['state'], 'handoff')
        self.assertEqual(detail['error']['code'], 'target_owned_by_other_release')
        from sqlalchemy import select
        with self.h.app.state.store.transaction() as session:
            jobs = list(session.scalars(select(Job).where(Job.incident_id == other, Job.kind == 'deploy')))
            self.assertEqual([j.payload['stage'] for j in jobs], ['dev'])
        self.assert_preserved_for_reconcile(incident, job)

    def test_valid_prd_heartbeat_and_report_still_complete(self):
        incident, job, _ = self.approved_prd_claim()
        self.assertEqual(self.heartbeat(job).status_code, 200)
        self.assertEqual(self.h.report(job, self.written(job)).status_code, 200)
        self.assertEqual(self.job_status(job['id']), 'completed')
        self.flow.verified('prd')
        self.assertEqual(self.h.detail(incident)['state'], 'recovery_review')


class CoreSafeResultEvidenceTests(FlowCase):
    """CORE-04: 실패한 시험·업무 확인과 성공 배포의 정제 필드를 사건 data와 /release에 보존한다."""

    def test_failed_test_build_preserves_sanitized_test_ledger(self):
        incident, job, result = self.flow.built()
        result['ok'] = False
        result['tests'].update(after_passed=False, failed=1, report_sha256='e' * 64, raw_log=RAW)
        result['tests']['executed_tests'] = [
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed',
             'phase': 'before', 'stdout': RAW},
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed',
             'phase': 'after', 'traceback': 'token=' + SECRET}]
        result['error'] = {'code': 'after_not_passed', 'message': 'Traceback token=' + SECRET}
        self.assertEqual(self.h.report(job, result).status_code, 200)
        detail = self.h.detail(incident)
        self.assertEqual(detail['state'], 'handoff')
        record = detail['data']['test_build']
        self.assertEqual(record['job_id'], job['id'])
        self.assertIs(record['accepted'], False)
        self.assertIs(record['ok'], False)
        self.assertEqual(record['rejection_code'], 'test_build_evidence_invalid')
        self.assertEqual(record['error_code'], 'after_not_passed')
        tests = record['tests']
        self.assertEqual(tests['report_sha256'], 'e' * 64)
        self.assertIs(tests['after_passed'], False)
        self.assertIs(tests['before_failed'], True)
        self.assertEqual(tests['failed'], 1)
        self.assertEqual(tests['total'], 2)
        self.assertEqual(tests['executed_tests'], [
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed', 'phase': 'before'},
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed', 'phase': 'after'}])
        released = self.release_response(incident)
        self.assertIsNone(released['release'])
        self.assertEqual(released['test_build'], record)
        blob = self.everything(incident)
        self.assertNotIn(RAW, blob)
        self.assertNotIn(SECRET, blob)
        self.assertIsNone(self.h.claim(['collect', 'deploy']))

    def test_contradicting_after_failure_row_is_kept_as_evidence(self):
        incident, job, result = self.flow.built()
        extra = {'id': 'tests/test_existing.py::test_failure', 'file': 'tests/test_existing.py',
                 'status': 'failed', 'phase': 'after'}
        result['tests']['executed_tests'].append(extra)
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'handoff')
        record = self.release_response(incident)['test_build']
        self.assertIs(record['accepted'], False)
        self.assertTrue(extra in record['tests']['executed_tests'])
        self.assertEqual(record['tests']['failed'], 0)

    def test_unsafe_or_malformed_failure_fields_are_dropped_not_stored(self):
        incident, job, result = self.flow.built()
        result['ok'] = False
        result['tests']['report_sha256'] = 'not-a-digest ' + SECRET
        result['tests']['executed_tests'] = [{'id': '../../etc/passwd', 'file': RAW, 'status': 'failed', 'phase': 'after'},
                                             'tests/test_main.py::test_run', 42]
        result['error'] = 'Traceback (most recent call last) token=' + SECRET
        self.assertEqual(self.h.report(job, result).status_code, 200)
        record = self.h.detail(incident)['data']['test_build']
        self.assertEqual(record['error_code'], 'error_code_invalid')
        self.assertIsNone(record['tests']['report_sha256'])
        self.assertEqual(record['tests']['executed_tests'], [])
        self.assertEqual(record['tests']['rows_rejected'], 3)
        blob = self.everything(incident)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(RAW, blob)
        self.assertNotIn('etc/passwd', blob)

    def test_successful_test_build_record_matches_release_ledger(self):
        incident, job, result = self.flow.built()
        self.assertEqual(self.h.report(job, result).status_code, 200)
        released = self.release_response(incident)
        record = released['test_build']
        self.assertIs(record['accepted'], True)
        self.assertIsNone(record['rejection_code'])
        self.assertIsNone(record['error_code'])
        self.assertEqual(record['tests']['report_sha256'], '4' * 64)
        self.assertEqual(record['tests']['executed_tests'], released['release']['tests']['executed_tests'])

    def test_failed_business_verification_preserves_safe_rows_and_error_code(self):
        cases = {
            'rows': ([{'environment': 'dev', 'service': 'order', 'image': NEW_IMAGE, 'raw_response': RAW,
                       'business': {'ok': False, 'profile_id': 'order', 'status_code': 500,
                                    'body_sha256': 'b' * 64, 'body': RAW}}], 'business_status_unexpected',
                     [{'environment': 'dev', 'service': 'order', 'image': NEW_IMAGE,
                       'business': {'ok': False, 'profile_id': 'order', 'status_code': 500, 'body_sha256': 'b' * 64}}],
                     'business_status_unexpected'),
            'no_rows': (None, {'code': 'rollout_not_observed', 'message': RAW}, [], 'rollout_not_observed'),
            'unsafe_error': ([], 'Traceback token=' + SECRET, [], 'error_code_invalid'),
        }
        for name, (rows, error, saved_rows, saved_code) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                incident, job, result = self.flow.built()
                self.assertEqual(self.h.report(job, result).status_code, 200)
                self.flow.collect_stage('dev')
                self.flow.deployed('dev')
                verify = self.h.claim(['verify'])
                self.assertIsNotNone(verify)
                self.assertEqual(self.h.report(verify, {'ok': False, 'write_outcome': 'not_written',
                    'verification': rows, 'error': error}).status_code, 200)
                self.assertEqual(self.h.detail(incident)['state'], 'failed')
                dev = self.release_response(incident)['release']['stage_results']['dev']
                self.assertIs(dev['deployed'], True)
                self.assertIs(dev['verified'], False)
                self.assertEqual(dev['verification'], saved_rows)
                self.assertEqual(dev['error_code'], saved_code)
                self.assertEqual(dev['verify_job_id'], verify['id'])
                blob = self.everything(incident)
                self.assertNotIn(RAW, blob)
                self.assertNotIn(SECRET, blob)
                self.assertIsNone(self.h.claim(['collect', 'deploy', 'verify']))

    def test_successful_deploy_preserves_write_outcome_without_raw_fields(self):
        incident, job, result = self.flow.built()
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.flow.collect_stage('dev')
        deploy = self.h.claim(['deploy'])
        self.assertEqual(self.h.report(deploy, self.written(deploy, raw_patch=RAW)).status_code, 200)
        released = self.release_response(incident)
        dev = released['release']['stage_results']['dev']
        self.assertEqual(dev['write_outcome'], 'written')
        self.assertEqual(dev['deploy_job_id'], deploy['id'])
        self.assertEqual([t['write_outcome'] for t in dev['targets']], ['written'])
        self.assertEqual(dev['targets'][0]['previous_image'], deploy['payload']['targets'][0]['previous_image'])
        self.assertEqual(self.h.detail(incident)['data']['release']['stage_results']['dev'], dev)
        self.assertNotIn(RAW, self.everything(incident))
        self.flow.verified('dev')
        dev = self.release_response(incident)['release']['stage_results']['dev']
        self.assertEqual(dev['write_outcome'], 'written')
        self.assertTrue(dev['verified'])


class CoreDatabaseBootstrapTests(unittest.TestCase):
    """신규 빈 독립 DB 스키마를 lifespan 또는 명시 명령에서 멱등 초기화한다."""

    def settings(self, path, **extra):
        from app.application.settings import ApplicationSettings
        return ApplicationSettings(database_url=f'sqlite:///{path}', testing=True, **extra)

    @staticmethod
    def tables(path):
        with contextlib.closing(sqlite3.connect(path)) as db:
            return {row[0] for row in db.execute("select name from sqlite_master where type='table'")}

    def test_lifespan_initializes_fresh_isolated_store_idempotently(self):
        from app.application.main import create_app
        with tempfile.TemporaryDirectory(prefix='lp-app-boot-', dir=os.environ.get('TMPDIR')) as td:
            path = Path(td) / 'fresh.db'
            for _ in range(2):
                app = create_app(self.settings(path))
                try:
                    with TestClient(app) as client:
                        ready = client.get(PREFIX + '/readyz').json()
                        self.assertTrue(ready['checks']['database'])
                finally:
                    app.state.store.engine.dispose()
            self.assertEqual(self.tables(path), TABLES)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('select count(*) from application_mutex').fetchone()[0], 1)

    def test_initialization_failure_marks_database_false_then_recovers(self):
        """정돈-02: readyz 조회는 초기화를 하지 않는다. 복구는 lifespan의 재시도 스레드가 맡는다."""
        import time
        from app.application.main import create_app
        with tempfile.TemporaryDirectory(prefix='lp-app-boot-', dir=os.environ.get('TMPDIR')) as td:
            missing = Path(td) / 'not-yet-mounted'
            path = missing / 'later.db'
            app = create_app(self.settings(path, database_init_retry_seconds=0))
            try:
                with TestClient(app) as client:
                    self.assertEqual(client.get(PREFIX + '/healthz').status_code, 200)
                    first = client.get(PREFIX + '/readyz')
                    self.assertEqual(first.status_code, 503)
                    self.assertIs(first.json()['checks']['database'], False)
                    self.assertNotIn(td, first.text)
                    callers, real = [], app.state.store.bootstrap
                    def spy():
                        callers.append(threading.current_thread().name)
                        return real()
                    with patch.object(app.state.store, 'bootstrap', side_effect=spy):
                        for _ in range(3):
                            self.assertIs(client.get(PREFIX + '/readyz').json()['checks']['database'], False)
                        missing.mkdir()
                        deadline = time.monotonic() + 10
                        while not app.state.store.ready() and time.monotonic() < deadline:
                            time.sleep(0.05)
                    # Only the lifespan retry thread initialized; no request thread did.
                    self.assertTrue(callers)
                    self.assertEqual(set(callers), {'application-db-retry'})
                    second = client.get(PREFIX + '/readyz')
                    self.assertIs(second.json()['checks']['database'], True)
            finally:
                app.state.store.engine.dispose()
            self.assertEqual(self.tables(path), TABLES)

    def test_bootstrap_error_is_not_hidden_by_existing_incident_table(self):
        from app.application.main import create_app
        with tempfile.TemporaryDirectory(prefix='lp-app-boot-', dir=os.environ.get('TMPDIR')) as td:
            app = create_app(self.settings(Path(td) / 'partial.db'))
            try:
                app.state.store.initialize()
                self.assertTrue(app.state.store.ready())
                with patch.object(app.state.store, 'initialize', side_effect=RuntimeError('원문 비노출 시험')):
                    with TestClient(app) as client:
                        ready = client.get(PREFIX + '/readyz')
                        self.assertEqual(ready.status_code, 503)
                        self.assertIs(ready.json()['checks']['database'], False)
                        self.assertNotIn('원문 비노출 시험', ready.text)
                self.assertTrue(app.state.store.bootstrap())
                self.assertTrue(app.state.store.ready())
            finally:
                app.state.store.engine.dispose()

    def test_explicit_initialize_command_is_idempotent_and_refuses_unisolated_database(self):
        from app.application import bootstrap
        from app.application.settings import ApplicationSettings
        with tempfile.TemporaryDirectory(prefix='lp-app-boot-', dir=os.environ.get('TMPDIR')) as td:
            path = Path(td) / 'command.db'
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = bootstrap.main([], settings=self.settings(path))
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out.getvalue()), {'ok': True, 'code': 'database_initialized'})
            self.assertEqual(self.tables(path), TABLES)
            never = Path(td) / 'never.db'
            refused = (ApplicationSettings(database_url='postgresql://synthetic@127.0.0.1/liveprobe', isolated_database=True),
                       ApplicationSettings(database_url='postgresql://synthetic@127.0.0.1/liveprobe_application'),
                       ApplicationSettings(database_url=f'sqlite:///{never}'),
                       ApplicationSettings())
            for settings in refused:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = bootstrap.main([], settings=settings)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out.getvalue()), {'ok': False, 'code': 'database_unconfigured'})
                self.assertNotIn('127.0.0.1', out.getvalue())
            self.assertFalse(never.exists())


class CoreModelDefaultTests(unittest.TestCase):
    """보완 계약 §5: 분석·수정안 gpt-6.2-sol/medium, 반론 gpt-6.2-astra/high, 검증 gpt-6.2-sol/medium, 분류 gpt-6-luna/low."""
    EXPECTED = {'classifier': ('gpt-6-luna', 'low'), 'analyst': ('gpt-6.2-sol', 'medium'),
                'specialist': ('gpt-6.2-sol', 'medium'), 'patch': ('gpt-6.2-sol', 'medium'),
                'critic': ('gpt-6.2-astra', 'high'), 'verifier': ('gpt-6.2-sol', 'medium')}

    def test_role_defaults_follow_addendum_lineup(self):
        from app.application.settings import ApplicationSettings
        with patch.dict(os.environ, {}, clear=True):
            from_env = ApplicationSettings.from_env()
        for settings in (ApplicationSettings(), from_env):
            self.assertEqual(settings.model, 'gpt-6.2-sol')
            self.assertEqual({role: (settings.model_for(role), settings.effort_for(role)) for role in self.EXPECTED},
                             self.EXPECTED)


if __name__ == '__main__':
    unittest.main()
