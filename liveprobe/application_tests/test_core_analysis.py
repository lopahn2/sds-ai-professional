"""모델 transport만 바꿔 실제 durable 분석 coordinator를 검증한다."""
import copy
import importlib
import json
import unittest
from app.llm.codex_cli import LLMResult, Usage
from test_core_support import Harness

CITATIONS = [{'ref': 'E1-order', 'quote': 'order failed'},
             {'ref': 'source:src/main.py', 'quote': 'return 1 / 0'}]
DIFF = '--- a/src/main.py\n+++ b/src/main.py\n@@ -1,2 +1,2 @@\n def run():\n-    return 1 / 0\n+    return 0\n--- /dev/null\n+++ b/tests/test_main.py\n@@ -0,0 +1,2 @@\n+def test_run():\n+    assert run() == 0\n'


class SyntheticTransport:
    name = 'synthetic-transport'

    def __init__(self, level=1, confidence=.95, citations=True, risk='low'):
        self.level, self.confidence, self.citations, self.risk = level, confidence, citations, risk
        self.calls = []

    def complete(self, role, prompt, schema, model, effort, timeout):
        self.calls.append({'role': role, 'prompt': prompt, 'schema': schema, 'model': model, 'effort': effort})
        citations = copy.deepcopy(CITATIONS) if self.citations else []
        if role == 'classifier':
            data = {'level': self.level, 'risk': self.risk, 'reason_ko': '앱 계산 오류',
                    'confidence': .95, 'citations': citations}
        elif role == 'patch':
            data = {'source_snapshot_id': 'snapshot-one', 'diff': DIFF,
                    'summary_ko': '0 나눗셈 제거', 'risk_ko': '동작 확인 필요',
                    'tests_added': ['tests/test_main.py::test_run'], 'code_refs': ['src/main.py']}
        elif role in ('critic', 'verifier'):
            data = {'accepted': True, 'confidence': self.confidence, 'unresolved': [],
                    'summary_ko': '원인·수정 가능성 확인', 'citations': citations}
        else:
            data = {'summary_ko': '0으로 나누는 코드', 'confidence': self.confidence,
                    'fixable': True, 'unresolved': [], 'citations': citations}
        return LLMResult(text=json.dumps(data), data=data, usage=Usage(input_tokens=10, output_tokens=20),
                         provider=self.name, model=model, effort=effort)


