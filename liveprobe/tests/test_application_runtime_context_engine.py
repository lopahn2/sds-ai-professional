"""현재 건강·관측 부족·역할 컨텍스트와 실제 엔진 재사용 회귀."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import engine as E
from app.agent_groups import EvidenceStore
from app.config import AppConfig, Settings
from app.evidence import Bundle, EvidenceItem
from app.llm.base import LLMError, LLMResult, Usage
from app.llm import RouterOutput
from app import router
from app.safety import Denied, check_action


class ContextAndRouting(unittest.TestCase):
    def bundle(self):
        return Bundle('stg', 'svc', items=[EvidenceItem('deployment', 'stg/Deployment/svc', {})],
            deployment={'images': ['image'], 'current_revision': 1},
            pods=[{'name': 'svc-1', 'containers': [{'ready': True}]}],
            service={'ready_endpoints': 1}, metrics={}, logs=[])
    def test_currently_healthy_is_not_fault_evidence(self):
        result = router.decide(self.bundle(), 15, cfg=AppConfig(), signal_reasons=['ApplicationException'])
        self.assertEqual(result.features.get('evidence_state'), 'currently_healthy')
        self.assertTrue(result.policy.get('recheck_required'))
        self.assertFalse(result.policy.get('handoff_only'))
        self.assertNotEqual(result.level, 'SIMPLE')
    def test_missing_evidence_does_not_become_healthy(self):
        bundle = self.bundle(); bundle.pods = []
        result = router.decide(bundle, 15, cfg=AppConfig())
        self.assertEqual(result.features.get('evidence_state'), 'insufficient')
    def test_healthy_protected_target_keeps_high_risk(self):
        bundle = self.bundle()
        bundle.items.append(EvidenceItem('namespace', 'stg/Namespace/stg', {}))
        result = router.decide(bundle, 15, cfg=AppConfig())
        self.assertEqual(result.level, 'HIGH_RISK')
    def test_stale_oom_signal_is_not_current_fault_observation(self):
        result = router.decide(self.bundle(), 15, cfg=AppConfig(), signal_reasons=['OOMKilled'])
        self.assertEqual(result.features['evidence_state'], 'currently_healthy')
        self.assertTrue(result.policy['recheck_required'])
        self.assertFalse(result.policy.get('handoff_only'))
        self.assertGreaterEqual(router.LEVEL_ORDER[result.level], router.LEVEL_ORDER['NORMAL'])

    def test_actual_error_log_is_current_fault_evidence(self):
        bundle = self.bundle()
        bundle.logs = ['2026-11-01T01:00:00Z ERROR 실제 예외']
        result = router.decide(bundle, 15, cfg=AppConfig())
        self.assertEqual(result.features['evidence_state'], 'fault_observed')

    def test_missing_protected_and_healthy_network_keep_high_risk_floor(self):
        for missing, signals in ((False, ['NetworkDrop']), (True, [])):
            bundle = self.bundle()
            if missing:
                bundle.pods = []
                bundle.items.append(EvidenceItem('namespace', 'stg/Namespace/stg', {}))
            result = router.decide(bundle, 15, cfg=AppConfig(), signal_reasons=signals)
            self.assertEqual(result.level, 'HIGH_RISK')

    def test_context_cap_preserves_late_counter_evidence_and_rag(self):
        store = EvidenceStore({'E1_deployment': {'text': '반복' * 10000},
            'E7_logs': ['2026-11-01T01:00:00Z 정상', '2026-11-01T01:01:00Z 반대근거 오류 지속']},
            [{'ref': 'R1', 'content': '한글 실행 안내', 'source': '수집문서'}], view_chars=1200)
        view = store.view(('E1', 'E7'))
        self.assertLessEqual(len(view), 1200)
        self.assertIn('반대근거 오류 지속', view)
        self.assertIn('한글 실행 안내', view)
    def test_simple_and_high_risk_context_bounds_preserve_late_counter_evidence(self):
        from app.workflows import _ctx
        state = {'incident': {'workload': 'svc'}, 'features': {},
                 'evidence': {'E1_deployment': {'text': '긴 원문' * 10000},
                              'E7_logs': ['2026-01-01T00:00:00Z 정상',
                                          '2026-01-01T00:01:00Z 반대 근거 오류 지속']},
                 'rag': [{'ref': 'R1', 'source': '원문 링크', 'doc': '문서', 'score': .8,
                          'content': '한글 복구 안내' * 10000}]}
        context = _ctx(state)
        self.assertLessEqual(len(context), 12000)
        self.assertIn('반대 근거 오류 지속', context)
        self.assertIn('한글 복구 안내', context)
        self.assertIn('source=원문 링크', context)

    def test_context_orders_observed_and_kubernetes_event_timestamps(self):
        store = EvidenceStore({'E5_events': [
            {'last': '2026-01-01T00:02:00Z', 'message': '나중 사건'},
            {'last': '2026-01-01T00:01:00Z', 'message': '먼저 사건'}],
            'E7_logs': [{'observed_at': '2026-01-01T00:02:00Z', 'text': '나중 로그'},
                        {'observed_at': '2026-01-01T00:01:00Z', 'text': '먼저 로그'}]})
        view = store.view(('E5', 'E7'))
        self.assertLess(view.index('먼저 사건'), view.index('나중 사건'))
        self.assertLess(view.index('먼저 로그'), view.index('나중 로그'))

    def test_application_lease_blocks_legacy_action(self):
        from app.runtime_fencing import LEASE_ANNOTATION
        dep = {'metadata': {'labels': {'liveprobe.io/managed': 'true', 'app.kubernetes.io/part-of': 'sys'},
                            'annotations': {LEASE_ANNOTATION: 'lease-1'}}}
        with self.assertRaises(Denied):
            check_action(AppConfig(), NS(get_deployment=lambda *a: dep), 'rollout_undo', 'stg', 'Deployment', 'svc', system='sys')


class RealEngine(unittest.TestCase):
    def test_frozen_collection_is_reused_without_new_reads(self):
        from dataclasses import asdict
        bundle = Bundle('stg', 'svc', logs=['과거 정제 원문'])
        workflow = NS(snapshot={'collection': asdict(bundle)})
        class Session:
            def get(self, *args): return workflow
        @contextmanager
        def session(): yield Session()
        engine = E.Engine(AppConfig(), Settings(background=False), None, None, None)
        engine._workflow_run_id = 7
        with patch.object(E.db, 'session', session), patch.object(E, 'collect') as collect:
            first = engine._collect_or_reuse('stg', 'svc', None)
        self.assertEqual(first.logs, ['과거 정제 원문'])
        collect.assert_not_called()

    def test_new_collection_is_json_persisted_then_reused_as_a_detached_bundle(self):
        import json
        import httpx
        from app.models import WorkflowRun
        workflow = NS(incident_id=77, snapshot={'unrelated': {'keep': True}})
        class Session:
            def get(self, cls, key): return workflow if cls is WorkflowRun else None
            def scalar(self, statement): return workflow
        @contextmanager
        def session(): yield Session()
        kube = NS(list_pods=Mock(return_value=[]), get_deployment=Mock(return_value=None),
                  get_service=Mock(return_value=None), list_events=Mock(return_value=[]))
        engine = E.Engine(AppConfig(), Settings(background=False), kube, None, None)
        engine._workflow_run_id = 7
        with patch.object(E.db, 'session', session), patch.object(E.runlog, 'gate'), patch.object(E.runlog, 'emit'), patch('httpx.get', return_value=httpx.Response(
                200, json={'data': {'result': []}}, request=httpx.Request('GET', 'http://offline.invalid'))):
            first = engine._collect_or_reuse('stg', 'svc', None)
            self.assertGreater(len(first.items), 0)
            self.assertIn('collection', workflow.snapshot)
            workflow.snapshot = json.loads(json.dumps(workflow.snapshot))
            first.logs.append('호출자 변경은 봉인 자료를 바꾸지 않음')
            second = engine._collect_or_reuse('stg', 'svc', None)
        self.assertEqual(kube.list_pods.call_count, 2)  # missing workload: one bounded recollection, then cached reuse
        self.assertEqual(len(workflow.snapshot['collection_attempts']), 2)
        self.assertEqual(second.logs, [])
        self.assertIsInstance(second.items[0], EvidenceItem)
        self.assertTrue(hasattr(second.items[0].collected_at, 'isoformat'))
        self.assertEqual(workflow.snapshot['unrelated'], {'keep': True})

    def test_read_checkpoint_reuses_completed_call_without_provider(self):
        rows = []
        workflow = NS(id=7, snapshot={})
        class Session:
            def get(self, cls, key): return workflow
            def scalar(self, stmt): return workflow
            def add(self, row): row.id = 100 + len(rows); rows.append(row)
            def flush(self): pass
        @contextmanager
        def session(): yield Session()
        provider = NS(name='대체전송', complete=Mock(return_value=LLMResult('',
            {'level': 'NORMAL', 'rationale': '근거', 'confidence': .9}, Usage(1, 2))))
        engine = E.Engine(AppConfig(), Settings(background=False), None, provider, None)
        engine._workflow_run_id = 7
        with patch.object(E.db, 'session', session), patch.object(E, 'audit'), \
                patch.object(E.runlog, 'gate'), patch.object(E.runlog, 'emit'), patch.object(E.broker, 'publish'):
            first = engine._call(1, 'router', '고정 자료', {}, RouterOutput, [])
            second = engine._call(1, 'router', '고정 자료', {}, RouterOutput, [])
        self.assertEqual(provider.complete.call_count, 1)
        self.assertEqual(first.agent_run_id, second.agent_run_id)
        self.assertTrue(second.ok)

    def test_actual_rag_citations_not_all_retrieved_hits(self):
        from app.analysis_context import rag_usage
        hits = [{'ref': 'R1', 'chunk_id': 1, 'content': '직접 원문'}, {'ref': 'R2', 'chunk_id': 2, 'content': '다른 원문'}]
        output = {'evidence': [{'ref': 'R1', 'quote': '직접 원문', 'supports': '원인'}]}
        result = rag_usage(hits, '[R1] 직접 원문', output)
        self.assertEqual(result, {'retrieved': ['R1', 'R2'], 'delivered': ['R1'], 'cited': ['R1']})


if __name__ == '__main__': unittest.main()
