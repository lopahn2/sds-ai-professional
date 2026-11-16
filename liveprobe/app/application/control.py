"""사건 상태 전이와 durable runner 큐. 네트워크/명령 실행 권한을 갖지 않는다."""
import copy
from datetime import datetime, timezone
import secrets
import time
import uuid
from sqlalchemy import select
from pydantic import ValidationError
from .contracts import Target
from .safety import fail, scrub, sha, text_sha, safe_source_path
from .storage import Incident, Event, Job, RunnerObservation
from .releases import ReleaseControl
from .catalog import Catalog

SERVICE_SYSTEMS = {
    'order': 'samsung-trade', 'inventory': 'samsung-trade', 'catalog': 'samsung-trade',
    'payment': 'samsung-trade', 'trade-member': 'samsung-trade',
    'delivery': 'samsung-delivery', 'notification': 'samsung-delivery',
    'delivery-member': 'samsung-delivery', 'business-api': 'samsung-business',
}


def uid():
    return uuid.uuid4().hex


class ControlPlane(ReleaseControl):
    def __init__(self, store, settings, clock=None):
        self.store, self.settings = store, settings
        self.clock = clock or time.time
        self.catalog = Catalog(settings)
        self.human_authorized = lambda subject, scope: False
        # Injected server-owned read-only merge observer. No runner/human request can set this.
        self.merge_authorized = lambda document: False

    def require_store(self):
        if not self.store.ready():
            fail(503, 'database_unconfigured', '독립 저장소 초기화가 필요합니다.')

    def event(self, session, incident, kind, data=None):
        session.add(Event(incident_id=incident.id, occurred_at=self.clock(), kind=kind,
                          document=scrub(data or {})))

    def transition(self, session, incident, state, details=None):
        document = copy.deepcopy(incident.document)
        document.setdefault('stages', []).append({'state': state, 'occurred_at': self.clock(),
                                                'details': scrub(details or {})})
        incident.state, incident.document = state, document
        self.event(session, incident, state, details)

    def data(self, incident, key, value):
        document = copy.deepcopy(incident.document)
        document.setdefault('data', {})[key] = scrub(value)
        incident.document = document

    def stop(self, session, incident, code, state='handoff'):
        document = copy.deepcopy(incident.document)
        if state == 'handoff':
            from ..handoff_classes import record, RECOVERY_EXHAUSTED
            data = document.get('data', {})
            attempts = [{'kind': 'patch_revision', 'reason': r.get('reason')}
                        for r in data.get('patch_revisions', [])]
            attempts.extend({'kind': 'role_review', 'role': r.get('role')}
                            for r in (data.get('analysis') or {}).get('roles', []))
            exhausted = {'analysis_unresolved_or_low_confidence', 'citation_not_grounded',
                         'citations_missing', 'model_output_invalid', 'patch_paths_invalid',
                         'patch_tests_missing', 'patch_code_reference_invalid', 'test_build_evidence_invalid'}
            # Missing proof, identity, and any uncertain write default to the authority boundary.
            classes = {k: RECOVERY_EXHAUSTED for k in exhausted} if attempts else {}
            decision = record(code, classes, attempts=attempts, unknown_write='unknown' in code)
            document['handoff_classification'] = decision
            document['error'] = {'code': code, 'class': decision['class'], 'message_ko': decision['label_ko']}
        else:
            document['error'] = {'code': code, 'message_ko': '실행 단계가 실패했습니다.'}
        incident.document = document
        self.transition(session, incident, state, {'code': code})

    def enqueue(self, session, incident, kind, payload):
        job_id = uid()
        job = Job(id=job_id, incident_id=incident.id, kind=kind, payload=copy.deepcopy(payload),
                  idempotency_key='application:' + job_id, created_at=self.clock(), status='queued', fence=0)
        session.add(job)
        self.event(session, incident, 'job_queued', {'job_id': job_id, 'kind': kind})
        return job

    def create(self, request, actor):
        self.require_store()
        if request.service not in SERVICE_SYSTEMS:
            fail(422, 'service_unknown', '등록된 서비스가 아닙니다.')
        if not self.catalog.configured:
            fail(503, 'profiles_unconfigured', '등록 카탈로그·실행 프로필이 필요합니다.')
        if request.reproduction is not None:
            # 현재 계약에 등록된 읽기 요청이 없으므로 자유 재현 명령은 받지 않는다.
            fail(422, 'reproduction_unregistered', '등록된 읽기 재현 요청만 허용합니다.')
        now = self.clock()
        reason = scrub(request.reason)
        signal = None
        if request.signal is not None:
            signal = scrub(request.signal.model_dump(mode='json'))
            signal['sample'] = signal['sample'][:2000]
            signal['sha256'] = text_sha(signal['sample'])
        with self.store.transaction() as session:
            # The common transaction mutex makes duplicate detection atomic even
            # across API instances. Never merge a different env/service/signature.
            candidates = session.scalars(select(Incident).where(Incident.state.not_in(['completed', 'failed', 'handoff'])).order_by(Incident.created_at))
            for existing in candidates:
                doc = existing.document
                if (doc['environment'] == request.environment and doc['service'] == request.service and
                    ' '.join(doc['reason'].split()) == ' '.join(reason.split())):
                    self.event(session, existing, 'signal_observed', {'actor': actor['subject'], 'signal': signal})
                    return self.serialize(existing)
            incident = Incident(id=uid(), created_at=now, state='detected', document={
                'environment': request.environment, 'service': request.service,
                'system': SERVICE_SYSTEMS[request.service], 'reason': reason,
                'stages': [], 'error': None, 'data': {'catalog_sha256': self.catalog.sha256}})
            if signal is not None:
                self.data(incident, 'signal', signal)
            session.add(incident)
            self.event(session, incident, 'detected', {'actor': actor['subject']})
            self.transition(session, incident, 'collecting')
            self.enqueue(session, incident, 'collect', {'environment': request.environment,
                'service': request.service, 'since': datetime.fromtimestamp(now - 900, timezone.utc).isoformat().replace('+00:00', 'Z'),
                'catalog_sha256': self.catalog.sha256})
            result = self.serialize(incident)
        return result

    def create_linked(self, request, actor, *, origin, application_id=None, request_sha256=None):
        """Create exactly one code-action execution for the durable core reservation.

        The core owns authorization/eligibility. This method owns atomic idempotence under
        the Application mutex. Existing unrelated documents are never rebound or merged.
        """
        import re
        self.require_store()
        required = {'installation_id', 'incident_id', 'action_id', 'request_sha256'}
        if (not isinstance(origin, dict) or set(origin) != required or
            not isinstance(origin['installation_id'], str) or not origin['installation_id'] or
            len(origin['installation_id']) > 128 or type(origin['incident_id']) is not int or origin['incident_id'] < 1 or
            not isinstance(origin['action_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', origin['action_id']) or
            not re.fullmatch(r'[a-f0-9]{64}', str(origin['request_sha256']))):
            fail(422, 'unified_origin_invalid')
        expected_id = sha({key: origin[key] for key in ('installation_id', 'incident_id', 'action_id')})
        if application_id is not None and application_id != expected_id:
            fail(409, 'unified_application_id_conflict')
        if request_sha256 is not None and request_sha256 != origin['request_sha256']:
            fail(409, 'unified_request_conflict')
        if request.service not in SERVICE_SYSTEMS or request.environment not in ('dev', 'prd'):
            fail(422, 'service_unknown')
        if not self.catalog.configured:
            fail(503, 'profiles_unconfigured')
        if request.reproduction is not None:
            fail(422, 'reproduction_unregistered')
        request_fingerprint = sha(request.model_dump(mode='json'))
        with self.store.transaction() as session:
            existing = session.get(Incident, expected_id)
            if existing is not None:
                data = existing.document.get('data', {})
                if (data.get('unified_origin') != origin or
                    data.get('unified_request_fingerprint') != request_fingerprint):
                    fail(409, 'unified_request_conflict')
                return self.serialize(existing)
            now = self.clock()
            incident = Incident(id=expected_id, created_at=now, state='detected', document={
                'environment': request.environment, 'service': request.service,
                'system': SERVICE_SYSTEMS[request.service], 'reason': scrub(request.reason),
                'stages': [], 'error': None, 'data': {'catalog_sha256': self.catalog.sha256,
                    'unified_origin': copy.deepcopy(origin), 'unified_request_fingerprint': request_fingerprint}})
            session.add(incident)
            if request.signal is not None:
                signal = scrub(request.signal.model_dump(mode='json'))
                signal['sample'] = signal['sample'][:2000]
                signal['sha256'] = text_sha(signal['sample'])
                self.data(incident, 'signal', signal)
            self.event(session, incident, 'detected', {'actor': actor['subject'], 'origin': origin})
            self.transition(session, incident, 'collecting')
            self.enqueue(session, incident, 'collect', {'environment': request.environment, 'service': request.service,
                'since': datetime.fromtimestamp(now - 900, timezone.utc).isoformat().replace('+00:00', 'Z'),
                'catalog_sha256': self.catalog.sha256})
            return self.serialize(incident)

    def get(self, session, incident_id):
        incident = session.get(Incident, incident_id)
        if incident is None:
            fail(404, 'incident_not_found', '사건을 찾을 수 없습니다.')
        return incident

    def serialize(self, incident):
        doc = copy.deepcopy(incident.document)
        if (incident.state in ('awaiting_user_test','awaiting_prd_approval','completed') and
            (doc.get('error') or {}).get('code') == 'write_outcome_unresolved' and
            (doc.get('data', {}).get('release', {}).get('stage_results', {}).get('dev') or {}).get('verified') is True):
            doc['error'] = None
            doc.pop('handoff_classification', None)
        return {'id': incident.id, 'state': incident.state, 'created_at': incident.created_at, **doc}

    def detail(self, incident_id):
        self.require_store()
        # 정돈-02: 조회는 공용 쓰기 잠금 없이 읽고 rollback한다(진행 중 보고·승인 쓰기를 막지 않는다).
        with self.store.read() as session:
            return self.serialize(self.get(session, incident_id))

    LIST_STATE_GROUPS = {
        'automatic': ('detected', 'collecting', 'source_pinning', 'analysis_queued', 'analyzing', 'patch_proposed',
                      'test_build_queued', 'test_build_passed', 'reconciliation_required',
                      *(env + suffix for env in ('dev', 'stg', 'prd')
                        for suffix in ('_targets_collecting', '_deploy_queued', '_verifying', '_verified'))),
        'waiting_people': ('awaiting_user_test', 'awaiting_prd_approval'),
        'handoff': ('handoff',),
        'closed': ('completed', 'failed'),
    }

    def list_incidents(self, *, environment=None, service=None, system=None, state=None, group=None, search=None,
                       limit=200, offset=0, as_of=None, lower=None, upper=None):
        """정돈-04: 필터를 DB에서 먼저 적용한 뒤 총건수·페이지를 같은 조건으로 계산한다(최신 200건 고정 아님).

        as_of(첫 페이지의 최신 created_at)를 넘기면 이후 페이지가 새 사건 유입에 밀리지 않는다. 조회 전용 세션."""
        from sqlalchemy import func, or_
        self.require_store()
        scope = []                      # environment·service·system·search: the range the user is looking at
        if lower is not None:
            scope.append(Incident.created_at >= lower.timestamp())
        if upper is not None:
            scope.append(Incident.created_at < upper.timestamp())
        for key, value in (('environment', environment), ('service', service), ('system', system)):
            if value:
                scope.append(Incident.document[key].as_string() == value)
        if search and search.strip():
            term = search.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            scope.append(or_(Incident.document['reason'].as_string().ilike(f'%{term}%', escape='\\'),
                             Incident.id.like(f'{term}%', escape='\\')))
        status = []                     # state·group: the choice within that range
        if state:
            status.append(Incident.state == state)
        if group:
            status.append(Incident.state.in_(self.LIST_STATE_GROUPS[group]))
        with self.store.read() as session:
            newest = session.scalar(select(func.max(Incident.created_at)))
            anchor = newest if as_of is None or newest is None else min(float(as_of), newest)
            pinned = [Incident.created_at <= anchor] if anchor is not None else []
            total = session.scalar(select(func.count()).select_from(Incident).where(*scope, *status, *pinned)) or 0
            rows = session.scalars(select(Incident).where(*scope, *status, *pinned)
                                   .order_by(Incident.created_at.desc(), Incident.id.desc()).offset(offset).limit(limit))
            items = [self.serialize(row) for row in rows]
            # 상태 분포는 상태·묶음 선택만 빼고 같은 범위로 센다(선택지별 결과 수 안내용).
            by_state = dict(session.execute(select(Incident.state, func.count()).where(*scope, *pinned)
                                            .group_by(Incident.state)).all())
            newer = (session.scalar(select(func.count()).select_from(Incident).where(Incident.created_at > anchor)) or 0
                     if anchor is not None else 0)
        groups = {name: sum(by_state.get(s, 0) for s in members) for name, members in self.LIST_STATE_GROUPS.items()}
        return {'incidents': items, 'total': total, 'limit': limit, 'offset': offset, 'as_of': anchor,
                'newer_since_as_of': newer, 'by_state': by_state, 'by_group': groups,
                'filters': {'environment': environment or None, 'service': service or None, 'system': system or None,
                            'state': state or None, 'group': group or None,
                            'search': (search or '').strip() or None}}

    def events(self, incident_id, after=0):
        self.require_store()
        with self.store.read() as session:
            self.get(session, incident_id)
            rows = session.scalars(select(Event).where(Event.incident_id == incident_id, Event.id > after).order_by(Event.id).limit(500))
            return [{'id': row.id, 'incident_id': incident_id, 'occurred_at': row.occurred_at,
                     'kind': row.kind, 'data': row.document} for row in rows]

    def expire(self, session):
        for job in list(session.scalars(select(Job).where(Job.status == 'leased', Job.lease_until <= self.clock()))):
            incident = self.get(session, job.incident_id)
            if job.kind == 'deploy':
                self.preserve_unknown_write(session, incident, job, 'write_lease_expired')
            else:
                job.status = 'queued'
                job.lease_hash = None

    def preserve_unknown_write(self, session, incident, job, reason):
        """실행 중일 수 있는 deploy는 취소·미실행으로 바꾸지 않고 원래 job·대상 잠금을 유지한 채 read-only 조정만 배정한다."""
        job.status = 'reconciliation_required'
        self.transition(session, incident, 'reconciliation_required', {'job_id': job.id, 'reason': reason})
        self.enqueue(session, incident, 'reconcile', {'original_job_id': job.id, 'original_kind': job.kind,
            'original_payload': job.payload, 'idempotency_key': job.idempotency_key, 'confirm_metadata_only': job.payload.get('stage') == 'dev'})

    def revoke_write_authorization(self, session, incident, job):
        """claim 이후 승인·시험 만료/권한 회수/지문 변경. 새 쓰기 허가를 멈추고 승격은 무효화하되 쓰기 사실은 reconcile로 확인한다."""
        reason = 'write_authorization_revoked'
        release = copy.deepcopy(incident.document['data'].get('release'))
        if release:
            release['valid'] = False
            release['invalidation_reason'] = reason
            for key in ('user_test', 'approval'):
                if release.get(key):
                    release[key]['invalidated'] = True
            self.data(incident, 'release', release)
        document = copy.deepcopy(incident.document)
        document['error'] = {'code': reason, 'message_ko': '배포 실행 중 승인·시험·권한·대상 지문이 무효가 되어 실제 쓰기 결과의 재확인이 필요합니다.'}
        incident.document = document
        self.preserve_unknown_write(session, incident, job, reason)

    def job_phase_valid(self, incident, job):
        if not incident.document.get('data', {}).get('unified_origin'):
            return True  # preserve historical standalone behavior
        if job.kind == 'collect':
            expected = (job.payload.get('environment', '') + '_targets_collecting'
                        if job.payload.get('purpose') == 'release_target' else 'collecting')
        elif job.kind == 'deploy':
            expected = job.payload.get('stage', '') + '_deploy_queued'
        elif job.kind == 'verify':
            expected = job.payload.get('stage', '') + '_verifying'
        else:
            expected = {'source': 'source_pinning', 'test_build': 'test_build_queued',
                        'reconcile': 'reconciliation_required'}.get(job.kind)
        return expected is not None and incident.state == expected

    def lease_violation(self, session, job):
        """heartbeat/결과 보고 직전 재검증. 상태 변경은 호출자가 커밋한 뒤 409로 응답한다."""
        self.expire(session)
        if job.status != 'leased':
            return 'job_lease_expired'
        incident = self.get(session, job.incident_id)
        if not self.job_phase_valid(incident, job):
            if job.kind == 'deploy':
                self.preserve_unknown_write(session, incident, job, 'write_phase_changed')
            else:
                job.status = 'cancelled'
                self.event(session, incident, 'job_cancelled', {'job_id': job.id, 'reason': 'incident_phase_changed'})
            return 'job_state_conflict'
        if job.kind == 'deploy' and not self.deployment_authorized(self.get(session, job.incident_id), job):
            self.revoke_write_authorization(session, self.get(session, job.incident_id), job)
            return 'job_authorization_revoked'
        return None

    RUNNER_OBSERVATION_IDS = {None: 1, 'source': 2, 'cluster': 3}

    def runner_recent(self, role=None):
        if role is None and self.settings.unified_mode:
            return self.runner_recent('source') and self.runner_recent('cluster')
        if role not in self.RUNNER_OBSERVATION_IDS or not self.store.ready():
            return False
        with self.store.read() as session:
            pulse = session.get(RunnerObservation, self.RUNNER_OBSERVATION_IDS[role])
            recent = bool(pulse and 0 <= self.clock() - pulse.last_claim_at <= self.settings.lease_seconds * 2)
            if recent or role is None:
                return recent
            # A long build's authenticated heartbeats can keep its lease alive without a new claim.
            return session.scalar(select(Job.id).where(Job.status == 'leased', Job.kind.in_(self.RUNNER_KINDS[role]),
                Job.lease_until > self.clock(), Job.worker_id.like('subject:%')).limit(1)) is not None

    def runner_status_for_incident(self, incident_id):
        """Read-only connection observation for the next actual external job's role.

        A cluster poll must not make an offline source runner look connected. Model
        analysis and human gates do not require an external runner at that moment.
        """
        self.require_store()
        result = {'observed': None, 'role': None, 'last_claim_at': None, 'lease_expires_at': None}
        with self.store.read() as session:
            incident = self.get(session, incident_id)
            jobs = session.scalars(select(Job).where(Job.incident_id == incident_id,
                Job.status.in_(['queued', 'leased'])).order_by(Job.created_at, Job.id))
            job = next((row for row in jobs if self.job_phase_valid(incident, row)), None)
            if job is None:
                return result
            role = 'source' if job.kind in self.RUNNER_KINDS['source'] else 'cluster'
            pulse = session.get(RunnerObservation, self.RUNNER_OBSERVATION_IDS[role])
            recent = bool(pulse and 0 <= self.clock() - pulse.last_claim_at <= self.settings.lease_seconds * 2)
            lease = job.lease_until if job.status == 'leased' else None
            result.update(role=role, last_claim_at=pulse.last_claim_at if pulse else None,
                          lease_expires_at=lease, observed=recent or bool(lease and lease > self.clock()))
            return result

    MODEL_FAILURE_CODES = ('model_unconfigured', 'model_transport_failed', 'model_output_invalid',
                           'model_app_preamble_unavailable')

    def model_status(self, configured):
        """정돈-07: '구성됨'과 '호출 성공 관측'을 분리한다. 저장된 역할 완료·모델 실패 이벤트만 읽는다(모델 호출 없음).

        state: unconfigured / configured_unverified(성공 기록 없음) / recent_success / recent_failure(마지막 기록이 실패)."""
        from sqlalchemy import func
        with self.store.read() as session:
            success = session.scalar(select(func.max(Event.occurred_at)).where(Event.kind == 'role_completed'))
            failure_at, failure_code = None, None
            for row in session.scalars(select(Event).where(Event.kind.in_(['handoff', 'failed']))
                                       .order_by(Event.id.desc()).limit(200)):
                code = (row.document or {}).get('code')
                if code in self.MODEL_FAILURE_CODES:
                    failure_at, failure_code = row.occurred_at, code
                    break
        if failure_at is not None and (success is None or failure_at > success):
            state = 'recent_failure'
        elif success is not None:
            state = 'recent_success'
        else:
            state = 'configured_unverified' if configured else 'unconfigured'
        return {'state': state, 'configured': bool(configured), 'last_success_at': success,
                'last_failure_at': failure_at, 'last_failure_code': failure_code}

    def waiting_people(self):
        """사람의 시험·승인을 기다리는 사건 수(자동 진행과 구분). 조회 전용."""
        from sqlalchemy import func
        with self.store.read() as session:
            rows = dict(session.execute(select(Incident.state, func.count())
                                        .where(Incident.state.in_(self.LIST_STATE_GROUPS['waiting_people']))
                                        .group_by(Incident.state)).all())
        return {'awaiting_user_test': rows.get('awaiting_user_test', 0),
                'awaiting_prd_approval': rows.get('awaiting_prd_approval', 0)}

    RUNNER_KINDS = {'source': frozenset({'source', 'test_build'}),
                    'cluster': frozenset({'collect', 'deploy', 'verify', 'reconcile'})}

    def runner_kinds(self, actor, *, linked=False):
        role = (actor or {}).get('runner_role')
        if role in self.RUNNER_KINDS:
            return self.RUNNER_KINDS[role]
        if self.settings.unified_mode or linked:
            fail(403, 'runner_role_separation_required')
        return frozenset({'collect', 'source', 'test_build', 'deploy', 'verify', 'reconcile'})

    def runner_lease(self, job, actor):
        """Principal binding survives stolen lease tokens and spoofed worker labels."""
        if job is None:
            fail(404, 'job_not_found')
        # Bound jobs are recognizable without schema changes to historical application_jobs.
        if (job.worker_id or '').startswith('subject:'):
            if not actor or job.worker_id != 'subject:' + sha(actor.get('subject')):
                fail(403, 'runner_job_subject_conflict')
            if job.kind not in self.runner_kinds(actor, linked=True):
                fail(403, 'runner_job_kind_forbidden')
        elif self.settings.unified_mode:
            fail(403, 'runner_legacy_lease_forbidden')

    def claim(self, request, actor=None):
        self.require_store()
        allowed = set(request.capabilities) & self.runner_kinds(actor)
        with self.store.transaction() as session:
            observation_id = self.RUNNER_OBSERVATION_IDS[(actor or {}).get('runner_role')]
            pulse = session.get(RunnerObservation, observation_id)
            if pulse is None:
                session.add(RunnerObservation(id=observation_id, last_claim_at=self.clock()))
            else:
                pulse.last_claim_at = self.clock()
            self.expire(session)
            session.flush()
            job = None
            for candidate in session.scalars(select(Job).where(Job.status == 'queued', Job.kind.in_(allowed))
                                             .order_by(Job.created_at, Job.id)):
                owner = self.get(session, candidate.incident_id)
                linked = bool(owner.document.get('data', {}).get('unified_origin'))
                if linked and not self.job_phase_valid(owner, candidate):
                    candidate.status = 'cancelled'
                    self.event(session, owner, 'job_cancelled', {'job_id': candidate.id, 'reason': 'incident_phase_changed'})
                    continue
                if linked and not (actor or {}).get('runner_role'):
                    continue
                job = candidate
                break
            if job is None:
                return {'job': None}
            incident = self.get(session, job.incident_id)
            release = incident.document.get('data', {}).get('release') or {}
            target = job.payload.get('stage', job.payload.get('environment'))
            if target == 'stg' or (release and release.get('protocol') != 'dev-prd/v2' and job.kind != 'reconcile'):
                job.status = 'cancelled'
                self.stop(session, incident, 'legacy_release_read_only' if release else 'environment_retired')
                return {'job': None}
            if job.kind == 'deploy' and not self.deploy_claim_valid(session, self.get(session, job.incident_id), job):
                return {'job': None}
            token = secrets.token_urlsafe(32)
            worker = ('subject:' + sha(actor['subject'])) if actor and actor.get('runner_role') else request.worker_id
            job.status, job.worker_id, job.lease_hash = 'leased', worker, text_sha(token)
            job.fence += 1
            job.lease_until = self.lease_deadline(session, job)
            return {'job': {'id': job.id, 'kind': job.kind, 'incident_id': job.incident_id,
                'payload': copy.deepcopy(job.payload), 'lease_token': token, 'fencing_token': job.fence,
                'idempotency_key': job.idempotency_key, 'lease_expires_at': job.lease_until}}

    def lease_deadline(self, session, job):
        deadline = self.clock() + self.settings.lease_seconds
        if job.kind == 'deploy' and job.payload.get('stage') == 'prd':
            release = self.get(session, job.incident_id).document['data']['release']
            deadline = min(deadline, release['approval']['expires_at'], release['user_test']['expires_at'])
        return deadline

    def lease_check(self, job, request):
        if job is None:
            fail(404, 'job_not_found')
        if job.lease_hash != text_sha(request.lease_token) or job.fence != request.fencing_token:
            fail(409, 'job_fence_conflict')

    def heartbeat(self, job_id, request, actor=None):
        self.require_store()
        with self.store.transaction() as session:
            job = session.get(Job, job_id)
            self.runner_lease(job, actor)
            self.lease_check(job, request)
            code = self.lease_violation(session, job)
            if code is None:
                # prd lease는 승인·사용자 시험 만료를 넘어 연장하지 않는다.
                job.lease_until = self.lease_deadline(session, job)
                return {'job_id': job.id, 'fencing_token': job.fence, 'lease_expires_at': job.lease_until}
        # 보존 전이를 커밋한 뒤 거부한다(트랜잭션 안에서 실패하면 전이가 롤백된다).
        fail(409, code)

    def report(self, job_id, request, actor=None):
        self.require_store()
        result_hash = sha(request.result)
        with self.store.transaction() as session:
            job = session.get(Job, job_id)
            self.runner_lease(job, actor)
            self.lease_check(job, request)
            if job.result_hash is not None:
                if job.result_hash != result_hash:
                    fail(409, 'job_result_conflict')
                return job.receipt
            code = self.lease_violation(session, job)
            if code is not None:
                # 거부한 보고의 주장은 사실로 반영하지 않고 지문과 선언 결과만 남긴다. reconcile이 실제 상태를 재조회한다.
                outcome = request.result.get('write_outcome')
                self.event(session, self.get(session, job.incident_id), 'job_report_rejected', {
                    'job_id': job.id, 'kind': job.kind, 'code': code, 'result_sha256': result_hash,
                    'claimed_write_outcome': outcome if outcome in ('written', 'not_written', 'unknown') else None})
            else:
                incident = self.get(session, job.incident_id)
                result = scrub(request.result)
                self.apply_result(session, incident, job, result)
                if job.kind == 'deploy' and result.get('write_outcome') == 'unknown':
                    job.status = 'reconciliation_required'
                else:
                    job.status = 'completed'
                job.result_hash = result_hash
                job.receipt = {'job_id': job.id, 'incident_id': incident.id, 'result_sha256': result_hash,
                               'state': incident.state, 'accepted_at': self.clock()}
                self.event(session, incident, 'job_reported', {'job_id': job.id, 'kind': job.kind,
                                                             'result_sha256': result_hash})
                return job.receipt
        fail(409, code)

    def apply_result(self, session, incident, job, result):
        if job.kind == 'collect' and job.payload.get('purpose') == 'release_target':
            return self.collect_release_result(session, incident, job, result)
        if job.kind == 'test_build':
            return self.test_build_result(session, incident, job, result)
        if job.kind == 'deploy':
            return self.deploy_result(session, incident, job, result)
        if job.kind == 'verify':
            return self.verify_result(session, incident, job, result)
        if job.kind == 'reconcile':
            return self.reconcile_result(session, incident, job, result)
        if job.kind == 'collect':
            if result.get('ok') is not True:
                return self.stop(session, incident, 'collect_failed', 'failed')
            try:
                target = Target.model_validate(result['target']).model_dump()
            except (ValidationError, KeyError):
                return self.stop(session, incident, 'collect_target_invalid')
            doc = incident.document
            if (target['environment'], target['service'], target['system']) != (doc['environment'], doc['service'], doc['system']):
                return self.stop(session, incident, 'collect_target_mismatch')
            if not result.get('logs') or not result.get('evidence') or not result.get('observed_at'):
                return self.stop(session, incident, 'collect_evidence_missing')
            allowed_services = {doc['service'], *self.catalog.dependencies.get(doc['service'], {}).values()}
            raw_logs, raw_evidence = result['logs'], result['evidence']
            if not isinstance(raw_logs, list) or not isinstance(raw_evidence, list):
                return self.stop(session, incident, 'collect_evidence_invalid')
            for row in raw_logs + raw_evidence:
                if (not isinstance(row, dict) or not isinstance(row.get('service'), str) or
                    row['service'] not in allowed_services or row.get('environment') != doc['environment'] or
                    not isinstance(row.get('text'), str) or not row['text']):
                    return self.stop(session, incident, 'collect_evidence_scope_invalid')
            logs = [{k: row[k] for k in ('service', 'environment', 'observed_at', 'text') if k in row}
                    for row in raw_logs]
            evidence = [{k: row[k] for k in ('id', 'service', 'environment', 'observed_at', 'source', 'text', 'quality') if k in row}
                        for row in raw_evidence]
            for row in logs + evidence:
                if len(row['text']) > 2000:
                    row['text'] = row['text'][:2000]
                    if row.get('quality') == 'observed':
                        row['quality'] = 'truncated'
            self.data(incident, 'collection', {'target': target, 'logs': logs, 'evidence': evidence, 'observed_at': result['observed_at']})
            self.transition(session, incident, 'source_pinning')
            self.enqueue(session, incident, 'source', {'target': target, 'profile_id': self.catalog.profiles[doc['service']]['id']})
        elif job.kind == 'source':
            source = result.get('source_snapshot') or {}
            target = incident.document['data']['collection']['target']
            if result.get('ok') is not True or source.get('provenance_verified') is not True:
                return self.stop(session, incident, 'source_provenance_unverified')
            if source.get('base_image') != target['image'] or not source.get('id') or source.get('profile_id') != job.payload['profile_id']:
                return self.stop(session, incident, 'source_image_mismatch')
            files = source.get('files')
            hashes = source.get('file_sha256')
            if not isinstance(files, dict) or not files or not isinstance(hashes, dict):
                return self.stop(session, incident, 'source_files_missing')
            for path, body in files.items():
                if not safe_source_path(path) or not isinstance(body, str) or text_sha(body) != hashes.get(path):
                    return self.stop(session, incident, 'source_integrity_invalid')
            services = source.get('affected_services', [])
            if not services or any(service not in SERVICE_SYSTEMS for service in services) or incident.document['service'] not in services:
                return self.stop(session, incident, 'source_affected_services_invalid')
            profile = self.catalog.profiles[incident.document['service']]
            if set(services) != set(profile['affected_services']):
                return self.stop(session, incident, 'source_affected_services_invalid')
            source = {k: source.get(k) for k in ('id', 'base_commit', 'base_image', 'files', 'file_sha256', 'profile_id', 'affected_services', 'provenance_verified')}
            self.data(incident, 'source_snapshot', source)
            self.transition(session, incident, 'analysis_queued')
        else:
            self.stop(session, incident, 'result_stage_unimplemented')
