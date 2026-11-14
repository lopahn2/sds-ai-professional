"""사건 전체 예산·실제 공급자 시도·재개 입력 계약의 순수 회귀."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import AppConfig
from app.llm.base import LLMError, LLMResult, Usage
from app.llm.codex_pool import Account, CodexPoolProvider


class IncidentRuntimeContract(unittest.TestCase):
    def runtime(self, **limits):
        from app.runtime_budget import IncidentRuntime
        state = {}
        @contextmanager
        def storage():
            yield state
        runtime = IncidentRuntime(AppConfig(), storage, clock=lambda: 10, **limits)
        return runtime, state

    def test_pool_failed_attempt_and_success_are_both_accounted(self):
        from app.runtime_budget import complete
        runtime, state = self.runtime()
        first = NS(complete=lambda *a: (_ for _ in ()).throw(LLMError('quota', '합성 한도', usage=Usage())))
        second = NS(complete=lambda *a: LLMResult('', {}, Usage(10, 2)))
        pool = CodexPoolProvider([Account('primary', first), Account('spare', second)])
        complete(runtime, pool, 'router', '비공개 프롬프트', {}, 'gpt-6-luna', 'low', 1)
        self.assertEqual(state['provider_attempts'], 2)
        self.assertEqual(state['known_input_tokens'], 10)
        self.assertIsNone(state['input_tokens'])
        self.assertEqual(len(state['spans']), 2)
        self.assertNotIn('비공개 프롬프트', str(state))
        self.assertEqual(pool.accounts[0].calls, 1)

    def test_provider_retry_is_stopped_before_transport_when_incident_cap_exhausted(self):
        from app.runtime_budget import complete
        runtime, state = self.runtime(max_attempts=1)
        calls = []
        def fail(*a):
            calls.append(1)
            raise LLMError('quota', '합성 한도')
        pool = CodexPoolProvider([Account('primary', NS(complete=fail)), Account('spare', NS(complete=fail))])
        with self.assertRaises(LLMError) as caught:
            complete(runtime, pool, 'router', '자료', {}, 'gpt-6-luna', 'low', 1)
        self.assertEqual(caught.exception.kind, 'budget')
        self.assertEqual(len(calls), 1)

    def test_interpretation_consumes_same_incident_budget(self):
        from app.runtime_budget import complete
        runtime, state = self.runtime(max_attempts=1)
        provider = NS(complete=lambda *a: LLMResult('', {}, Usage(0, 0)))
        complete(runtime, provider, 'router', '자료', {}, 'gpt-6-luna', 'low', 1)
        with self.assertRaises(LLMError):
            complete(runtime, provider, 'interpreter', '자료', {}, 'gpt-6-luna', 'low', 1)
        self.assertEqual(state['provider_attempts'], 1)
        self.assertEqual(state['input_tokens'], 0)

    def test_quota_wait_cannot_overrun_deadline(self):
        runtime, state = self.runtime(deadline_s=2)
        runtime.wait(1)
        with self.assertRaises(LLMError): runtime.wait(2)
        self.assertEqual(state['quota_wait_s'], 1)

    def test_completed_read_reuse_and_input_fingerprint_change(self):
        from app.runtime_resume import fingerprint, read_step, write_step
        state, calls = {}, []
        def read():
            calls.append(1)
            return {'summary': '정제 결과'}
        key = fingerprint({'code': '1', 'input': '가', 'settings': {'limit': 2}})
        self.assertEqual(read_step(state, 'analysis', key, read), {'summary': '정제 결과'})
        self.assertEqual(read_step(state, 'analysis', key, read), {'summary': '정제 결과'})
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError): read_step(state, 'analysis', fingerprint({'code': '2'}), read)
        state['write'] = {'fingerprint': key, 'outcome': 'unknown'}
        def write(): self.fail('결과 불명 쓰기를 재전송함')
        reconciles = []
        result = write_step(state, 'write', key, write, lambda: reconciles.append(1) or {'outcome': 'written'})
        self.assertEqual(result['outcome'], 'written')
        self.assertEqual(len(reconciles), 1)


if __name__ == '__main__': unittest.main()
