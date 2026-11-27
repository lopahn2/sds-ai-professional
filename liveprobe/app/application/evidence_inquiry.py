"""One bounded read-only inquiry while retaining the exact deployed source and original case evidence."""
from datetime import datetime, timezone
import json
import re
from sqlalchemy import select

from .safety import scrub, sha
from .storage import Incident, Job

PURPOSE = 'review_evidence'
EXHAUSTED = 'additional_observation_exhausted'


def queue(control, session, incident):
    data = incident.document['data']
    if data.get('evidence_inquiry') or data.get('release') or data.get('patch_revisions'):
        return False
    deadline = (data.get('analysis_lease') or {}).get('until', 0)
    if deadline <= control.clock():
        return False
    review = ((data.get('analysis') or {}).get('roles') or [{}])[-1]
    result = review.get('result') or {}
    question = next((str(s) for s in result.get('unresolved') or [] if isinstance(s, str) and s.strip()), None)
    if not question:
        question = '같은 배포 버전의 새 로그나 이벤트에서 현재 원인 후보를 구별할 수 있습니까?'
    question = str(scrub(question))[:500]
    review_data = {'call_id': review.get('call_id'), 'role': review.get('role'), 'question': question,
                   'deadline': deadline, 'state': 'waiting'}
    control.data(incident, 'evidence_inquiry', review_data)
    control.transition(session, incident, 'collecting', {'reason_ko': '원인을 구별할 추가 관측을 기다립니다.'})
    # Evidence is gathered only for the case and dependencies allowed by its catalog.
    since = datetime.fromtimestamp(control.clock() - 900, timezone.utc).isoformat().replace('+00:00', 'Z')
    job = control.enqueue(session, incident, 'collect', {'purpose': PURPOSE,
        'environment': incident.document['environment'], 'service': incident.document['service'],
        'since': since, 'catalog_sha256': control.catalog.sha256})
    control.data(incident, 'evidence_inquiry', {**review_data, 'job_id': job.id})
    control.event(session, incident, 'investigation_started',
        {'job_id': job.id, 'role': review.get('role'), 'request_ko': question, 'call_id': review.get('call_id')})
    return True


def expire(control, session):
    rows = session.scalars(select(Incident).where(Incident.state == 'collecting',
        Incident.document['data']['evidence_inquiry']['deadline'].as_float() <= control.clock()))
    for incident in rows:
        if control.environment_paused(incident.document.get('environment')):
            continue
        inquiry = incident.document['data']['evidence_inquiry']
        job = session.get(Job, inquiry.get('job_id'))
        if job and job.status in ('leased', 'queued'):
            job.status = 'cancelled'
        finish(control, session, incident, inquiry, [], '추가 수집 기한 안에 새 근거가 도착하지 않았습니다.')


def finish(control, session, incident, inquiry, refs, message):
    control.data(incident, 'evidence_inquiry', {**inquiry, 'evidence_refs': refs, 'result_ko': message,
                 'state': 'reviewing' if refs else 'exhausted'})
    control.event(session, incident, 'investigation_finished', {'job_id': inquiry['job_id'],
        'call_id': inquiry.get('call_id'), 'reason_ko': message, 'evidence_refs': refs})
    if refs:
        control.transition(session, incident, 'analysis_queued')
    else:
        control.stop(session, incident, EXHAUSTED)


def close_review(control, session, incident, outcome, code=None):
    inquiry = (incident.document.get('data') or {}).get('evidence_inquiry') or {}
    if inquiry.get('state') != 'reviewing':
        return
    result = {'state': 'reviewed', 'review_outcome': outcome, 'review_finished_at': control.clock()}
    if code:
        result['review_code'] = code
    control.data(incident, 'evidence_inquiry', {**inquiry, **result})
    control.event(session, incident, 'investigation_reviewed', {
        'job_id': inquiry.get('job_id'), 'call_id': inquiry.get('call_id'),
        'evidence_refs': inquiry.get('evidence_refs', []), 'status': outcome, 'code': code,
    })


def fact_key(row):
    text = row['text']
    # A watch resourceVersion, a clock tick or a different evidence-row ID cannot buy a new model round.
    if row.get('source') in ('deployment', 'rollout'):
        try:
            item = json.loads(text)
        except (TypeError, ValueError):
            item = None
        if isinstance(item, dict):
            item.pop('resource_version', None)
            for condition in item.get('conditions') or []:
                if isinstance(condition, dict):
                    condition.pop('lastTransitionTime', None)
            text = item
    elif isinstance(text, str):
        text = re.sub(r'^\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?\]?\s+', '', text)
    return sha([row.get('environment'), row.get('service'), row.get('source'), text])


def accept(control, session, incident, job, target, logs, evidence, observed_at):
    data = incident.document['data']
    inquiry = data['evidence_inquiry']
    if inquiry.get('job_id') != job.id or inquiry.get('state') != 'waiting':
        return
    old = data['collection']
    if (inquiry['deadline'] <= control.clock() or
            any(target.get(k) != old['target'].get(k) for k in ('uid', 'image', 'template_sha256', 'container'))):
        finish(control, session, incident, inquiry, [], '수집 기한 또는 배포 버전이 달라 추가 근거를 연결할 수 없습니다.')
        return
    known = {fact_key(r) for r in old['evidence']}
    delta = []
    for row in evidence:
        key = fact_key(row)
        if key not in known:
            known.add(key)
            delta.append({**row, 'id': 'followup:' + key})
    if not delta:
        finish(control, session, incident, inquiry, [], '동일한 배포에서 새 원인을 구별할 자료를 찾지 못했습니다.')
        return
    # Old IDs, raw log history and version binding remain stable for previously stored citations.
    previous_logs = {fact_key(r) for r in old['logs']}
    control.data(incident, 'collection', {**old, 'evidence': [*old['evidence'], *delta],
        'logs': [*old['logs'], *(r for r in logs if fact_key(r) not in previous_logs)],
        'followup_observed_at': observed_at})
    finish(control, session, incident, inquiry, [r['id'] for r in delta], '새 관측을 분석·반론·독립 검증에 전달했습니다.')
