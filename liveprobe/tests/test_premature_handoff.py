"""Premature router handoff correction; all DB tests use the UUID-isolated fixture."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import needs_db
from test_api import client  # noqa: F401
from app import db
from app.config import AppConfig, Settings
from app.engine import Engine
from app.evidence import Bundle
from app.models import Action, AuditLog, Evidence, Incident, RoutingDecision, WorkflowRun
from app.observed_recovery import recovery_proof
from app.router import decide, extract_features


def healthy(age=2):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age)
    b = Bundle('dev', 'delivery')
    b.deployment = {'replicas': 1, 'ready_replicas': 1, 'available_replicas': 1, 'unavailable_replicas': 0,
                    'images': ['img'], 'revisions': [], 'current_revision': 1}
    b.pods = [{'name': 'delivery-pod', 'uid': 'stable-pod-uid', 'containers': [{'name': 'delivery', 'ready': True, 'restart_count': 21,
                'waiting_reason': None, 'last_terminated_at': (stamp - timedelta(minutes=2)).isoformat(),
                'last_terminated_reason': 'Error', 'last_exit_code': 1}]}]
    b.service = {'ready_endpoints': 1, 'matching_pods': ['delivery-pod'], 'selector': {'app': 'delivery'}}
    for source, payload in [('deployment', b.deployment), ('pod_status', b.pods), ('service', b.service),
                            ('endpoints', {'ready_addresses': 1}), ('k8s_event', [])]:
        b.add(source, 'dev/Deployment/delivery', deepcopy(payload), collected_at=stamp)
    return b


def pair():
    first = healthy(2)
    second = deepcopy(first)
    for it in second.items:
        it.collected_at += timedelta(seconds=1)
    return first, second


def engine(monkeypatch, bundles):
    import app.engine as module
    pending = iter(bundles)
    monkeypatch.setattr(module, 'collect', lambda *args, **kwargs: deepcopy(next(pending)))
    cfg = AppConfig(recovery={'observation_interval_s': 0.1})
    e = Engine(cfg, Settings(), SimpleNamespace(), SimpleNamespace(name='test'), None)
    monkeypatch.setattr(e, '_sleep', lambda seconds: None)
    return e


def seed(status='open', signals=None, legacy=False):
    with db.session() as s:
        inc = Incident(fingerprint='dev/delivery', namespace='dev', workload='delivery', status=status,
                       signal_reasons=signals or ['CrashLoopBackOff'], title='prior crash',
                       status_reason='original router handoff', handoff={'summary': 'original reason'})
        s.add(inc)
        s.flush()
        if legacy:
            s.add(RoutingDecision(incident_id=inc.id, level='COMPLEX', workflow='complex_group',
                features={**extract_features(healthy()), 'evidence_state': 'currently_healthy'},
                rationale='original router handoff', decided_by='evidence_state', policy={'handoff_only': True}))
            s.add(WorkflowRun(incident_id=inc.id, status='succeeded', phase='handed_off', snapshot={'original': True}))
        return inc.id


@pytest.mark.parametrize('gap', [None, 'loki', 'code_refs'])
def test_healthy_router_requests_recheck_not_handoff(gap):
    b = healthy()
    if gap:
        b.add(gap, 'dev/delivery', error='unavailable')
    d = decide(b, 15)
    assert d.policy.get('recheck_required') and not d.policy.get('handoff_only')
    assert d.level != 'HIGH_RISK'


@pytest.mark.parametrize('kind', ['exception', 'invalid', 'low'])
def test_classifier_bounded_fallback_and_real_risk(kind):
    from test_router_workflows import family1
    calls = []
    def classifier(prompt):
        calls.append(prompt)
        if kind == 'exception':
            raise RuntimeError('timeout')
        return {} if kind == 'invalid' else {'level': 'SIMPLE', 'confidence': 0.1, 'rationale': 'uncertain'}
    d = decide(family1(), 15, classifier)
    assert len(calls) == 2 and d.level == 'NORMAL' and d.decided_by == 'rules_fallback'
    assert not d.policy.get('handoff_only') and len(d.features['classifier_attempts']) == 2
    risk = decide(family1(), 15, classifier, signal_reasons=['NetworkDrop'])
    assert risk.level == 'HIGH_RISK'


def test_classifier_retry_can_succeed():
    from test_router_workflows import family1
    outcomes = iter([{}, {'level': 'NORMAL', 'confidence': 0.9, 'rationale': 'verified'}])
    assert decide(family1(), 15, lambda _: next(outcomes)).level == 'NORMAL'


def test_old_restarts_and_warnings_are_not_current_failure():
    first, second = pair()
    second.events = [{'type': 'Warning', 'reason': 'Unhealthy', 'last':
                      (first.items[0].collected_at - timedelta(minutes=1)).isoformat()}]
    proof = recovery_proof(first, second, ['CrashLoopBackOff'])
    assert proof and proof['cause_confirmed'] is False and proof['ai_remediation'] is False
    assert len(proof['observations']) == 2


@pytest.mark.parametrize('flaw', ['restart', 'termination', 'unready', 'endpoint', 'critical', 'unknown',
                                   'fresh_error', 'unstamped_error', 'fresh_warning', 'same_time', 'different_target',
                                   'wrong_selector_target', 'replaced_pod', 'old_baseline', 'nan_metric'])
def test_health_proof_rejects_active_unknown_or_incomplete(flaw):
    first, second = pair()
    signals = ['CrashLoopBackOff']
    c = second.pods[0]['containers'][0]
    if flaw == 'restart': c['restart_count'] += 1
    if flaw == 'termination': c['last_terminated_at'] = datetime.now(timezone.utc).isoformat()
    if flaw == 'unready': c['ready'] = False
    if flaw == 'endpoint': second.service['ready_endpoints'] = 0
    if flaw == 'critical': second.items[0].error = 'timeout'
    if flaw == 'unknown': signals.append('UnmappedBusinessFailure')
    if flaw == 'fresh_error': second.logs = [f'[delivery] {datetime.now(timezone.utc).isoformat()} ERROR current']
    if flaw == 'unstamped_error': second.logs = ['[delivery] ERROR current']
    if flaw == 'fresh_warning': second.events = [{'type': 'Warning', 'last': datetime.now(timezone.utc).isoformat()}]
    if flaw == 'same_time': second.items = deepcopy(first.items)
    if flaw == 'different_target': second.namespace = 'prd'
    if flaw == 'wrong_selector_target': second.service['matching_pods'] = ['other-workload-pod']
    if flaw == 'replaced_pod': second.pods[0]['uid'] = 'replacement-pod-uid'
    if flaw == 'old_baseline':
        for item in first.items: item.collected_at -= timedelta(hours=1)
    if flaw == 'nan_metric': second.metrics['http_5xx_rate_per_s'] = {'delivery': float('nan')}
    assert recovery_proof(first, second, signals) is None


def test_latency_needs_observed_metrics_not_readiness():
    first, second = pair()
    assert recovery_proof(first, second, ['TargetHighLatency']) is None
    for b in (first, second):
        b.metrics = {'mean_latency_ms_1m': {'delivery': 12}}
        b.add('prometheus', 'dev/delivery', b.metrics)
    assert recovery_proof(first, second, ['TargetHighLatency'])
    second.metrics['mean_latency_ms_1m']['delivery'] = 900
    assert recovery_proof(first, second, ['TargetHighLatency']) is None


@needs_db
def test_engine_resolves_observation_and_persists_both(db_url, monkeypatch):
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(e, '_run_group', lambda *args: pytest.fail('healthy incident must not run a group'))
    iid = seed()
    e.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'recovery_review' and inc.handoff['observed_recovery']['ai_remediation'] is False
        assert len(list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))) == 10
        run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == iid))
        assert run.status == 'succeeded' and len(run.snapshot['observations']) == 1
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        from app.narrative import build_narrative
        story = build_narrative(s, iid)
        assert story['incident']['recovery_kind'] == 'observed'
        assert '소명 확인 대기' in story['headline']['stage']['label']
        assert '자동 복구 완료' not in str(story)
        assert '소명 근거' in story['headline']['next']


@needs_db
def test_latest_collection_wins_but_failed_history_retained(db_url, monkeypatch):
    first, second = pair()
    failed = deepcopy(first)
    failed.items[0].error = 'timeout'
    e = engine(monkeypatch, [failed, first, second])
    iid = seed()
    e.process(iid)
    with db.session() as s:
        assert s.get(Incident, iid).status == 'recovery_review'
        rows = list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))
        assert len(rows) == 15 and sum(bool(row.error) for row in rows) == 1
        run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == iid))
        assert len(run.snapshot['collection_attempts']) == 2


@needs_db
def test_critical_collection_exhaustion_is_not_authority(db_url, monkeypatch):
    a, b = pair()
    a.items[0].error = b.items[0].error = 'timeout'
    e = engine(monkeypatch, [a, b])
    iid = seed()
    e.process(iid)
    with db.session() as s:
        h = s.get(Incident, iid).handoff['handoff_classification']
        assert h['class'] == 'bounded_recovery_exhausted' and len(h['attempts']) == 2


REASSESS = {'confirm': True, 'reason': '조기 이관 읽기 재확인'}


@needs_db
@pytest.mark.parametrize('active', [False, True])
def test_reassessment_never_writes_and_archives_original(client, monkeypatch, active):
    from app.main import STATE
    first, second = pair()
    if active:
        second.pods[0]['containers'][0]['restart_count'] += 1
    e = engine(monkeypatch, [first, second])
    monkeypatch.setitem(STATE, 'engine', e)
    monkeypatch.setattr(e, '_propose_and_run', lambda *args: pytest.fail('reassessment may not write'))
    monkeypatch.setattr(e, '_run_group', lambda *args: pytest.fail('reassessment may not run write-capable groups'))
    iid = seed('handed_off', legacy=True)
    r = client.post(f'/api/incidents/{iid}/reassess', json=REASSESS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['read_only'] is True and data['cluster_changed'] is False
    assert data['status'] == ('handed_off' if active else 'recovery_review')
    if active:
        assert data['handoff']['reassessment']['outcome'] == 'diagnosed'
    else:
        assert data['handoff']['observed_recovery']['cause_confirmed'] is False
    archived = data['handoff']['reassessments'][0]
    assert archived['status_reason'] == 'original router handoff' and archived['handoff']['summary'] == 'original reason'
    with db.session() as s:
        assert len(list(s.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)))) == 2
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        assert s.scalar(select(AuditLog.id).where(AuditLog.incident_id == iid,
            AuditLog.event == 'incident_reassessment_started')) is not None
    assert client.get(f'/api/incidents/{iid}').json()['incident']['status'] == data['status']
    if not active:
        assert client.get(f'/api/incidents/{iid}').json()['incident']['recovery_kind'] == 'observed'
        item = next(i for i in client.get('/api/incidents').json()['items'] if i['id'] == iid)
        assert item['recovery_kind'] == 'observed'


@needs_db
@pytest.mark.parametrize('conflict', ['action', 'run', 'control', 'command', 'claimed', 'risk', 'arbitrary',
                                     'unknown_command', 'unknown_write', 'malformed_features'])
def test_reassessment_rejects_unsafe_history(client, monkeypatch, conflict):
    from app.main import STATE
    from app.models import RunControl, OperatorCommand
    e = engine(monkeypatch, [])
    monkeypatch.setitem(STATE, 'engine', e)
    iid = seed('handed_off', legacy=True)
    with db.session() as s:
        inc = s.get(Incident, iid)
        if conflict == 'action': s.add(Action(incident_id=iid, type='rollout_undo', target='dev/Deployment/delivery', policy_mode='auto', status='denied'))
        if conflict == 'run': s.add(WorkflowRun(incident_id=iid, status='running', phase='analysis'))
        if conflict == 'control': s.add(RunControl(incident_id=iid, state='stopping'))
        if conflict == 'command': s.add(OperatorCommand(incident_id=iid, actor='x', instruction='restart', status='executing'))
        if conflict == 'unknown_command': s.add(OperatorCommand(incident_id=iid, actor='x', instruction='restart', status='failed', error='process_restarted_outcome_requires_inspection'))
        if conflict == 'unknown_write': inc.handoff = {**inc.handoff, 'handoff_classification': {'unknown_write': True}}
        if conflict == 'claimed': inc.handoff = {**inc.handoff, 'claimed_by': 'other'}
        if conflict == 'risk': inc.signal_reasons = ['NetworkDrop']
        if conflict == 'arbitrary':
            rd = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
            rd.decided_by = 'rules'
        if conflict == 'malformed_features':
            rd = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
            rd.features = {'evidence_state': 'currently_healthy'}
    assert client.post(f'/api/incidents/{iid}/reassess', json=REASSESS).status_code == 409
    with db.session() as s:
        assert s.get(Incident, iid).status == 'handed_off'
        assert not s.get(Incident, iid).handoff.get('reassessments')


@needs_db
def test_reassessment_auth_and_explicit_read_only(client, monkeypatch):
    from app.main import STATE
    monkeypatch.setitem(STATE, 'engine', engine(monkeypatch, []))
    iid = seed('handed_off', legacy=True)
    path = f'/api/incidents/{iid}/reassess'
    assert client.post(path, json=REASSESS, headers={'X-LiveProbe-Client': ''}).status_code == 403
    assert client.post(path, json=REASSESS, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    for body in ({**REASSESS, 'confirm': False}, {**REASSESS, 'reason': ''}, {**REASSESS, 'read_only': False},
                 {**REASSESS, 'observed_recovery_only': False}):
        assert client.post(path, json=body).status_code == 422


def test_exception_clearance_requires_fresh_code_evidence():
    first, second = pair()
    for b in (first, second):
        b.metrics = {'http_5xx_rate_per_s': {'delivery': 0}, 'http_rate_per_s': {'delivery': 1}}
        b.add('prometheus', 'dev/delivery', b.metrics)
    assert recovery_proof(first, second, ['ApplicationException']) is None
    second.add('loki', 'dev/delivery', {'lines': []})
    second.code_refs = {'exceptions': [{'exception': 'CurrentFailure'}]}
    second.add('code_refs', 'dev/delivery', second.code_refs)
    assert recovery_proof(first, second, ['ApplicationException']) is None
    second.code_refs['exceptions'] = []
    assert recovery_proof(first, second, ['ApplicationException'])


@needs_db
def test_new_core_failure_stops_before_group_not_keyerror(db_url, monkeypatch):
    first, second = pair()
    second.items[0].error = 'timeout'
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(e, '_run_decision', lambda *args: pytest.fail('failed fresh collection may not run group'))
    iid = seed()
    e.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'handed_off'
        assert inc.handoff['handoff_classification']['class'] == 'bounded_recovery_exhausted'


@needs_db
def test_new_rules_high_risk_in_reassessment_retained_and_cannot_replay(client, monkeypatch):
    from app.main import STATE
    first, second = pair()
    second.service.update(selector={'app': 'broken'}, matching_pods=[], pods_labeled_app_eq_service=['delivery-pod'])
    e = engine(monkeypatch, [first, second])
    monkeypatch.setitem(STATE, 'engine', e)
    iid = seed('handed_off', legacy=True)
    response = client.post(f'/api/incidents/{iid}/reassess', json=REASSESS)
    assert response.status_code == 200, response.text
    assert response.json()['handoff']['handoff_classification']['class'] == 'authority_boundary'
    with db.session() as s:
        assert s.get(Incident, iid).severity_level == 'HIGH_RISK'
    assert client.post(f'/api/incidents/{iid}/reassess', json=REASSESS).status_code == 409


@needs_db
def test_verified_group_no_action_resolves_only_with_new_health_proof(db_url, monkeypatch):
    import app.engine as module
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(module, 'run_group', lambda *args: {'status': 'verified', 'reason': 'read before deciding',
        'budget': {'agent_calls': 6}, 'summary': '원인 미확정', 'action': None})
    iid = seed('analyzing')
    e._run_group(iid, decide(first, 15), first, {'signals': ['CrashLoopBackOff'], 'workload': 'delivery'})
    with db.session() as s:
        assert s.get(Incident, iid).status == 'recovery_review'


@needs_db
def test_verified_group_no_action_unproven_is_diagnostic_not_authority(db_url, monkeypatch):
    import app.engine as module
    first, second = pair()
    second.pods[0]['containers'][0]['restart_count'] += 1
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(module, 'run_group', lambda *args: {'status': 'verified', 'reason': 'cause unclear',
        'budget': {'agent_calls': 6}, 'summary': '원인 미확정'})
    iid = seed('analyzing')
    e._run_group(iid, decide(first, 15), first, {'signals': ['CrashLoopBackOff'], 'workload': 'delivery'})
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'handed_off'
        h = inc.handoff['handoff_classification']
        assert h['class'] == 'bounded_recovery_exhausted' and h['budget']['agent_calls'] == 6


def test_classifier_does_not_multiply_exhausted_engine_retries():
    from app.router import ClassifierRecoveryExhausted
    from test_router_workflows import family1
    calls = []
    def exhausted(prompt):
        calls.append(prompt)
        raise ClassifierRecoveryExhausted('shared budget used')
    assert decide(family1(), 15, exhausted).level == 'NORMAL'
    assert len(calls) == 1


@needs_db
def test_concurrent_reassessments_admit_only_one_live_run(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.main import STATE, reassess_incident, ReassessIn
    from fastapi import HTTPException
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    entered, release = Event(), Event()
    original = e.reassess_read_only
    def held(iid, run_id):
        entered.set()
        assert release.wait(10), 'concurrent test failed to release read-only lane'
        original(iid, run_id)
    monkeypatch.setattr(e, 'reassess_read_only', held)
    monkeypatch.setitem(STATE, 'engine', e)
    iid = seed('handed_off', legacy=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(reassess_incident, iid, ReassessIn(**REASSESS), 'local-operator')
        assert entered.wait(10)
        try:
            with pytest.raises(HTTPException) as denied:
                reassess_incident(iid, ReassessIn(**REASSESS), 'local-operator')
            assert denied.value.status_code == 409
        finally:
            release.set()
        assert winner.result(timeout=10)['status'] == 'recovery_review'
    with db.session() as s:
        assert len(s.get(Incident, iid).handoff['reassessments']) == 1
        assert len(list(s.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)))) == 2


@needs_db
def test_active_fault_runs_group_and_existing_executor_boundary(db_url, monkeypatch):
    import app.engine as module
    first, second = pair()
    for b in (first, second):
        b.pods[0]['containers'][0].update(ready=False, waiting_reason='CrashLoopBackOff')
    e = engine(monkeypatch, [first, second, deepcopy(second)])
    monkeypatch.setattr(e, '_classify', lambda *args: {'level': 'COMPLEX', 'rationale': 'active crash', 'confidence': 0.9})
    group_calls, executor_calls = [], []
    def group(*args):
        group_calls.append(True)
        return {'status': 'verified', 'summary': '현재 장애', 'action': {'propose': True, 'type': 'rollout_restart',
                'kind': 'Deployment', 'name': 'delivery', 'params': {}}}
    def executor(iid, level, proposal, summary):
        executor_calls.append((level, proposal['type']))
        return False  # existing executor denies: retain the actual authority boundary
    monkeypatch.setattr(module, 'run_group', group)
    monkeypatch.setattr(e, '_propose_and_run', executor)
    iid = seed()
    e.process(iid)
    assert group_calls == [True] and executor_calls == [('COMPLEX', 'rollout_restart')]
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'handed_off' and 'observed_recovery' not in inc.handoff
        assert inc.handoff['handoff_classification']['class'] == 'authority_boundary'


@needs_db
def test_old_group_input_gets_two_fresh_observations(db_url, monkeypatch):
    old = healthy(600)
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    iid = seed('analyzing')
    recovered, _ = e._observe_recovery(iid, old, {'signals': ['CrashLoopBackOff']}, phase='group_no_action')
    assert recovered
    with db.session() as s:
        assert len(list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))) == 10
        observations = s.get(Incident, iid).handoff['observed_recovery']['observations']
        assert len(observations) == 2
        assert all(datetime.fromisoformat(o['observed_at']) > old.items[0].collected_at for o in observations)


@needs_db
def test_fault_cleared_during_group_gets_two_new_healthy_observations(db_url, monkeypatch):
    import app.engine as module
    active = healthy(10)
    active.pods[0]['containers'][0].update(ready=False, waiting_reason='CrashLoopBackOff')
    first, second = pair()
    e = engine(monkeypatch, [first, second])
    monkeypatch.setattr(module, 'run_group', lambda *args: {'status': 'verified', 'summary': '추가 현재 상태 확인 필요'})
    iid = seed('analyzing')
    e._run_group(iid, decide(active, 15), active, {'signals': ['CrashLoopBackOff'], 'workload': 'delivery'})
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == 'recovery_review'
        assert len(inc.handoff['observed_recovery']['observations']) == 2
        assert len(list(s.scalars(select(Evidence).where(Evidence.incident_id == iid)))) == 10
