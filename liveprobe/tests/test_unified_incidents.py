"""Disposable DB tests: logical incident continuity without model/cluster calls."""
import copy
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app import db
from app.models import Incident, Evidence, AgentRun, CodeActionLink, Action
from app.application.main import create_app
from app.application.settings import ApplicationSettings
from app.application.contracts import CreateIncident, Signal
from app.application.storage import Incident as AppIncident, Job
from app.application.safety import text_sha
from app.unified import UnifiedService, active_link, canonical_request, effective_status_expr
from conftest import needs_db

REFS = [{'file': 'services/order.py', 'line': 12, 'symbol': 'parse_order'}]
ACTOR = {'subject': 'operator-test', 'kind': 'human'}

@pytest.fixture
def service(db_url, monkeypatch):
    application = create_app(ApplicationSettings(database_url='sqlite:///:memory:', testing=True))
    application.state.store.initialize()
    coordinator = UnifiedService(application, 'test-installation')
    monkeypatch.setattr('app.unified._SERVICE', coordinator)
    return coordinator


def seed(*, status='handed_off', severity='COMPLEX', verified=True):
    remediation = {'kind':'code_fix','why':'verified code error','code_refs':copy.deepcopy(REFS),'repro':None}
    with db.session() as s:
        s.add(Incident(id=1,fingerprint='test/1',namespace='dev',workload='order',status=status,
            severity_level=severity,title='order parsing failure',handoff={'workflow_status':'verified' if verified else 'unverified',
            'remediation':{**remediation,'source':'llm'}}))
        s.flush()
        s.add(Evidence(incident_id=1,source='code_refs',payload={'frames':copy.deepcopy(REFS)}))
        s.add(AgentRun(incident_id=1,role='group_verifier',provider='fake',model='synthetic',status='succeeded',
            output={'verdict':'supported','remediation':remediation}))

@needs_db
def test_replay_one_child_one_job_and_core_history_unchanged(service):
    seed()
    with db.session() as s: before=copy.deepcopy(s.get(Incident,1).handoff)
    first=service.continue_incident(1,ACTOR)
    again=service.continue_incident(1,ACTOR)
    assert first['action_id']==again['action_id'] and first['application_id']==again['application_id']
    assert first['state']=='collecting' and first['blocks_cluster']
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(CodeActionLink))==1
        assert s.get(Incident,1).handoff==before and s.get(Incident,1).status=='handed_off'
        assert s.scalar(select(effective_status_expr()).where(Incident.id==1))=='analyzing'
    with service.control.store.read() as s:
        assert s.scalar(select(func.count()).select_from(AppIncident))==1
        assert s.scalar(select(func.count()).select_from(Job))==1

@needs_db
def test_child_commit_response_loss_reuses_child(service,monkeypatch):
    seed(); original=service.control.create_linked
    def committed(*args,**kwargs):
        original(*args,**kwargs)
        raise TimeoutError('synthetic response lost')
    monkeypatch.setattr(service.control,'create_linked',committed)
    with pytest.raises(TimeoutError): service.continue_incident(1,ACTOR)
    assert service.summary(1)['state']=='requested'
    monkeypatch.setattr(service.control,'create_linked',original)
    assert service.continue_incident(1,ACTOR)['state']=='collecting'
    with service.control.store.read() as s: assert s.scalar(select(func.count()).select_from(Job))==1

@needs_db
@pytest.mark.parametrize('changes,code',[({'severity':'HIGH_RISK'},'high_risk'),({'verified':False},'unverified_code_cause'),({'status':'analyzing'},'incident_active')])
def test_reject_unsafe_or_unverified_continuation(service,changes,code):
    seed(**changes)
    with pytest.raises(HTTPException) as exc: service.continue_incident(1,ACTOR)
    assert exc.value.detail['code']==code
    with service.control.store.read() as s: assert s.scalar(select(func.count()).select_from(Job))==0

@needs_db
def test_failed_action_unknown_write_blocks(service):
    seed()
    with db.session() as s: s.add(Action(incident_id=1,type='rollout_restart',target='dev/Deployment/order',policy_mode='auto',
        status='failed',executed=True,after_state={'reconciliation':{'status':'required','write_outcome':'unknown'}}))
    with pytest.raises(HTTPException) as exc: service.continue_incident(1,ACTOR)
    assert exc.value.detail['code']=='unknown_write'

@needs_db
def test_reads_never_create_link_or_job(service):
    seed()
    for _ in range(3): assert service.summary(1)['eligible']
    with db.session() as s: assert s.scalar(select(func.count()).select_from(CodeActionLink))==0
    with service.control.store.read() as s: assert s.scalar(select(func.count()).select_from(Job))==0

@needs_db
def test_feature_off_keeps_existing_fence(service,monkeypatch):
    seed(); service.continue_incident(1,ACTOR)
    monkeypatch.setattr('app.unified._SERVICE',None)
    with db.session() as s:
        assert active_link(s,'dev','order') and not active_link(s,'prd','order')