class CoreAnalysisTests(unittest.TestCase):
    def test_missing_controller_contract_uses_registered_service_and_test_root(self):
        from pathlib import Path
        from app.application.catalog import Catalog
        from app.application.settings import ApplicationSettings
        root = Path(__file__).resolve().parents[2] / 'deploy' / 'samsung'
        catalog = Catalog(ApplicationSettings(catalog_file=str(root / 'system-catalog.json'),
                                               profiles_file=str(root / 'application-profiles.json')))
        expected = {'order': ('src/test/java/io/liveprobe/target/order/OrderControllerTest.java', 'orderable'),
                    'catalog': ('tests/test_business.py', '상품 상세'),
                    'payment': ('tests/test_business.py', '결제 ID'),
                    'notification': ('tests/test_business.py', 'queue_depth'),
                    'delivery': ('test/index.js', 'delayed=true')}
        for service, (path, predicate) in expected.items():
            requirement = catalog.missing_controller_test(service)
            self.assertEqual(requirement['profile_id'], catalog.profiles[service]['id'])
            self.assertEqual(requirement['test_file'], path)
            self.assertTrue(requirement['test_cases'])
            self.assertIn(predicate, ' '.join(requirement['expectations_ko']))
        self.assertIsNone(catalog.missing_controller_test('inventory'))
        catalog.profiles['catalog']['test_root'] = '../outside'
        self.assertIsNone(catalog.missing_controller_test('catalog'))

    def worker(self, h):
        try:
            importlib.import_module('app.application.worker')
        except ModuleNotFoundError:
            self.fail('실제 Application 분석 worker가 필요하다')
        return h.app.state.worker

    def test_real_worker_roles_and_patch_enqueue(self):
        provider = SyntheticTransport(level=2)
        h = Harness(provider)
        self.addCleanup(h.close)
        incident = h.seed_source()
        self.assertTrue(self.worker(h).run_once())
        detail = h.detail(incident)
        self.assertEqual(detail['state'], 'test_build_queued')
        self.assertEqual([c['role'] for c in provider.calls], ['classifier', 'analyst', 'specialist', 'critic', 'verifier', 'patch'])
        self.assertEqual([(c['model'], c['effort']) for c in provider.calls], [
            ('gpt-6-luna', 'low'), ('gpt-6.2-sol', 'medium'), ('gpt-6.2-sol', 'medium'),
            ('gpt-6.2-astra', 'high'), ('gpt-6.2-sol', 'medium'), ('gpt-6.2-sol', 'medium')])
        self.assertTrue(all('synthetic-sensitive-value' not in c['prompt'] for c in provider.calls))
        job = h.claim(['test_build'])
        self.assertEqual(job['payload']['patch']['source_snapshot_id'], 'snapshot-one')
        self.assertEqual(job['payload']['patch']['tests_added'], ['tests/test_main.py::test_run'])
        self.assertTrue(detail['data']['analysis']['roles'])
        self.assertFalse(self.worker(h).run_once())

    def test_low_confidence_uncited_and_high_risk_handoff_without_build(self):
        for kwargs in ({'confidence': .2}, {'citations': False}, {'risk': 'high'}):
            with self.subTest(kwargs=kwargs):
                provider = SyntheticTransport(**kwargs)
                h = Harness(provider)
                self.addCleanup(h.close)
                incident = h.seed_source()
                self.worker(h).run_once()
                if 'confidence' in kwargs:
                    self.assertEqual(h.detail(incident)['state'], 'collecting')
                    self.assertEqual(h.report(h.claim(['collect']), h.collect()).status_code, 200)
                self.assertEqual(h.detail(incident)['state'], 'handoff')
                self.assertTrue(h.claim(['test_build', 'deploy']) is None)
                self.assertFalse(any(c['role'] == 'patch' for c in provider.calls))

    def test_unresolved_requires_new_scoped_observation_in_cause_and_independent_verifier(self):
        class Inquiring(SyntheticTransport):
            def complete(self, role, prompt, **kwargs):
                result = super().complete(role, prompt, **kwargs)
                if role in ('analyst', 'verifier'):
                    if '"review_evidence"' not in prompt:
                        result.data['unresolved'] = ['어떤 주문 입력이 실패했습니까?']
                    else:
                        import re
                        ref = re.search(r'followup:[a-f0-9]{64}', prompt).group()
                        result.data['citations'].append({'ref': ref, 'quote': 'quantity zero failed'})
                return result
        provider = Inquiring()
        h = Harness(provider)
        self.addCleanup(h.close)
        iid = h.seed_source()
        self.assertTrue(self.worker(h).run_once())
        job = h.claim(['collect'])
        self.assertEqual(job['payload']['purpose'], 'review_evidence')
        self.assertFalse(self.worker(h).run_once())
        outcome = h.collect()
        outcome['evidence'].append({**outcome['evidence'][0], 'text': 'quantity zero failed', 'id': 'E2-order'})
        self.assertEqual(h.report(job, outcome).status_code, 200)
        self.assertTrue(self.worker(h).run_once())
        detail = h.detail(iid)
        self.assertEqual(detail['state'], 'test_build_queued')
        data = detail['data']
        self.assertEqual(data['evidence_inquiry']['state'], 'reviewed')
        self.assertEqual(data['collection']['evidence'][0]['id'], 'E1-order')
        key = data['evidence_inquiry']['evidence_refs'][0]
        verified = next(r for r in data['analysis']['roles'] if r['role'] == 'verifier')
        self.assertTrue(any(x['ref'] == key for x in verified['result']['citations']))
        self.assertEqual(data['source_snapshot']['id'], 'snapshot-one')

    def test_stale_additional_collection_cannot_reopen_case(self):
        provider = SyntheticTransport(confidence=.2)
        h = Harness(provider)
        self.addCleanup(h.close)
        iid = h.seed_source()
        self.worker(h).run_once()
        job = h.claim(['collect'])
        self.assertIsNotNone(job)
        h.now += 901
        self.assertFalse(self.worker(h).run_once())
        self.assertEqual(h.detail(iid)['state'], 'handoff')
        self.assertEqual(h.report(job, h.collect()).status_code, 409)
        self.assertIsNone(h.claim(['source', 'test_build', 'deploy']))

    def test_observed_and_truncated_are_the_only_accepted_quality_strings(self):
        for quality in ('observed', 'truncated', .95, 1, True, '0.95', 'inferred', None):
            with self.subTest(quality=quality):
                provider = SyntheticTransport()
                h = Harness(provider)
                self.addCleanup(h.close)
                incident = h.create()
                collection = h.collect()
                collection['evidence'][0]['quality'] = quality
                h.report(h.claim(['collect']), collection)
                h.report(h.claim(['source']), h.source())
                h.app.state.worker.run_once()
                expected = 'test_build_queued' if quality in ('observed', 'truncated') else 'handoff'
                self.assertEqual(h.detail(incident)['state'], expected)
                if expected == 'handoff':
                    self.assertEqual(len(provider.calls), 0)
                    self.assertTrue(h.claim(['test_build', 'deploy']) is None)

    def test_role_environment_overrides_and_nullable_usage_are_recorded(self):
        from dataclasses import replace
        from unittest.mock import patch
        from app.application.settings import ApplicationSettings
        class MissingUsage(SyntheticTransport):
            def complete(self, *args, **kwargs):
                result = super().complete(*args, **kwargs)
                result.usage = Usage(input_tokens=0)
                return result
        with patch.dict('os.environ', {'APPLICATION_MODEL': 'synthetic-common',
                     'APPLICATION_CLASSIFIER_MODEL': 'synthetic-router',
                     'APPLICATION_CRITIC_EFFORT': 'medium'}, clear=True):
            config = ApplicationSettings.from_env()
        provider = MissingUsage()
        h = Harness(provider)
        self.addCleanup(h.close)
        h.app.state.control.settings = replace(h.settings, model=config.model,
            classifier_model=config.classifier_model, critic_model=config.critic_model,
            critic_effort=config.critic_effort)
        incident = h.seed_source()
        h.app.state.worker.run_once()
        calls = {row['role']: row for row in provider.calls}
        self.assertEqual(calls['classifier']['model'], 'synthetic-router')
        self.assertEqual(calls['analyst']['model'], 'synthetic-common')
        self.assertEqual(calls['critic']['model'], 'synthetic-common')
        self.assertEqual(calls['critic']['effort'], 'medium')
        roles = h.detail(incident)['data']['analysis']['roles']
        for row in roles:
            self.assertEqual(row['usage']['input_tokens'], 0)
            self.assertIsNone(row['usage']['output_tokens'])
            self.assertIsNone(row['usage']['cached_input_tokens'])
            self.assertEqual(row['model'], calls[row['role']]['model'])

    def test_evidence_quality_and_staleness_block_before_provider(self):
        for modifier in ('quality', 'time'):
            h = Harness(SyntheticTransport())
            self.addCleanup(h.close)
            incident = h.create()
            collect = h.collect()
            if modifier == 'quality':
                collect['evidence'][0]['quality'] = .1
            else:
                collect['evidence'][0]['observed_at'] = '2020-01-01T00:00:00Z'
            self.assertEqual(h.report(h.claim(['collect']), collect).status_code, 200)
            if h.detail(incident)['state'] == 'source_pinning':
                self.assertEqual(h.report(h.claim(['source']), h.source()).status_code, 200)
                self.worker(h).run_once()
            self.assertEqual(h.detail(incident)['state'], 'handoff')
            self.assertEqual(len(h.app.state.worker.provider.calls), 0)


if __name__ == '__main__':
    unittest.main()
