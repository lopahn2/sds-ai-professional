"""Operator-requested observations on the current serving version, independent of historical release proof."""
import copy
import json
import re

from sqlalchemy import select

from .contracts import CurrentVersionRequest
from .qa_release import hexval, number, original_collection, preserved, receipt_digest_valid
from .safety import fail, scrub, sha, text_sha
from .storage import Job

KIND = 'current_version_check'
WAITING = {'awaiting_user_test', 'awaiting_prd_approval', 'awaiting_merge'}
PRESERVED = WAITING | {'failed', 'handoff', 'completed', 'recovery_review', 'qa_failed'}
EXPIRY = 600
MESSAGES = {
    'busy': '현재 실행 중인 조사나 배포가 끝난 뒤 현재 버전을 재검증할 수 있습니다.',
    'inactive_environment': '원래 영향 환경이 퇴역했거나 관측되지 않았습니다.',
    'operation_unmatched': '원래 실패한 요청의 메서드·경로·입력을 등록된 업무 시험에 연결할 근거가 없습니다.',
    'operation_unsupported': '원래 실패한 요청 중 등록된 읽기 시험으로 확인할 수 없는 동작이 있습니다.',
    'inventory_reservation': '재고 예약 처리에서 예외가 발생했습니다. 등록된 재고 조회 시험으로는 당시 예약 요청과 수량을 확인할 수 없습니다.',
    'pending': '현재 버전 확인을 요청했습니다. 실행기 결과를 기다립니다.',
    'expired': '기한 안에 실행기 결과를 받지 못했습니다. 현재 버전을 다시 확인할 수 있습니다.',
    'changed': '확인 중 사건의 릴리스나 단계가 바뀌었습니다.',
    'receipt_invalid': '실행기 결과를 이 사건·업무 요청·실행 버전에 대조할 수 없습니다.',
    'deployment_unverified': '현재 Deployment의 준비 상태나 대상이 확인되지 않았습니다.',
    'pod_image_unverified': '준비된 Pod의 실제 이미지가 일치하지 않습니다.',
    'deployment_changed_during_probe': '시험 도중 배포 대상 또는 현재 이미지가 바뀌었습니다.',
    'business_unobserved': '원래 요청의 업무 응답을 관측하지 못했습니다.',
    'business_mismatch': '요청 응답이 등록된 업무 성공 조건을 충족하지 못했습니다.',
    'request_failed': '현재 버전에서도 원래 요청이 실패했습니다.',
    'observation_unavailable': '실행기에서 현재 배포 버전 또는 업무 요청을 확인할 수 없습니다.',
}


def original_check(document, profile):
    """Only use attributed failures; checking a different successful read cannot settle an incident."""
    data = document.get('data') or {}
    verification = (profile or {}).get('verification') or {}
    service, env = document['service'], document['environment']
    path = verification.get('path')
    rows = [((data.get('signal') or {}).get('sample'), 'signal')]
    basis, collection = original_collection(data)
    for key in ('logs', 'evidence'):
        rows.extend((r.get('text'), basis + '.' + key) for r in collection.get(key, [])
                    if isinstance(r, dict) and r.get('service') == service and r.get('environment') == env)

    failures = []
    evidence_hashes = set()
    for raw, source in rows:
        if not isinstance(raw, str):
            continue
        if raw.strip():
            evidence_hashes.add(text_sha(raw))
        # The catalog's stock GET cannot exercise the application's reservation POST.
        # A concurrent successful stock lookup must never settle that original failure.
        if (service == 'inventory' and 'StockCampaignController.reserve(' in raw
                and 'AppFaultBusiness.calculateReservationQuantity(' in raw):
            return None, 'inventory_reservation'
        for line in raw.splitlines():
            pos = line.find('{')
            if pos < 0:
                continue
            try:
                obs = json.loads(line[pos:])
            except ValueError:
                continue
            if (isinstance(obs, dict) and obs.get('event') == 'http_access' and obs.get('service') == service
                    and obs.get('environment', env) == env and type(obs.get('status')) is int and 400 <= obs['status'] <= 599):
                failures.append((obs.get('method'), obs.get('uri'), text_sha(line[pos:]), source))

    # Some imported releases retain an attributed reproduction receipt rather than the original log line.
    # A free-text reproduction description, suggested test name or proposed patch is not execution evidence.
    reproduction = data.get('reproduction') or document.get('reproduction')
    if isinstance(reproduction, dict):
        if (reproduction.get('service') != service or reproduction.get('environment') != env
                or type(reproduction.get('status')) is not int or not 400 <= reproduction['status'] <= 599
                or not hexval(reproduction.get('evidence_sha256')) or reproduction['evidence_sha256'] not in evidence_hashes
                or any(reproduction.get(k) for k in ('body', 'headers', 'parameters', 'query', 'inputs'))):
            return None, 'operation_unmatched'
        # All registered operation arguments are already part of the exact path (including its query).
        failures.append((reproduction.get('method'), reproduction.get('path'), reproduction['evidence_sha256'], 'reproduction'))

    if not failures:
        return None, 'operation_unmatched'
    from .qa_release import RECOVERY_KINDS
    if (not path or verification.get('kind') not in RECOVERY_KINDS
            or any(method != 'GET' or route != path for method, route, _, _ in failures)):
        return None, 'operation_unsupported'
    _, _, evidence, source = failures[0]
    return {'method': 'GET', 'path': path, 'evidence_sha256': evidence, 'source': source}, None


