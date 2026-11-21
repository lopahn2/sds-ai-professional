"""HTTP replay and external source lease checks against an isolated application database."""
import json
from pathlib import Path

from sqlalchemy import select
from app.application.storage import Event, SourceInquiry
from test_core_support import Harness, HUMAN, MACHINE, PREFIX, IMAGE


def test_reconnect_replays_only_bound_progress_and_rejects_stale_or_unknown_files():
    h = Harness()
    try:
        incident = h.create()
        job = h.claim(['collect'])
        lease = {key: job[key] for key in ('lease_token', 'fencing_token')}
        url = PREFIX + '/runner/jobs/' + job['id'] + '/progress'
        def send(**fields):
            return h.client.post(url, headers=MACHINE, json={**lease, **fields}).status_code
        assert send(seq=1, phase='inspect_resources') == 200
        assert send(seq=1, phase='inspect_resources') == 200
        assert send(seq=1, phase='read_logs') == 409
        assert send(seq=2, phase='edit_file', file='src/unrelated.py') == 422
        activity = PREFIX + '/incidents/' + incident + '/activity'
        first = h.client.get(activity, headers=HUMAN).json()
        progress = [e for e in first['events'] if e['event'] == 'runner_progress']
        assert len(progress) == 1
        assert send(seq=2, phase='read_logs') == 200
        second = h.client.get(activity, headers=HUMAN, params={'after':first['cursor']}).json()
        assert [(r['data']['job_id'], r['data']['seq']) for r in second['events']] == [(job['id'], 2)]
        h.now += 60
        assert send(seq=3, phase='read_logs') == 409
    finally:
        h.close()


def test_readonly_investigation_reclaim_attempt_progress_and_duplicate_receipt():
    h = Harness()
    try:
        src = {'Authorization': 'Bearer synthetic-only-source'}
        other = {'Authorization': 'Bearer synthetic-only-cluster'}
        keyfile = Path(h.settings.machine_identity_file)
        keyfile.write_text(json.dumps({'principals':[
            {'subject': kind, 'token':'synthetic-only-'+kind, 'scopes':['runner','runner:'+kind]}
            for kind in ('source','cluster')]}))
        job_id = 'probe_isolated'
        payload = {'image':IMAGE,'service':'order','environment':'dev','profile_id':'order',
                   'frames':[{'file':'/srv/main.py','line':2}], 'question_ko':'배포 버전의 실패 경로 확인'}
        with h.app.state.store.transaction() as session:
            session.add(SourceInquiry(id=job_id, incident_id=11, payload=payload, created_at=h.now,
                                      expires_at=h.now+100, fence=0, state='queued'))
        def claim(actor):
            return h.client.post(PREFIX+'/runner/claim',headers=actor,json={'worker_id':'test','capabilities':['investigate']}).json()['job']
        def post(job, operation, actor=src, **fields):
            return h.client.post(PREFIX+'/runner/jobs/'+job_id+'/'+operation, headers=actor, json={
                'lease_token':job['lease_token'],'fencing_token':job['fencing_token'],**fields})
        assert claim(other) is None
        first = claim(src)
        assert first['kind'] == 'investigate'
        assert post(first,'progress',other,seq=1,phase='pin_source').status_code == 403
        assert post(first,'progress',seq=1,phase='pin_source').status_code == 200
        h.now += 31
        second = claim(src)
        assert second['fencing_token'] == first['fencing_token']+1
        assert post(first,'progress',seq=2,phase='read_file',file='main.py').status_code == 409
        assert post(second,'progress',seq=1,phase='read_file',file='unrelated.py').status_code == 422
        assert post(second,'progress',seq=1,phase='read_file',file='main.py').status_code == 200
        assert post(second,'progress',seq=1,phase='read_file',file='main.py').status_code == 200
        result = {'ok':True, 'image':IMAGE, 'snapshot_id':'a'*64, 'findings':[
            {'line':2,'file':'main.py','file_sha256':'b'*64,'excerpt':'return result'}]}
        receipt = post(second,'result',result=result)
        assert receipt.status_code == 200, receipt.text
        assert post(second,'result',result=result).json() == receipt.json()
        with h.app.state.store.read() as session:
            events = list(session.scalars(select(Event).where(Event.incident_id==job_id).order_by(Event.id)))
            assert [(e.document['attempt'],e.document['phase']) for e in events] == [(1,'pin_source'), (2,'read_file')]
            assert session.get(SourceInquiry,job_id).result['findings'][0]['file'] == 'main.py'
        assert claim(src) is None
    finally:
        h.close()


