"""Read a bounded excerpt from the deployed source through a source-only worker lease."""
import re
import secrets
import time

from sqlalchemy import or_, select
from .application.safety import fail, safe_source_path, scrub, sha, text_sha
from .application.storage import Event, SourceInquiry
from . import runlog

JOB_PREFIX = 'probe_'
DIGEST = re.compile(r'[^\s@]+@sha256:[0-9a-f]{64}')
HASH = re.compile(r'^[0-9a-f]{64}$')


def frame_matches(frame, path):
    original = frame['file']
    return original == path or original.endswith('/' + path) or path.endswith('/' + original)


def read_source_evidence(iid, question_ko, code_refs, deadline, investigation_seq=None):
    from . import unified
    from .models import Evidence, Incident
    coordinator=unified._SERVICE
    frames=[{'file': f['file'],'line': f['line']} for f in (code_refs.get('frames') or [])[:20]
            if isinstance(f,dict) and isinstance(f.get('file'),str) and len(f['file'])<=360 and type(f.get('line')) is int and f['line']>0]
    if not coordinator or not frames or deadline<=time.monotonic():
        return None
    control=coordinator.control
    if not control.store.ready():
        return None
    with coordinator.sessions() as s:
        case=s.get(Incident,iid)
        if not case or case.namespace not in ('dev','prd'):
            return None
        profile=control.catalog.profiles.get(case.workload)
        if not profile:
            return None
        row=s.scalar(select(Evidence).where(Evidence.incident_id==iid,Evidence.source=='deployment',
            Evidence.target==f'{case.namespace}/Deployment/{case.workload}',Evidence.error.is_(None)).order_by(Evidence.collected_at.desc(),Evidence.id.desc()).limit(1))
        images=(row.payload or {}).get('images',[]) if row else []
        expected=[i for i in images if isinstance(i,str) and DIGEST.fullmatch(i) and
                  i.split('@')[0]==profile.get('image_repository')]
        if len(expected)!=1:
            return None
        payload={'service':case.workload,'environment':case.namespace,'profile_id':profile['id'],
                 'image':expected[0],'frames':frames,'question_ko':scrub(question_ko)[:500]}
    # The same snapshot and frames cannot purchase another investigation by rewording a question.
    key=JOB_PREFIX+sha([coordinator.instance,iid,payload['image'],frames])[:48]
    now=control.clock()
    with control.store.transaction() as s:
        row=s.get(SourceInquiry,key)
        if row is None:
            row=SourceInquiry(id=key,incident_id=iid,payload=payload,created_at=now,
                expires_at=now+max(0,deadline-time.monotonic()),state='queued',fence=0)
            s.add(row)
        elif row.state=='finished':
            return row.result
    cursor = 0
    while time.monotonic() < deadline:
        with control.store.read() as session:
            row = session.get(SourceInquiry, key)
            events = list(session.scalars(select(Event).where(
                Event.incident_id == key, Event.kind == 'source_progress', Event.id > cursor
            ).order_by(Event.id)))
            progress = [(item.id, item.occurred_at, item.document) for item in events]
            done, result = row.state == 'finished', row.result
            expired = row.expires_at <= control.clock()
        for cursor, observed_at, entry in progress:
            title = '배포 버전과 소스 대조' if entry['phase'] == 'pin_source' else '배포 소스 파일 확인'
            runlog.emit(iid, 'investigation_progress', title, {
                **entry, 'at': observed_at, 'investigation_seq': investigation_seq,
            }, actor='source_runner')
        if done:
            return result
        if expired:
            return None
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return None


