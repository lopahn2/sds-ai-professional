"""Safe operator projections of persisted model and runner activity."""
from .safety import safe_source_path, fail, scrub

PHASES = {
    'collect': ('inspect_resources','read_logs'), 'source': ('pin_source',),
    'test_build': ('reproduce','edit_file','regression','build'),
    'qa': ('test_journey',), 'recovery': ('observe_original_request',),
    'current_version_check': ('observe_original_request',),
    'deploy': ('apply_release',), 'verify': ('verify_release',), 'reconcile': ('reconcile_write',),
    'investigate': ('read_logs','pin_source','read_file'),
}


def record_contact(session, job, now, kind):
    from .storage import RunnerContact
    record = session.get(RunnerContact, (job.id, job.fence))
    if record is None:
        record = RunnerContact(job_id=job.id, attempt=job.fence, claimed_at=now if kind == 'claim' else None)
        session.add(record)
    record.last_contact_at, record.expires_at = now, job.lease_until
    if kind == 'heartbeat':
        record.last_heartbeat_at = now


def active_jobs(session, incident, now):
    from sqlalchemy import and_, select
    from .storage import Job, RunnerContact
    query = select(Job, RunnerContact).outerjoin(RunnerContact, and_(
        RunnerContact.job_id == Job.id, RunnerContact.attempt == Job.fence)).where(
        Job.incident_id == incident.id, Job.status.in_(('queued', 'leased'))).order_by(Job.created_at, Job.id)
    if incident.state in ('completed', 'failed', 'handoff'):
        query = query.where(Job.kind == 'current_version_check')
    rows = session.execute(query)
    result = []
    for job, receipt in rows:
        if job.kind == 'current_version_check':
            from .current_version import same_context
            if job.payload['deadline'] <= now or not same_context(incident, job):
                continue
        if job.status == 'queued' and job.fence == 0:
            state = 'waiting'
        elif receipt is None:
            state = 'unobserved'
        elif job.status == 'queued' or receipt.expires_at <= now:
            state = 'contact_lost'
        else:
            state = 'reclaimed' if job.fence > 1 else 'leased'
        result.append({'job_id': job.id, 'kind': job.kind, 'attempt': job.fence, 'state': state,
            'queued_at': job.created_at, 'claimed_at': receipt.claimed_at if receipt else None,
            'last_heartbeat_at': receipt.last_heartbeat_at if receipt else None,
            'last_contact_at': receipt.last_contact_at if receipt else None,
            'expires_at': receipt.expires_at if receipt else job.lease_until})
    return result


def progress(control, job_id, request, actor):
    from sqlalchemy import select
    from .storage import Job, Event
    control.require_store()
    with control.store.transaction() as session:
        job=session.get(Job,job_id)
        control.runner_lease(session,job,actor)
        control.lease_check(job,request)
        error=control.lease_violation(session,job)
        if not error:
            if request.phase not in PHASES.get(job.kind, ()):
                fail(422,'unknown_activity_phase')
            path=request.file
            if path is not None:
                if request.phase not in ('edit_file','read_file') or not safe_source_path(path):
                    fail(422,'activity_file_invalid')
                if request.phase=='edit_file' and '+++ b/'+path not in (job.payload.get('patch') or {}).get('diff','').splitlines():
                    fail(422,'activity_file_not_in_patch')
            elif request.phase in ('edit_file','read_file'):
                fail(422,'activity_file_required')
            data={'job_id':job.id,'kind':job.kind,'attempt':job.fence,'seq':request.seq,'phase':request.phase,'file':path}
            prev=session.scalar(select(Event).where(Event.incident_id==job.incident_id,Event.kind=='runner_progress',
                Event.document['job_id'].as_string()==job.id, Event.document['attempt'].as_integer()==job.fence)
                .order_by(Event.id.desc()).limit(1))
            if prev and prev.document==data:
                return {'accepted':True}
            if prev and prev.document['seq']>=request.seq:
                fail(409,'activity_sequence_conflict')
            record_contact(session, job, control.clock(), 'progress')
            control.event(session,control.get(session,job.incident_id),'runner_progress',data)
            return {'accepted':True}
    fail(409,error)


def change_for_event(control, iid, event_id):
    from .stage_readings import file_excerpt
    from .storage import Event, Job
    control.require_store()
    with control.store.read() as session:
        control.get(session, iid)
        event = session.get(Event, event_id)
        if not event or event.incident_id != iid or event.kind != 'runner_progress':
            fail(404, 'file_change_not_observed')
        data = event.document
        path = data.get('file')
        job = session.get(Job, data.get('job_id'))
        if (data.get('phase') != 'edit_file' or not safe_source_path(path) or not job
            or job.incident_id != iid or job.kind != 'test_build'):
            fail(404, 'file_change_not_observed')
        # This worker accepts unified patches with --- / +++ headers and no renames.
        patch = (job.payload.get('patch') or {}).get('diff', '')
        return file_excerpt(patch, path)