@needs_db
def test_new_root_dedup_sanitized_signal_and_conflict(service):
    sample='password=synthetic-not-a-real-secret parse failure'
    request=CreateIncident(environment='dev',service='order',reason='parse failure',signal=Signal(source='test',
        observed_at=datetime.now(timezone.utc),sample=sample,sha256=text_sha(sample)))
    first=service.create(request,'request-id-001',ACTOR)
    second=service.create(request,'request-id-001',ACTOR)
    assert first['incident_id']==second['incident_id']
    signal=service.control.detail(first['code_action']['application_id'])['data']['signal']
    assert 'synthetic-not-a-real-secret' not in signal['sample'] and signal['sha256']==text_sha(signal['sample'])
    with pytest.raises(HTTPException) as exc:
        service.create(CreateIncident(environment='dev',service='order',reason='different'),'request-id-001',ACTOR)
    assert exc.value.detail['code']=='request_fingerprint_conflict'

@needs_db
def test_terminal_child_pending_reconcile_keeps_lock_and_active_projection(service):
    seed(); appid=service.continue_incident(1,ACTOR)['application_id']
    with service.control.store.transaction() as s:
        s.get(AppIncident,appid).state='failed'
        s.add(Job(id='reconcile-test',incident_id=appid,kind='reconcile',status='queued',created_at=1,payload={},idempotency_key='test-reconcile',fence=0))
    service.sync_one(1)
    assert service.summary(1)['blocks_cluster']
    with db.session() as s:
        assert active_link(s,'dev','order')
        assert s.scalar(select(effective_status_expr()).where(Incident.id==1))=='analyzing'

def test_canonical_signal_stays_valid_after_scrub():
    sample='Bearer synthetic-secret-token other'
    request=CreateIncident(environment='dev',service='order',reason='failure',signal=Signal(source='test',observed_at=datetime.now(timezone.utc),sample=sample,sha256=text_sha(sample)))
    normalized=canonical_request(request)
    CreateIncident.model_validate(normalized)
    assert 'synthetic-secret-token' not in normalized['signal']['sample']

@needs_db
def test_list_filter_counts_and_terminal_time_use_stable_projection(service,monkeypatch):
    from app.main import app, STATE
    from app.incident_counts import incident_counts
    from fastapi.testclient import TestClient
    seed(); appid=service.continue_incident(1,ACTOR)['application_id']
    monkeypatch.setitem(STATE,'unified',service)
    c=TestClient(app,base_url='http://liveprobe.localhost',headers={'X-LiveProbe-Client':'dashboard'})
    first=c.get('/api/incidents',params={'status':'analyzing','limit':1}).json()
    assert first['total']==1 and first['items'][0]['kubernetes_status']=='handed_off'
    assert c.get('/api/incidents',params={'status':'handed_off'}).json()['total']==0
    with db.session() as s: assert incident_counts(s)['in_progress']==1
    with service.control.store.transaction() as s:
        service.control.transition(s,s.get(AppIncident,appid),'completed')
    service.sync_one(1)
    one=c.get('/api/incidents/1').json()['incident']
    service.sync_one(1)
    two=c.get('/api/incidents/1').json()['incident']
    assert one['closed_at']==two['closed_at'] and one['updated_at']==two['updated_at']
    assert two['status']=='resolved' and two['kubernetes_status']=='handed_off'
    assert c.get('/api/incidents',params={'status':'resolved'}).json()['total']==1
    with db.session() as s: assert incident_counts(s)['in_progress']==0
    c.close()

@needs_db
@pytest.mark.parametrize('path,body',[('close',{'reason':'confirmed record','confirm':True}),
    ('control',{'action':'stop'}),('messages',{'body':'restart this service'}),
    ('handoff/claim',{}),('reassess',{'reason':'read-only review','confirm':True}),('interpretations',{'confirm':True})])
def test_direct_core_writes_cannot_bypass_code_action(service,monkeypatch,path,body):
    from fastapi.testclient import TestClient
    from app.main import app,STATE
    seed();service.continue_incident(1,ACTOR)
    monkeypatch.setitem(STATE,'unified',service)
    monkeypatch.setitem(STATE,'engine',object())
    c=TestClient(app,base_url='http://liveprobe.localhost',headers={'X-LiveProbe-Client':'dashboard'})
    response=c.post('/api/incidents/1/'+path,json=body)
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='code_action_owns_target'
    with db.session() as s: assert s.get(Incident,1).status=='handed_off'
    c.close()


def test_openapi_resolves_unified_body_contracts_and_validation_never_echoes_input():
    from app.main import app
    from fastapi.testclient import TestClient
    schema=app.openapi()
    assert '/api/incidents/{iid}/code-action/review-merge' in schema['paths']
    c=TestClient(app,base_url='http://liveprobe.localhost',headers={'X-LiveProbe-Client':'dashboard'})
    response=c.post('/api/unified/incidents',json={'reason':'secret-do-not-echo','signal':{'sample':'secret-do-not-echo'}})
    assert response.status_code==422 and 'secret-do-not-echo' not in response.text
    assert response.headers['Cache-Control']=='no-store'
    c.close()

