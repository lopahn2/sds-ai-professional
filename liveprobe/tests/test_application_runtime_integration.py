"""실제 Engine/Graph 경로. 대체 경계는 session, HTTP, provider transport뿐이다."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, Null
from sqlalchemy.sql import operators

from app import engine as E
from app.agent_groups import GroupMessage
from app.config import AppConfig, Settings
from app.evidence import Bundle, EvidenceItem
from app.llm import RouterOutput
from app.llm.base import LLMError, LLMResult, Usage
from app.models import Action, AgentRun, AuditLog, Citation, Incident, RunEvent, WorkflowRun


class Rows(list):
    def all(self): return list(self)
    def first(self): return self[0] if self else None


class MemorySession:
    """Only SQL transport is substituted; predicates used by these paths remain evaluated."""
    def __init__(self, *rows): self.rows = list(rows)
    def get(self, cls, key):
        return next((r for r in self.rows if isinstance(r, cls) and getattr(r, 'id', None) == key), None)
    def add(self, row):
        if getattr(row, 'id', None) is None: row.id = 100 + len(self.rows)
        self.rows.append(row)
    def flush(self): pass
    def match(self, expr, row):
        if isinstance(expr, BooleanClauseList):
            values = [self.match(c, row) for c in expr.clauses]
            return all(values) if expr.operator is operators.and_ else any(values)
        left = getattr(row, expr.left.key)
        right = None if isinstance(expr.right, Null) else expr.right.value
        if expr.operator is operators.in_op: return left in right
        if expr.operator is operators.is_: return left is right
        return expr.operator(left, right)
    def scalars(self, stmt):
        desc = stmt.column_descriptions[0]
        cls = desc.get('entity')
        rows = [r for r in self.rows if cls is not None and isinstance(r, cls)
                and all(self.match(c, r) for c in stmt._where_criteria)]
        if stmt._order_by_clauses:
            rows.sort(key=lambda r: r.id, reverse='DESC' in str(stmt._order_by_clauses[0]))
        if stmt._limit_clause is not None: rows = rows[:stmt._limit_clause.value]
        return Rows(rows if desc['expr'] is cls else [getattr(r, desc['name']) for r in rows])
    def scalar(self, stmt):
        if 'coalesce' in str(stmt):
            return max((r.seq for r in self.rows if isinstance(r, RunEvent)), default=0)
        return self.scalars(stmt).first()
    @contextmanager
    def session(self): yield self


def answer():
    return {'summary': 'OOMKilled', 'evidence': [{'ref': 'E2_pods', 'quote': 'OOMKilled', 'supports': 'OOMKilled'}],
            'root_cause_candidates': [{'cause': 'OOMKilled', 'confidence': .95, 'evidence_refs': ['E2_pods']}],
            'counter_evidence': [], 'confidence': .95, 'verdict': 'supported',
            'candidate_verdicts': [{'index': 0, 'verdict': 'supported', 'reason': '직접 관측'}],
            'next_action': 'handoff', 'recommended_steps': [], 'action': None}


def incident(status='open'):
    return Incident(id=1, namespace='stg', workload='svc', status=status, title='합성 사건',
                    signal_reasons=['ApplicationException'], created_at=datetime.now(timezone.utc), handoff={})


def test_engine_records_only_cited_rows_and_rag_usage_without_raw_prompt():
    out = answer()
    out['evidence'].append({'ref': 'R1', 'quote': '직접 원문', 'supports': 'OOMKilled'})
    provider = NS(name='offline', complete=Mock(return_value=LLMResult('', out, Usage(1, 2))))
    memory = MemorySession(incident(), WorkflowRun(id=7, incident_id=1, status='running', snapshot={}))
    engine = E.Engine(AppConfig(), Settings(background=False), None, provider, None)
    engine._workflow_run_id = 7
    hits = [{'ref': 'R1', 'chunk_id': 1, 'score': .9, 'content': '직접 원문'},
            {'ref': 'R2', 'chunk_id': 2, 'score': .8, 'content': '전달만 된 원문'},
            {'ref': 'R3', 'chunk_id': 3, 'score': .7, 'content': '검색만 된 원문'}]
    prompt = '[R1] 직접 원문\n[R2] 전달만 된 원문\n비공개 합성 프롬프트'
    with patch.object(E.db, 'session', memory.session):
        result = engine._call(1, 'k8s_analyst', prompt, {}, GroupMessage, hits,
                              role_model=engine.cfg.workflows.group_roles['openai']['k8s_analyst'])
    assert result.ok
    assert [r.chunk_id for r in memory.rows if isinstance(r, Citation)] == [1]
    audit = next(r for r in memory.rows if isinstance(r, AuditLog) and r.event == 'agent_run')
    assert audit.detail['rag_usage'] == {'retrieved': ['R1', 'R2', 'R3'], 'delivered': ['R1', 'R2'], 'cited': ['R1']}
    assert '비공개 합성 프롬프트' not in json.dumps(audit.detail, ensure_ascii=False)


def test_rag_marker_alone_is_not_body_delivery_and_hidden_quote_is_not_cited():
    from app.analysis_context import rag_usage
    hits = [{'ref': 'R1', 'content': '실제 본문은 전달하지 않음'}]
    out = {'evidence': [{'ref': 'R1', 'quote': '실제 본문', 'supports': '원인'}]}
    assert rag_usage(hits, '검색 결과 [R1] 메타데이터만 존재', out) == {
        'retrieved': ['R1'], 'delivered': [], 'cited': []}


def test_restart_preserves_legacy_failed_status_and_marks_unknown_write_for_requery():
    inc = incident('acting')
    action = Action(id=8, incident_id=1, status='executing', type='rollout_undo',
                    target='stg/Deployment/svc', params={}, after_state={'old': True})
    run = WorkflowRun(id=7, incident_id=1, status='running', snapshot={})
    memory = MemorySession(inc, action, run)
    engine = E.Engine(AppConfig(), Settings(background=False), None, None, None)
    with patch.object(E.db, 'session', memory.session):
        engine.recover_on_startup()
        engine.recover_on_startup()
    assert inc.status == action.status == run.status == 'failed'
    assert action.after_state['old'] is True
    assert action.after_state['reconciliation'] == {'status': 'required', 'write_outcome': 'unknown'}
    assert run.snapshot['write_reconciliation'] == {'action_ids': [8], 'status': 'required'}
    assert engine.q.empty()


def test_restart_resumes_frozen_read_pipeline_without_recollecting_or_new_run():
    from app.runtime_resume import source_fingerprint
    inc = incident('analyzing')
    cfg = AppConfig()
    frozen = json.loads(json.dumps(asdict(Bundle('stg', 'svc')), default=str))
    run = WorkflowRun(id=7, incident_id=1, status='running', phase='analysis', snapshot={
        'collection': frozen, 'execution': {'config': cfg.model_dump(mode='json'),
            'code_sha256': source_fingerprint(), 'model_snapshot': None}})
    memory = MemorySession(inc, run)
    # These transports must stay unused: the persisted bundle already completed this read.
    kube, provider = Mock(), Mock()
    engine = E.Engine(cfg, Settings(background=False), kube, provider, None)
    with patch.object(E.db, 'session', memory.session):
        engine.recover_on_startup()
        assert inc.status == 'analyzing'
        assert run.status == 'resume_pending'
        assert engine.q.get_nowait() == 1
        engine._process(1)
    assert inc.status == 'handed_off'
    assert run.status == 'succeeded'
    assert [r.id for r in memory.rows if isinstance(r, WorkflowRun)] == [7]
    assert run.snapshot['collection'] == frozen
    kube.assert_not_called()
    assert kube.mock_calls == []
    assert provider.mock_calls == []


def test_rag_search_results_are_frozen_in_the_same_workflow_snapshot():
    memory = MemorySession(WorkflowRun(id=7, incident_id=1, status='running', snapshot={}))
    rag = NS(search=Mock(return_value=[NS(chunk_id=1, doc='문서', source='runbook', chunk_ix=0,
        score=.8, hybrid_score=.8, source_uri='local://runbook', locator='문단 1', section='복구',
        license=None, attribution=None, content='고정된 원문')]))
    engine = E.Engine(AppConfig(), Settings(background=False), None, None, rag)
    engine._workflow_run_id = 7
    with patch.object(E.db, 'session', memory.session):
        first = engine._rag('동일 검색')
        first[0]['content'] = '호출자 변조'
        second = engine._rag('동일 검색')
    assert rag.search.call_count == 1
    assert second[0]['content'] == '고정된 원문'
    assert 'rag_searches' in memory.get(WorkflowRun, 7).snapshot


def test_completed_cache_is_scoped_to_role_even_when_prompt_and_model_match():
    out = answer()
    memory = MemorySession(incident(), WorkflowRun(id=7, incident_id=1, status='running', snapshot={}))
    provider = NS(name='offline', complete=Mock(side_effect=lambda *a: LLMResult('', deepcopy(out), Usage(1, 1))))
    engine = E.Engine(AppConfig(), Settings(background=False), None, provider, None)
    engine._workflow_run_id = 7
    rm = engine.cfg.llm.roles['signal_analyst']
    with patch.object(E.db, 'session', memory.session):
        engine._call(1, 'signal_analyst', '동일 원문', {}, GroupMessage, [], role_model=rm)
        engine._call(1, 'result_verifier', '동일 원문', {}, GroupMessage, [], role_model=rm)
    assert provider.complete.call_count == 2


def test_router_group_validation_retries_and_interpreter_share_one_persisted_cap():
    from app.runtime_budget import for_incident
    from app.interpret import Interpreter
    cfg = AppConfig()
    cfg.llm.timeout_s = 1
    cfg.workflows.max_agent_calls = {level: 1 for level in cfg.workflows.max_agent_calls}
    inc = incident()
    inc.handoff = {'summary': 'An unfamiliar synthetic failure'}
    memory = MemorySession(inc, WorkflowRun(id=7, incident_id=1, status='running', snapshot={}))
    replies = [LLMResult('', {'level': 'NORMAL', 'rationale': '합성 판단', 'confidence': .9}, Usage(2, 1)),
               LLMResult('', {'invalid': 'schema'}, Usage(3, 1))]
    provider = NS(name='offline', complete=Mock(side_effect=replies))
    engine = E.Engine(cfg, Settings(background=False), None, provider, None)
    engine._workflow_run_id = 7
    engine._incident_runtime = for_incident(cfg, 1)
    with patch.object(E.db, 'session', memory.session):
        assert engine._classify(1, '민감 원문 없는 합성 입력')['level'] == 'NORMAL'
        rm = cfg.workflows.group_roles['openai']['k8s_analyst']
        group = engine._call(1, 'k8s_analyst', '합성 그룹 입력', {}, GroupMessage, [], role_model=rm)
        assert not group.ok and group.error_kind == 'budget'
        assert group.input_tokens == 3 and group.output_tokens == 1
        assert Interpreter(cfg, provider).run(1) == 0
    state = memory.get(WorkflowRun, 7).snapshot['incident_runtime']
    assert state['provider_attempts'] == provider.complete.call_count == 2
    assert state['known_input_tokens'] == 5
    assert len(state['spans']) == 2
    assert {s['parent'] for s in state['spans']} == {'routing', 'analysis'}
    assert '합성 그룹 입력' not in json.dumps(state, ensure_ascii=False)
    blocked = [r for r in memory.rows if isinstance(r, AgentRun) and r.role == 'interpreter']
    assert blocked and blocked[-1].attempts == 0


def test_quota_deadline_failure_retains_prior_attempt_usage_in_agent_run():
    from app.runtime_budget import IncidentRuntime
    state = {}
    @contextmanager
    def storage(): yield state
    cfg = AppConfig()
    cfg.llm.timeout_s = 1
    provider = NS(name='offline', next_available_in=lambda: 60,
                  complete=Mock(side_effect=LLMError('quota', '합성 한도', usage=Usage(7, 2), duration_ms=11)))
    engine = E.Engine(cfg, Settings(background=False, quota_recheck_s=1), None, provider, None)
    engine._incident_runtime = IncidentRuntime(cfg, storage, clock=lambda: 0, deadline_s=1.5)
    memory = MemorySession(incident())
    with patch.object(E.db, 'session', memory.session):
        result = engine._call(1, 'router', '합성 입력', {}, RouterOutput, [])
    assert not result.ok and result.error_kind == 'budget'
    row = next(r for r in memory.rows if isinstance(r, AgentRun))
    assert row.input_tokens == 7 and row.output_tokens == 2
    assert row.attempts == 1 and row.duration_ms == 11


def test_source_fingerprint_includes_safety_and_dependency_inputs(tmp_path):
    from app import runtime_resume as resume
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'llm').mkdir()
    for name in ('engine', 'agent_groups', 'workflows', 'router', 'evidence', 'rag', 'config',
                 'proposals', 'remediation', 'model_settings', 'runtime_resume', 'safety'):
        (app / (name + '.py')).write_text('# original')
    lock = tmp_path / 'requirements.txt'
    lock.write_text('synthetic==1')
    with patch.object(resume, '__file__', str(app / 'runtime_resume.py')):
        before = resume.source_fingerprint()
        (app / 'safety.py').write_text('# changed safety')
        after = resume.source_fingerprint()
        assert after != before
        lock.write_text('synthetic==2')
        assert resume.source_fingerprint() != after


@pytest.mark.parametrize('level', ['SIMPLE', 'HIGH_RISK'])
def test_engine_uses_same_configured_acceptance_floor_for_all_groups(level):
    cfg = AppConfig()
    cfg.workflows.min_agent_confidence = .9
    out = answer()
    out['confidence'] = .8
    out['root_cause_candidates'][0]['confidence'] = .8
    provider = NS(name='offline', complete=Mock(side_effect=lambda *a: LLMResult('', deepcopy(out), Usage(1, 1))))
    memory = MemorySession(incident('analyzing'))
    engine = E.Engine(cfg, Settings(background=False), None, provider, None)
    decision = E.R.Decision(level, E.R.WORKFLOW[level], {}, '합성 판단', .9, 'rules',
                           policy={'selected_model': cfg.llm.roles['signal_analyst'].model})
    bundle = Bundle('stg', 'svc', pods=[{'reason': 'OOMKilled'}])
    with patch.object(E.db, 'session', memory.session):
        engine._run_decision(1, decision, bundle, {'workload': 'svc'}, 'adaptive', {})
    inc = memory.get(Incident, 1)
    assert inc.status == 'handed_off'
    assert inc.handoff['workflow_status'] == 'unverified'
    assert not any(isinstance(r, Action) for r in memory.rows)


@pytest.mark.parametrize('level,first_role', [
    ('SIMPLE', 'signal_analyst'), ('NORMAL', 'k8s_analyst'),
    ('COMPLEX', 'k8s_log_analyst'), ('HIGH_RISK', 'evidence_curator')])
def test_first_analysis_roles_receive_rag_body_with_explicit_usage(level, first_role):
    cfg = AppConfig()
    out = answer()
    provider = NS(name='offline', complete=Mock(side_effect=lambda *a: LLMResult('', deepcopy(out), Usage(1, 1))))
    rag = NS(search=Mock(return_value=[NS(chunk_id=1, doc='원문 문서', source='runbook', chunk_ix=0,
        score=.8, hybrid_score=.8, source_uri='local://runbook', locator='문단1', section='복구',
        license=None, attribution=None, content='한글 RAG 본문을 실제 전달함')]))
    memory = MemorySession(incident('analyzing'))
    engine = E.Engine(cfg, Settings(background=False), None, provider, rag)
    decision = E.R.Decision(level, E.R.WORKFLOW[level], {}, '합성 판단', .9, 'rules',
                           policy={'selected_model': cfg.llm.roles['signal_analyst'].model})
    with patch.object(E.db, 'session', memory.session):
        engine._run_decision(1, decision, Bundle('stg', 'svc', pods=[{'reason': 'OOMKilled'}]),
                             {'workload': 'svc'}, 'adaptive', {})
    run = next(r for r in memory.rows if isinstance(r, AgentRun) and r.role == first_role)
    audit = next(r for r in memory.rows if isinstance(r, AuditLog) and r.event == 'agent_run'
                 and r.detail['agent_run_id'] == run.id)
    assert audit.detail['rag_usage'] == {'retrieved': ['R1'], 'delivered': ['R1'], 'cited': []}
    assert not any(isinstance(r, Citation) for r in memory.rows)


def test_quota_wait_has_its_own_safe_trace_without_consuming_provider_attempt():
    from app.runtime_budget import IncidentRuntime
    state = {}
    @contextmanager
    def storage(): yield state
    runtime = IncidentRuntime(AppConfig(), storage, clock=lambda: 10, deadline_s=10)
    runtime.wait(.5)
    assert state['provider_attempts'] == 0
    assert state['wait_spans'][0]['parent'] == 'quota_wait'
    assert state['wait_spans'][0]['wait_s'] == .5
    assert state['wait_spans'][0]['trace_id'] == state['trace_id']


def test_validation_retry_spans_share_logical_call_but_have_distinct_rounds():
    from app.runtime_budget import for_incident
    cfg = AppConfig()
    cfg.llm.timeout_s = 1
    memory = MemorySession(incident(), WorkflowRun(id=7, incident_id=1, status='running', snapshot={}))
    provider = NS(name='offline', complete=Mock(side_effect=[
        LLMResult('', {}, Usage(2, 1)),
        LLMResult('', {'level': 'NORMAL', 'rationale': '합성', 'confidence': .9}, Usage(3, 1))]))
    engine = E.Engine(cfg, Settings(background=False), None, provider, None)
    engine._workflow_run_id = 7
    engine._incident_runtime = for_incident(cfg, 1)
    with patch.object(E.db, 'session', memory.session):
        result = engine._call(1, 'router', '원문을 trace에 남기지 않음', {}, RouterOutput, [])
    assert result.ok
    spans = memory.get(WorkflowRun, 7).snapshot['incident_runtime']['spans']
    assert [s['validation_round'] for s in spans] == [1, 2]
    assert spans[0]['logical_call_id'] == spans[1]['logical_call_id']
    assert spans[0]['span_id'] != spans[1]['span_id']
    assert '원문을 trace에 남기지 않음' not in json.dumps(spans, ensure_ascii=False)


def test_collection_target_mismatch_never_silently_reuses_or_recollects():
    run = WorkflowRun(id=7, incident_id=1, snapshot={'collection': asdict(Bundle('prd', 'other'))})
    memory = MemorySession(run)
    kube = Mock()
    engine = E.Engine(AppConfig(), Settings(background=False), kube, None, None)
    engine._workflow_run_id = 7
    with patch.object(E.db, 'session', memory.session), pytest.raises(ValueError, match='대상'):
        engine._collect_or_reuse('stg', 'svc', None)
    assert kube.mock_calls == []


def test_resume_with_changed_code_fails_closed_before_any_transport():
    cfg = AppConfig()
    inc = incident('analyzing')
    run = WorkflowRun(id=7, incident_id=1, status='resume_pending', snapshot={
        'collection': asdict(Bundle('stg', 'svc')), 'execution': {
            'code_sha256': '0' * 64, 'config': cfg.model_dump(mode='json')}})
    memory = MemorySession(inc, run)
    kube, provider = Mock(), Mock()
    engine = E.Engine(cfg, Settings(background=False), kube, provider, None)
    with patch.object(E.db, 'session', memory.session), pytest.raises(RuntimeError, match='지문'):
        engine._process(1)
    assert engine._workflow_run_id == 7
    assert inc.status == 'analyzing'
    assert kube.mock_calls == provider.mock_calls == []


def test_resume_failure_cleanup_terminalizes_its_owned_pending_run():
    inc = incident('analyzing')
    run = WorkflowRun(id=7, incident_id=1, status='resume_pending', snapshot={})
    memory = MemorySession(inc, run)
    engine = E.Engine(AppConfig(), Settings(background=False), None, None, None)
    with patch.object(E.db, 'session', memory.session):
        engine._record_worker_failure(1, RuntimeError('합성 지문 충돌'), worker_run_id=7)
    assert inc.status == 'failed'
    assert run.status == 'failed'


def test_resume_pending_blocks_foreign_cleanup_and_owner_can_stop_it():
    from app import runlog
    inc = incident('analyzing')
    run = WorkflowRun(id=7, incident_id=1, status='resume_pending', snapshot={})
    memory = MemorySession(inc, run)
    assert runlog.cleanup_blocker_locked(memory, 1) is runlog.CleanupResult.PENDING
    with patch.object(E.db, 'session', memory.session):
        runlog.finalize_stop_result(1, worker_run_id=7)
    assert run.status == 'failed' and run.phase == 'stopped'


@pytest.mark.parametrize('test_name,extra', [
    ('test_duplicate_row_and_bundle_texts_use_one_slot_and_keep_agent_conclusion', ()),
    ('test_only_valid_returned_representatives_are_fanned_out', ('partial', 1)),
    ('test_only_valid_returned_representatives_are_fanned_out', ('empty', 0)),
    ('test_only_valid_returned_representatives_are_fanned_out', ('failure', 0)),
    ('test_item_limit_applies_to_unique_inputs_and_never_marks_unsent_items', ()),
    ('test_equal_truncated_prefix_does_not_merge_different_full_raw_hashes', ()),
    ('test_same_text_with_distinct_reading_kinds_is_not_merged', ()),
])
def test_existing_interpret_assertions_with_budget_aware_session_transport(monkeypatch, test_name, extra):
    # Preserve the existing test bodies/assertions; extend ONLY their session transport.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('budget_aware_w6', Path(__file__).with_name('test_p18_w6_interpret_dedup.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.Session
    class BudgetedSession(original):
        def __init__(self, lines):
            super().__init__(lines)
            self.rows[WorkflowRun] = [WorkflowRun(id=7, incident_id=1, status='running', snapshot={})]
        def scalar(self, statement):
            return next(iter(self.scalars(statement)), None)
    monkeypatch.setattr(module, 'Session', BudgetedSession)
    getattr(module, test_name)(monkeypatch, *extra)


def test_restart_does_not_resume_read_checkpoint_when_any_action_was_already_proposed():
    inc = incident('analyzing')
    run = WorkflowRun(id=7, incident_id=1, status='running', snapshot={'collection': asdict(Bundle('stg', 'svc'))})
    memory = MemorySession(inc, run, Action(id=8, incident_id=1, status='proposed'))
    engine = E.Engine(AppConfig(), Settings(background=False), None, None, None)
    with patch.object(E.db, 'session', memory.session):
        engine.recover_on_startup()
    assert inc.status == run.status == 'failed'
    assert engine.q.empty()
