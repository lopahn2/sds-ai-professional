"""장애 캠페인 실제 HTTP 회귀 시험."""
from test_contract import HttpCase, app


class CampaignTests(HttpCase):
    def test_detail_validation_fault_raises_distinct_stack_then_recovers(self):
        self.assertEqual(self.chaos('code_bug_validation')[0], 200)
        status, body, _ = self.request('/api/products/1')
        self.assertEqual(status, 500)
        self.assertEqual(body['exception'], 'TypeError')
        self.assertIn('in product_detail', self.output.getvalue())
        self.chaos('code_bug_validation', False)
        self.assertEqual(self.request('/api/products/1')[0], 200)

    def test_price_response_serialization_fault_then_recovers(self):
        self.chaos('code_bug_serialization')
        status, body, _ = self.request('/api/products/1/price', 'POST', {'price': 81000})
        self.assertEqual(status, 500)
        self.assertEqual(body['exception'], 'ValueError')
        self.assertIn('in price_response', self.output.getvalue())
        self.chaos('code_bug_serialization', False)
        status, body, _ = self.request('/api/products/1/price', 'POST', {'price': 81000})
        self.assertEqual(status, 200)
        self.assertEqual(body['product']['price'], 81000)

    def test_business_invariant_corrupts_cache_until_explicit_reconcile(self):
        self.chaos('business_invariant')
        self.assertEqual(self.request('/api/products/1')[1]['product']['price'], 7900)
        runtime = self.server.runtime
        runtime.tick()
        self.assertEqual(runtime.errors['PriceConsistencyError'], 1)
        self.chaos('business_invariant', False)
        self.assertEqual(self.request('/api/products/1')[1]['product']['price'], 7900)
        self.assertEqual(self.request('/api/products/reconcile', 'POST', {})[0], 200)
        self.assertEqual(self.request('/api/products/1')[1]['product']['price'], 79000)
        runtime.business.check_invariants()

    def test_batch_fault_has_business_stack_and_recovers(self):
        runtime = self.server.runtime
        self.chaos('background_job_failure', params={'interval_seconds': 1})
        runtime.tick()
        self.assertEqual(runtime.errors['IndexRebuildError'], 1)
        self.assertIn('IndexRebuildError', self.output.getvalue())
        self.assertEqual(self.request(runtime.business.api_prefix)[0], 200)
        self.chaos('background_job_failure', False)
        runtime.business.background_job()

    def test_dependency_fault_is_real_connection_failure_and_502(self):
        self.chaos('dependency_failure')
        status, body, _ = self.request('/api/products/1', 'GET', None)
        self.assertEqual(status, 502)
        self.assertEqual(body['exception'], 'DependencyError')
        self.assertIn('ConnectionRefusedError', self.output.getvalue())
        self.chaos('dependency_failure', False)
        self.assertIn(self.request('/api/products/1', 'GET', None)[0], (200, 202))

    def test_new_faults_are_exposed_by_control_api(self):
        names = {row['name'] for row in self.request('/api/chaos')[1]['faults']}
        self.assertTrue({'code_bug_validation', 'code_bug_serialization', 'business_invariant',
                         'background_job_failure', 'dependency_failure', 'startup_config_error',
                         'cpu_spin', 'worker_exhaustion', 'liveness_fail'}.issubset(names))
