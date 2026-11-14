"""제어면 heartbeat와 실행기 쓰기 직전 가드를 연결한다. K8s 전송만 대체하며 실제 외부 쓰기는 없다."""
import json
from pathlib import Path
import time
import unittest

import test_core_release as release_tests
from test_core_analysis import SyntheticTransport
from test_core_support import Harness, HUMAN, MACHINE, PREFIX
from liveprobe.application_runner.worker import LeaseGuard
from liveprobe.application_runner.deploy import KubeTransport


class ClientTransport:
    def __init__(self, client):
        self.client = client
        self.statuses = []

    def post(self, path, body, timeout=20):
        response = self.client.post(PREFIX + path, headers=MACHINE, json=body)
        self.statuses.append(response.status_code)
        if response.status_code != 200:
            raise OSError('application_api_transport_failed')
        return response.json()


class RawKube:
    def __init__(self):
        self.writes = 0

    def call_api(self, path, method, **kwargs):
        if method == 'PATCH':
            self.writes += 1
        return {}


class GuardApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(SyntheticTransport())
        self.addCleanup(self.h.close)
        flow = release_tests.CoreReleaseTests()
        flow.h = self.h
        self.incident = flow.awaiting()
        approval = self.h.client.post(PREFIX + f'/incidents/{self.incident}/approve-prd', headers=HUMAN,
                                     json=flow.approval_body(self.incident))
        self.assertEqual(approval.status_code, 200)
        self.job = self.h.claim(['deploy'])
        self.api = ClientTransport(self.h.client)
        auth = {key: self.job[key] for key in ('lease_token', 'fencing_token')}
        self.guard = LeaseGuard(self.api, f'/runner/jobs/{self.job["id"]}/heartbeat', auth,
                                self.job, wall_clock=lambda: self.h.now, monotonic=time.monotonic)
        self.assertTrue(self.guard.refresh())
        self.raw = RawKube()
        self.kube = KubeTransport(self.raw)
        self.kube.guard = self.guard
        self.kube.write_guard = self.guard.confirm_write

    def test_authorized_server_heartbeat_permits_the_transport(self):
        self.kube.patch('prd', 'order', [])
        self.assertEqual(self.raw.writes, 1)
        self.assertEqual(self.api.statuses[-1], 200)

    def test_revoked_server_authorization_blocks_transport_and_keeps_reconciliation(self):
        # 시험 장치가 만든 합성 identity만 변경한다. 운영 인증 파일은 접근하지 않는다.
        path = Path(self.h.settings.identity_file)
        identity = json.loads(path.read_text())
        identity['principals'][0]['scopes'].remove('prd-approver')
        path.write_text(json.dumps(identity))
        with self.assertRaisesRegex(ValueError, 'lease_lost'):
            self.kube.patch('prd', 'order', [])
        self.assertEqual(self.raw.writes, 0)
        self.assertEqual(self.api.statuses[-1], 409)
        self.assertEqual(self.h.detail(self.incident)['state'], 'reconciliation_required')
        self.assertIsNone(self.h.claim(['deploy', 'verify']))
        self.assertEqual(self.h.claim(['reconcile'])['kind'], 'reconcile')


if __name__ == '__main__':
    unittest.main()
