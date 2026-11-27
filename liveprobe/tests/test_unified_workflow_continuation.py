"""Actual SIMPLE graph and persisted independent review continue one incident."""
from copy import deepcopy
import pytest
from sqlalchemy import select, func
from app import db
from app.config import AppConfig, Settings
from app.evidence import Bundle
from app.engine import Engine
from app.models import Incident, AgentRun, Evidence, CodeActionLink
from app.router import Decision
from app.workflows import AgentCall, build_simple_graph
from app.unified import UnifiedService, auto_continue
from app.application.main import create_app
from app.application.settings import ApplicationSettings
from conftest import needs_db

REFS=[{'file':'services/order.py','line':12,'symbol':'parse_order'}]
QUOTE='TypeError: order parser failed'


def message(kind='code_fix'):
    cause='Order parser TypeError'
    return {'summary':cause,'summary_ko':'주문 코드의 파싱 오류입니다.',
        'evidence':[{'ref':'E7_error_logs','quote':QUOTE,'supports':cause}],
        'root_cause_candidates':[{'cause':cause,'confidence':.95,'evidence_refs':['E7_error_logs']}],
        'counter_evidence':[],'confidence':.95,'verdict':'supported',
        'candidate_verdicts':[{'index':0,'verdict':'supported','reason':'실제 예외 인용 확인'}],
        'next_action':'handoff','action':None,'recommended_steps':[],
        'remediation':{'kind':kind,'why':'observed code exception','code_refs':deepcopy(REFS),'repro':None}}


def state():
    return {'incident':{'namespace':'dev','workload':'order','severity_level':'SIMPLE'},
        'features':{},'evidence':{'E7_error_logs':[QUOTE],'E8_code_refs':{'frames':deepcopy(REFS)}},
        'rag':[],'min_confidence':.6,'allowlist':['rollout_undo']}


def test_real_simple_graph_verifies_code_cause_without_creating_kubernetes_action():
    roles=[]
    def call(role,prompt):
        roles.append(role)
        return AgentCall(True,message(),len(roles))
    result=build_simple_graph(call).invoke(state())
    assert roles==['signal_analyst','result_verifier']
    assert result['outcome']=='handoff' and result['handoff']['workflow_status']=='verified'
    assert not result.get('action')


@pytest.mark.parametrize('mutation',['conflicting_refs','low_confidence','forged_quote','high_risk'])
def test_unverified_or_conflicting_simple_code_cause_never_marks_verified(mutation):
    current=state(); a=message();v=message()
    if mutation=='conflicting_refs':
        extra={'file':'services/other.py','line':20,'symbol':'other'}
        current['evidence']['E8_code_refs']['frames'].append(extra)
        v['remediation']['code_refs']=[extra]
    elif mutation=='low_confidence':v['confidence']=.1
    elif mutation=='forged_quote':v['evidence'][0]['quote']='invented quote not in source'
    else:current['incident']['severity_level']='HIGH_RISK'
    result=build_simple_graph(lambda role,prompt:AgentCall(True,deepcopy(a if role=='signal_analyst' else v),1)).invoke(current)
    assert result['outcome']=='handoff'
    assert result['handoff'].get('workflow_status')!='verified'


@pytest.fixture
def coordinator(db_url,monkeypatch):
    application=create_app(ApplicationSettings(database_url='sqlite:///:memory:',testing=True))
    application.state.store.initialize()
    service=UnifiedService(application,'workflow-test')
    monkeypatch.setattr('app.unified._SERVICE',service)
    return service


