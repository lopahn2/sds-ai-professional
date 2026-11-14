import copy
import unittest
from test_core_support import Harness, HUMAN, MACHINE, PREFIX


class CoreQueueTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_claim_fence_idempotent_report_and_provenance_handoff(self):
        incident = self.h.create()
        self.assertEqual(self.h.detail(incident)['state'], 'collecting')
        job = self.h.claim(['collect'])
        self.assertEqual(job['kind'], 'collect')
        self.assertTrue(self.h.claim(['collect']) is None)
        bad = dict(job, fencing_token=job['fencing_token'] + 1)
        self.assertEqual(self.h.report(bad, self.h.collect()).status_code, 409)
        first = self.h.report(job, self.h.collect())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.h.report(job, self.h.collect()).json(), first.json())
        altered = self.h.collect()
        altered['ok'] = False
        self.assertEqual(self.h.report(job, altered).status_code, 409)
        source_job = self.h.claim(['source'])
        self.assertEqual(source_job['payload']['target']['image'], job['payload'].get('target', self.h.collect()['target'])['image'])
        self.assertEqual(self.h.report(source_job, self.h.source(False)).status_code, 200)
        detail = self.h.detail(incident)
        self.assertEqual(detail['state'], 'handoff')
        self.assertEqual(detail['error']['code'], 'source_provenance_unverified')
        self.assertTrue(self.h.claim(['test_build', 'deploy']) is None)
        text = str(detail) + str(first.json())
        self.assertNotIn('synthetic-sensitive-value', text)
        events = self.h.client.get(PREFIX + f'/incidents/{incident}/events', headers=HUMAN).json()['events']
        self.assertGreaterEqual(len(events), 4)

    def test_machine_cannot_attest_or_approve_and_identity_missing_closed(self):
        incident = self.h.create()
        for suffix in ('user-test', 'approve-prd'):
            response = self.h.client.post(PREFIX + f'/incidents/{incident}/' + suffix, headers=MACHINE, json={})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.h.client.get(PREFIX + '/incidents', headers=MACHINE).status_code, 403)
        self.assertEqual(self.h.client.post(PREFIX + '/runner/claim', headers=HUMAN,
            json={'worker_id': 'x', 'capabilities': ['collect']}).status_code, 403)
        self.assertEqual(self.h.client.post(PREFIX + '/incidents', headers=HUMAN,
            json={'environment': 'dev', 'service': 'order', 'reason': 'x', 'system': 'override'}).status_code, 422)

    def test_read_lease_expiry_can_reclaim_and_survives_app_restart(self):
        from app.application.main import create_app
        from fastapi.testclient import TestClient
        incident = self.h.create()
        job = self.h.claim(['collect'])
        self.h.now += 31
        other = create_app(self.h.settings, clock=lambda: self.h.now)
        with TestClient(other) as client:
            response = client.post(PREFIX + '/runner/claim', headers=MACHINE,
                json={'worker_id': 'new', 'capabilities': ['collect']})
            newer = response.json()['job']
            self.assertEqual(newer['id'], job['id'])
            self.assertGreater(newer['fencing_token'], job['fencing_token'])
            self.assertEqual(self.h.report(job, self.h.collect()).status_code, 409)
        other.state.store.engine.dispose()


if __name__ == '__main__':
    unittest.main()