def test_file_change_requested_separately_and_only_for_its_incident_and_file():
    from app.application.storage import Job
    h = Harness()
    try:
        iid = h.create()
        other = h.create('inventory')
        patch = {'summary_ko':'빈 입력은 거절합니다.', 'tests_added':['tests/test_input.py'],
                 'diff':'--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new\n--- a/hidden.py\n+++ b/hidden.py\n@@ -1 +1 @@\n-other\n+unrelated'}
        with h.app.state.store.transaction() as session:
            session.add(Job(id='job-edit', kind='test_build', status='completed', incident_id=iid,
                idempotency_key='file-read',created_at=h.now,payload={'patch':patch},fence=1))
            entry=Event(incident_id=iid,occurred_at=h.now,kind='runner_progress',document={
                'job_id':'job-edit','kind':'test_build','attempt':1,'seq':2,'phase':'edit_file','file':'main.py'})
            session.add(entry)
            session.flush()
            event_id=entry.id
        path=f'/activity/{event_id}/change'
        def get(target,suffix):
            return h.client.get(PREFIX+'/incidents/'+target+suffix,headers=HUMAN)
        assert '+++ b/main.py' not in get(iid,'/activity').text
        result=get(iid,path).json()
        assert result['file']=='main.py' and '+new' in result['diff']
        assert 'hidden.py' not in result['diff'] and not result['truncated']
        assert get(other,path).status_code==404
    finally:
        h.close()


def test_historical_release_with_evidence_is_explained_without_writes():
    from app.application.storage import Incident
    h = Harness()
    try:
        iid, missing = h.create(), h.create('inventory')
        with h.app.state.store.transaction() as session:
            session.get(Incident,missing).state='failed'
            i=session.get(Incident,iid)
            i.state='completed'
            i.document={**i.document,'data':{'release':{
                'recovery':{'accepted':True,'outcome':'passed','observed_at':h.now},
                'artifact':{'diff_sha256':'b'*64},'stage_results':{'prd':{'verified':True}},
                'tests':{'before_failed':True,'after_passed':True,'report_sha256':'c'*64},
                'summary_ko':'빈 값을 처리하도록 수정했습니다.'},
                'analysis':{'roles':[{'role':'verifier','result':{'accepted':True,'citations':[{'ref':'E1'}]}}]}}}
        def events():
            with h.app.state.store.read() as s:
                return list(s.scalars(select(Event.id).order_by(Event.id)))
        before=events()
        rows=h.client.get(PREFIX+'/reviews/unexplained',headers=HUMAN).json()['items']
        assert [r['id'] for r in rows]==[missing]
        url=PREFIX+'/incidents/'+missing+'/accountability'
        assert h.client.get(PREFIX+'/incidents/'+iid+'/accountability',headers=HUMAN).json()['status']=='explained'
        assert events()==before
        review=h.client.post(url+'/reinvestigate',headers=HUMAN,json={'revision':0}).json()
        assert review['status']=='pending_operator'
        h.client.post(url+'/confirm',headers=HUMAN,json={'revision':review['revision'],'note':'당시 원인 미확정'})
        assert h.detail(missing)['state']=='failed'
    finally:
        h.close()


def test_activity_distinguishes_queue_lost_contact_reclaim_and_historical_observation():
    from app.application.storage import Incident, Job, RunnerContact
    h = Harness()
    try:
        iid = h.create()
        def live():
            return h.client.get(PREFIX + '/incidents/' + iid + '/activity', headers=HUMAN).json()
        original = live()['active_jobs'][0]
        assert original['state'] == 'waiting' and original['claimed_at'] is None
        first = h.claim(['collect'])
        lease = {key: first[key] for key in ('lease_token', 'fencing_token')}
        heartbeat = PREFIX + '/runner/jobs/' + first['id'] + '/heartbeat'
        assert live()['active_jobs'][0]['state'] == 'leased'
        h.now += 3
        assert h.client.post(heartbeat, headers=MACHINE, json=lease).status_code == 200
        current = live()['active_jobs'][0]
        assert current['last_heartbeat_at'] == h.now and current['last_contact_at'] == h.now
        cursor = live()['cursor']
        h.now += 31
        assert live()['active_jobs'][0]['state'] == 'contact_lost'
        assert live()['cursor'] == cursor
        second = h.claim(['collect'])
        current = live()['active_jobs'][0]
        assert current['state'] == 'reclaimed' and current['attempt'] == 2 and current['last_heartbeat_at'] is None
        assert h.client.post(heartbeat, headers=MACHINE, json=lease).status_code == 409
        assert h.report(second, h.collect()).status_code == 200
        view = live()
        assert any(e['event'] == 'job_claimed' and e['data']['attempt'] == 2 for e in view['events'])
        assert all(row['job_id'] != first['id'] for row in view['active_jobs'])
        with h.app.state.store.transaction() as s:
            assert s.get(RunnerContact, (first['id'], 1)).last_heartbeat_at == h.now - 31
            assert s.get(RunnerContact, (first['id'], 2)).last_heartbeat_at is None
            active = s.scalar(select(Job).where(Job.incident_id == iid, Job.kind == 'source'))
            active.status, active.fence, active.lease_until = 'leased', 1, h.now + 10
        assert live()['active_jobs'][0]['state'] == 'unobserved'
        with h.app.state.store.transaction() as s:
            s.get(Incident, iid).state = 'completed'
        assert live()['active_jobs'] == []
    finally:
        h.close()
