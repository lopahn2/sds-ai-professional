"""향후 기준선 발급기의 비적용 회귀. 클러스터·DB·모델 접근 없음."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).with_name('p18-campaign-gate.py')


class GateIssuer(unittest.TestCase):
    def module(self):
        self.assertTrue(SOURCE.exists(), '명시적 검증을 수행하는 새 기준선 발급기 필요')
        spec = importlib.util.spec_from_file_location('gate_issuer', SOURCE)
        if spec is None or spec.loader is None:
            raise ImportError('기준선 발급기 로더 없음')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def observation(self):
        return ({'metadata': {'generation': 2}, 'spec': {'replicas': 1},
                 'status': {'observedGeneration': 2, 'replicas': 1, 'readyReplicas': 1, 'updatedReplicas': 1}},
                {'status': 200}, {'status': 200, 'body': {'active': {}, 'release_active': {}}},
                {'verified': True, 'probes': [{'status': 200}]}, True)

    def test_healthy_observation_is_accepted(self):
        self.assertTrue(self.module().check_observation(*self.observation()))

    def test_each_unhealthy_field_is_explicitly_refused(self):
        m = self.module()
        for index in range(6):
            with self.subTest(index=index):
                dep, health, chaos, business, equal = self.observation()
                if index == 0: dep['status']['readyReplicas'] = 0
                elif index == 1: health['status'] = 503
                elif index == 2: chaos['body']['active'] = {'crash': True}
                elif index == 3: chaos['body']['release_active'] = {'crash': True}
                elif index == 4: business['verified'] = False
                else: equal = False
                with self.assertRaises(m.GateError):
                    m.check_observation(dep, health, chaos, business, equal)

    def test_optimized_cli_refuses_before_any_receipt(self):
        self.module()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'new-gates'
            result = subprocess.run([sys.executable, '-O', '-B', str(SOURCE), '--output', str(out)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(out.exists())

    def test_issuer_has_no_assert_safety_checks(self):
        self.module()
        self.assertEqual([x.lineno for x in ast.walk(ast.parse(SOURCE.read_text())) if isinstance(x, ast.Assert)], [])

    def test_model_future_and_stale_are_not_success(self):
        m = self.module()
        clock = datetime.now(timezone.utc)
        for seconds in (-3600, 600):
            call = {'created_at': (clock + timedelta(seconds=seconds)).isoformat(), 'status': 'succeeded', 'input_tokens': 1, 'output_tokens': 1}
            self.assertFalse(m.model_evidence([call], [{'state': 'available'}], clock)['actual_model_success'])
        call['created_at'] = (clock - timedelta(minutes=2)).isoformat()
        self.assertTrue(m.model_evidence([call], [{'state': 'available'}], clock)['actual_model_success'])

    def test_runtime_probe_validates_success_and_keeps_real_usage(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import json
        m = self.module()
        self.assertTrue(hasattr(m, 'runtime_model_probe'), '명시적 런타임 모델 점검 경로 필요')
        clock = datetime.now(timezone.utc).isoformat()
        payload = {'schema': 'liveprobe_runtime_model_probe/v1', 'success': True, 'at': clock,
                   'provider': 'codex_cli', 'account': 'primary', 'model': 'gpt-6.1-sol', 'effort': 'xhigh',
                   'response_ok': True, 'usage': {'input_tokens': 123, 'output_tokens': 7},
                   'duration_ms': 10, 'runtime_pod': 'fake-api-pod'}
        with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='출력하지 않을 진단')) as run:
            result = m.runtime_model_probe('primary')
        self.assertTrue(result['success'])
        self.assertEqual(result['usage']['input_tokens'], 123)
        self.assertEqual(result['kubectl_exit'], 0)
        self.assertIn('exec', run.call_args.args[0])
        self.assertLessEqual(run.call_args.kwargs['timeout'], 120)
        self.assertNotIn('stderr', result)

    def test_runtime_probe_refuses_malformed_or_failed_success(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import json
        m = self.module()
        self.assertTrue(hasattr(m, 'runtime_model_probe'), '명시적 런타임 모델 점검 경로 필요')
        payload = {'schema': 'liveprobe_runtime_model_probe/v1', 'success': True, 'at': datetime.now(timezone.utc).isoformat(),
                   'provider': 'codex_cli', 'account': 'primary', 'model': 'gpt-6.1-sol', 'effort': 'xhigh',
                   'response_ok': True, 'usage': {'input_tokens': 123, 'output_tokens': 7}, 'runtime_pod': 'fake-api-pod'}
        for flaw in ('exit', 'json', 'model', 'usage', 'response'):
            with self.subTest(flaw=flaw):
                row = copy.deepcopy(payload)
                if flaw == 'model': row['model'] = '다른모델'
                elif flaw == 'usage': row['usage']['output_tokens'] = None
                elif flaw == 'response': row['response_ok'] = False
                result = SimpleNamespace(returncode=1 if flaw == 'exit' else 0,
                                         stdout='{' if flaw == 'json' else json.dumps(row), stderr='출력하지 않을 진단')
                with patch.object(m.subprocess, 'run', return_value=result):
                    self.assertFalse(m.runtime_model_probe('primary')['success'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
