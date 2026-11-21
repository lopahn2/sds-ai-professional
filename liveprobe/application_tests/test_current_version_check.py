"""Current-version operator request → authenticated lease → observed business operation → disposition."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.application.contracts import Claim, Report
from app.application.current_version import KIND, original_check
from app.application.safety import sha, text_sha
from app.application.storage import Incident, Job
from liveprobe.application_runner.current_version import check_current_version
from liveprobe.application_runner.profiles import Registry
from test_core_support import Harness, HUMAN, PREFIX
from test_runner_recovery_probe import Observer, IMAGE

REG = Registry(Path(__file__).resolve().parents[2])
OLD = 'liveprobe/catalog@sha256:' + 'b' * 64
CLUSTER = {'subject':'deployer', 'runner_role':'cluster'}


class Current(Observer):
    def __init__(self, env):
        super().__init__()
        self.env = env

    def read(self, env, service):
        assert env == self.env and service == 'catalog'
        d = super().read('prd', service)
        d['metadata']['namespace'] = env
        return d

    def business(self, env, service, path):
        assert env == self.env
        return super().business('prd', service, path)


@pytest.fixture
def harness():
    h = Harness()
    h.app.state.control.catalog.profiles['catalog'] = REG.get('catalog')
    yield h
    h.close()


def seed(h, env, *, state='awaiting_prd_approval', operation='GET', path='/api/products'):
    data = {'signal':{'sample': json.dumps({'event':'http_access','service':'catalog', 'environment':env,
                                          'method':operation, 'uri':path, 'status':500})},
            'release':{'valid':True,'release_id':'old-release','plan_sha256':'2'*64, 'protocol':'dev-prd/v2',
                       'artifact':{'image':OLD},'user_test':{'id':'historic','passed':True},'stage_results':{}}}
    with h.app.state.store.transaction() as s:
        s.add(Incident(id='test-case',created_at=h.now-100,state=state,document={
            'service':'catalog','environment':env,'system':'samsung-trade','reason':'catalog failed',
            'stages':[], 'data':data}))
    return PREFIX + '/incidents/test-case/current-version-check'


def post(h, url, **kw):
    return h.client.post(url, headers=HUMAN, json={'request_id':'retry-001','release_id':'old-release',**kw})


def claim(h, actor=CLUSTER):
    return h.app.state.control.claim(Claim(worker_id='worker', capabilities=[KIND]), actor)['job']


def report(h, job, result):
    return h.app.state.control.report(job['id'],Report(result=result,lease_token=job['lease_token'],fencing_token=job['fencing_token']),CLUSTER)


@pytest.mark.parametrize('env', ['dev','prd'])
def test_registered_original_operation_on_current_image_retires_old_approval(harness, env):
    h = harness
    url = seed(h, env)
    assert h.client.get(url).status_code == 401
    assert h.client.get(url,headers=HUMAN).json()['eligible']
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: post(h,url),range(2)))
    assert [r.status_code for r in results] == [200,200]
    assert len({r.json()['latest']['job_id'] for r in results}) == 1
    assert post(h,url,release_id='another-release').status_code == 409
    assert post(h,url,request_id='another-request').status_code == 409
    assert claim(h, {'subject':'qa','runner_role':'qa'}) is None
    job = claim(h)
    assert OLD not in str(job['payload']) and job['payload']['environment'] == env
    assert h.client.get(url, headers=HUMAN).json()['latest']['status'] == 'running'
    c = h.app.state.control
    with pytest.raises(HTTPException) as blocked:
        c.approve('test-case',None,{'subject':'operator'})
    assert blocked.value.detail['code'] == 'current_version_check_pending'
    proof = check_current_version(REG,Current(env),job,lambda:h.now)
    assert proof['check']['image'] == IMAGE and proof['check']['outcome'] == 'passed'
    assert report(h,job,proof) == report(h,job,proof)
    saved = c.detail('test-case')
    assert saved['state'] == 'recovery_review'
    release = saved['data']['release']
    assert release['promotion_disposition'] == 'unnecessary' and release['valid'] is False
    assert release['artifact']['image'] == OLD and release['user_test'] == {'id':'historic','passed':True}
    assert 'approval' not in release and 'prd' not in release['stage_results']
    assert saved['data']['accountability']['recovery_observed_at']
    assert post(h,url).json()['latest']['promotion_unnecessary']
    with pytest.raises(HTTPException):
        c.approve('test-case',None,{'subject':'operator'})
    with c.store.read() as s:
        assert [j.kind for j in s.scalars(select(Job))] == [KIND]


@pytest.mark.parametrize('issue', ['rollout','different_env','different_request','failed','unobserved','timeout'])
def test_nonmatching_or_unsuccessful_check_preserves_approval(harness, issue):
    h = harness
    url = seed(h,'prd')
    assert post(h,url).status_code == 200
    job = claim(h)
    kube = Current('prd')
    if issue == 'rollout':
        kube.after_read = lambda:setattr(kube,'image',OLD)
    elif issue == 'failed':
        kube.status = 500
    elif issue == 'unobserved':
        kube.pods = lambda *args: []
    proof = check_current_version(REG,kube,job,lambda:h.now)
    if issue in ('different_env','different_request'):
        key,value = ('environment','dev') if issue == 'different_env' else ('request_id','someother')
        proof['check'][key] = value
        del proof['check']['receipt_id']
        proof['check']['receipt_id'] = sha(proof['check'])
    if issue == 'timeout':
        h.now += 700
        with pytest.raises(HTTPException):
            report(h, job, proof)
    else:
        report(h,job,proof)
    latest = h.client.get(url, headers=HUMAN).json()['latest']
    assert latest['status'] == ('failed' if issue == 'failed' else 'unverifiable')
    assert latest['reason_ko']
    assert h.detail('test-case')['state'] == 'awaiting_prd_approval'
    assert h.detail('test-case')['data']['release']['valid']


def test_unsupported_journey_and_archived_read_are_preserved(harness):
    h = harness
    url = seed(h,'dev',operation='POST',path='/api/products',state='completed')
    assert '읽기 시험' in h.client.get(url,headers=HUMAN).json()['reason_ko']
    assert post(h,url).status_code == 409
    assert claim(h) is None
    with h.app.state.store.transaction() as s:
        inc = h.app.state.control.get(s,'test-case')
        doc = deepcopy(inc.document)
        sample = json.loads(doc['data']['signal']['sample'])
        sample['method'] = 'GET'
        doc['data']['signal']['sample'] = json.dumps(sample)
        inc.document=doc
    assert post(h,url).status_code == 200
    job = claim(h)
    report(h, job, check_current_version(REG,Current('dev'),job,lambda:h.now))
    assert h.detail('test-case')['state'] == 'completed'
    assert h.detail('test-case')['data']['release']['valid']
    assert not h.client.get(url,headers=HUMAN).json()['latest']['promotion_unnecessary']


def test_saved_reproduction_requires_exact_source_digest_and_registered_arguments():
    sample = 'catalog failed while listing products'
    stored = {'method':'GET', 'path':'/api/products', 'status':500, 'service':'catalog', 'environment':'dev',
              'evidence_sha256':text_sha(sample)}
    doc = {'service':'catalog','environment':'dev','data':{'signal':{'sample':sample},'reproduction':stored}}
    op, reason = original_check(doc, REG.get('catalog'))
    assert reason is None and op['source'] == 'reproduction'
    for changed in ({'evidence_sha256':'f'*64}, {'environment':'prd'}, {'inputs':{'id':'other'}}):
        doc['data']['reproduction'] = {**stored, **changed}
        assert original_check(doc, REG.get('catalog')) == (None, 'operation_unmatched')
    doc['data']['reproduction'] = {**stored, 'path':'/api/products?other=1'}
    assert original_check(doc, REG.get('catalog')) == (None, 'operation_unsupported')


def test_readable_inventory_check_cannot_resolve_reservation_failure():
    log = 'at io.example.AppFaultBusiness.calculateReservationQuantity(x)\nat io.example.StockCampaignController.reserve(x)'
    observed = json.dumps({'event':'http_access','service':'inventory','method':'GET','uri':'/api/stock/a','status':500})
    doc = {'service':'inventory','environment':'dev','data':{'signal':{'sample':log},
           'collection':{'logs':[{'service':'inventory','environment':'dev','text':observed}]}}}
    assert original_check(doc, REG.get('inventory')) == (None, 'inventory_reservation')


def test_no_runner_expires_and_late_receipt_cannot_complete_a_new_attempt(harness):
    h = harness
    url = seed(h, 'dev')
    assert post(h,url).status_code == 200
    old = claim(h)
    stale = check_current_version(REG,Current('dev'),old,lambda:h.now)
    h.now = old['lease_expires_at'] + 1
    new = claim(h)
    assert new['fencing_token'] == old['fencing_token'] + 1
    report(h,new,stale)
    old_row = post(h,url).json()['latest']
    assert old_row['status'] == 'unverifiable'
    assert post(h,url,request_id='another-check-1').status_code == 200
    assert post(h,url).json()['latest'] == old_row
    h.now += 700
    assert claim(h) is None
    saved = h.detail('test-case')['data']['current_version_checks'][-1]
    assert saved['status'] == 'unverifiable' and '기한' in saved['reason_ko'] and saved['observed_at'] is None


def test_valid_approval_and_recheck_are_mutually_exclusive():
    from test_core_analysis import SyntheticTransport
    from test_core_release import CoreReleaseTests
    h = Harness(SyntheticTransport())
    try:
        flow = CoreReleaseTests()
        flow.h = h
        iid = flow.awaiting()
        approval = flow.approval_body(iid)
        c = h.app.state.control
        c.catalog.profiles['order'].update(verification=REG.get('order')['verification'],
                                            image_repository=REG.get('order')['image_repository'])
        with c.store.transaction() as s:
            item = c.get(s,iid)
            c.data(item,'signal',{'sample':json.dumps({'event':'http_access', 'method':'GET', 'status':500,
                   'uri':'/api/orders/check?item=a', 'service':'order','environment':'dev'})})
        url = PREFIX + '/incidents/' + iid
        def write(action):
            payload = approval if action == 'approve-prd' else {'request_id':'race-0001','release_id':approval['release_id']}
            return h.client.post(url+'/'+action,headers=HUMAN,json=payload)
        with ThreadPoolExecutor(2) as pool:
            approved,checked = pool.map(write, ['approve-prd','current-version-check'])
        assert sorted((approved.status_code,checked.status_code)) == [200,409]
        with c.store.read() as s:
            live = list(s.scalars(select(Job).where(Job.incident_id==iid, Job.status=='queued')))
            assert [j.kind for j in live] == (['deploy'] if approved.status_code == 200 else [KIND])
    finally:
        h.close()
