"""장애 캠페인 회귀: 실제 루프백 HTTP와 런타임 상태를 검사한다."""
from test_contract import HttpCase, app
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch


class CampaignRuntimeTests(HttpCase):
    def test_ttl_expiry_releases_memory(self):
        runtime = self.server.runtime
        self.request('/api/chaos', 'POST', {'fault': 'memory_leak', 'ttl_seconds': 1,
                                          'params': {'mib_per_second': 1}})
        runtime.tick()
        self.assertEqual(len(runtime.allocations), 1)
        time.sleep(1.05)
        self.assertNotIn('memory_leak', self.request('/api/chaos')[1]['active'])
        self.assertEqual(runtime.allocations, [])

    def test_worker_toggle_uses_retired_generation_event(self):
        runtime = self.server.runtime
        self.chaos('worker_exhaustion')
        retired = runtime.holder_stop
        old = list(runtime.holders)
        self.chaos('worker_exhaustion', False)
        self.chaos('worker_exhaustion')
        self.assertIsNot(runtime.holder_stop, retired)
        self.assertTrue(retired.is_set())
        self.chaos('worker_exhaustion', False)
        for thread in old:
            thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_enabling_batch_fault_resets_existing_job_deadline(self):
        runtime = self.server.runtime
        runtime.tick()
        self.chaos('background_job_failure', params={'interval_seconds': 1})
        runtime.tick()
        self.assertEqual(runtime.errors['SettlementError'], 1)

    def test_runtime_delete_unlinks_persisted_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.server.runtime
            runtime.state_dir = directory
            self.chaos('not_ready')
            path = Path(directory) / 'faults.json'
            self.assertTrue(path.exists())
            self.request('/api/chaos', 'DELETE')
            self.assertFalse(path.exists())

    def test_release_seed_stays_healthy_and_delete_cannot_mask_release(self):
        release = {'faults': {'code_bug': {'params': {}}, 'latency': {'params': {'seconds': 0.01}}}}
        with patch.dict(os.environ, {'RELEASE_FAULTS': json.dumps(release)}):
            server = app.create_server('127.0.0.1', 0, background=False)
        try:
            runtime = server.runtime
            runtime.set_fault({'fault': 'latency', 'params': {'seconds': 0}})
            row = next(row for row in runtime.chaos()['faults'] if row['name'] == 'latency')
            self.assertEqual(row['source'], 'release')
            self.assertEqual(runtime.params_of('latency')['seconds'], 0.01)
            runtime.clear()
            self.assertTrue(runtime.enabled('code_bug'))
        finally:
            server.runtime.stop.set()
            server.server_close()

    def test_traffic_includes_actual_cancel_route(self):
        business = self.server.runtime.business
        paths = [business.traffic()[1] for _ in range(12)]
        self.assertTrue(any(path.endswith('/cancel') for path in paths))

    def test_old_duplicate_charge_remains_invalid_until_reconcile(self):
        business = self.server.runtime.business
        self.chaos('business_invariant')
        payment = business.create_payment({'order_id': '오래된 중복', 'amount': 12000})
        business.find(payment['id'])['created_at'] = '2000-01-01T00:00:00+00:00'
        self.chaos('business_invariant', False)
        with self.assertRaises(app.DuplicateChargeError):
            business.check_invariants()
        business.reconcile()
        business.check_invariants()

    def test_evicted_duplicate_charge_remains_invalid_until_reconcile(self):
        business = self.server.runtime.business
        self.chaos('business_invariant')
        business.create_payment({'order_id': '이력 밖 중복', 'amount': 12000})
        self.chaos('business_invariant', False)
        for index in range(101):
            business.create_payment({'order_id': '정상-' + str(index), 'amount': 1})
        charged = business.charged
        with self.assertRaises(app.DuplicateChargeError):
            business.check_invariants()
        business.reconcile()
        self.assertEqual(business.charged, charged - 12000)
        business.check_invariants()

    def test_cancel_validation_fails_before_mutation(self):
        self.chaos('code_bug_validation')
        status, body, _ = self.request('/api/payments/1/cancel', 'POST', {})
        self.assertEqual(status, 500)
        self.assertEqual(body['exception'], 'KeyError')
        self.assertEqual(self.server.runtime.business.find('1')['status'], 'paid')
        self.chaos('code_bug_validation', False)
        self.assertEqual(self.request('/api/payments/1/cancel', 'POST', {})[0], 200)