@needs_db
def test_real_simple_graph_engine_persistence_and_auto_hook_continue_same_root(coordinator,monkeypatch):
    cfg=AppConfig()
    engine=Engine(cfg,Settings(_env_file=None),None,None,None)
    bundle=Bundle('dev','order',logs=[QUOTE],code_refs={'frames':deepcopy(REFS)})
    with db.session() as s:
        s.add(Incident(id=1,fingerprint='simple-test',namespace='dev',workload='order',status='analyzing',severity_level='SIMPLE',title='order code exception'))
        s.flush();s.add(Evidence(incident_id=1,source='code_refs',payload=bundle.code_refs))
    def call(role,prompt):
        output=message()
        with db.session() as s:
            run=AgentRun(incident_id=1,role=role,provider='synthetic-test',model='synthetic',status='succeeded',output=output)
            s.add(run);s.flush();run_id=run.id
        return AgentCall(True,output,run_id)
    monkeypatch.setattr(engine,'_agent_runner',lambda *a,**kw:call)
    monkeypatch.setattr(engine,'_rag',lambda query:[])
    decision=Decision('SIMPLE','simple_group',{},'test',.95,'rules',policy={'selected_model':'synthetic'})
    engine._run_decision(1,decision,bundle,{'namespace':'dev','workload':'order','signals':['AppError']},'adaptive',{})
    with db.session() as s:
        inc=s.get(Incident,1)
        assert inc.handoff['workflow_status']=='verified'
        assert inc.handoff['remediation']['source']=='llm'
        assert inc.handoff['code_cause_verification']['verified']
    auto_continue(1)
    assert coordinator.summary(1)['linked']
    assert coordinator.summary(1)['state']=='collecting'


@needs_db
def test_both_code_marker_survives_successful_mitigation_and_rejects_new_failed_review(coordinator):
    engine=Engine(AppConfig(),Settings(_env_file=None),None,None,None)
    bundle=Bundle('dev','order',logs=[QUOTE],code_refs={'frames':deepcopy(REFS)})
    output=message('both')
    with db.session() as s:
        s.add(Incident(id=1,fingerprint='both-test',namespace='dev',workload='order',status='resolved',severity_level='COMPLEX',title='mitigated but needs code fix',handoff={}))
        s.flush()
        s.add(Evidence(incident_id=1,source='code_refs',payload=bundle.code_refs))
        s.add(AgentRun(incident_id=1,role='group_verifier',provider='synthetic-test',model='synthetic',status='succeeded',output=output))
    engine._save_remediation(1,bundle,output,verified=True)
    assert coordinator.summary(1)['eligible']
    with db.session() as s:
        s.add(AgentRun(incident_id=1,role='group_verifier',provider='synthetic-test',model='synthetic',status='failed',output=None))
    assert not coordinator.summary(1)['eligible']
    engine._save_remediation(1,bundle,output,verified=False)
    with db.session() as s: assert s.get(Incident,1).handoff['code_cause_verification']['verified'] is False
    assert not coordinator.summary(1)['eligible']


@needs_db
def test_group_both_marker_survives_actual_engine_early_return_after_mitigation(coordinator,monkeypatch):
    import app.engine as engine_module
    engine=Engine(AppConfig(),Settings(_env_file=None),None,None,None)
    bundle=Bundle('dev','order',logs=[QUOTE],code_refs={'frames':deepcopy(REFS)})
    output=message('both')
    with db.session() as s:
        s.add(Incident(id=1,fingerprint='both-return',namespace='dev',workload='order',status='analyzing',severity_level='COMPLEX',title='code failure'))
        s.flush()
        s.add(Evidence(incident_id=1,source='code_refs',payload=bundle.code_refs))
        review=AgentRun(incident_id=1,role='group_verifier',provider='synthetic-test',model='synthetic',status='succeeded',output=output)
        s.add(review);s.flush();review_id=review.id
    result={'status':'verified','remediation':output['remediation'],'summary':'verified code and mitigation',
        'action':{'propose':True,'type':'rollout_restart','why':'bounded process mitigation'},
        'root_cause_candidates':output['root_cause_candidates'],'confidence':.95}
    monkeypatch.setattr(engine_module,'run_group',lambda *a,**kw:deepcopy(result))
    monkeypatch.setattr(engine,'_rag',lambda query:[])
    def mitigation(iid,level,proposal,summary):
        assert proposal['type']=='rollout_restart'
        engine._set_status(iid,'resolved',status_reason='synthetic mitigation verified')
        return True
    monkeypatch.setattr(engine,'_propose_and_run',mitigation)
    engine._run_group(1,Decision('COMPLEX','complex_group',{},'test',.95,'rules',policy={}),bundle,
        {'namespace':'dev','workload':'order','signals':['AppError']})
    with db.session() as s:
        inc=s.get(Incident,1)
        assert inc.status == 'resolved'
        assert inc.handoff['workflow_status'] == 'verified'
        assert inc.handoff['code_cause_verification']['verified']
        assert inc.handoff['code_cause_verification']['verifier_run_id']==review_id
    auto_continue(1)
    assert coordinator.summary(1)['state']=='collecting'
