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

    def test_node_regression_names_are_corrected_before_any_build_job(self):
        invalid_test = ('--- /dev/null\n+++ b/test/repair.js\n@@ -0,0 +1,2 @@\n'
                        '+const name = "restore the profile";\n+test(name, () => assertHealthy());\n')
        valid_test = ('--- /dev/null\n+++ b/test/repair.js\n@@ -0,0 +1 @@\n'
                      '+test("restore the profile", () => assertHealthy());\n')

        class NodePatch(SyntheticTransport):
            def complete(self, role, prompt, schema, model, effort, timeout):
                result = super().complete(role, prompt, schema, model, effort, timeout)
                if role == 'patch':
                    source_edit, _ = DIFF.split('--- /dev/null', 1)
                    result.data['diff'] = source_edit + (valid_test if 'patch_node_test_not_declared' in prompt else invalid_test)
                    result.data['tests_added'] = ['test/repair.js::restore the profile']
                return result

        provider = NodePatch()
        h = Harness(provider)
        self.addCleanup(h.close)
        incident = h.seed_source()
        self.assertTrue(self.worker(h).run_once())
        self.assertEqual(h.detail(incident)['state'], 'test_build_queued')
        calls = [call for call in provider.calls if call['role'] == 'patch']
        self.assertEqual(len(calls), 2)
        self.assertIn('replace dynamic test names', calls[-1]['prompt'])
        job = h.claim(['test_build'])
        self.assertTrue(job['payload']['patch']['diff'].endswith(valid_test))
        self.assertIsNone(h.claim(['test_build']))

    def test_generated_java_test_compile_failure_allows_one_patch_revision(self):
        provider = SyntheticTransport()
        h = Harness(provider)
        self.addCleanup(h.close)
        incident = h.seed_source()
        self.assertTrue(self.worker(h).run_once())
        job = h.claim(['test_build'])
        failure = {'ok': False, 'write_outcome': 'not_written', 'error': 'java_test_compile_failed'}

        self.assertEqual(h.report(job, failure).status_code, 200)
        self.assertEqual(h.detail(incident)['state'], 'analysis_queued')
        call_count = len(provider.calls)
        self.assertTrue(self.worker(h).run_once())
        self.assertEqual([call['role'] for call in provider.calls[call_count:]], ['patch'])
        self.assertIn('AutoCloseable', provider.calls[-1]['prompt'])
        revised_job = h.claim(['test_build'])
        self.assertEqual(h.report(revised_job, failure).status_code, 200)
        self.assertEqual(h.detail(incident)['state'], 'handoff')
        self.assertIsNone(h.claim(['deploy']))

    def test_native_post_patch_failure_reuses_analysis_and_preserves_tests(self):
        failure_id = 'tests/test_compat.py::test_contract'
        regression_id = 'tests/test_main.py::test_run'
        original_failure = {'id': failure_id, 'file': 'tests/test_compat.py', 'phase': 'after', 'status': 'failed'}

        class RevisingTransport(SyntheticTransport):
            def __init__(self, scenario):
                super().__init__()
                self.scenario = scenario

            def complete(self, role, **kwargs):
                result = super().complete(role, **kwargs)
                if role == 'patch' and 'after_not_passed' in kwargs['prompt']:
                    result.data['diff'] = DIFF.replace('+    return 0\n', '+    return int(0)\n')
                    if self.scenario == 'changed_test':
                        result.data['diff'] = result.data['diff'].replace('assert run() == 0', 'assert True')
                    if self.scenario == 'unchanged_diff':
                        result.data['diff'] = DIFF
                return result

        for scenario in ('pass', 'skip', 'repeat_failure', 'changed_test', 'unchanged_diff', 'unknown_write', 'no_failure_row'):
            with self.subTest(scenario=scenario):
                provider = RevisingTransport(scenario)
                h = Harness(provider)
                try:
                    incident = h.seed_source()
                    worker = self.worker(h)
                    self.assertTrue(worker.run_once())
                    job = h.claim(['test_build'])
                    failed = {'ok': False, 'write_outcome': 'unknown' if scenario == 'unknown_write' else 'not_written',
                              'error': 'after_not_passed', 'tests': {'before_failed': True, 'after_passed': False,
                                  'regression_executed': True, 'total': 2, 'failed': 1, 'report_sha256': 'a' * 64,
                                  'executed_tests': [
                                      {'id': regression_id, 'file': 'tests/test_main.py', 'phase': 'before', 'status': 'failed'},
                                      {'id': regression_id, 'file': 'tests/test_main.py', 'phase': 'after', 'status': 'passed'},
                                  ]}}
                    if scenario != 'no_failure_row':
                        failed['tests']['executed_tests'].append({**original_failure, 'stdout': 'omitted-regression-raw-output'})
                    self.assertEqual(h.report(job, failed).status_code, 200)
                    if scenario in ('unknown_write', 'no_failure_row'):
                        self.assertEqual(h.detail(incident)['state'], 'handoff')
                        self.assertFalse(worker.run_once())
                        continue
                    detail = h.detail(incident)
                    self.assertEqual(detail['state'], 'analysis_queued')
                    self.assertEqual(detail['data']['patch_revisions'][0]['failed_tests'], [original_failure])
                    self.assertNotIn('release', detail['data'])
                    self.assertIsNone(h.claim(['deploy']))
                    call_count = len(provider.calls)
                    self.assertTrue(worker.run_once())
                    calls = provider.calls[call_count:]
                    self.assertEqual([call['role'] for call in calls], ['patch'])
                    self.assertIn(failure_id, calls[0]['prompt'])
                    self.assertNotIn('omitted-regression-raw-output', calls[0]['prompt'])
                    if scenario in ('changed_test', 'unchanged_diff'):
                        self.assertEqual(h.detail(incident)['error']['code'], 'patch_revision_invalid')
                        self.assertIsNone(h.claim(['test_build']))
                        continue
                    revised = h.claim(['test_build'])
                    if scenario == 'repeat_failure':
                        self.assertEqual(h.report(revised, failed).status_code, 200)
                        self.assertEqual(h.detail(incident)['state'], 'handoff')
                        self.assertFalse(worker.run_once())
                        continue
                    verified_tests = copy.deepcopy(failed['tests'])
                    verified_tests.update(after_passed=True, failed=0)
                    verified_tests['executed_tests'][-1] = {**original_failure, 'status': 'skipped' if scenario == 'skip' else 'passed'}
                    artifact = {'image': 'local/order@sha256:' + '2' * 64, 'source_snapshot_id': 'snapshot-one',
                                'diff_sha256': revised['payload']['patch']['diff_sha256'],
                                'source_tree_sha256': 'b' * 64, 'build_receipt_sha256': 'c' * 64}
                    self.assertEqual(h.report(revised, {'ok': True, 'tests': verified_tests, 'artifact': artifact}).status_code, 200)
                    self.assertEqual(h.detail(incident)['state'], 'dev_targets_collecting' if scenario == 'pass' else 'handoff')
                    self.assertIsNone(h.claim(['deploy']))
                finally:
                    h.close()

    def test_sensitive_patch_is_discarded_and_only_one_fresh_candidate_is_allowed(self):
        marker = 'synthetic-regression-marker'

        class SensitiveCandidate(SyntheticTransport):
            def __init__(self, repeat):
                super().__init__()
                self.repeat = repeat

            def complete(self, role, **kwargs):
                result = super().complete(role, **kwargs)
                patch_calls = sum(call['role'] == 'patch' for call in self.calls)
                if role == 'patch' and (self.repeat or patch_calls == 1):
                    result.data['diff'] += f'+    password = "{marker}"\n'
                return result

        for repeats in (False, True):
            with self.subTest(repeats=repeats):
                provider = SensitiveCandidate(repeats)
                h = Harness(provider)
                self.addCleanup(h.close)
                incident = h.seed_source()
                self.assertTrue(self.worker(h).run_once())
                patches = [call for call in provider.calls if call['role'] == 'patch']
                self.assertEqual(len(patches), 2)
                self.assertIn('patch_contains_sensitive_material', patches[1]['prompt'])
                self.assertNotIn(marker, patches[1]['prompt'])
                detail = h.detail(incident)
                self.assertNotIn(marker, json.dumps(detail, ensure_ascii=False))
                if repeats:
                    self.assertEqual(detail['state'], 'handoff')
                    self.assertIsNone(h.claim(['test_build']))
                else:
                    self.assertEqual(detail['state'], 'test_build_queued')
                    self.assertEqual(h.claim(['test_build'])['payload']['patch']['diff'], DIFF)

    def test_worker_normalizes_text_patch_metadata_before_queuing_native_build(self):
        from app.application.worker import _normalize_hunk_counts
        from app.application.worker import _strip_git_text_file_headers
        from app.application.safety import text_sha

        first = '@@ -1,2 +1,2 @@'
        displaced = DIFF.replace(first, '@@ -1,1 +1,1 @@')
        displaced = 'diff --git a/src/main.py b/src/main.py\nindex aaaaaaa..bbbbbbb 100644\n' + displaced
        displaced = displaced.replace('--- /dev/null\n+++ b/tests/test_main.py',
                                      'diff --git a/tests/test_main.py b/tests/test_main.py\n'
                                      'new file mode 100644\n--- /dev/null\n+++ b/tests/test_main.py')
        displaced = displaced.replace('@@ -0,0 +1,2 @@', '@@ -0,0 +1,1 @@')
        unbalanced = DIFF.replace(first, '@@ -1,1 +1,2 @@')
        self.assertEqual(_normalize_hunk_counts(unbalanced), unbalanced)
        original = 'def run():\n    return 1 / 0\n'
        self.assertEqual(_normalize_hunk_counts(unbalanced, {'src/main.py': original}), DIFF)
        self.assertEqual(_normalize_hunk_counts(unbalanced, {'src/main.py': original.replace('1 / 0', '2 / 0')}),
                         unbalanced)
        wrong_offset = unbalanced.replace('@@ -1,1 +1,2 @@', '@@ -2,1 +1,2 @@')
        self.assertEqual(_normalize_hunk_counts(wrong_offset, {'src/main.py': original}), wrong_offset)
        self.assertEqual(_normalize_hunk_counts(DIFF), DIFF)
        unsupported = displaced.replace('new file mode 100644', 'new file mode 100755')
        self.assertIn('new file mode 100755', _strip_git_text_file_headers(unsupported))
        renamed = 'diff --git a/src/other.py b/src/main.py\n' + DIFF
        self.assertEqual(_strip_git_text_file_headers(renamed), renamed)

        class WrongContextCount(SyntheticTransport):
            def __init__(self, diff):
                super().__init__()
                self.diff = diff

            def complete(self, role, **kwargs):
                result = super().complete(role, **kwargs)
                if role == 'patch':
                    result.data['diff'] = self.diff
                return result

        for diff in (displaced, unbalanced):
            with self.subTest(diff=diff):
                provider = WrongContextCount(diff)
                h = Harness(provider)
                self.addCleanup(h.close)
                incident = h.seed_source()
                self.assertTrue(self.worker(h).run_once())
                self.assertEqual(sum(call['role'] == 'patch' for call in provider.calls), 1)
                detail = h.detail(incident)
                self.assertEqual(detail['state'], 'test_build_queued')
                self.assertEqual(detail['data']['analysis']['roles'][-1]['result']['diff'], diff)
                queued = h.claim(['test_build'])['payload']['patch']
                self.assertEqual(queued['diff'], DIFF)
                self.assertEqual(queued['diff_sha256'], text_sha(DIFF))

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

    def test_prompt_keeps_citable_observations_and_distinct_log_context(self):
        provider = SyntheticTransport()
        h = Harness(provider)
        self.addCleanup(h.close)
        incident = h.create()
        collection = h.collect()
        duplicate = {k: v for k, v in collection['evidence'][0].items() if k not in ('id', 'source', 'quality')}
        earlier = {**duplicate, 'observed_at': '2030-03-17T17:46:39Z'}
        collection['logs'].extend([duplicate, earlier])
        self.assertEqual(h.report(h.claim(['collect']), collection).status_code, 200)
        self.assertEqual(h.report(h.claim(['source']), h.source()).status_code, 200)
        self.assertTrue(self.worker(h).run_once())
        prompt = provider.calls[0]['prompt'].split('\nRole: classifier\n', 1)[1]
        context, _ = json.JSONDecoder().raw_decode(prompt)
        self.assertEqual(context['collection']['evidence'], collection['evidence'])
        self.assertEqual(context['collection']['logs'][1:], [earlier])
        self.assertEqual(len(context['collection']['logs']), 2)
        self.assertNotIn('synthetic-sensitive-value', prompt)
        self.assertEqual(h.detail(incident)['data']['collection']['logs'][1:], [duplicate, earlier])

    def test_cached_prompt_keeps_exact_bytes_for_new_roles_and_citation_retries(self):
        from app.application.safety import scrub
        from app.application.worker import _prompt_context

        pinned = {'source_snapshot': {'files': {'src/main.py': 'return "값"\n'}},
                  'collection': {'evidence': [{'id': 'E1', 'text': 'payload token=synthetic-only'}]}}
        prepared = scrub(pinned)
        for dynamic in ({}, {'analyst': {'citations': CITATIONS}},
                        {'prior_review': {'source_excerpts': [{'ref': 'E1', 'quote': 'payload'}]}}):
            context = {**pinned, **dynamic}
            self.assertEqual(_prompt_context(context, prepared),
                             json.dumps(scrub(context), ensure_ascii=False, sort_keys=True))

    def test_claim_returns_a_detached_view_of_the_pinned_source(self):
        h = Harness(SyntheticTransport())
        self.addCleanup(h.close)
        expected = h.source()['source_snapshot']['files']
        iid = h.seed_source()
        claimed_id, _, document = self.worker(h)._claim()
        self.assertEqual(claimed_id, iid)
        document['data']['source_snapshot']['files']['src/main.py'] = 'candidate-local-view'
        self.assertEqual(h.detail(iid)['data']['source_snapshot']['files'], expected)

    def test_citation_retry_names_rejected_refs_without_relaxing_the_verbatim_gate(self):
        class MisquotingTransport(SyntheticTransport):
            def __init__(self, correct_on_retry):
                super().__init__()
                self.correct_on_retry = correct_on_retry

            def complete(self, role, prompt, **kwargs):
                result = super().complete(role, prompt, **kwargs)
                if role == 'classifier':
                    payload = prompt.split('\nRole: classifier\n', 1)[1]
                    context, _ = json.JSONDecoder().raw_decode(payload)
                    prior = context.get('prior_review')
                    result.data['citations'] = (prior['source_excerpts'] if prior and self.correct_on_retry else [
                        {'ref': 'E1-order', 'quote': 'order was successful'},
                        {'ref': 'untrusted-not-a-source', 'quote': 'made up evidence'},
                        CITATIONS[1],
                    ])
                return result

        for corrected in (True, False):
            with self.subTest(corrected=corrected):
                provider = MisquotingTransport(corrected)
                h = Harness(provider)
                self.addCleanup(h.close)
                incident = h.seed_source()
                self.assertTrue(self.worker(h).run_once())
                retried = provider.calls[1]['prompt'].split('\nRole: classifier\n', 1)[1]
                context, _ = json.JSONDecoder().raw_decode(retried)
                self.assertEqual(context['prior_review']['rejected_citations'], [
                    {'position': 1, 'ref': 'E1-order'}, {'position': 2, 'ref': None},
                ])
                self.assertEqual(context['prior_review']['source_excerpts'], CITATIONS[:1])
                self.assertNotIn('untrusted-not-a-source', retried)
                self.assertNotIn('order was successful', retried)
                detail = h.detail(incident)
                self.assertEqual(detail['data']['analysis']['roles'][0]['result']['citations'][0]['quote'],
                                 'order was successful')
                self.assertEqual(detail['state'], 'test_build_queued' if corrected else 'handoff')
                self.assertEqual(sum(call['role'] == 'classifier' for call in provider.calls), 2)
                self.assertEqual(bool(h.claim(['test_build'])), corrected)
                if not corrected:
                    self.assertEqual(detail['error']['code'], 'citation_not_grounded')

    def test_python_worker_adds_a_specialist_for_independent_source_frames(self):
        fees = '  File "/fixture/src/main.py", line 12, in fees\n    1 / 0\nZeroDivisionError: zero divisor'
        receipt = '  File "/fixture/src/main.py", line 19, in receipt\nValueError: invalid amount format'
        unrelated = '  File "/fixture/dependency/format.py", line 19, in receipt\nValueError: invalid amount format'
        separator = '\nThe above exception was the direct cause of the following exception:\n'
        for traces, expected in (([fees, receipt], 2), ([fees, fees], 1),
                                 ([fees + separator + receipt], 1), ([fees, unrelated], 1)):
            with self.subTest(traces=traces):
                provider = SyntheticTransport(level=1)
                h = Harness(provider)
                self.addCleanup(h.close)
                incident = h.create()
                collection = h.collect()
                for index, trace in enumerate(traces, 2):
                    text = '\n'.join('2030-03-17T17:46:40.000Z ' + line for line in trace.splitlines())
                    collection['evidence'].append({**collection['evidence'][0], 'id': f'E{index}-order', 'text': text})
                self.assertEqual(h.report(h.claim(['collect']), collection).status_code, 200)
                self.assertEqual(h.report(h.claim(['source']), h.source()).status_code, 200)
                self.assertTrue(self.worker(h).run_once())
                detail = h.detail(incident)
                self.assertEqual(detail['state'], 'test_build_queued')
                routing = detail['data']['analysis_routing']
                self.assertEqual(routing['model_level'], 1)
                self.assertEqual(routing['effective_level'], expected)
                self.assertEqual(any(call['role'] == 'specialist' for call in provider.calls), expected == 2)
                if expected == 2:
                    self.assertEqual(routing['observed_exception_frames'],
                                     [['ValueError', 'src/main.py:receipt'], ['ZeroDivisionError', 'src/main.py:fees']])

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
        self.assertEqual(data['evidence_inquiry']['review_outcome'], 'patch_proposed')
        self.assertEqual(data['collection']['evidence'][0]['id'], 'E1-order')
        key = data['evidence_inquiry']['evidence_refs'][0]
        verified = next(r for r in data['analysis']['roles'] if r['role'] == 'verifier')
        self.assertTrue(any(x['ref'] == key for x in verified['result']['citations']))
        self.assertEqual(data['source_snapshot']['id'], 'snapshot-one')

    def test_fixability_uncertainty_can_request_one_observation_and_closes_with_evidence(self):
        class Unevaluable(SyntheticTransport):
            def complete(self, role, prompt, **kwargs):
                result = super().complete(role, prompt, **kwargs)
                if role in ('analyst', 'verifier'):
                    if role == 'analyst':
                        result.data['fixable'] = False
                    if role == 'analyst' and '"review_evidence"' not in prompt:
                        result.data['unresolved'] = ['실패 시점의 주문 수량은 무엇입니까?']
                    else:
                        import re
                        ref = re.search(r'followup:[a-f0-9]{64}', prompt).group()
                        result.data['citations'].append({'ref': ref, 'quote': 'quantity zero failed'})
                return result

        for expires in (False, True):
            with self.subTest(expires=expires):
                provider = Unevaluable()
                h = Harness(provider)
                self.addCleanup(h.close)
                iid = h.seed_source()
                self.assertTrue(self.worker(h).run_once())
                job = h.claim(['collect'])
                self.assertEqual(job['payload']['purpose'], 'review_evidence')
                self.assertIsNone(h.claim(['source', 'test_build', 'deploy']))
                followup = h.collect()
                followup['evidence'].append({**followup['evidence'][0], 'text': 'quantity zero failed', 'id': 'E2-order'})
                self.assertEqual(h.report(job, followup).status_code, 200)
                original = h.detail(iid)['data']['evidence_inquiry']
                self.assertEqual(original['state'], 'reviewing')
                self.assertEqual(len(original['evidence_refs']), 1)
                if expires:
                    h.now += 901
                self.assertEqual(self.worker(h).run_once(), not expires)
                detail = h.detail(iid)
                self.assertEqual(detail['state'], 'handoff')
                expected = 'additional_observation_exhausted' if expires else 'application_fix_not_supported'
                final = detail['data']['evidence_inquiry']
                self.assertEqual((final['state'], final['review_outcome'], final['review_code']),
                                 ('reviewed', 'handoff', expected))
                self.assertEqual(final['evidence_refs'], original['evidence_refs'])
                self.assertIsNone(h.claim(['collect', 'source', 'test_build', 'deploy']))
                self.assertTrue(all(call['role'] != 'patch' for call in provider.calls))

    def test_unsupported_source_repair_requires_a_grounded_independent_verdict(self):
        class ScopeReview(SyntheticTransport):
            def __init__(self, verdict):
                super().__init__()
                self.verdict = verdict

            def complete(self, role, **kwargs):
                result = super().complete(role, **kwargs)
                if role == 'analyst':
                    result.data.update(fixable=False, confidence=.4 if self.verdict == 'low_confidence' else .95)
                elif role == 'verifier':
                    if self.verdict == 'source_only':
                        result.data['citations'] = [CITATIONS[1]]
                    if self.verdict == 'disagree':
                        result.data.update(accepted=False, unresolved=['같은 주문의 목격된 오류에 입력 검증이 관여했습니까?'])
                return result

        for verdict in ('confirm', 'source_only', 'disagree', 'low_confidence'):
            with self.subTest(verdict=verdict):
                provider = ScopeReview(verdict)
                h = Harness(provider)
                self.addCleanup(h.close)
                iid = h.seed_source()
                self.assertTrue(self.worker(h).run_once())
                detail = h.detail(iid)
                calls = [call['role'] for call in provider.calls]
                if verdict == 'low_confidence':
                    self.assertEqual(calls, ['classifier', 'analyst'])
                else:
                    self.assertEqual(calls, ['classifier', 'analyst', 'verifier'])
                    self.assertIn('"source_fix_review"', provider.calls[-1]['prompt'])
                if verdict == 'confirm':
                    self.assertEqual((detail['state'], detail['error']['code']), ('handoff', 'application_fix_not_supported'))
                    self.assertIsNone(h.claim(['collect']))
                else:
                    self.assertEqual(detail['state'], 'collecting')
                    job = h.claim(['collect'])
                    self.assertEqual(job['payload']['purpose'], 'review_evidence')
                    if verdict == 'disagree':
                        self.assertIn('입력 검증', detail['data']['evidence_inquiry']['question'])
                self.assertIsNone(h.claim(['source', 'test_build', 'deploy']))

    def test_scope_disagreement_can_continue_to_code_repair_after_new_observation(self):
        class Reconsidering(SyntheticTransport):
            def __init__(self, can_correct):
                super().__init__()
                self.can_correct = can_correct

            def complete(self, role, prompt, **kwargs):
                import re
                result = super().complete(role, prompt, **kwargs)
                if role == 'analyst' and (not self.can_correct or '"review_evidence"' not in prompt):
                    result.data['fixable'] = False
                if role == 'verifier' and '"source_fix_review"' in prompt:
                    result.data.update(accepted=False, unresolved=['새 입력에서 같은 오류를 재현할 수 있습니까?'])
                if role in ('analyst', 'verifier') and '"review_evidence"' in prompt:
                    ref = re.search(r'followup:[a-f0-9]{64}', prompt).group()
                    result.data['citations'].append({'ref': ref, 'quote': 'quantity zero failed'})
                return result

        for can_correct in (True, False):
            with self.subTest(can_correct=can_correct):
                provider = Reconsidering(can_correct)
                h = Harness(provider)
                self.addCleanup(h.close)
                iid = h.seed_source()
                self.assertTrue(self.worker(h).run_once())
                job = h.claim(['collect'])
                evidence = h.collect()
                evidence['evidence'].append({**evidence['evidence'][0], 'text': 'quantity zero failed', 'id': 'E2-order'})
                self.assertEqual(h.report(job, evidence).status_code, 200)
                self.assertTrue(self.worker(h).run_once())
                detail = h.detail(iid)
                self.assertEqual(detail['state'], 'test_build_queued' if can_correct else 'handoff')
                inquiry = detail['data']['evidence_inquiry']
                self.assertEqual(inquiry['review_outcome'], 'patch_proposed' if can_correct else 'handoff')
                if not can_correct:
                    self.assertEqual(inquiry['review_code'], 'analysis_unresolved_or_low_confidence')
                    self.assertIsNone(h.claim(['test_build']))
                self.assertEqual([c['role'] for c in provider.calls].count('patch'), int(can_correct))
                self.assertIsNone(h.claim(['collect', 'deploy']))

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
