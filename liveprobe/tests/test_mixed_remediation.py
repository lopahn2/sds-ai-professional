"""Full core coordinator + Application receipts + bounded typed executor, without any cluster."""
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import actions, db
from app.application.storage import Incident as AppIncident, Job
from app.environments import QA_RELEASE_PROTOCOL
from app.models import Action, AgentRun, Evidence, Incident
from app.unified import active_link, defer_mixed
from conftest import needs_db, mk_pod
from test_typed_actions import ExecKube, cfg
from test_unified_incidents import ACTOR, REFS, service

IMAGE = 'local/order@sha256:' + 'a' * 64
REL = 'dev-release-1'


@pytest.fixture
def mixed(service, request):
    kind = getattr(request, 'param', 'both')
    remediation = {'kind': kind, 'why': '예외와 메모리 제한', 'code_refs': REFS, 'repro': None}
    message = {'remediation': remediation, 'verdict': 'supported', 'next_action': 'act',
               'action': {'type': 'set_memory_limit', 'target': 'order', 'memory_limit_mi': 640},
               'evidence': [{'ref': 'E2_pods', 'quote': 'OOMKilled', 'supports': 'OOMKilled'}]}
    with db.session() as session:
        inc = Incident(id=1, fingerprint='mixed', namespace='prd', workload='order', system='liveprobe-target',
            status='handed_off', severity_level='COMPLEX', title='결제 예외 및 메모리 부족',
            handoff={'remediation': {**remediation, 'source': 'llm'}})
        session.add(inc)
        session.flush()
        session.add(Evidence(incident_id=1, source='code_refs', payload={'frames': REFS}))
        session.add(Evidence(incident_id=1, source='pod_status', target='prd/pods(app=order)',
            payload=[{'containers': [{'name': 'order', 'last_terminated_reason': 'OOMKilled'}]}]))
        for role in ['recovery_planner', 'group_verifier']:
            run = AgentRun(incident_id=1, role=role, provider='fake', model='synthetic', status='succeeded', output=message)
            session.add(run)
            session.flush()
        inc.handoff = {**inc.handoff, 'code_cause_verification': {'verified': True, 'verifier_run_id': run.id}}
    app_id = service.continue_incident(1, ACTOR)['application_id']
    kube = ExecKube()
    dep = kube.deployments['order']
    dep['metadata']['namespace'] = 'dev'
    dep['spec']['template']['spec']['containers'][0]['image'] = IMAGE
    pod = mk_pod('order-pod', 'order', 'order-rs', image=IMAGE)
    pod['status'].update(phase='Running', conditions=[{'type': 'Ready', 'status': 'True'}])
    kube.pods = [pod]
    engine = SimpleNamespace(exec_kube=kube, cfg=cfg(target_namespaces=['dev', 'prd'], rollout_verify_timeout_seconds=0))
    service.mixed_engine = lambda: engine
    with service.control.store.transaction() as s:
        app = s.get(AppIncident, app_id)
        document = copy.deepcopy(app.document)
        stage = {'deploy_job_id': 'deploy-mixed', 'verify_job_id': 'verify-mixed', 'write_outcome': 'written',
                 'deployed': True, 'verified': True, 'targets': [{'environment': 'dev', 'service': 'order',
                 'uid': 'uid-order', 'image': IMAGE}], 'verification': [{'environment': 'dev', 'service': 'order',
                 'image': IMAGE, 'business': {'ok': True, 'status_code': 200, 'body_sha256': '3'*64}}]}
        assert (document['data'].get('mixed_required') is True) == (kind == 'both')
        document['data'].update(release={'release_id': REL, 'valid': True, 'protocol': QA_RELEASE_PROTOCOL,
            'infra_gate': service.control.infra_gate_for(document['data']), 'affected_services': ['order'],
            'artifact': {'image': IMAGE}, 'stage_results': {'dev': stage}})
        app.document = document
        service.control.transition(s, app, 'dev_infra_pending')
        for kind in ('deploy', 'verify'):
            s.add(Job(id=kind+'-mixed', kind=kind, incident_id=app_id, status='completed', created_at=1,
                idempotency_key=kind+'-mixed', fence=3, result_hash='2'*64, receipt={'result_sha256': '2'*64},
                payload={'stage': 'dev', 'release_id': REL, 'artifact': {'image': IMAGE}}))
    return service, app_id, kube


@needs_db
def test_committed_kubernetes_receipt_advances_release_before_any_prd_write(mixed):
    coordinator, app_id, kube = mixed
    assert {'application_id': app_id, 'release_id': REL} in coordinator.control.mixed_pending()
    receipt = coordinator.advance_mixed(app_id, REL)
    assert receipt['state'] == 'verified'
    assert coordinator.control.record_mixed_infra(app_id, receipt)['state'] == 'prd_targets_collecting'
    assert not coordinator.control.mixed_pending()
    with coordinator.control.store.read() as s:
        app = s.get(AppIncident, app_id)
        assert app.document['data']['release']['infra']['receipt_sha256'] == receipt['receipt_sha256']
        assert all(job.kind != 'deploy' or job.payload.get('stage') != 'prd' for job in s.scalars(select(Job)))
    assert len(kube.writes) == 1


