"""Chronological pagination and canonical linked rows across two separate databases."""
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db
from app.console_history import console_history_router
from app.human_sessions import local_read_key
from app.models import Incident
from app.application.storage import Incident as Native
from test_unified_incidents import service, seed, ACTOR


def test_observed_domains_classify_unlinked_core_incidents(service, operator_gateway):
    import os
    from app.application.contracts import CreateIncident
    from app.models import Evidence

    service.application.state.local_read_key = local_read_key(os.environ['LIVEPROBE_LOCAL_READ_KEY_FILE'])
    app = FastAPI()
    app.include_router(console_history_router({'unified': service}))
    intake = service.create(CreateIncident(environment='dev', service='order', reason='주문 오류'),
                            'isolated-app-intake', ACTOR)
    app_id = intake['incident_id']
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    reasons = {
        1555: ['ApplicationException', 'NoEndpoints', 'TargetHigh5xxRate', 'TargetServiceNoEndpoints'],
        1556: ['ErrImagePull', 'ImagePullBackOff', 'TargetWorkloadImagePullFailing'],
        1558: ['ApplicationException'],
        1559: ['TargetHigh5xxRate', 'NetworkDrop'],
        1560: ['UnclassifiedLegacyReason'],
    }
    with db.session() as session:
        for iid, signals in reasons.items():
            session.add(Incident(id=iid, fingerprint=f'dev/test-{iid}', namespace='dev', workload='catalog',
                                 severity_level='COMPLEX', status='handed_off', signal_reasons=signals,
                                 created_at=observed_at + timedelta(seconds=iid-1555)))
        session.flush()
        # Routine evidence collection on an image failure does not introduce another signal domain.
        session.add(Evidence(incident_id=1556, source='logs', payload={'excerpt': 'ApplicationException'}))

    with TestClient(app, base_url='http://liveprobe.localhost') as client:
        def query(kind, **params):
            response = client.get('/api/console/incidents', params={'kind': kind, **params})
            assert response.status_code == 200, response.text
            return response.json()

        for kind, ids in [('all', {app_id, *reasons}), ('mixed', {1555, 1559}),
                          ('application', {app_id, 1558}), ('kubernetes', {1556, 1560})]:
            page = query(kind)
            assert page['total'] == len(page['items']) == len(ids)
            assert {row['id'] for row in page['items']} == ids
            if kind != 'all':
                assert all(row['kind'] == kind for row in page['items'])
        old_page = query('mixed', as_of=1556)
        assert [row['id'] for row in old_page['items']] == [1555]
        assert old_page['newer_since_as_of'] == 0  # legacy bookmark also bounds core ID
        first_page = query('mixed', limit=1)
        assert [row['id'] for row in first_page['items']] == [1559]
        second_page = query('mixed', as_of=first_page['as_of'], offset=1, limit=1)
        row, = second_page['items']
        assert row['id'] == 1555 and not row.get('code_action') and not row.get('native_id')
        assert service.summary(1555)['blocked_code'] == 'unverified_code_cause'

        # A later mixed row increments the mixed counter without shifting the bookmarked page.
        with db.session() as session:
            session.add(Incident(id=1561, fingerprint='dev/test-later', namespace='dev', workload='payment',
                                 signal_reasons=['TargetHighLatency', 'TargetWorkloadCrashLooping'],
                                 created_at=datetime.fromtimestamp(first_page['as_of'] / 1e6, timezone.utc)
                                            + timedelta(seconds=1)))
        anchored = query('mixed', as_of=first_page['as_of'], offset=1, limit=1)
        assert anchored['total'] == 2 and anchored['newer_since_as_of'] == 1
        assert [row['id'] for row in anchored['items']] == [1555]