def test_observations(record):
    rows = (record.get('tests') or {}).get('executed_tests', [])
    outcomes = {}
    for phase, label in (('before', '변경 전'), ('after', '변경 후')):
        tests = [r for r in rows if r.get('phase') == phase and r.get('status') in ('passed','failed')]
        if tests:
            failed = sum(r['status'] == 'failed' for r in tests)
            outcomes[phase + '_ko'] = f'{label} 시험 {len(tests)}건 중 실패 {failed}건'
    return outcomes


def activity(control, iid, after):
    from .storage import Event, Job
    from sqlalchemy import select
    control.require_store()
    with control.store.read() as s:
        incident=control.get(s,iid)
        doc=incident.document
        from .display_readings import projection, role_key
        readings = projection(s, incident)
        # Role call ids are stored with new records. Earlier completions have their event id as identity.
        qs=select(Event).where(Event.incident_id==iid,Event.id>after).order_by(Event.id).limit(501)
        rows=list(s.scalars(qs)); items=[]
        job_ids = {d['job_id'] for event in rows[:500] if (d := event.document or {}).get('job_id') is not None}
        jobs = {job.id: job for job in s.scalars(select(Job).where(Job.incident_id == iid, Job.id.in_(job_ids)))} if job_ids else {}
        for event in rows[:500]:
            d=event.document or {}
            out={k:d[k] for k in ('job_id','kind','role','model','effort','attempt','seq','phase','file','call_id','reason_ko','status','attempt_id','text','before_ko','after_ko','request_ko','evidence_refs') if k in d}
            job = jobs.get(d.get('job_id'))
            if job and job.kind in ('deploy', 'verify', 'reconcile'):
                payload = (job.payload.get('original_payload') or {}) if job.kind=='reconcile' else job.payload
                if payload.get('stage') in ('dev','prd'):
                    out['job_environment'] = payload['stage']
            if event.kind in ('investigation_started', 'investigation_finished'):
                inquiry = (doc.get('data') or {}).get('evidence_inquiry') or {}
                if inquiry.get('job_id') == d.get('job_id'):
                    key, field = ('question', 'request_ko') if event.kind == 'investigation_started' else ('result', 'reason_ko')
                    reading = readings.get('investigation/' + key)
                    if reading:
                        out[field] = reading['ko']
                        out['readings'] = {field: reading}
            if event.kind=='runner_progress' and out.get('file'):
                patch=(job.payload.get('patch') or {}) if job and job.incident_id==iid else {}
                out.update(change_ko=patch.get('summary_ko'),tests=patch.get('tests_added',[]))
            if event.kind=='role_completed':
                # A legacy record can be joined only by its role and the digest of the exact saved output.
                rec=next((r for r in (doc.get('data') or {}).get('analysis',{}).get('roles',[])
                          if (r.get('call_id') and r['call_id']==d.get('call_id')) or
                          (r.get('output_sha256') and r['output_sha256']==d.get('output_sha256') and r.get('role')==d.get('role'))),None)
                if rec:
                    evidence=rec.get('result') or {}
                    for field in ('confidence','level','risk','accepted','fixable','reason_ko','summary_ko','unresolved','follow_up','risk_ko'):
                        if field in evidence:out[field]=evidence[field]
                    out['citations']=[{'ref':str(r['ref'])[:300],'quote':str(r.get('quote',''))[:400]}
                                      for r in (evidence.get('citations') or [])[:20] if isinstance(r,dict) and r.get('ref')]
                    out['effort']=rec.get('effort')
                    roles = (doc.get("data") or {}).get("analysis", {}).get("roles", [])
                    prefix = role_key(rec, roles.index(rec)) + "/"
                    observed = {key.removeprefix(prefix): value for key, value in readings.items()
                                if key.startswith(prefix)}
                    out["readings"] = observed
                    for field in ("summary_ko", "reason_ko", "risk_ko"):
                        if field in observed:
                            out[field] = observed[field]["ko"]
                    out["summary_ko"] = out.get("summary_ko") or out.get("reason_ko")
                    for field in ("unresolved", "follow_up"):
                        out[field + "_ko"] = [observed.get(field + "/" + str(i), {}).get("ko")
                                              for i in range(len(out.get(field) or []))]

            items.append({'id':event.id,'at':event.occurred_at,'event':event.kind,'data':scrub(out)})
        return {'id':iid,'state':incident.state,'stages':[
            {'state':row.get('state'),'occurred_at':row.get('occurred_at'),
             **({'round': row['details']['round']} if isinstance((row.get('details') or {}).get('round'), int) else {})}
            for row in doc.get('stages',[])],
            'events':items,'active_jobs':active_jobs(s, incident, control.clock()),'has_more':len(rows)>500,'cursor':items[-1]['id'] if items else after}
