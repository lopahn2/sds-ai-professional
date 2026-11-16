"""One canonical root through the real split queue/release pipeline; all transports synthetic."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import BigInteger, Integer, JSON, MetaData, create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.application.merge_policy import GitHubMergeObserver
from app.application.safety import sha
from app.models import (Incident as CoreIncident, CodeActionLink, Evidence, AgentRun, Action,
                        OperatorCommand, WorkflowRun, AuditLog)
from app.change_models import ChangeRequest
from app.unified import UnifiedService, effective_status_expr
from test_core_analysis import SyntheticTransport
from test_core_support import Harness, NEW_IMAGE, PREFIX, target

SOURCE = {'Authorization': 'Bearer synthetic-code-runner'}
CLUSTER = {'Authorization': 'Bearer synthetic-deploy-runner'}
TESTER = {'Authorization': 'Bearer synthetic-human-tester'}
APPROVER = {'Authorization': 'Bearer synthetic-human-approver'}


class UnifiedReleaseFlowTests(unittest.TestCase):
    def test_one_root_positive_dev_test_merge_prd_flow_preserves_native_history(self):
        h = Harness(SyntheticTransport(level=2), repositories_file='synthetic-policy-no-network')
        self.addCleanup(h.close)
        Path(h.settings.machine_identity_file).write_text(json.dumps({'principals': [
            {'subject': 'code-account', 'token': 'synthetic-code-runner', 'scopes': ['runner', 'runner:source']},
            {'subject': 'deploy-account', 'token': 'synthetic-deploy-runner', 'scopes': ['runner', 'runner:cluster']}]}))
        Path(h.settings.identity_file).write_text(json.dumps({'principals': [
            {'subject': 'tester-person', 'token': 'synthetic-human-tester', 'scopes': ['incident:read', 'incident:create', 'tester']},
            {'subject': 'approver-person', 'token': 'synthetic-human-approver', 'scopes': ['incident:read', 'prd-approver']}]}))
        # Separate disposable core DB. Clone only the schema types unsupported by SQLite;
        # ORM models/queries/transactions and UnifiedService remain the production ones.
        core_engine = create_engine('sqlite:///' + str(h.path / 'core-only-tests.db'))
        self.addCleanup(core_engine.dispose)
        metadata = MetaData()
        for model in (CoreIncident, CodeActionLink, Evidence, AgentRun, Action, OperatorCommand,
                      WorkflowRun, AuditLog, ChangeRequest):
            table = model.__table__.to_metadata(metadata)
            for column in table.columns:
                if isinstance(column.type, JSONB):
                    column.type = JSON()
                if column.primary_key and isinstance(column.type, BigInteger):
                    column.type = Integer()
        metadata.create_all(core_engine)
        sessions = sessionmaker(core_engine, expire_on_commit=False)
        @contextmanager
        def core_session():
            with sessions() as session:
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
        coordinator = UnifiedService(h.app, 'synthetic-release-installation', session_factory=core_session)
        refs = [{'file': 'src/main.py', 'line': 2, 'symbol': 'run'}]
        remediation = {'kind': 'code_fix', 'why': 'verified synthetic code exception', 'code_refs': refs, 'repro': None}
        with core_session() as session:
            session.add(CoreIncident(id=1, fingerprint='synthetic/root', namespace='dev', workload='order',
                status='handed_off', severity_level='COMPLEX', title='원본 K8s 장애',
                handoff={'workflow_status': 'verified', 'remediation': {**remediation, 'source': 'llm'}}))
            session.flush()
            session.add(Evidence(id=1, incident_id=1, source='code_refs', payload={'frames': refs}))
            session.add(AgentRun(id=1, incident_id=1, role='group_verifier', provider='synthetic', model='synthetic',
                                 status='succeeded', output={'verdict': 'supported', 'remediation': remediation}))
        with core_session() as session:
            native_handoff = copy.deepcopy(session.get(CoreIncident, 1).handoff)
            native_evidence = copy.deepcopy(session.get(Evidence, 1).payload)
        summary = coordinator.continue_incident(1, {'subject': 'operator', 'kind': 'human'})
        child = summary['application_id']
        root_action = summary['action_id']
        c = h.app.state.control

        def observe(state, status):
            self.assertEqual(c.detail(child)['state'], state)
            coordinator.sync_one(1)
            summary = coordinator.summary(1)
            self.assertEqual(summary['incident_id'], 1)
            self.assertEqual(summary['application_id'], child)
            self.assertEqual(summary['action_id'], root_action)
            with core_session() as session:
                self.assertEqual(session.scalar(select(effective_status_expr()).where(CoreIncident.id == 1)), status)
                self.assertEqual(session.get(CoreIncident, 1).handoff, native_handoff)
                self.assertEqual(session.get(CoreIncident, 1).status, 'handed_off')
                self.assertEqual(session.get(Evidence, 1).payload, native_evidence)
                self.assertEqual(session.scalar(select(func.count()).select_from(CodeActionLink)), 1)

        def claim(auth, kind):
            response = h.client.post(PREFIX + '/runner/claim', headers=auth,
                                    json={'worker_id': 'untrusted-shared-worker-label', 'capabilities': [kind]})
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()['job']
            self.assertIsNotNone(job)
            self.assertEqual(job['incident_id'], child)
            self.assertEqual(job['kind'], kind)
            return job

        def report(auth, job, result):
            response = h.client.post(PREFIX + '/runner/jobs/' + job['id'] + '/result', headers=auth,
                json={'lease_token': job['lease_token'], 'fencing_token': job['fencing_token'], 'result': result})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['incident_id'], child)
            return response.json()

        def collect_stage(stage):
            job = claim(CLUSTER, 'collect')
            self.assertEqual(job['payload']['environment'], stage)
            collection = h.collect()
            collection['target'] = target(stage)
            for row in collection['logs'] + collection['evidence']:
                row['environment'] = stage
            report(CLUSTER, job, collection)

        def deploy(stage):
            job = claim(CLUSTER, 'deploy')
            self.assertEqual(job['payload']['stage'], stage)
            self.assertEqual(job['payload']['artifact']['image'], NEW_IMAGE)
            report(CLUSTER, job, {'ok': True, 'write_outcome': 'written',
                'targets': [dict(t, image=NEW_IMAGE, resource_version='11') for t in job['payload']['targets']], 'error': None})

        def verify(stage):
            job = claim(CLUSTER, 'verify')
            report(CLUSTER, job, {'ok': True, 'write_outcome': 'not_written',
                'targets': [dict(t, image=NEW_IMAGE, resource_version='11') for t in job['payload']['targets']],
                'verification': [{'environment': stage, 'service': 'order', 'image': NEW_IMAGE,
                                  'business': {'ok': True, 'profile_id': 'order', 'body_sha256': 'a' * 64, 'status_code': 200}}],
                'error': None})

        observe('collecting', 'analyzing')
        report(CLUSTER, claim(CLUSTER, 'collect'), h.collect())
        observe('source_pinning', 'analyzing')
        report(SOURCE, claim(SOURCE, 'source'), h.source())
        self.assertTrue(h.app.state.worker.run_once())  # synthetic provider, production coordinator
        observe('test_build_queued', 'analyzing')
        build = claim(SOURCE, 'test_build')
        report(SOURCE, build, {'ok': True, 'tests': {
            'before_failed': True, 'after_passed': True, 'regression_executed': True, 'total': 2, 'failed': 0,
            'executed_tests': [
                {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed', 'phase': 'before'},
                {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'passed', 'phase': 'after'}],
            'report_sha256': '4' * 64}, 'artifact': {'image': NEW_IMAGE, 'source_snapshot_id': 'snapshot-one',
            'diff_sha256': build['payload']['patch']['diff_sha256'], 'source_tree_sha256': '5' * 64,
            'build_receipt_sha256': '6' * 64}, 'error': None})
        observe('dev_targets_collecting', 'analyzing')
        collect_stage('dev')
        deploy('dev')
        verify('dev')
        collect_stage('prd')
        observe('awaiting_user_test', 'action_pending')
        release = c.release_get(child)['release']
        fingerprints = {key: release[key] for key in ('release_id', 'artifact_sha256', 'plan_sha256')}
        fingerprints['image'] = release['artifact']['image']
        premature = h.client.post(PREFIX + '/incidents/' + child + '/approve-prd', headers=APPROVER,
                                 json={**fingerprints, 'user_test_id': 'not-tested'})
        self.assertEqual(premature.status_code, 409)
        test = h.client.post(PREFIX + '/incidents/' + child + '/user-test', headers=TESTER,
                            json={**fingerprints, 'passed': True, 'notes_ko': '합성 dev 시험 결과'})
        self.assertEqual(test.status_code, 200, test.text)
        self.assertEqual(test.json()['user_test']['subject'], 'tester-person')
        observe('awaiting_prd_approval', 'action_pending')
        approval = {**fingerprints, 'user_test_id': test.json()['user_test']['id']}
        self.assertEqual(h.client.post(PREFIX + '/incidents/' + child + '/approve-prd',
                                      headers=TESTER, json=approval).status_code, 403)
        self.assertEqual(h.client.post(PREFIX + '/incidents/' + child + '/approve-prd',
                                      headers=SOURCE, json=approval).status_code, 403)
        self.assertEqual(h.client.post(PREFIX + '/incidents/' + child + '/approve-prd',
                                      headers=APPROVER, json=approval).status_code, 409)
        calls = []
        def merged(observer, **kwargs):
            calls.append(kwargs)
            return {'repository': 'owner/source', 'pr_number': 3, 'url': 'https://github.com/owner/source/pull/3',
                    'merge_commit': 'a' * 40, 'merged_by': 'human-reviewer',
                    'source_tree_sha256': release['artifact']['source_tree_sha256'], 'human_merge_observed': True}
        with patch.object(GitHubMergeObserver, 'observe', merged):
            checked = h.client.post(PREFIX + '/incidents/' + child + '/review-merge', headers=APPROVER,
                                    json={**fingerprints, 'pr_number': 3})
            self.assertEqual(checked.status_code, 200, checked.text)
            approved = h.client.post(PREFIX + '/incidents/' + child + '/approve-prd', headers=APPROVER, json=approval)
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()['approval']['subject'], 'approver-person')
            observe('prd_deploy_queued', 'analyzing')
            deploy('prd')
            verify('prd')
        self.assertGreaterEqual(len(calls), 4)  # review, approve, queue, claim, report rechecks
        observe('completed', 'resolved')
        self.assertFalse(coordinator.summary(1)['blocks_cluster'])
        with c.store.read() as session:
            from app.application.storage import Job
            self.assertEqual({job.worker_id for job in session.scalars(select(Job))}, {
                'subject:' + sha('code-account'),
                'subject:' + sha('deploy-account')})
        self.assertEqual(c.detail(child)['data']['unified_origin']['incident_id'], 1)


if __name__ == '__main__':
    unittest.main()