def test_combined_history_with_concurrent_insert_and_all_three_kinds(service, operator_gateway, monkeypatch):
    import os
    service.application.state.local_read_key = local_read_key(os.environ['LIVEPROBE_LOCAL_READ_KEY_FILE'])
    app = FastAPI()
    app.include_router(console_history_router({'unified':service}))
    seed()
    child = service.continue_incident(1, ACTOR)['application_id']
    first = datetime.now(timezone.utc) - timedelta(seconds=100)
    def add_native(iid, ts):
        with service.control.store.transaction() as session:
            session.add(Native(id=iid,state='collecting',created_at=ts,document={
                'id':iid,'environment':'dev','system':'samsung-trade','service':'order',
                'reason':'격리 결제 조회 실패','stages':[],'data':{}}))
    with db.session() as session:
        session.get(Incident,1).created_at = first
        session.add(Incident(id=2,status='analyzing',created_at=first+timedelta(seconds=2),
                             workload='order',namespace='dev',fingerprint='core-only',title='격리 장애'))
    add_native('native-mid', first.timestamp()+1)
    with TestClient(app,base_url='http://liveprobe.localhost') as client:
        def get(**params):
            r=client.get('/api/console/incidents',params=params)
            assert r.status_code == 200, r.text
            return r.json()
        initial=get(limit=1)
        assert initial['total'] == 3 and initial['items'][0]['id'] == 2
        add_native('later', (initial['as_of']+2000000)/1000000)
        second=get(offset=1,limit=1,as_of=initial['as_of'])
        assert [r['id'] for r in second['items']] == ['native-mid']
        assert second['newer_since_as_of'] == 1
        third=get(offset=2,limit=1,as_of=initial['as_of'])
        assert [(r['id'],r['kind']) for r in third['items']] == [(1,'mixed')]
        assert third['items'][0]['code_action']['application_id'] == child
        for kind, expected in [('kubernetes',2),('mixed',1),('application','native-mid')]:
            rows=get(kind=kind,as_of=initial['as_of'])
            assert rows['total'] == 1 and rows['items'][0]['id'] == expected
        assert get(status='analyzing',kind='application')['items'][0]['id'] == 'native-mid'
        assert get(search='격리 결제',as_of=initial['as_of'])['total'] == 1


def test_invalid_intake_does_not_reserve_service_or_add_history(service):
    import pytest
    from fastapi import HTTPException
    from sqlalchemy import select, func
    from app.application.contracts import CreateIncident
    from app.models import CodeActionLink
    with pytest.raises(HTTPException) as err:
        service.create(CreateIncident(environment='dev', service='order', reason='시험', reproduction='not registered'),
                       'invalid-request-test', ACTOR)
    assert err.value.status_code == 422
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Incident)) == 0
        assert session.scalar(select(func.count()).select_from(CodeActionLink)) == 0


def test_source_worker_observations_return_to_the_correct_core_investigation(service, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import select
    from app.application.contracts import Progress, Report
    from app.models import Evidence, RunEvent
    from app.source_investigation import claim, lease, read_source_evidence
    seed()
    image = 'registry.local/order@sha256:'+'a'*64
    monkeypatch.setitem(service.control.catalog.profiles['order'],'image_repository','registry.local/order')
    with db.session() as session:
        session.add(Evidence(incident_id=1,source='deployment',target='dev/Deployment/order',
                             payload={'images':[image]}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        stop=time.monotonic()+6
        investigation=pool.submit(read_source_evidence,1,'오류 줄에서 어떤 값이 반환됐습니까?',
                                  {'frames':[{'file':'/deploy/order.py','line':2}]},stop,19)
        source={'runner_role':'source','subject':'isolated-reading-worker'}
        job=None
        while not job and time.monotonic()<stop:
            job=claim(service.control,source)
            if not job:time.sleep(.03)
        assert job and job['payload']['image']==image
        auth={key:job[key] for key in ('lease_token','fencing_token')}
        lease(service.control,job['id'],Progress(**auth,phase='read_file',seq=1,file='order.py'),source,'progress')
        finding={'file':'order.py','line':2,'file_sha256':'b'*64,'excerpt':'return value'}
        lease(service.control,job['id'],Report(**auth,result={
            'ok':True,'image':image,'snapshot_id':'c'*64,'findings':[finding]}),source,'result')
        assert investigation.result(timeout=7)['findings']==[finding]
    with db.session() as session:
        event=session.scalar(select(RunEvent).where(RunEvent.incident_id==1,RunEvent.kind=='investigation_progress'))
        assert event.detail['job_id']==job['id'] and event.detail['investigation_seq']==19
        assert event.detail['file']=='order.py' and event.detail['attempt']==1


def test_preexisting_invalid_intake_stays_in_history_and_releases_unused_target(service):
    import pytest
    from fastapi import HTTPException
    from sqlalchemy import select,func
    from app.models import AuditLog,CodeActionLink
    from app.unified import active_link
    from app.application.contracts import CreateIncident
    seed()
    request=CreateIncident(environment='dev',service='order',reason='이전 접수',reproduction='not registered')
    with db.session() as session:
        service._reserve(session,session.get(Incident,1),request,ACTOR)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            service._dispatch(1)
        assert error.value.status_code==422
    with db.session() as session:
        link=session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id==1))
        assert link.state=='intake_rejected' and link.projected_status=='failed'
        assert not active_link(session,'dev','order')
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event=='code_intake_rejected'))==1
        assert session.get(Incident,1).handoff is not None
    assert not service.summary(1)['blocks_cluster']
