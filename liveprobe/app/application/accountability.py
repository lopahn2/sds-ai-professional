"""Read historical release receipts in place and append versioned operator decisions."""
from datetime import datetime, timezone
from sqlalchemy import func,select
from pydantic import BaseModel,Field
from .safety import fail,scrub
from .storage import Incident


def stamp(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if isinstance(value,(float,int)) else None


def report(incident, *, investigate=False):
    doc=incident.document
    data=doc.get('data',{})
    release=data.get('release') or {}
    signal=data.get('signal') or doc.get('signal') or {}
    recovery=release.get('recovery') or {}
    accepted=recovery.get('accepted') is True and recovery.get('outcome')=='passed'
    result=dict(status='unreviewed' if incident.state in ('completed','failed','recovery_review') else 'not_applicable',
        detection_ko=scrub(doc.get('reason') or '당시 감지 조건 미기록'),recovery_ko='정상화 원인 미기록',
        recovery_observed_at=stamp(recovery.get('observed_at')) if accepted else None,
        limits_ko=None,evidence_refs=[],decision_at=None,decision_by=None,decision_note=None,revision=0)
    stored=data.get('accountability') or {}
    if not investigate:
        if not stored and incident.state in ('completed','failed'):
            historical = report(incident, investigate=True)
            if historical['status'] == 'explained':
                return historical
        return {**result,**stored, 'awaiting_closure': incident.state == 'recovery_review'}
    refs=result['evidence_refs']
    if signal.get('sha256'):refs.append('감지 신호 '+str(signal['sha256'])[:12])
    tests=release.get('tests') or {}
    if tests.get('report_sha256'):refs.append('재현·회귀 시험 '+tests['report_sha256'][:12])
    if accepted:refs.append('prd 복구 확인 '+str(recovery.get('receipt_id',''))[:12])
    current = next((r for r in reversed(data.get('current_version_checks') or [])
                    if r.get('promotion_unnecessary') and r.get('status') == 'passed'
                    and r.get('environment') == doc.get('environment') and r.get('service') == doc.get('service')), None)
    if current:
        refs.append('현재 버전 업무 재검증 ' + str(current['receipt_id'])[:12])
        return {**result, 'status': 'pending_operator', 'recovery_observed_at': stamp(current['observed_at']),
                'recovery_ko': f"{current['environment']}의 현재 버전에서 {current['operation']} 요청이 정상화됐습니다.",
                'limits_ko': '현재 업무 동작을 확인했습니다. 과거 장애의 원인과 정상화 이유는 별도 소명이 필요합니다.'}
    verified=next((r.get('result') or {} for r in reversed((data.get('analysis') or {}).get('roles',[]))
                   if r.get('role')=='verifier'),{})
    if (verified.get('accepted') is True and verified.get('citations') and tests.get('before_failed') is True
        and tests.get('after_passed') is True and accepted and (release.get('stage_results',{}).get('prd') or {}).get('verified') is True
        and (release.get('artifact') or {}).get('diff_sha256') and release.get('summary_ko')):
        result.update(status='explained',recovery_ko=release['summary_ko'],
                      limits_ko='동일 변경의 재현·회귀 시험, 배포 버전과 원래 실패 지점의 운영 관측을 대조했습니다.')
    else:
        gaps=[]
        if not verified.get('citations'):gaps.append('원인 독립 검증 근거')
        if not (tests.get('before_failed') and tests.get('after_passed')):gaps.append('동일 시험의 실패·통과 대조')
        if not accepted:gaps.append('원래 업무 요청의 prd 복구 관측')
        result.update(status='pending_operator',recovery_ko='확인 가능한 자료만으로 정상화의 원인을 입증하지 못했습니다.',
                      limits_ko='추가 확인이 필요한 자료 · '+', '.join(gaps or ['원인과 변경·배포 버전의 연결']))
    return result


def append(control,session,incident,value):
    value={**value,'revision':(incident.document.get('data',{}).get('accountability') or {}).get('revision',0)+1}
    control.data(incident,'accountability',value)
    control.event(session,incident,'recovery_accountability',value)
    return value


def conclude(control,session,incident,details=None):
    value=append(control,session,incident,report(incident,investigate=True))
    control.transition(session,incident,'completed' if value['status']=='explained' else 'recovery_review',details)


class Decision(BaseModel):
    revision:int=Field(ge=0)
    note:str|None=Field(None,max_length=2000)


def routes(app, control, actor_for, prefix):
    from fastapi import Request, Query
    @app.get(prefix+'/incidents/{iid}/accountability')
    def read(iid:str,request:Request):
        actor_for(app,request,'incident:read')
        with control.store.read() as s:return report(control.get(s,iid))

    @app.post(prefix+'/incidents/{iid}/accountability/{action}')
    def decide(iid:str,action:str,body:Decision,request:Request):
        actor=actor_for(app,request,'incident:create')
        with control.store.transaction() as s:
            i=control.get(s,iid);current=report(i)
            if current['revision']!=body.revision or current['status'] not in ('unreviewed','pending_operator'):fail(409,'review_changed')
            if action=='reinvestigate':value=report(i,investigate=True)
            elif action=='confirm' and current['status']=='pending_operator':
                value={**current,'status':'confirmed','decision_at':stamp(control.clock()),'decision_by':actor['subject'],
                       'decision_note':scrub((body.note or '').strip()) or None}
            else:fail(409,'investigation_required')
            result=append(control,s,i,value)
            if i.state=='recovery_review' and result['status'] in ('confirmed','explained'):
                control.transition(s,i,'completed',{'accountability_revision':result['revision']})
            return result

    @app.get(prefix+'/reviews/unexplained')
    def list_missing(request:Request,limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
        actor_for(app,request,'incident:read')
        doc=Incident.document
        with control.store.read() as s:
            cond=(Incident.state.in_(['completed','failed','recovery_review']),
                  func.coalesce(doc['data']['accountability']['status'].as_string(),'').not_in(['confirmed','explained']))
            # Inspect only plausible historic receipts. Do not hydrate source snapshots or create reviews.
            from types import SimpleNamespace
            data = doc['data']
            proof_candidates = s.execute(select(Incident.id, Incident.state, data['release'], data['analysis']['roles'])
                .where(*cond, data['release']['recovery']['accepted'].as_boolean().is_(True),
                       func.coalesce(data['accountability']['status'].as_string(),'') == '')).yield_per(50)
            explained = []
            for iid, state, release, roles in proof_candidates:
                evidence = {'data':{'release':release, 'analysis':{'roles':roles or []}}}
                if report(SimpleNamespace(state=state,document=evidence),investigate=True)['status'] == 'explained':
                    explained.append(iid)
            if explained:
                cond = (*cond, Incident.id.not_in(explained))
            total=s.scalar(select(func.count()).select_from(Incident).where(*cond))
            rows=s.execute(select(Incident.id,Incident.state,doc['service'].as_string(),doc['environment'].as_string(),
                           doc['reason'].as_string(),doc['data']['unified_origin']['incident_id'].as_integer())
                           .where(*cond).order_by(Incident.created_at.desc(),Incident.id.desc()).offset(offset).limit(limit))
            return {'items':[dict(id=i,status=st,workload=w,namespace=ns,title=scrub(t or '')[:240],root_id=root) for i,st,w,ns,t,root in rows],'total':total}
