"""그룹 최종 수용과 인용 경계를 실제 그래프로 검사한다."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent_groups import EvidenceStore, GroupController, check_citations, finalize
from app.config import AppConfig
from app.workflows import AgentCall, build_simple_graph, build_high_risk_graph


def message(cause='OOMKilled', confidence=.95):
    return {'summary': cause, 'evidence': [{'ref': 'E2_pods', 'quote': 'OOMKilled', 'supports': cause}],
            'root_cause_candidates': [{'cause': cause, 'confidence': confidence, 'evidence_refs': ['E2_pods']}],
            'counter_evidence': [], 'confidence': confidence, 'verdict': 'supported',
            'candidate_verdicts': [{'index': 0, 'verdict': 'supported', 'reason': 'OOMKilled 직접 관측'}],
            'next_action': 'act', 'recommended_steps': [],
            'action': {'type': 'rollout_undo', 'target': 'svc', 'why': '이전 버전 복구'}, 'remediation': None}


class Acceptance(unittest.TestCase):
    def result(self, primary, verifier, others=None):
        store = EvidenceStore({'E2_pods': {'reason': 'OOMKilled', 'healthy': 'Ready'}})
        ctl = GroupController('NORMAL', AppConfig(), lambda *a: None)
        msgs = {'k8s_analyst': primary, 'group_verifier': verifier, **(others or {})}
        state = {'primary_role': 'k8s_analyst', 'msgs': msgs,
                 'reports': {k: check_citations(store, v) for k, v in msgs.items()},
                 'incident': {'workload': 'svc'}, 'features': {'has_previous_revision': True}}
        return finalize(ctl, store, state)['result']

    def test_final_confidence_floor(self):
        primary, verifier = message(), message(confidence=.1)
        result = self.result(primary, verifier)
        self.assertEqual(result['status'], 'unverified')
        self.assertFalse(result['action']['propose'])

    def test_supported_secondary_cannot_replace_unsupported_top(self):
        primary, verifier = message(), message()
        primary['root_cause_candidates'].append({'cause': '메모리 부족', 'confidence': .9, 'evidence_refs': ['E2_pods']})
        primary['evidence'].append({'ref': 'E2_pods', 'quote': 'OOMKilled', 'supports': '메모리 부족'})
        verifier['candidate_verdicts'] = [
            {'index': 0, 'verdict': 'insufficient', 'reason': '불충분'},
            {'index': 1, 'verdict': 'supported', 'reason': '직접 관측'}]
        result = self.result(primary, verifier)
        self.assertEqual(result['status'], 'unverified')
        self.assertFalse(result['action']['propose'])
        self.assertTrue(all(c['verifier'] == 'supported' for c in result['root_cause_candidates']))

    def test_real_unrelated_quote_is_not_causal_support(self):
        primary, verifier = message(), message()
        for msg in (primary, verifier):
            msg['evidence'][0].update(quote='Ready', supports='현재 준비 상태')
        self.assertEqual(self.result(primary, verifier)['status'], 'unverified')

    def test_real_unrelated_quote_with_falsely_labeled_support_is_rejected(self):
        primary, verifier = message(), message()
        for msg in (primary, verifier):
            msg['evidence'][0]['quote'] = 'Ready'
        self.assertEqual(self.result(primary, verifier)['status'], 'unverified')

    def test_unresolved_escalation_cannot_reenter_final_acceptance(self):
        primary, verifier = message(), message()
        primary['_acceptance_rejected'] = '승격 후에도 미해결'
        self.assertEqual(self.result(primary, verifier)['status'], 'unverified')

    def test_challenger_counter_evidence_survives(self):
        challenger = message()
        challenger.update(verdict='contradicted', counter_evidence=['다른 배포에서 오류가 계속됨'])
        result = self.result(message(), message(), {'challenger': challenger})
        self.assertEqual(result['status'], 'unverified')
        self.assertIn('다른 배포에서 오류가 계속됨', result['counter_evidence'])

    def test_positive_control(self):
        self.assertEqual(self.result(message(), message())['status'], 'verified')

    def test_simple_rejects_forged_quotes_even_when_actions_agree(self):
        state = {'incident': {'workload': 'svc'}, 'features': {'has_previous_revision': True},
                 'has_previous_revision': True, 'min_confidence': .6, 'allowlist': ['rollout_undo'],
                 'evidence': {'E2_pods': {'reason': 'OOMKilled'}}, 'rag': []}
        answer = message()
        answer['evidence'][0]['quote'] = '없는 원문'
        result = build_simple_graph(lambda *a: AgentCall(True, deepcopy(answer), 1)).invoke(state)
        self.assertEqual(result['outcome'], 'handoff')

    def test_invalid_remediation_keeps_its_specific_handoff_reason(self):
        state = {'incident': {'workload': 'svc'}, 'features': {},
                 'evidence': {'E8_code_refs': {'frames': []}}, 'rag': []}
        output = message()
        output['remediation'] = {'kind': 'both', 'why': '합성 위치',
            'code_refs': [{'file': '/app/forged.py', 'line': 9, 'symbol': 'f'}], 'repro': None}
        result = build_simple_graph(lambda *a: AgentCall(True, deepcopy(output), 1)).invoke(state)
        self.assertEqual(result['outcome'], 'handoff')
        self.assertIn('remediation', result['reason'])

    def test_high_risk_invalid_evidence_never_verified(self):
        state = {'incident': {'workload': 'svc'}, 'features': {}, 'min_confidence': .6,
                 'evidence': {'E2_pods': {'reason': 'OOMKilled'}}, 'rag': []}
        answer = message()
        answer['evidence'][0]['quote'] = '없는 원문'
        result = build_high_risk_graph(lambda *a: AgentCall(True, deepcopy(answer), 1)).invoke(state)
        self.assertEqual(result['handoff'].get('workflow_status'), 'unverified')


if __name__ == '__main__':
    unittest.main()