@needs_db
def test_application_human_scope_is_required_for_details_and_creation(service,monkeypatch,tmp_path):
    import json
    from dataclasses import replace
    from app.main import app,STATE
    from fastapi.testclient import TestClient
    identity=tmp_path/'human.json'
    identity.write_text(json.dumps({'principals':[{'subject':'reader','token':'synthetic-reader-token-012345','scopes':['incident:read']}]}))
    service.application.state.settings=replace(service.application.state.settings,identity_file=str(identity))
    seed();service.continue_incident(1,ACTOR)
    monkeypatch.setitem(STATE,'unified',service)
    c=TestClient(app,base_url='http://liveprobe.localhost',headers={'X-LiveProbe-Client':'dashboard'})
    assert c.get('/api/incidents/1/code-action/detail').status_code==401
    headers={'Authorization':'Bearer synthetic-reader-token-012345'}
    assert c.get('/api/incidents/1/code-action/detail',headers=headers).status_code==200
    assert c.post('/api/incidents/1/code-action',headers=headers,json={}).status_code==403
    assert c.post('/api/incidents/1/code-action/approve-prd',headers=headers,json={}).status_code in (403,422)
    c.close()

@needs_db
def test_app_origin_and_foreign_incident_commands_cannot_bypass_target_safety(service):
    from app.models import OperatorCommand
    from app.unified import guard_command_targets
    from app.commands import CommandError
    seed();service.continue_incident(1,ACTOR)
    with db.session() as s:
        s.add(Incident(id=2,fingerprint='other',namespace='dev',workload='catalog',status='handed_off',title='other'))
        s.flush()
        command=OperatorCommand(incident_id=2,actor='test',instruction='synthetic',status='awaiting_confirmation',
            plan={'operations':[{'kind':'Deployment','namespace':'dev','name':'order'}]})
        s.add(command);s.flush()
        with pytest.raises(CommandError): guard_command_targets(s,command)
    with pytest.raises(HTTPException):
        service.create(CreateIncident(environment='dev',service='order',reason='new signal'),'request-id-foreign',ACTOR)


def test_flow_continuation_is_a_real_link_without_model_usage():
    from app.unified import append_code_flow
    flow={'nodes':[{'id':'outcome','type':'outcome','depth':3}], 'edges':[]}
    projection={'action_id':'abc','projected_status':'analyzing','state_ko':'코드 분석 중','state_changed_at':None}
    result=append_code_flow(flow,projection)
    node=result['nodes'][-1]
    assert node['type']=='code_action' and node['depth']==4
    assert node['model'] is None and node['tokens'] is None and node['cost_usd'] is None
    assert result['edges'][0]['source']=='outcome' and flow['nodes']==[{'id':'outcome','type':'outcome','depth':3}]


@needs_db
def test_pg_command_confirmation_and_code_continuation_share_target_first_lock_order(service, monkeypatch):
    """Force both admissions to overlap: old Incident->target order deadlocked here."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread
    from types import SimpleNamespace
    from app.models import OperatorCommand
    from app.commands import CommandService
    import app.unified as unified
    seed()
    with db.session() as s:
        command = OperatorCommand(incident_id=1, actor='test', instruction='synthetic no-write admission',
            status='awaiting_confirmation', plan_sha256='a'*64,
            plan={'operations':[{'op':'secret_update','kind':'Secret','namespace':'dev','name':'test-only','keys':['x']}]},
            rollback={'operations':[{}]})
        s.add(command);s.flush(); cid=command.id
    target_held, command_waiting = Event(), Event()
    original = unified.target_lock
    def interleaved(session, namespace, workload):
        if current_thread().name.startswith('continuation'):
            original(session, namespace, workload)
            target_held.set()
            assert command_waiting.wait(5), 'command did not reach admission'
        else:
            command_waiting.set()
            original(session, namespace, workload)
    monkeypatch.setattr(unified,'target_lock',interleaved)
    def continuation():
        try:
            service.continue_incident(1,ACTOR)
        except HTTPException as exc:
            return exc.detail['code']
        raise AssertionError('pending command must block continuation')
    command_service = CommandService(SimpleNamespace(),None,None)
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='continuation') as code_pool:
        code_result=code_pool.submit(continuation)
        assert target_held.wait(5)
        with ThreadPoolExecutor(max_workers=1,thread_name_prefix='command') as command_pool:
            command_result=command_pool.submit(command_service.confirm,cid,'a'*64)
            assert code_result.result(timeout=10)=='cluster_work_active'
            assert command_result.result(timeout=10)['status']=='awaiting_secret'
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(CodeActionLink))==0
        assert s.get(OperatorCommand,cid).result is None  # confirmation only; no secret writer called