def _checks(incident):
    return (incident.document.get('data') or {}).get('current_version_checks') or []


def _save_checks(incident, rows):
    # Preserve the existing record count; scrub() on the whole history would truncate it after 2,000 rows.
    document = dict(incident.document)
    document['data'] = {**document['data'], 'current_version_checks': rows}
    incident.document = document


def _release(incident):
    return (incident.document.get('data') or {}).get('release') or {}


def _observation(session, incident, record, now):
    item = dict(record)
    if item['status'] == 'queued':
        job = session.get(Job, item['job_id'])
        if not job or job.status not in ('leased', 'queued') or not same_context(incident, job):
            item.update(status='unverifiable', reason_ko=MESSAGES['changed'])
        elif job.payload['deadline'] <= now:
            item.update(status='unverifiable', reason_ko=MESSAGES['expired'])
        elif job.status == 'leased':
            item['status'] = 'running'
    return item


def _latest(session, incident, now):
    records = _checks(incident)
    return _observation(session, incident, records[-1], now) if records else None


def pending(session, incident, now):
    item = _latest(session, incident, now)
    return item is not None and item['status'] in ('queued', 'running')


def expire(control, session):
    elapsed = session.scalars(select(Job).where(Job.kind == KIND, Job.status.in_(('queued', 'leased')),
        Job.created_at <= control.clock() - EXPIRY))
    for job in elapsed:
        incident = control.get(session, job.incident_id)
        job.status = 'cancelled'
        rows = copy.deepcopy(_checks(incident))
        row = next((r for r in rows if r['job_id'] == job.id), None)
        if row and row['status'] == 'queued':
            row.update(status='unverifiable', reason_ko=MESSAGES['expired'])
            _save_checks(incident, rows)
            control.event(session, incident, 'current_version_check_reported', {
                'job_id':job.id, 'kind':KIND, 'status':row['status'], 'reason_ko':row['reason_ko']})


def same_context(incident, job):
    records = _checks(incident)
    return (bool(records) and records[-1]['job_id'] == job.id and incident.state == job.payload['incident_state']
            and _release(incident).get('release_id') == job.payload['release_id']
            and _release(incident).get('plan_sha256') == job.payload['release_plan_sha256'])


def view(control, session, incident):
    doc = incident.document
    latest = _latest(session, incident, control.clock())
    env, service = doc['environment'], doc['service']
    if env not in ('dev', 'prd') or control.environment_paused(env):
        code = 'inactive_environment'
    elif incident.state not in PRESERVED:
        code = 'busy'
    elif latest and latest['status'] in ('queued', 'running'):
        code = 'pending'
    elif session.scalar(select(Job.id).where(Job.incident_id == incident.id, Job.kind != KIND,
                            Job.status.in_(('queued', 'leased', 'reconciliation_required'))).limit(1)):
        code = 'busy'
    else:
        _, code = original_check(doc, control.catalog.profiles.get(service))
    return {'eligible': code is None, 'reason_ko': MESSAGES.get(code), 'release_id': _release(incident).get('release_id'),
            'environment': env, 'service': service, 'latest': latest}


def request_check(control, iid, body, actor):
    control.require_store()
    with control.store.transaction() as session:
        incident = control.get(session, iid)
        records = _checks(incident)
        for item in records:
            if item['request_id'] == body.request_id:
                if item.get('release_id') != body.release_id:
                    fail(409, 'current_version_request_conflict')
                return {**view(control, session, incident), 'latest':_observation(session, incident, item, control.clock())}
        current = view(control, session, incident)
        if body.release_id != current['release_id']:
            fail(409, 'release_fingerprint_conflict', MESSAGES['changed'])
        if not current['eligible']:
            fail(409, 'current_version_check_unavailable', current['reason_ko'])
        now = control.clock()
        profile = control.catalog.profiles[current['service']]
        operation, _ = original_check(incident.document, profile)
        payload = {'request_id': body.request_id, 'service': current['service'], 'environment': current['environment'],
                   'profile_id': profile['id'], 'operation': operation, 'incident_state': incident.state,
                   'release_id': body.release_id, 'release_plan_sha256': _release(incident).get('plan_sha256'),
                   'requested_at': now, 'deadline': now + EXPIRY}
        job = control.enqueue(session, incident, KIND, payload)
        record = {'request_id': body.request_id, 'job_id': job.id, 'release_id': body.release_id, 'requested_at': now,
                  'requested_by': actor['subject'], 'status': 'queued', 'image': None, 'observed_at': None,
                  'environment': current['environment'], 'service': current['service'], 'reason_ko': None,
                  'operation': operation['method'] + ' ' + operation['path'], 'promotion_unnecessary': False}
        existing = list(records)
        if existing and existing[-1]['status'] == 'queued':
            existing[-1] = current['latest']
        _save_checks(incident, [*existing, scrub(record)])
        control.event(session, incident, 'current_version_check_requested', {'job_id': job.id, 'kind': KIND})
        return view(control, session, incident)


