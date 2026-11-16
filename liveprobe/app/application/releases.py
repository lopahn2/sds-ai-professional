"""불변 산출물 승격과 사람 시험/승인. runner는 승인 레코드를 만들 수 없다."""
import copy
from datetime import datetime, timezone
import re
import uuid
from sqlalchemy import select
from ..environments import RELEASE_PROTOCOL, release_protocol
from .contracts import Target
from .safety import fail, sha, test_file
from .storage import Job
from .result_evidence import error_code, tests_evidence, target_rows, verification_rows, OUTCOMES

HEX = re.compile(r'^[a-f0-9]{64}$')
IMAGE = re.compile(r'^\S+@sha256:[a-f0-9]{64}$')


def identifier():
    return uuid.uuid4().hex


class ReleaseControl:
    def release_fingerprint(self, release):
        fields = {'release_id': release['release_id'], 'artifact_sha256': release['artifact_sha256'],
                    'targets': release['targets'], 'affected_services': release['affected_services']}
        if 'protocol' in release:
            fields['protocol'] = release['protocol']
        return sha(fields)

    def release_get(self, incident_id):
        detail = self.detail(incident_id)
        return {'incident_id': incident_id, 'state': detail['state'], 'release': detail['data'].get('release'),
                'test_build': detail['data'].get('test_build')}

    def _invalidate(self, session, incident, reason):
        release = copy.deepcopy(incident.document['data'].get('release'))
        if release:
            release['valid'] = False
            release['invalidation_reason'] = reason
            for key in ('user_test', 'approval'):
                if release.get(key):
                    release[key]['invalidated'] = True
            self.data(incident, 'release', release)
        for job in session.scalars(select(Job).where(Job.incident_id == incident.id, Job.status.in_(['queued', 'leased']))):
            if job.kind == 'deploy' and job.status == 'leased':
                # 이미 외부 쓰기 중이면 취소를 성공으로 가장하지 않는다.
                job.status = 'reconciliation_required'
                self.enqueue(session, incident, 'reconcile', {'original_job_id': job.id, 'original_kind': 'deploy',
                    'original_payload': job.payload, 'idempotency_key': job.idempotency_key})
            elif job.kind != 'reconcile':
                job.status = 'cancelled'
        self.stop(session, incident, reason)

    def invalidate_release(self, incident_id, reason):
        self.require_store()
        allowed = {'artifact_rebuilt', 'targets_changed', 'plan_changed', 'source_changed'}
        if reason not in allowed:
            fail(422, 'invalidation_reason_invalid')
        with self.store.transaction() as session:
            incident = self.get(session, incident_id)
            self._invalidate(session, incident, reason)

    @staticmethod
    def test_rows(executed, added):
        if not isinstance(executed, list) or not executed:
            return None
        rows, observed = [], {}
        for row in executed:
            if isinstance(row, str):
                # Legacy names can be normalized, but carry no pass/fail proof.
                rows.append({'id': row, 'file': test_file(row) or row, 'status': None, 'phase': None})
                continue
            if not isinstance(row, dict):
                return None
            test_id, file = row.get('id'), row.get('file')
            status, phase = row.get('status'), row.get('phase')
            if (not isinstance(test_id, str) or not isinstance(file, str) or test_file(test_id) != file or
                status not in ('passed', 'failed', 'skipped', 'error') or phase not in ('before', 'after')):
                return None
            if (test_id, phase) in observed:
                if observed[test_id, phase] != status:
                    return None
                continue  # identical aggregate rows across registered runtime modes
            observed[test_id, phase] = status
            if phase == 'after' and status in ('failed', 'error'):
                return None
            rows.append({'id': test_id, 'file': file, 'status': status, 'phase': phase})
        if not added or any(test_file(test_id) is None or
            observed.get((test_id, 'before')) not in ('failed', 'error') or
            observed.get((test_id, 'after')) != 'passed' for test_id in added):
            return None
        return rows

    def test_build_result(self, session, incident, job, result):
        source = incident.document['data']['source_snapshot']
        patch = incident.document['data']['patch']
        tests, artifact = result.get('tests'), result.get('artifact')
        record = {'job_id': job.id, 'ok': result.get('ok') if type(result.get('ok')) is bool else None,
                  'accepted': False, 'rejection_code': 'test_build_evidence_invalid',
                  'error_code': error_code(result.get('error')), 'tests': tests_evidence(tests)}
        self.data(incident, 'test_build', record)
        # Revisions concern a local patch only. No deployment or unknown external write is replayed.
        repairable = {'regression_not_failed', 'regression_not_passed', 'before_not_reproduced',
                      'after_tests_failed', 'patch_context_mismatch', 'hunk_count_mismatch',
                      'added_test_method_required', 'added_regression_required', 'hunk_offset',
                      'missing_hunk', 'invalid_hunk', 'source_test_report_missing'}
        if (result.get('ok') is False and result.get('write_outcome') == 'not_written'
            and record['error_code'] in ('lease_lost', 'runner_execution_failed') and
            not incident.document['data'].get('isolated_build_retry')):
            self.data(incident, 'isolated_build_retry', {'prior_job_id':job.id, 'code':record['error_code']})
            self.enqueue(session, incident, 'test_build', copy.deepcopy(job.payload))
            self.transition(session, incident, 'test_build_queued', {'reason':'isolated_build_retry'})
            return
        if result.get('ok') is False and result.get('write_outcome') == 'not_written' and record['error_code'] in repairable:
            revisions = list(incident.document['data'].get('patch_revisions', []))
            if len(revisions) < 1:
                revisions.append({'job_id': job.id, 'reason': record['error_code'], 'diff_sha256': patch['diff_sha256']})
                self.data(incident, 'patch_revisions', revisions)
                self.transition(session, incident, 'analysis_queued', {'reason': 'patch_revision_requested'})
                return
        if not isinstance(tests, dict) or not isinstance(artifact, dict):
            return self.stop(session, incident, 'test_build_evidence_invalid')
        required = ('image', 'source_snapshot_id', 'diff_sha256', 'source_tree_sha256', 'build_receipt_sha256')
        executed = self.test_rows(tests.get('executed_tests'), patch['tests_added'])
        if (result.get('ok') is not True or tests.get('before_failed') is not True or
            tests.get('after_passed') is not True or tests.get('regression_executed') is not True or
            type(tests.get('total')) is not int or tests['total'] < 1 or
            type(tests.get('failed')) is not int or tests['failed'] != 0 or
            executed is None or
            not HEX.fullmatch(str(tests.get('report_sha256', ''))) or
            any(not artifact.get(k) for k in required) or not IMAGE.fullmatch(str(artifact.get('image', ''))) or
            artifact.get('source_snapshot_id') != source['id'] or artifact.get('diff_sha256') != patch['diff_sha256'] or
            not HEX.fullmatch(str(artifact.get('source_tree_sha256', ''))) or
            not HEX.fullmatch(str(artifact.get('build_receipt_sha256', '')))):
            return self.stop(session, incident, 'test_build_evidence_invalid')
        if incident.document['data'].get('release'):
            return self._invalidate(session, incident, 'artifact_rebuilt')
        record.update(accepted=True, rejection_code=None)
        record['tests']['executed_tests'] = executed
        self.data(incident, 'test_build', record)
        tests = record['tests']
        artifact = {k: artifact[k] for k in required}
        release = {'protocol': RELEASE_PROTOCOL, 'release_id': identifier(), 'artifact': artifact, 'artifact_sha256': sha(artifact),
            'affected_services': source['affected_services'], 'targets': {}, 'plan_sha256': None,
            'valid': True, 'user_test': None, 'approval': None, 'stage_results': {},
            'summary_ko': patch['summary_ko'], 'risk_ko': patch['risk_ko'],
            'rollback_ko': '각 대상의 이전 image digest로 UID/RV·현재 이미지·구성 지문을 다시 확인한 뒤 같은 CAS 교체를 수행합니다. 데이터 안전성을 확인할 수 없으면 자동 원복하지 않고 이관합니다.',
            'tests': {k: tests[k] for k in ('before_failed', 'after_passed', 'regression_executed', 'total', 'failed', 'executed_tests', 'report_sha256')}}
        self.data(incident, 'release', release)
        self.transition(session, incident, 'test_build_passed', {'artifact_sha256': release['artifact_sha256']})
        self.collect_release_targets(session, incident, 'dev')

    def collect_release_targets(self, session, incident, stage):
        release = incident.document['data']['release']
        self.transition(session, incident, stage + '_targets_collecting')
        for service in release['affected_services']:
            self.enqueue(session, incident, 'collect', {'environment': stage, 'service': service,
                'since': datetime.fromtimestamp(self.clock() - 900, timezone.utc).isoformat().replace('+00:00', 'Z'),
                'catalog_sha256': incident.document['data'].get('catalog_sha256', ''),
                'purpose': 'release_target', 'release_id': release['release_id']})

    def collect_release_result(self, session, incident, job, result):
        from .control import SERVICE_SYSTEMS
        release = copy.deepcopy(incident.document['data']['release'])
        stage, service = job.payload['environment'], job.payload['service']
        if not release['valid'] or job.payload['release_id'] != release['release_id']:
            return self.stop(session, incident, 'release_generation_conflict')
        try:
            observed = Target.model_validate(result['target']).model_dump()
        except Exception:
            return self.stop(session, incident, 'release_target_invalid')
        if (result.get('ok') is not True or observed['environment'] != stage or observed['service'] != service or
            observed['system'] != SERVICE_SYSTEMS[service]):
            return self.stop(session, incident, 'release_target_mismatch')
        targets = release['targets'].setdefault(stage, [])
        new = dict(observed)
        new['previous_image'] = new.pop('image')
        old = next((target for target in targets if target['service'] == service), None)
        if old and old != new:
            return self._invalidate(session, incident, 'targets_changed')
        if old is None:
            targets.append(new)
        targets.sort(key=lambda target: target['service'])
        self.data(incident, 'release', release)
        if len(targets) != len(release['affected_services']):
            return
        if stage == 'prd':
            release['plan_sha256'] = self.release_fingerprint(release)
            self.data(incident, 'release', release)
            self.transition(session, incident, 'awaiting_user_test')
        else:
            self.queue_deploy(session, incident, stage)

    def unified_prd_gate(self, incident):
        if incident.document.get('data', {}).get('unified_origin'):
            # Re-observe through a server-owned transport; request metadata is never proof.
            try:
                return self.merge_authorized(incident.document) is True
            except Exception:
                return False
        return True

    def prd_authorized(self, release, consumed_by=None):
        approval, test = release.get('approval') or {}, release.get('user_test') or {}
        return bool(release_protocol(release) == RELEASE_PROTOCOL and release.get('valid') is True and
            release.get('plan_sha256') == self.release_fingerprint(release) and
            sha(release['artifact']) == release.get('artifact_sha256') and
            release.get('stage_results', {}).get('dev', {}).get('verified') is True and
            approval.get('kind') == 'human' and approval.get('invalidated') is False and
            approval.get('consumed_by_job_id') == consumed_by and approval.get('expires_at', 0) > self.clock() and
            self.human_authorized(approval.get('subject'), 'prd-approver') and
            approval.get('artifact_sha256') == release['artifact_sha256'] and
            approval.get('plan_sha256') == release['plan_sha256'] and approval.get('user_test_id') == test.get('id') and
            test.get('kind') == 'human' and test.get('passed') is True and test.get('invalidated') is False and
            self.human_authorized(test.get('subject'), 'tester') and test.get('expires_at', 0) > self.clock() and
            test.get('release_id') == release['release_id'] and test.get('image') == release['artifact']['image'] and
            test.get('artifact_sha256') == release['artifact_sha256'] and test.get('plan_sha256') == release['plan_sha256'])

    def queue_deploy(self, session, incident, stage):
        release = copy.deepcopy(incident.document['data']['release'])
        if stage == 'prd' and not self.unified_prd_gate(incident):
            fail(409, 'pull_request_merge_unobserved', '시험한 소스의 PR·사람 병합 확인이 필요합니다.')
        if stage == 'prd' and (incident.state != 'awaiting_prd_approval' or not self.prd_authorized(release)):
            return self._invalidate(session, incident, 'approval_or_release_invalid')
        targets = release['targets'][stage]
        if not release['valid'] or sha(release['artifact']) != release['artifact_sha256']:
            return self._invalidate(session, incident, 'artifact_rebuilt')
        # 이 제품 내부 대상 경합을 차단한다. 제품 간 lease는 외부 runner가 별도로 검증한다.
        for active in session.scalars(select(Job).where(Job.kind == 'deploy', Job.status.in_(['queued', 'leased', 'reconciliation_required']))):
            active_targets = {(target['environment'], target['service']) for target in active.payload['targets']}
            if active_targets.intersection((target['environment'], target['service']) for target in targets):
                return self.stop(session, incident, 'target_owned_by_other_release')
        attempt = identifier()
        payload = {'stage': stage, 'targets': targets, 'artifact': release['artifact'],
            'authorization_kind': 'user_prd' if stage == 'prd' else 'auto_dev', 'protocol': RELEASE_PROTOCOL,
            'release_id': release['release_id'], 'attempt_id': attempt,
            'idempotency_key': 'application-release:' + release['release_id'] + ':' + stage + ':' + attempt,
            'profile_id': incident.document['data']['source_snapshot']['profile_id'],
            'artifact_sha256': release['artifact_sha256'], 'plan_sha256': release['plan_sha256']}
        job = self.enqueue(session, incident, 'deploy', payload)
        job.idempotency_key = payload['idempotency_key']
        if stage == 'prd':
            release['approval']['consumed_by_job_id'] = job.id
            self.data(incident, 'release', release)
        self.transition(session, incident, stage + '_deploy_queued', {'authorization_kind': payload['authorization_kind']})

    def deployment_authorized(self, incident, job):
        release = incident.document['data'].get('release') or {}
        stage = job.payload['stage']
        if not self.job_phase_valid(incident, job):
            return False
        valid = (release_protocol(release) == RELEASE_PROTOCOL and stage in ('dev', 'prd') and release.get('valid') is True and release.get('release_id') == job.payload['release_id'] and
                 release.get('artifact') == job.payload['artifact'] and sha(release['artifact']) == release['artifact_sha256'] and
                 release['artifact_sha256'] == job.payload['artifact_sha256'] and
                 release.get('targets', {}).get(stage) == job.payload['targets'])
        if stage == 'prd':
            valid = (valid and self.unified_prd_gate(incident) and incident.state == 'prd_deploy_queued' and self.prd_authorized(release, job.id) and
                     release.get('plan_sha256') == job.payload['plan_sha256'])
        return valid

    def deploy_claim_valid(self, session, incident, job):
        valid = self.deployment_authorized(incident, job)
        if not valid:
            job.status = 'cancelled'
            self._invalidate(session, incident, 'approval_or_release_invalid')
        return valid

    def observed_targets_valid(self, targets, payload):
        if not isinstance(targets, list) or len(targets) != len(payload['targets']):
            return False
        expected = {(t['environment'], t['service']): t for t in payload['targets']}
        seen = set()
        for target in targets:
            if not isinstance(target, dict):
                return False
            key = (target.get('environment'), target.get('service'))
            pinned = expected.get(key)
            if not pinned or key in seen or target.get('image') != payload['artifact']['image']:
                return False
            if any(field in target and target[field] != pinned.get(field) for field in ('uid', 'container', 'system')) or target.get('uid') != pinned.get('uid'):
                return False
            if not target.get('resource_version'):
                return False
            seen.add(key)
        return len(seen) == len(expected)

    def deploy_result(self, session, incident, job, result):
        release = copy.deepcopy(incident.document['data']['release'])
        stage = job.payload['stage']
        outcome = result.get('write_outcome')
        safe_outcome = outcome if outcome in OUTCOMES else None
        safe_targets = target_rows(result.get('targets'))
        stage_result = {'deployed': False, 'verified': False, 'deploy_job_id': job.id,
                        'write_outcome': safe_outcome, 'targets': safe_targets,
                        'error_code': error_code(result.get('error'))}
        if outcome == 'unknown':
            release['stage_results'][stage] = stage_result
            self.data(incident, 'release', release)
            self.preserve_unknown_write(session, incident, job, 'write_result_unknown')
            return
        if result.get('ok') is not True or outcome != 'written':
            written = outcome == 'written'
            stage_result['error'] = 'deploy_failed'
            release['stage_results'][stage] = stage_result
            self.data(incident, 'release', release)
            return self.stop(session, incident, stage + '_partial_deploy_requires_review' if written else stage + '_deploy_failed',
                             'handoff' if written else 'failed')
        if not self.observed_targets_valid(result.get('targets'), job.payload):
            release['stage_results'][stage] = stage_result
            self.data(incident, 'release', release)
            return self.stop(session, incident, 'deploy_readback_mismatch')
        stage_result['deployed'] = True
        release['stage_results'][stage] = stage_result
        self.data(incident, 'release', release)
        self.transition(session, incident, stage + '_verifying')
        self.enqueue(session, incident, 'verify', copy.deepcopy(job.payload))

    def verification_valid(self, rows, payload):
        if not isinstance(rows, list) or len(rows) != len(payload['targets']):
            return False
        expected = {(target['environment'], target['service']) for target in payload['targets']}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                return False
            environment, service = row.get('environment'), row.get('service')
            if not isinstance(environment, str) or not isinstance(service, str):
                return False
            key = (environment, service)
            business = row.get('business')
            if (key not in expected or key in seen or row.get('image') != payload['artifact']['image'] or
                not isinstance(business, dict) or business.get('ok') is not True or
                business.get('profile_id') != self.catalog.profiles[service]['id'] or
                not isinstance(business.get('body_sha256'), str) or not HEX.fullmatch(business['body_sha256']) or
                type(business.get('status_code')) is not int or business['status_code'] != 200):
                return False
            seen.add(key)
        return seen == expected

    def verify_result(self, session, incident, job, result):
        release = copy.deepcopy(incident.document['data']['release'])
        verification = result.get('verification')
        stage = job.payload['stage']
        stage_result = release['stage_results'][stage]
        stage_result.update(verification=verification_rows(verification), verify_job_id=job.id,
                            error_code=error_code(result.get('error')))
        self.data(incident, 'release', release)
        if (result.get('ok') is not True or result.get('write_outcome') != 'not_written' or
            not self.verification_valid(verification, job.payload)):
            stage_result['error'] = stage + '_business_verification_failed'
            self.data(incident, 'release', release)
            return self.stop(session, incident, stage + '_business_verification_failed', 'failed')
        stage_result['verified'] = True
        self.data(incident, 'release', release)
        self.transition(session, incident, stage + '_verified')
        if stage == 'dev':
            self.collect_release_targets(session, incident, 'prd')
        else:
            self.transition(session, incident, 'completed')

    def reconcile_result(self, session, incident, job, result):
        payload = job.payload['original_payload']
        release = copy.deepcopy(incident.document['data'].get('release') or {})
        original = session.get(Job, job.payload['original_job_id'])
        confirmed = (result.get('ok') is True and result.get('write_outcome') == 'written' and
                     self.observed_targets_valid(result.get('targets'), payload))
        # 무효화된 승인도 이미 발생한 쓰기 사실을 지우는 근거가 될 수 없다.
        # 원래 payload의 읽기 재조회 결과를 보존하고, 확정 전에는 원래 deploy의 대상 잠금을 해제하지 않는다.
        stage = payload['stage']
        release.setdefault('stage_results', {})[stage] = {
            'deployed': confirmed, 'verified': False,
            'deploy_job_id': job.payload['original_job_id'], 'reconcile_job_id': job.id,
            'write_outcome': result.get('write_outcome') if result.get('write_outcome') in OUTCOMES else None,
            'targets': target_rows(result.get('targets')), 'verification': verification_rows(result.get('verification')),
            'error_code': error_code(result.get('error'))}
        if confirmed and original is not None:
            original.status = 'reconciled'
        self.data(incident, 'release', release)
        if (not release.get('valid') or
            stage == 'prd' and not self.prd_authorized(release, job.payload['original_job_id'])):
            return self.stop(session, incident, 'release_invalidated_during_write')
        if not confirmed:
            if (stage == 'dev' and release.get('protocol') == RELEASE_PROTOCOL and original and
                payload.get('release_id') == release['release_id'] and
                job.payload.get('confirm_metadata_only') is True and result.get('error') == 'metadata_only_released' and
                result.get('write_outcome') == 'not_written' and not release.get('metadata_retry') and
                self.observed_original_targets(result.get('targets'), payload)):
                original.status='completed'
                release['metadata_retry'] = {'original_job_id':original.id, 'reconcile_job_id':job.id}
                release['targets'].pop('dev',None)
                self.data(incident,'release',release)
                updated = dict(incident.document)
                updated['error'] = None
                updated.pop('handoff_classification', None)
                incident.document = updated
                self.collect_release_targets(session,incident,'dev')
                return
            return self.stop(session, incident, 'write_outcome_unresolved')
        # 조정은 읽기만 수행하며 원래 배포를 새 lease로 재실행하지 않는다.
        class Original:
            pass
        observed = Original()
        observed.payload = payload
        observed.id = original.id if original else job.payload['original_job_id']
        observed.idempotency_key = job.payload['idempotency_key']
        self.deploy_result(session, incident, observed, result)

    @staticmethod
    def observed_original_targets(rows, payload):
        if not isinstance(rows, list) or len(rows) != len(payload['targets']):
            return False
        expected = {(t['environment'],t['service'],t['uid'],t['previous_image']) for t in payload['targets']}
        return {(r.get('environment'),r.get('service'),r.get('uid'),r.get('image'))
                for r in rows if isinstance(r,dict)} == expected

    def review_dev_metadata(self, incident_id):
        self.require_store()
        with self.store.transaction() as session:
            incident=self.get(session,incident_id)
            release=incident.document.get('data',{}).get('release') or {}
            stage=release.get('stage_results',{}).get('dev',{})
            if (incident.state!='handoff' or release.get('protocol')!=RELEASE_PROTOCOL or not release.get('valid')
                or release.get('metadata_retry') or stage.get('error_code')!='metadata_only_observed'):
                fail(409,'metadata_review_unavailable')
            original=session.get(Job,stage['deploy_job_id'])
            if not original or original.status!='reconciliation_required' or original.payload['stage']!='dev':
                fail(409,'metadata_review_unavailable')
            self.enqueue(session,incident,'reconcile',{'original_job_id':original.id,'original_kind':'deploy',
                'original_payload':original.payload,'idempotency_key':original.idempotency_key,'confirm_metadata_only':True})
            self.transition(session,incident,'reconciliation_required',{'reason':'verify_prior_metadata_only'})
            return {'incident_id':incident.id,'state':incident.state}

    def matching_release(self, incident, body):
        release = copy.deepcopy(incident.document['data'].get('release') or {})
        if (not release.get('valid') or release.get('release_id') != body.release_id or
            release.get('artifact_sha256') != body.artifact_sha256 or release.get('plan_sha256') != body.plan_sha256 or
            release.get('artifact', {}).get('image') != body.image or
            sha(release['artifact']) != release['artifact_sha256'] or self.release_fingerprint(release) != release['plan_sha256']):
            fail(409, 'release_fingerprint_conflict', '시험·승인할 버전 또는 대상이 변경되었습니다.')
        return release

    def user_test(self, incident_id, body, actor):
        self.require_store()
        with self.store.transaction() as session:
            incident = self.get(session, incident_id)
            if incident.state != 'awaiting_user_test':
                fail(409, 'user_test_state_conflict', 'dev 사용자 시험 대기 상태가 아닙니다.')
            release = self.matching_release(incident, body)
            if release_protocol(release) != RELEASE_PROTOCOL:
                fail(409, 'legacy_release_read_only')
            if not release['stage_results'].get('dev', {}).get('verified'):
                fail(409, 'dev_verification_required')
            from .safety import scrub
            test = {'id': identifier(), 'subject': actor['subject'], 'kind': 'human', 'passed': body.passed,
                'notes_ko': scrub(body.notes_ko), 'occurred_at': self.clock(), 'expires_at': self.clock() + self.settings.approval_seconds,
                'release_id': body.release_id, 'artifact_sha256': body.artifact_sha256,
                'plan_sha256': body.plan_sha256, 'image': body.image, 'invalidated': False}
            release['user_test'] = test
            self.data(incident, 'release', release)
            if body.passed:
                self.transition(session, incident, 'awaiting_prd_approval', {'user_test_id': test['id'], 'subject': actor['subject']})
            else:
                self.stop(session, incident, 'user_test_failed', 'failed')
            return {'incident_id': incident_id, 'state': incident.state, 'user_test': test}

    def approve(self, incident_id, body, actor):
        self.require_store()
        with self.store.transaction() as session:
            incident = self.get(session, incident_id)
            if not self.unified_prd_gate(incident):
                fail(409, 'pull_request_merge_unobserved', '시험한 소스의 PR·사람 병합 확인이 필요합니다.')
            if incident.state != 'awaiting_prd_approval':
                fail(409, 'approval_state_conflict', '사용자 시험 후 명시 승인이 필요합니다.')
            release = self.matching_release(incident, body)
            test = release.get('user_test') or {}
            if (test.get('id') != body.user_test_id or test.get('invalidated') is not False or
                test.get('kind') != 'human' or test.get('passed') is not True or test.get('expires_at', 0) <= self.clock()):
                fail(409, 'user_test_invalid_or_expired')
            approval = {'id': identifier(), 'subject': actor['subject'], 'kind': 'human', 'user_test_id': test['id'],
                'occurred_at': self.clock(), 'expires_at': min(test['expires_at'], self.clock() + self.settings.approval_seconds),
                'artifact_sha256': release['artifact_sha256'], 'plan_sha256': release['plan_sha256'], 'invalidated': False,
                'consumed_by_job_id': None}
            release['approval'] = approval
            self.data(incident, 'release', release)
            self.event(session, incident, 'human_prd_approved', {'approval_id': approval['id'], 'subject': actor['subject']})
            self.queue_deploy(session, incident, 'prd')
            return {'incident_id': incident_id, 'state': incident.state,
                    'approval': incident.document['data']['release']['approval']}