def claim(control, actor):
    if (actor or {}).get('runner_role')!='source': return None
    now=control.clock()
    with control.store.transaction() as s:
        row=s.scalar(select(SourceInquiry).where(SourceInquiry.expires_at>now,
            or_(SourceInquiry.state=='queued',(SourceInquiry.state=='leased') & (SourceInquiry.lease_until<=now)))
            .order_by(SourceInquiry.created_at).limit(1))
        if row is None: return None
        token=secrets.token_urlsafe(32)
        row.worker=actor['subject'];row.token_hash=text_sha(token);row.fence+=1;row.state='leased'
        row.lease_until=min(row.expires_at,now+control.settings.lease_seconds)
        return {'id':row.id,'kind':'investigate','incident_id':str(row.incident_id),'payload':row.payload,
                'lease_token':token,'fencing_token':row.fence,'lease_expires_at':row.lease_until,'idempotency_key':row.id}


def lease(control, job_id, req, actor, operation):
    with control.store.transaction() as s:
        row=s.get(SourceInquiry,job_id)
        if row is None: fail(404,'job_not_found')
        if (actor or {}).get('runner_role')!='source' or row.worker != actor['subject']: fail(403,'runner_job_subject_conflict')
        if row.fence!=req.fencing_token or row.token_hash!=text_sha(req.lease_token): fail(409,'job_fence_conflict')
        if operation=='result' and row.state=='finished' and row.receipt_sha==sha(req.result):
            return row.receipt
        now=control.clock()
        if row.state!='leased' or min(row.expires_at,row.lease_until)<=now: fail(409,'job_lease_expired')
        if operation=='heartbeat':
            row.lease_until=min(row.expires_at,now+control.settings.lease_seconds)
            return {'job_id':row.id,'fencing_token':row.fence,'lease_expires_at':row.lease_until}
        if operation == 'progress':
            if req.phase not in ('pin_source', 'read_file'):
                fail(422, 'unknown_activity_phase')
            if req.phase == 'read_file':
                if not safe_source_path(req.file) or not any(frame_matches(f, req.file) for f in row.payload['frames']):
                    fail(422, 'activity_file_invalid')
            elif req.file is not None:
                fail(422, 'activity_file_invalid')
            data = {'job_id': row.id, 'seq': req.seq, 'attempt': row.fence,
                    'phase': req.phase, 'file': req.file}
            last = s.scalar(select(Event).where(Event.incident_id == row.id, Event.kind == 'source_progress',
                Event.document['attempt'].as_integer() == row.fence).order_by(Event.id.desc()).limit(1))
            if last and last.document == data:
                return {'accepted': True}
            if last and last.document['seq'] >= req.seq:
                fail(409, 'activity_sequence_conflict')
            s.add(Event(incident_id=row.id, kind='source_progress', occurred_at=now, document=data))
            return {'accepted': True}
        result=req.result
        if result.get('ok') is True:
            if result.get('image')!=row.payload['image'] or not HASH.fullmatch(str(result.get('snapshot_id',''))): fail(422,'source_snapshot_mismatch')
            findings=[]
            if not isinstance(result.get('findings'), list) or len(result['findings']) > 20:
                fail(422, 'source_finding_invalid')
            for finding in result['findings']:
                if not isinstance(finding,dict) or not safe_source_path(finding.get('file')) or not HASH.fullmatch(str(finding.get('file_sha256',''))):fail(422,'source_finding_invalid')
                match=next((f for f in row.payload['frames'] if f['line']==finding.get('line') and frame_matches(f, finding['file'])),None)
                if not match or not isinstance(finding.get('excerpt'),str): fail(422,'source_frame_mismatch')
                findings.append({k:scrub(finding[k]) for k in ('file','line','file_sha256','excerpt')})
                findings[-1]['excerpt']=findings[-1]['excerpt'][:700]
            row.result={'snapshot_id':result['snapshot_id'],'image':result['image'],'findings':findings} if findings else None
        row.receipt_sha=sha(result);row.state='finished'
        row.receipt={'job_id':row.id,'incident_id':str(row.incident_id),'result_sha256':row.receipt_sha,'state':row.state,'accepted_at':now}
        return row.receipt
