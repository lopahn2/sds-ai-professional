"""모델 전송만 대체하는 독립 실행 회귀 시험."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.llm.codex_cli import CodexCLIProvider, SYSTEM_PREAMBLE


class ProviderContract(unittest.TestCase):
    def test_legacy_output_is_parseable_but_cannot_bypass_new_acceptance(self):
        from jsonschema import Draft202012Validator, ValidationError
        from app.llm import AGENT_SCHEMA, AgentOutput
        from app.llm.base import strict_schema, validation_schema
        from app.workflows import AgentCall, build_simple_graph
        legacy = {'summary': 'bad tag', 'root_cause_candidates': [{'cause': 'bad tag', 'confidence': .9,
            'evidence_refs': ['E2_pods']}], 'counter_evidence': [], 'confidence': .9,
            'next_action': 'rollout_undo', 'recommended_steps': [], 'action': None}
        Draft202012Validator(validation_schema(AGENT_SCHEMA)).validate(legacy)
        AgentOutput.model_validate(legacy)
        self.assertEqual(set(strict_schema(AGENT_SCHEMA)['required']), set(AGENT_SCHEMA['properties']))
        with self.assertRaises(ValidationError):
            Draft202012Validator(validation_schema(AGENT_SCHEMA)).validate({**legacy, 'confidence': 'bad'})
        state = {'incident': {'workload': 'svc'}, 'features': {}, 'evidence': {}, 'rag': [],
                 'has_previous_revision': True, 'min_confidence': .6}
        result = build_simple_graph(lambda *a: AgentCall(True, legacy, 1)).invoke(state)
        self.assertEqual(result['outcome'], 'handoff')
        self.assertEqual(result['handoff']['workflow_status'], 'unverified')

    def test_optional_preamble_preserves_sandbox_and_schema(self):
        received = []
        class Transport:
            returncode = 0
            def __init__(self, cmd, **kwargs):
                received.append((cmd, kwargs))
            def communicate(self, text, timeout):
                received.append(text)
                return '\n'.join(json.dumps(row) for row in [
                    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
                    {'type': 'turn.completed', 'usage': {'input_tokens': 0, 'output_tokens': 0}}]), ''
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
        with patch.dict(os.environ, {'UNAPPROVED_VALUE': '합성값'}, clear=False), \
                patch('app.llm.codex_cli.subprocess.Popen', Transport):
            for preamble in (None, '애플리케이션 전용 지시\n'):
                provider = CodexCLIProvider(system_preamble=preamble)
                result = provider.complete('analyst', '자료', schema, '모델', 'low', 3)
                self.assertTrue(result.data['ok'])
        self.assertEqual(received[1], SYSTEM_PREAMBLE + '자료')
        self.assertEqual(received[3], '애플리케이션 전용 지시\n자료')
        cmd, options = received[2]
        self.assertIn('--output-schema', cmd)
        self.assertIn('--ignore-user-config', cmd)
        self.assertIn('shell_tool', cmd)
        self.assertIn('browser_use', cmd)
        self.assertNotIn('UNAPPROVED_VALUE', options['env'])
        self.assertEqual(options['cwd'], options['env']['HOME'])


if __name__ == '__main__':
    unittest.main()
