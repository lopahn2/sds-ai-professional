"""장애·위조·경합 경계의 테스트 우선 추가 회귀."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import unittest
from test_core_analysis import SyntheticTransport
from test_core_release import CoreReleaseTests
from test_core_support import Harness, HUMAN, MACHINE, PREFIX


class CoreBoundaryTests(unittest.TestCase):
    def test_raw_credentials_and_arbitrary_result_extensions_never_persist(self):
        h = Harness()
        self.addCleanup(h.close)
        incident = h.create()
        result = h.collect()
        result['logs'][0]['debug_blob'] = 'never-persist-this-arbitrary-field'
        result['evidence'][0]['opaque_payload'] = 'never-persist-this-arbitrary-field'
        result['extra'] = 'never-persist-this-arbitrary-field'
        h.report(h.claim(['collect']), result)
        self.assertNotIn('never-persist-this-arbitrary-field', json.dumps(h.detail(incident)))

    def test_production_settings_require_isolated_postgres_and_explicit_init(self):
        from app.application.settings import ApplicationSettings
        from app.application.storage import Store
        sqlite = Store(ApplicationSettings(database_url='sqlite:///:memory:'))
        self.assertIsNone(sqlite.engine)
        old = Store(ApplicationSettings(database_url='postgresql://synthetic:synthetic@127.0.0.1/liveprobe', isolated_database=True))
        self.assertIsNone(old.engine)
        app_store = Store(ApplicationSettings(database_url='postgresql://synthetic:synthetic@127.0.0.1/liveprobe_application_test', isolated_database=True))
        self.assertIsNotNone(app_store.engine)
        self.assertEqual(app_store.engine.dialect.name, 'postgresql')
        # create_engine는 연결하지 않는다. 실제 PostgreSQL 접속/initialize는 호출하지 않는다.
        app_store.engine.dispose()

    def test_concurrent_claim_has_one_durable_winner(self):
        h = Harness()
        self.addCleanup(h.close)
        h.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _: h.claim(['collect']), range(2)))
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_unknown_result_never_throws_or_returns_raw_data(self):
        h = Harness(SyntheticTransport())
        self.addCleanup(h.close)
        incident = h.seed_source()
        h.app.state.worker.run_once()
        result = h.report(h.claim(['test_build']), {'ok': True, 'tests': {'executed_tests': [{'file': 'x'}]}})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(h.detail(incident)['state'], 'handoff')

    def test_real_profiles_and_raw_catalog_hash_are_in_jobs(self):
        from app.application.main import create_app
        from app.application.settings import ApplicationSettings
        from fastapi.testclient import TestClient
        import hashlib
        h = Harness()
        self.addCleanup(h.close)
        root = Path(__file__).resolve().parents[2]
        settings = ApplicationSettings(database_url=h.settings.database_url, testing=True,
            identity_file=h.settings.identity_file, machine_identity_file=h.settings.machine_identity_file,
            profiles_file=str(root / 'deploy/samsung/application-profiles.json'),
            catalog_file=str(root / 'deploy/samsung/system-catalog.json'))
        app = create_app(settings, clock=lambda: h.now)
        with TestClient(app) as client:
            response = client.post(PREFIX + '/incidents', headers=HUMAN,
                json={'environment': 'dev', 'service': 'order', 'reason': '합성 시험'})
            self.assertEqual(response.status_code, 201)
            job = client.post(PREFIX + '/runner/claim', headers=MACHINE,
                json={'worker_id': 'synthetic', 'capabilities': ['collect']}).json()['job']
            self.assertEqual(job['payload']['catalog_sha256'], hashlib.sha256((root / 'deploy/samsung/system-catalog.json').read_bytes()).hexdigest())
            client.post(PREFIX + '/runner/jobs/' + job['id'] + '/result', headers=MACHINE,
                json={'lease_token': job['lease_token'], 'fencing_token': job['fencing_token'], 'result': h.collect()})
            source_job = client.post(PREFIX + '/runner/claim', headers=MACHINE,
                json={'worker_id': 'synthetic', 'capabilities': ['source']}).json()['job']
            self.assertEqual(source_job['payload']['profile_id'], 'samsung-order-v1')
        app.state.store.engine.dispose()


class CoreApprovalBoundaryTests(CoreReleaseTests):
    # 상위 시험도 재실행하여 공통 장치 변경의 통합 영향 확인.
    def test_revoked_identity_blocks_previously_approved_deploy(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 200)
        Path(self.h.settings.identity_file).unlink()
        self.assertTrue(self.h.claim(['deploy']) is None)

    def test_target_fingerprint_change_cancels_queued_prd(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 200)
        from app.application.storage import Incident
        with self.h.app.state.store.transaction() as session:
            row = session.get(Incident, incident)
            document = copy.deepcopy(row.document)
            document['data']['release']['targets']['prd'][0]['resource_version'] = '999'
            row.document = document
        self.assertTrue(self.h.claim(['deploy']) is None)

    def test_source_lost_after_stg_cannot_attest(self):
        incident = self.awaiting()
        self.h.app.state.control.invalidate_release(incident, 'source_changed')
        release = self.release(incident)
        self.assertFalse(release['valid'])
        self.assertTrue(self.h.claim(['deploy']) is None)
