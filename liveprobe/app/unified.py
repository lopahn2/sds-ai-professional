"""One logical incident, two durable stores. No repository or cluster transport authority.

GET projections never repair the saga. Only explicit POST or a fresh engine boundary
may create/retry a link. A committed link blocks competing K8s writes even when the
Application database is unavailable.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import uuid
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text, or_
from sqlalchemy.exc import IntegrityError

from . import db
from .models import Action, AgentRun, CodeActionLink, Evidence, Incident, OperatorCommand, utcnow
from .application.contracts import CreateIncident
from .application.qa_release import projection as release_projection, work_of
from .application.safety import sha, scrub, text_sha

_SERVICE = None
TERMINAL = {'completed', 'failed', 'handoff'}
STATE_KO = {
    'recovery_review': '서비스 복구 관측 · 운영자 소명 확인 대기',
    'intake_rejected': '접수 종료 · 등록되지 않은 재현 요청',
    'requested': '코드 조치 연결 대기', 'unavailable': '코드 조치 상태 미관측',
    'detected': '코드 오류 접수', 'collecting': '실행기 근거 수집 대기',
    'source_pinning': '소스 출처 확인', 'analysis_queued': '코드 분석 대기', 'analyzing': '코드 분석 중',
    'patch_proposed': '코드 수정안 준비', 'test_build_queued': '재현·회귀 시험 대기',
    'test_build_passed': '재현·회귀 시험 통과', 'awaiting_user_test': 'dev 사용자 시험 대기',
    'awaiting_prd_approval': 'prd 승인 대기', 'awaiting_merge': '사람 병합 확인 대기',
    'completed': 'prd 검증 완료', 'failed': '코드 조치 실패', 'handoff': '운영자 인계',
    'reconciliation_required': '실제 배포 결과 확인 필요',
    'qa_queued': 'dev 업무 QA 대기', 'qa_passed': 'dev 업무 QA 통과', 'qa_failed': 'dev QA 보완 필요',
    'dev_infra_pending': 'dev 인프라 조치 확인 대기', 'dev_infra_verified': 'dev 인프라 조치 확인',
    'awaiting_recovery': 'prd 업무 복구 확인 중',
}
BLOCK_KO = {
    'feature_disabled': '통합 코드 조치가 아직 구성되지 않았습니다.',
    'historical_environment': '과거 환경은 기록만 조회할 수 있습니다.',
    'incident_active': '현재 K8s 분석이 끝난 뒤 코드 조치를 연결할 수 있습니다.',
    'high_risk': '권한·데이터 안전 경계를 운영자가 확인해야 합니다.',
    'unverified_code_cause': '코드 원인에 대한 독립 검증과 실제 코드 위치가 필요합니다.',
    'unknown_write': '이미 시작한 변경의 실제 결과를 먼저 확인해야 합니다.',
    'cluster_work_active': '진행 중인 클러스터 변경이 끝난 뒤 연결할 수 있습니다.',
    'service_unregistered': '등록된 서비스와 실행 프로필이 필요합니다.',
}


def canonical_request(request):
    payload = scrub(request.model_dump(mode='json'))
    if payload.get('signal') is not None:
        payload['signal']['sample'] = payload['signal']['sample'][:2000]
        payload['signal']['sha256'] = text_sha(payload['signal']['sample'])
    return payload


def unknown_write(value):
    if isinstance(value, dict):
        if value.get('write_outcome') == 'unknown' or value.get('unknown_write') is True or value.get('inflight') is not None:
            return True
        reconciliation = value.get('reconciliation') or value.get('write_reconciliation')
        if isinstance(reconciliation, dict) and reconciliation.get('status') == 'required':
            return True
        return any(unknown_write(v) for v in value.values())
    return isinstance(value, list) and any(unknown_write(v) for v in value)


def reject(code, status=409):
    raise HTTPException(status, {'code': code, 'message_ko': BLOCK_KO.get(code, '요청을 안전하게 처리할 수 없습니다.')})


def target_lock(session, namespace, workload):
    """Same lock order for link reservation and K8s write admission."""
    if session.get_bind().dialect.name == 'postgresql':
        key = int.from_bytes(hashlib.sha256(f'{namespace}/{workload}'.encode()).digest()[:8], 'big', signed=True)
        session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def active_link(session, namespace, workload, *, except_id=None):
    target_lock(session, namespace, workload)
    query = select(CodeActionLink, Incident).join(Incident, Incident.id == CodeActionLink.incident_id).where(
        Incident.workload == workload, Incident.namespace.in_(['dev', 'prd'] if namespace == 'dev' else [namespace]))
    if except_id is not None:
        query = query.where(Incident.id != except_id)
    from .mixed_remediation import required
    for link, incident in session.execute(query):
        if incident.namespace != namespace and not required(incident):
            continue
        if link.state == 'intake_rejected':
            continue
        if _SERVICE is None or link.state != 'linked':
            return True
        try:
            if _SERVICE.blocks_target(link.application_id):
                return True
        except Exception:
            return True  # no write authority can be inferred from an unavailable store
    return False


def require_no_code_action(session, incident):
    if incident and active_link(session, incident.namespace, incident.workload):
        reject('code_action_owns_target')


def command_impacts(command, namespace, workload):
    operations = (command.plan or {}).get('operations') or []
    if not operations:
        return True  # not-yet-planned commands may select any target
    return any(not op.get('namespace') or op.get('namespace') == namespace and (
        op.get('kind') != 'Deployment' or op.get('name') == workload) for op in operations)


def guard_command_targets(session, command):
    """Registered Deployment targets are exact; indirect/cluster changes fail closed."""
    operations = (command.plan or {}).get('operations') or []
    targets = set()
    inc = session.get(Incident, command.incident_id)
    if inc:
        targets.add((inc.namespace, inc.workload))
    for op in operations:
        namespace = op.get('namespace')
        if namespace and op.get('kind') == 'Deployment':
            targets.add((namespace, op.get('name')))
        else:
            query = select(Incident.namespace, Incident.workload).join(
                CodeActionLink, CodeActionLink.incident_id == Incident.id)
            if namespace:
                query = query.where(Incident.namespace == namespace)
            targets.update(session.execute(query).all())
    for namespace, workload in sorted(targets):
        if active_link(session, namespace, workload):
            from .commands import CommandError
            raise CommandError('code_action_owns_target')


def verified_code_cause(incident, refs, runs):
    handoff = incident.handoff or {}
    remediation = handoff.get('remediation') or {}
    marker = handoff.get('code_cause_verification')
    # A fresh explicit rejection supersedes any historical verified handoff.
    if isinstance(marker, dict):
        accepted = marker.get('verified') is True
    else:
        accepted = handoff.get('workflow_status') == 'verified'
    if (not accepted or remediation.get('source') != 'llm'
            or remediation.get('kind') not in ('code_fix', 'both')):
        return False
    try:
        from .remediation import Remediation
        parsed = Remediation.model_validate(remediation)
        declared = [ref.model_dump() for ref in parsed.code_refs]
        if any(not ref['file'] or not ref['symbol'] for ref in declared):
            return False
    except (ValueError, TypeError):
        return False
    observed = [f for e in refs if e.source == 'code_refs' and not e.error and isinstance(e.payload, dict)
                for f in e.payload.get('frames', [])]
    if not declared or any(not any(all(f.get(k) == r.get(k) for k in ('file', 'line', 'symbol')) for f in observed)
                           for r in declared):
        return False
    independent = [r for r in runs if r.role in ('group_verifier', 'result_verifier')]
    if not independent:
        return False
    latest = max(independent, key=lambda r: r.id)
    if isinstance(marker, dict) and marker.get('verifier_run_id') != latest.id:
        return False
    output = latest.output or {}
    return bool(latest.status == 'succeeded' and not output.get('_acceptance_rejected')
        and output.get('verdict') == 'supported'
        and (output.get('remediation') or {}).get('kind') == remediation.get('kind')
        and (output.get('remediation') or {}).get('code_refs') == declared)


class UnifiedService:
    def __init__(self, application, installation_id, *, session_factory=db.session):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,63}', installation_id or ''):
            raise ValueError('unified_installation_id_required')
        self.application = application
        application.state.unified_coordinator = self
        self.control = application.state.control
        self.instance = installation_id
        self.sessions = session_factory
        self._stop = threading.Event()
        self._thread = None

    def mixed_required(self, application_id):
        from .mixed_remediation import required
        with self.sessions() as session:
            incident = session.scalar(select(Incident).join(CodeActionLink, CodeActionLink.incident_id == Incident.id)
                                      .where(CodeActionLink.application_id == application_id))
            return bool(incident and required(incident))

    def advance_mixed(self, application_id, release_id):
        from .mixed_remediation import advance
        engine = getattr(self, 'mixed_engine', lambda: None)()
        return advance(self, application_id, release_id, engine)

    def advance_pending_mixed(self):
        # The Application read, Kubernetes action and receipt consumption are separate committed phases.
        # Losing a response keeps the same release/action visible to the next poll.
        for pending in self.control.mixed_pending():
            if self._stop.is_set():
                break
            try:
                app_id, release_id = pending['application_id'], pending['release_id']
                if not app_id or not release_id:
                    continue
                decision = self.advance_mixed(app_id, release_id)
                self.control.record_mixed_infra(app_id, decision)
            except Exception:
                continue  # Already consumed or temporarily unavailable. Read current state on the next poll.

    def blocks_target(self, app_id):
        document = self.control.detail(app_id)
        if document['state'] not in TERMINAL or unknown_write(document):
            return True
        from .application.storage import Job
        with self.control.store.read() as session:
            return session.scalar(select(Job.id).where(Job.incident_id == app_id, ((Job.kind == 'deploy') & Job.status.in_(['queued', 'leased', 'reconciliation_required'])) |
                ((Job.kind == 'reconcile') & Job.status.in_(['queued', 'leased']))).limit(1)) is not None

    def sync_one(self, iid):
        with self.sessions() as session:
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid))
            if not link or link.state == 'intake_rejected':
                return
            app_id = link.application_id
        try:
            document = self.linked_document(iid)
            app_state = document['state']
            status = projected_status(app_state)
            if app_state in TERMINAL and self.blocks_target(app_id):
                status = 'analyzing'
            stages = document.get('stages') or []
            last = stages[-1] if stages else {}
            stamp = last.get('occurred_at') if last.get('state') == app_state else None
            try:
                changed_at = datetime.fromtimestamp(stamp, timezone.utc) if type(stamp) in (float, int) else None
            except (ValueError, OverflowError, OSError):
                changed_at = None
            observation = 'observed'
        except Exception:
            app_state = status = None
            observation = 'unavailable'
        with self.sessions() as session:
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid).with_for_update())
            if link is None or link.application_id != app_id:
                return
            if status is not None:
                link.application_state, link.projected_status = app_state, status
                link.observed_at = utcnow()
                link.state_changed_at = changed_at
            link.observation = observation

    def start(self):
        if self._thread is not None:
            return
        def sync():
            while not self._stop.is_set():
                try:
                    self.advance_pending_mixed()
                except Exception:
                    pass  # Application store unavailable. Existing core projections can still refresh.
                try:
                    with self.sessions() as session:
                        ids = list(session.scalars(select(CodeActionLink.incident_id).where(or_(
                            CodeActionLink.state == 'requested',
                            CodeActionLink.projected_status.in_(['analyzing', 'acting', 'action_pending', 'open', 'recovery_review']),
                            CodeActionLink.observation != 'observed'))))
                    for iid in ids:
                        if self._stop.is_set():
                            break
                        try:
                            with self.sessions() as session:
                                link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid))
                                pending = link is not None and link.state == 'requested'
                            if pending:
                                self._dispatch(iid)
                            else:
                                self.sync_one(iid)
                        except Exception:
                            pass  # next tick retries the same durable reservation only
                except Exception:
                    pass  # retains last projection; no false completion or write replay
                self._stop.wait(2)
        self._thread = threading.Thread(target=sync, name='unified-projection', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _incident(self, session, iid):
        incident = session.get(Incident, iid)
        if incident is None:
            reject('incident_not_found', 404)
        return incident

    def _busy(self, session, inc, *, exclude_action_id=None):
        ids = select(Incident.id).where(Incident.namespace == inc.namespace, Incident.workload == inc.workload)
        from .actions import workload_lock
        query = select(Action.id).where(or_(Action.incident_id.in_(ids), Action.lock_key == workload_lock(inc.namespace, inc.workload)),
                Action.status.in_(['proposed', 'pending_approval', 'approved', 'executing', 'indeterminate']))
        if exclude_action_id is not None:
            query = query.where(Action.id != exclude_action_id)
        if session.scalar(query.limit(1)):
            return True
        commands = session.scalars(select(OperatorCommand).where(OperatorCommand.status.in_(
            ['planning', 'awaiting_confirmation', 'awaiting_secret', 'executing', 'rolling_back'])))
        if any(command_impacts(c, inc.namespace, inc.workload) for c in commands):
            return True
        from .change_models import ChangeRequest
        return session.scalar(select(ChangeRequest.id).where(ChangeRequest.incident_id.in_(ids),
            ChangeRequest.status.not_in(['completed', 'cancelled', 'error', 'patch_failed', 'build_failed',
                                         'dev_failed', 'stg_failed', 'prd_failed'])).limit(1)) is not None

    def _unknown_target(self, session, inc):
        from .models import WorkflowRun
        ids = select(Incident.id).where(Incident.namespace == inc.namespace, Incident.workload == inc.workload)
        if any(unknown_write(a.after_state) for a in session.scalars(select(Action).where(Action.incident_id.in_(ids)))):
            return True
        if any(unknown_write(w.snapshot) for w in session.scalars(select(WorkflowRun).where(WorkflowRun.incident_id.in_(ids)))):
            return True
        from .change_models import ChangeRequest
        if any(unknown_write(c.stages) or '결과 불명' in (c.error or '') for c in session.scalars(
                select(ChangeRequest).where(ChangeRequest.incident_id.in_(ids)))):
            return True
        if any(unknown_write(c.result) or unknown_write(c.verification) or unknown_write(c.rollback)
               or '결과 불명' in (c.error or '') for c in session.scalars(
                   select(OperatorCommand)) if command_impacts(c, inc.namespace, inc.workload)):
            return True
        return False

    def eligibility(self, session, inc):
        if inc.namespace not in ('dev', 'prd'):
            return 'historical_environment'
        if inc.severity_level not in ('SIMPLE', 'NORMAL', 'COMPLEX'):
            return 'high_risk'
        classification = ((inc.handoff or {}).get('handoff_classification') or {})
        if classification.get('unknown_write') is True:
            return 'unknown_write'
        if inc.status not in ('handed_off', 'resolved'):
            return 'incident_active'
        if self._unknown_target(session, inc):
            return 'unknown_write'
        if self._busy(session, inc):
            return 'cluster_work_active'
        if not self.control.catalog.configured or inc.workload not in self.control.catalog.profiles:
            return 'service_unregistered'
        evidence = list(session.scalars(select(Evidence).where(Evidence.incident_id == inc.id)))
        runs = list(session.scalars(select(AgentRun).where(AgentRun.incident_id == inc.id)))
        if not verified_code_cause(inc, evidence, runs):
            return 'unverified_code_cause'
        return None

    def _reserve(self, session, inc, request, actor, request_id=None):
        action_id = uuid.uuid4().hex
        origin = {'installation_id': self.instance, 'incident_id': inc.id, 'action_id': action_id}
        application_id = sha(origin)
        payload = canonical_request(request)
        request_hash = sha(payload)
        row = CodeActionLink(action_id=action_id, incident_id=inc.id, application_id=application_id,
            request_id=request_id, request_sha256=request_hash, request=payload, state='requested', application_state='requested', projected_status='analyzing', observation='pending',
            created_by=actor['subject'])
        session.add(row)
        session.flush()
        db.audit(session, actor['subject'], 'code_action_requested', inc.id,
                 action_id=action_id, application_id=application_id, request_sha256=request_hash)
        return action_id

    def _dispatch(self, iid):
        # Separate transactions. A crash at any point reuses the same reserved child.
        with self.sessions() as session:
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid))
            origin = {'installation_id': self.instance, 'incident_id': iid, 'action_id': link.action_id,
                      'request_sha256': link.request_sha256}
            app_id, payload = link.application_id, copy.deepcopy(link.request)
            request_hash = link.request_sha256
            from .mixed_remediation import required
            mixed = required(self._incident(session, iid))
        try:
            self.control.create_linked(CreateIncident.model_validate(payload),
                {'subject': 'unified-coordinator', 'kind': 'machine'}, origin=origin,
                application_id=app_id, request_sha256=request_hash, mixed_required=mixed)
        except HTTPException as exc:
            if (exc.status_code == 422 and isinstance(exc.detail, dict)
                    and exc.detail.get('code') == 'reproduction_unregistered'):
                self._record_rejected_intake(iid, app_id, request_hash)
            raise
        with self.sessions() as session:
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid).with_for_update())
            if link.application_id != app_id or link.request_sha256 != request_hash:
                reject('link_fingerprint_conflict')
            if link.state != 'linked':
                link.state = 'linked'
                db.audit(session, 'unified-coordinator', 'code_action_linked', iid,
                         action_id=link.action_id, application_id=app_id)
        self.sync_one(iid)
        return self.summary(iid)

    def _record_rejected_intake(self, iid, app_id, request_hash):
        # Older versions reserved a core row before validating reproduction. Release it only if
        # this exact invalid reservation never created an Application incident or a runner job.
        from .application.storage import Incident as ApplicationIncident, Job
        with self.control.store.read() as app_session:
            if (app_session.get(ApplicationIncident, app_id) is not None or
                    app_session.scalar(select(Job.id).where(Job.incident_id == app_id).limit(1))):
                return
        with self.sessions() as session:
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid).with_for_update())
            if not (link and link.application_id == app_id and link.request_sha256 == request_hash and
                    link.state == 'requested' and link.request.get('reproduction') is not None):
                return
            link.state = link.application_state = 'intake_rejected'
            link.projected_status, link.observation = 'failed', 'observed'
            link.observed_at = link.state_changed_at = utcnow()
            db.audit(session, 'unified-coordinator', 'code_intake_rejected', iid,
                     action_id=link.action_id, reason='reproduction_unregistered')

    def continue_incident(self, iid, actor, *, automatic=False):
        with self.sessions() as session:
            inc = self._incident(session, iid)
            from .mixed_remediation import required
            if inc.namespace == 'prd' and required(inc):
                target_lock(session, 'dev', inc.workload)
            target_lock(session, inc.namespace, inc.workload)
            # The target lock serializes all link reservations and K8s admissions.
            inc = session.scalar(select(Incident).where(Incident.id == iid).with_for_update())
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid))
            if link is None:
                code = self.eligibility(session, inc)
                if code:
                    reject(code)
                if active_link(session, inc.namespace, inc.workload) or (
                        inc.namespace == 'prd' and required(inc) and active_link(session, 'dev', inc.workload)):
                    reject('code_action_owns_target')
                request = CreateIncident(environment=inc.namespace, service=inc.workload,
                    reason='K8s 분석에서 독립 검증된 사용자 코드 원인. 동일 사건의 코드 수정 조치입니다.')
                self._reserve(session, inc, request, actor)
        return self._dispatch(iid)

    def create(self, request, request_id, actor):
        payload = canonical_request(request)
        request_hash = sha(payload)
        request = CreateIncident.model_validate(payload)
        scoped_id = self.instance + ':' + request_id
        try:
            with self.sessions() as session:
                existing = session.scalar(select(CodeActionLink).where(CodeActionLink.request_id == scoped_id))
                if existing:
                    if existing.request_sha256 != request_hash:
                        reject('request_fingerprint_conflict')
                    iid = existing.incident_id
                else:
                    target_lock(session, request.environment, request.service)
                    self.control.validate_intake(request)
                    if request.environment not in ('dev', 'prd') or request.service not in self.control.catalog.profiles:
                        reject('service_unregistered', 422)
                    if active_link(session, request.environment, request.service):
                        reject('code_action_owns_target')
                    inc = Incident(fingerprint='application:' + sha(scoped_id), family_hint='application',
                        namespace=request.environment, workload=request.service,
                        system=self.control.catalog.profiles[request.service]['system'], status='handed_off',
                        title=f'{request.service} 애플리케이션 오류', summary='코드 수정 조치를 진행합니다.',
                        signal_count=1, signal_reasons=['ApplicationSignal'], handoff={'origin': 'application'},
                        created_at=utcnow(), updated_at=utcnow())
                    session.add(inc)
                    session.flush()
                    iid = inc.id
                    if self._unknown_target(session, inc):
                        reject('unknown_write')
                    if self._busy(session, inc):
                        reject('cluster_work_active')
                    self._reserve(session, inc, request, actor, scoped_id)
        except IntegrityError:
            with self.sessions() as session:
                link = session.scalar(select(CodeActionLink).where(CodeActionLink.request_id == scoped_id))
                if link is None or link.request_sha256 != request_hash:
                    reject('request_fingerprint_conflict')
                iid = link.incident_id
        return {'incident_id': iid, 'room_path': f'/incidents/{iid}', 'code_action': self._dispatch(iid)}

    def matches_origin(self, link, document):
        origin = {'installation_id': self.instance, 'incident_id': link.incident_id, 'action_id': link.action_id}
        return (document['id'] == link.application_id == sha(origin) and
                (document.get('data') or {}).get('unified_origin') == {**origin, 'request_sha256': link.request_sha256})

    def linked_document(self, iid, *, operator=False):
        with self.sessions() as session:
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid))
            if link is None or link.state != 'linked':
                reject('code_action_not_found', 404)
            session.expunge(link)
        document = self.control.operator_detail(link.application_id) if operator else self.control.detail(link.application_id)
        if not self.matches_origin(link, document):
            reject('link_fingerprint_conflict')
        return document

    def summary(self, iid):
        with self.sessions() as session:
            inc = self._incident(session, iid)
            link = session.scalar(select(CodeActionLink).where(CodeActionLink.incident_id == iid))
            result = {'enabled': True, 'linked': bool(link), 'incident_id': iid,
                'action_id': link.action_id if link else None, 'application_id': link.application_id if link else None,
                'state': 'not_linked', 'phase': 'kubernetes', 'state_ko': 'K8s 분석',
                'summary_ko': '동일 사건에서 코드 수정 조치를 이어갈 수 있습니다.',
                'next_ko': '', 'runner_observed': None, 'observed_at': None,
                'eligible': False, 'blocked_code': None, 'blocks_cluster': bool(link),
                'problem_ko': f'{inc.workload}에서 관측된 장애를 확인합니다.',
                'cause_ko': '독립 검증된 사용자 코드 원인입니다.' if (inc.handoff or {}).get('remediation', {}).get('source') == 'llm' and link else None}
            if not link:
                result['blocked_code'] = self.eligibility(session, inc)
                result['eligible'] = result['blocked_code'] is None
                result['next_ko'] = BLOCK_KO.get(result['blocked_code'], '코드 수정 조치를 연결하세요.')
                return result
            app_id, link_state = link.application_id, link.state
        result.update(state='requested', phase='code_action', state_ko=STATE_KO['requested'],
                      next_ko='서버가 저장된 요청으로 같은 코드 조치의 연결을 자동 재확인합니다.')
        if link_state == 'intake_rejected':
            result.update(state=link_state, state_ko=STATE_KO[link_state], blocks_cluster=False,
                          linked=False, blocked_code='reproduction_unregistered',
                          next_ko='원래 접수 기록은 보존됩니다. 등록된 서비스와 증상으로 접수할 수 있습니다.')
            return result
        if link_state != 'linked':
            return result
        try:
            document = self.linked_document(iid)
            state = document['state']
            runner_status = self.control.runner_status_for_incident(app_id)
            runner = runner_status['observed']
            stages = document.get('stages') or []
            observed = stages[-1].get('occurred_at') if stages else document.get('created_at')
            label = STATE_KO.get(state)
            if label is None:
                for env in ('dev', 'prd'):
                    if state.startswith(env + '_'):
                        label = env + ' ' + {'targets_collecting': '배포 대상 확인', 'deploy_queued': '배포 대기',
                            'verifying': '결과 검증 중', 'verified': '결과 검증 완료'}.get(state[len(env)+1:], '조치 진행 중')
                        break
            next_ko = {'awaiting_user_test': '시험자는 검증된 dev 버전을 시험하고 결과를 기록하세요.',
                'awaiting_prd_approval': '승인자는 시험 결과와 사람의 PR 병합을 확인한 뒤 해당 버전을 승인하세요.',
                'qa_failed': '실패한 업무 시험의 근거를 확인한 뒤 보완하거나 현재 버전의 QA를 다시 요청하세요.',
                'awaiting_recovery': '배포된 버전에서 원래 업무 요청이 복구됐는지 확인하고 있습니다.',
                'awaiting_merge': '등록 저장소에서 사람이 PR을 병합한 결과를 확인해야 합니다.',
                'completed': 'prd 검증이 완료됐습니다. 결과와 이력을 확인하세요.',
                'handoff': '권한·데이터 안전 또는 복구 한계를 운영자가 확인하세요.',
                'failed': '실패한 단계와 원인을 확인하세요.',
                'reconciliation_required': '이미 시작한 배포의 실제 결과를 먼저 확인합니다.'}.get(state)
            if next_ko is None:
                next_ko = '실행기가 다음 작업을 가져갈 때까지 기다립니다.' if not runner else '허용된 다음 단계를 자동으로 진행합니다.'
            result.update(state=state, state_ko=label or '코드 조치 진행 중',
                blocks_cluster=self.blocks_target(app_id),
                next_ko=next_ko, runner_observed=runner, observed_at=observed,
                runner_role=runner_status.get('role'), runner_last_claim_at=runner_status.get('last_claim_at'),
                **release_projection(document, state))
        except Exception:
            result.update(state='unavailable', state_ko=STATE_KO['unavailable'],
                          next_ko='저장소 연결을 확인하세요. 기존 조치의 성공 여부를 추정하지 않습니다.')
        return result

    def detail(self, iid):
        summary = self.summary(iid)
        if not summary['linked']:
            reject('code_action_not_found', 404)
        application = self.linked_document(iid, operator=True)
        app_id = application['id']
        return {'summary': summary, 'application': application,
                'release': self.control.release_get(app_id), 'events': self.control.events(app_id)}

    def auto_continue(self, iid):
        try:
            self.continue_incident(iid, {'subject': 'liveprobe', 'kind': 'machine'}, automatic=True)
        except HTTPException:
            pass  # a rejected optional continuation must not erase the K8s outcome
        except Exception:
            with self.sessions() as session:
                db.audit(session, 'unified-coordinator', 'code_action_link_pending', iid)


def defer_mixed(iid):
    if _SERVICE is None:
        return False
    from .mixed_remediation import required
    with _SERVICE.sessions() as session:
        incident = session.get(Incident, iid)
        return bool(incident and required(incident) and
                    (incident.handoff or {}).get('code_cause_verification', {}).get('verified') is True)


def configure(application=None, instance=None):
    global _SERVICE
    _SERVICE = UnifiedService(application, instance) if application is not None else None
    return _SERVICE


def auto_continue(iid):
    if _SERVICE is not None:
        _SERVICE.auto_continue(iid)


def projected_status(application_state):
    if application_state == 'recovery_review':
        return 'recovery_review'
    if application_state == 'completed':
        return 'resolved'
    if application_state == 'handoff':
        return 'handed_off'
    if application_state == 'failed':
        return 'failed'
    if work_of(application_state) in ('approval', 'qa_fix') or application_state == 'awaiting_merge':
        return 'action_pending'
    return 'analyzing'


def effective_status_expr():
    """Correlated SQL expression: filters/counts run before pagination, no App DB read."""
    from sqlalchemy import func
    return func.coalesce(select(CodeActionLink.projected_status).where(
        CodeActionLink.incident_id == Incident.id).correlate(Incident).scalar_subquery(), Incident.status)


def projection_rows(session, ids):
    return {link.incident_id: {'action_id': link.action_id, 'application_id': link.application_id,
        'state': link.application_state, 'projected_status': link.projected_status,
        'work': work_of(link.application_state),
        'state_ko': STATE_KO.get(link.application_state, '코드 조치 진행 중'),
        'observed_at': link.observed_at.isoformat() if link.observed_at else None,
        'state_changed_at': link.state_changed_at.isoformat() if link.state_changed_at else None,
        'observation': link.observation}
        for link in session.scalars(select(CodeActionLink).where(CodeActionLink.incident_id.in_(ids)))} if ids else {}


def apply_projection(item, projection):
    if not projection:
        item['work'] = 'approval' if item.get('status') == 'action_pending' else None
        return item
    value = dict(item)
    value['kubernetes_status'] = item.get('status')
    value['status'] = projection['projected_status']
    value['code_action'] = projection
    value['work'] = projection.get('work')
    if projection.get('state_changed_at'):
        value['updated_at'] = projection['state_changed_at']
    if value['status'] not in ('resolved', 'failed', 'handed_off', 'closed'):
        value['closed_at'] = None
    else:
        value['closed_at'] = projection.get('state_changed_at')
    value['stage'] = {'key': 'code_action', 'label': projection['state_ko']}
    return value


def project_items(session, items):
    projections = projection_rows(session, [i['id'] for i in items])
    return [apply_projection(i, projections.get(i['id'])) for i in items]


def append_code_flow(flow, projection):
    """A real continuation node, never a fabricated AgentRun or model usage record."""
    nodes = list(flow.get('nodes') or [])
    edges = list(flow.get('edges') or [])
    status = projection['projected_status']
    node_id = 'code-action-' + projection['action_id']
    previous = next((n for n in nodes if n.get('id') == 'outcome'), nodes[-1] if nodes else None)
    depth = max((n.get('depth', 0) for n in nodes), default=-1) + 1
    nodes.append({'id': node_id, 'type': 'code_action', 'label': '코드 수정 조치',
        'role': None, 'role_ko': '코드 수정 조치',
        'job_ko': '같은 사건의 소스 수정·시험·dev 검증·prd 승인 절차를 이어갑니다.',
        'status': 'done' if status == 'resolved' else 'failed' if status in ('failed', 'handed_off') else 'running',
        'model': None, 'effort': None, 'tokens': None, 'cost_usd': None, 'duration_ms': None,
        'inputs': [], 'conclusion_ko': projection['state_ko'], 'conclusion_raw': None,
        'lane': 0, 'depth': depth, 'group': None, 'at': projection.get('state_changed_at'), 'card': None})
    if previous:
        edges.append({'id': 'continue-' + node_id, 'source': previous['id'], 'target': node_id,
                      'kind': 'flow', 'label': '동일 사건의 코드 조치'})
    return {'nodes': nodes, 'edges': edges}
