"""독립 검토 결함의 제어면 회귀. 실제 API+폐기 SQLite, 합성 인증만 사용."""
import unittest
from sqlalchemy import select
from test_core_support import HUMAN, MACHINE, PREFIX
from test_core_release import CoreReleaseTests
from app.application.storage import Job


class CoreReviewFixes(unittest.TestCase):
    def setUp(self):
        self.flow = CoreReleaseTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.h = self.flow.h

    def test_unknown_report_retains_target_ownership_until_reconciled(self):
        first, build, built = self.flow.built()
        self.assertEqual(self.h.report(build, built).status_code, 200)
        self.flow.collect_stage('dev')
        deploy = self.h.claim(['deploy'])
        unknown = {'ok': False, 'write_outcome': 'unknown'}
        self.assertEqual(self.h.report(deploy, unknown).status_code, 200)
        self.assertEqual(self.h.report(deploy, unknown).status_code, 200)
        with self.h.app.state.store.transaction() as session:
            self.assertEqual(session.get(Job, deploy['id']).status, 'reconciliation_required')
        def create_other(service='order'):
            response = self.h.client.post(PREFIX+'/incidents', headers=HUMAN,
                json={'environment':'dev','service':service,'reason':'두 번째 독립 장애'})
            self.assertEqual(response.status_code, 201)
            return response.json()['id']
        self.h.create = create_other
        second, build, built = self.flow.built()
        self.assertNotEqual(first, second)
        self.assertEqual(self.h.report(build, built).status_code, 200)
        self.flow.collect_stage('dev')
        self.assertIsNone(self.h.claim(['deploy']))
        self.assertEqual(self.h.detail(first)['state'], 'reconciliation_required')

    def test_after_failed_row_cannot_be_hidden_by_zero_aggregate(self):
        incident, job, result = self.flow.built()
        result['tests']['executed_tests'].append({'id':'tests/test_existing.py::test_failure',
            'file':'tests/test_existing.py','status':'failed','phase':'after'})
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'handoff')
        self.assertIsNone(self.h.claim(['deploy','collect']))

    def test_prd_claim_lease_cannot_outlive_user_approval(self):
        incident = self.flow.awaiting()
        body = self.flow.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX+f'/incidents/{incident}/approve-prd',
            headers=HUMAN, json=body).status_code, 200)
        expires = self.flow.release(incident)['approval']['expires_at']
        self.h.now = expires - 15
        job = self.h.claim(['deploy'])
        self.assertLessEqual(job['lease_expires_at'], expires)

    def test_revoked_prd_approver_stops_heartbeat_and_preserves_unknown_job(self):
        import json
        from pathlib import Path
        incident = self.flow.awaiting()
        body = self.flow.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX+f'/incidents/{incident}/approve-prd',
            headers=HUMAN, json=body).status_code, 200)
        job = self.h.claim(['deploy'])
        identity = Path(self.h.settings.identity_file)
        values = json.loads(identity.read_text())
        values['principals'][0]['scopes'].remove('prd-approver')
        identity.write_text(json.dumps(values))
        response = self.h.client.post(PREFIX+f'/runner/jobs/{job["id"]}/heartbeat', headers=MACHINE,
            json={k:job[k] for k in ('lease_token','fencing_token')})
        self.assertEqual(response.status_code, 409)
        with self.h.app.state.store.transaction() as session:
            self.assertEqual(session.get(Job,job['id']).status, 'reconciliation_required')
        self.assertIsNone(self.h.claim(['deploy']))
        self.assertIsNotNone(self.h.claim(['reconcile']))