@needs_db
@pytest.mark.parametrize('mixed', ['code_fix','both'], indirect=True)
def test_normal_poll_consumes_decision_and_recovers_lost_ack(mixed, monkeypatch):
    coordinator, app_id, kube = mixed
    mixed_required = coordinator.mixed_required(app_id)
    consumer = coordinator.control.record_mixed_infra
    def lost_ack(*args):
        raise TimeoutError('receipt delivery interrupted')
    monkeypatch.setattr(coordinator.control, 'record_mixed_infra', lost_ack)
    coordinator.advance_pending_mixed()
    assert coordinator.control.detail(app_id)['state'] == 'dev_infra_pending'
    monkeypatch.setattr(coordinator.control, 'record_mixed_infra', consumer)
    coordinator.advance_pending_mixed()
    coordinator.advance_pending_mixed()
    doc = coordinator.control.detail(app_id)
    assert doc['state'] == 'prd_targets_collecting'
    assert doc['data']['release']['infra']['state'] == ('verified' if mixed_required else 'not_required')
    assert len(kube.writes) == int(mixed_required) and len(doc['data']['infra_history']) == 1
    assert coordinator.advance_mixed('unobserved-parent', REL)['state'] == 'waiting'


@needs_db
def test_verified_dev_before_typed_action_replay_preserves_prd(mixed):
    coordinator, app_id, kube = mixed
    assert defer_mixed(1) and coordinator.mixed_required(app_id)
    with db.session() as s:
        assert active_link(s, 'dev', 'order') and active_link(s, 'prd', 'order')
    first = coordinator.advance_mixed(app_id, REL)
    assert first['state'] == 'verified', first
    second = coordinator.advance_mixed(app_id, REL)
    assert first == second and len(kube.writes) == 1
    with db.session() as session:
        action = session.get(Action, int(first['action_id']))
        assert action.target == 'dev/Deployment/order' and action.after_state['checks']['limit_applied']
        assert session.get(Incident, 1).namespace == 'prd'
    assert first['dev_image'] == IMAGE and len(first['receipt_sha256']) == 64
    assert any(op['path'] == '/metadata/uid' and op['op'] == 'test' for op in kube.writes[0][-1])
    kube.deployments['order']['spec']['template']['spec']['containers'][0]['resources']['limits']['memory'] = '512Mi'
    assert coordinator.advance_mixed(app_id, REL)['reason'] == 'mixed_verified_effect_changed'
    assert len(kube.writes) == 1


@needs_db
@pytest.mark.parametrize('change', ['stale_release', 'inflight', 'unverified', 'wrong_image', 'wrong_uid',
                                    'other_owner', 'fabricated_evidence', 'proposal_disagreement', 'unknown_write',
                                    'competing_app_lease', 'competing_cluster_action'])
def test_mixed_requires_evidence_and_fenced_target_and_never_replays(mixed, change):
    coordinator, app_id, kube = mixed
    if change in ('inflight', 'unverified'):
        with coordinator.control.store.transaction() as s:
            job = s.get(Job, 'verify-mixed')
            if change == 'inflight': job.status = 'leased'
            else: job.receipt = None
    if change == 'wrong_image': kube.deployments['order']['spec']['template']['spec']['containers'][0]['image'] = 'old'
    if change == 'wrong_uid': kube.deployments['order']['metadata']['uid'] = 'replacement'
    if change == 'other_owner': kube.deployments['order']['metadata']['annotations']['liveprobe.io/application-fence'] = '8'
    if change in ('fabricated_evidence', 'proposal_disagreement'):
        with db.session() as s:
            row = s.scalar(select(AgentRun).where(AgentRun.role == 'recovery_planner'))
            msg = copy.deepcopy(row.output)
            if change == 'fabricated_evidence': msg['evidence'][0]['quote'] += ' never seen'
            else: msg['action']['type'] = 'rollout_restart'
            row.output = msg
    if change == 'unknown_write': kube.raise_on_patch = TimeoutError('ambiguous API timeout')
    if change == 'competing_app_lease':
        with coordinator.control.store.transaction() as s:
            s.add(Job(id='other-write', incident_id='other', kind='deploy', status='leased', created_at=1,
                      payload={'stage': 'dev', 'targets': [{'service': 'order'}]}, idempotency_key='other-write'))
    if change == 'competing_cluster_action':
        with db.session() as s:
            s.add(Action(incident_id=1, type='rollout_restart', target='dev/Deployment/order',
                         params={}, policy_mode='auto', status='executing', lock_key=actions.workload_lock('dev', 'order')))
    release = REL+'-stale' if change == 'stale_release' else REL
    first = coordinator.advance_mixed(app_id, release)
    assert first['state'] in ('blocked', 'waiting') and not kube.writes
    assert coordinator.advance_mixed(app_id, release)['state'] in ('blocked', 'waiting') and not kube.writes
    if change == 'unknown_write':
        with db.session() as s:
            assert s.scalar(select(Action)).status == 'indeterminate'