def validated_proof(job, result, profile, now):
    proof = result.get('check')
    if not isinstance(proof, dict) or result.get('write_outcome') != 'not_written':
        return None
    proof, error = preserved(proof)
    if error or not receipt_digest_valid(proof):
        return None
    payload = job.payload
    required = {'protocol': 'current-version/v1', 'contract_sha256': sha(payload), 'request_id': payload['request_id'],
                'incident_id': job.incident_id, 'job_id': job.id, 'attempt': job.fence, 'service': payload['service'],
                'environment': payload['environment']}
    observed = number(proof.get('observed_at'))
    if (any(proof.get(k) != v for k, v in required.items()) or observed is None
            or not payload['requested_at'] <= observed <= min(now + 60, payload['deadline'])
            or proof.get('outcome') not in ('passed', 'failed', 'unverifiable')
            or result.get('ok') is not (proof['outcome'] == 'passed')):
        return None
    if not isinstance(proof.get('reason', ''), str) or proof.get('reason', '') not in ('', *MESSAGES):
        return None
    if set(proof) - {'protocol', 'receipt_id', 'contract_sha256', 'request_id', 'incident_id', 'job_id', 'service',
                     'attempt', 'environment', 'observed_at', 'outcome', 'reason', 'before', 'after', 'image', 'body_sha256',
                     'status_code', 'path'}:
        return None
    for field in ('before', 'after'):
        if field in proof and (not isinstance(proof[field], dict)
                or set(proof[field]) != {'uid', 'generation', 'template_sha256', 'replicas', 'image'}):
            return None
    image = proof.get('image')
    if image is not None and (not isinstance(image, str) or not profile or not profile.get('image_repository')
            or not image.startswith(profile['image_repository']+'@sha256:') or not hexval(image.partition('@sha256:')[2])):
        return None
    if proof['outcome'] != 'unverifiable':
        pin = proof.get('before')
        if (not isinstance(pin, dict) or pin != proof.get('after') or not image or pin.get('image') != image
                or not isinstance(pin.get('uid'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', pin['uid'])
                or not hexval(pin.get('template_sha256'))
                or any(type(pin.get(k)) is not int or pin[k] < 1 for k in ('replicas', 'generation'))
                or proof.get('path') != payload['operation']['path']):
            return None
        if proof['outcome'] == 'passed' and (proof.get('status_code') != 200 or not hexval(proof.get('body_sha256'))):
            return None
    return proof


def accept(control, session, incident, job, result):
    records = copy.deepcopy(_checks(incident))
    row = next(r for r in records if r['job_id'] == job.id)
    proof = validated_proof(job, result, control.catalog.profiles.get(job.payload['service']), control.clock())
    code = (proof.get('reason') if proof else 'receipt_invalid')
    if proof:
        row.update(status=proof['outcome'], image=proof.get('image'), observed_at=proof['observed_at'],
                   receipt_id=proof['receipt_id'], receipt=proof)
    else:
        row['status'] = 'unverifiable'
    row['reason_ko'] = MESSAGES.get(code, MESSAGES['observation_unavailable']) if code else None
    retire = row['status'] == 'passed' and incident.state in WAITING
    if retire:
        release = copy.deepcopy(_release(incident))
        release.update(valid=False, promotion_disposition='unnecessary', disposition_reason='current_version_verified',
                       disposition_receipt_id=row['receipt_id'])
        control.data(incident, 'release', release)
        row['promotion_unnecessary'] = True
    _save_checks(incident, records)
    control.event(session, incident, 'current_version_check_reported', {
        'job_id': job.id, 'kind': KIND, 'status': row['status'], 'reason_ko':row['reason_ko'],
        'promotion_unnecessary':retire})
    if retire:
        # Current success proves recovery in this environment; it cannot explain the historical root cause.
        from .accountability import conclude
        conclude(control, session, incident, {'current_version_receipt_id': row['receipt_id']})


def routes(app, control, actor_for, prefix):
    from fastapi import Request

    @app.get(prefix + '/incidents/{iid}/current-version-check')
    def read(iid: str, request: Request):
        actor_for(app, request, 'incident:read')
        control.require_store()
        with control.store.read() as session:
            return view(control, session, control.get(session, iid))

    @app.post(prefix + '/incidents/{iid}/current-version-check')
    def recheck(iid: str, body: CurrentVersionRequest, request: Request):
        return request_check(control, iid, body, actor_for(app, request, 'tester'))
