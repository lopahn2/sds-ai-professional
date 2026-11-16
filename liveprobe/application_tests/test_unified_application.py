"""통합 경계: 합성 SQLite·identity·transport만 사용한다. 외부 모델/클러스터 호출 없음."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException
from sqlalchemy import func, select
from app.application.contracts import Approval, CreateIncident
from app.application.safety import sha
from app.application.storage import Event, Incident, Job
from test_core_support import Harness, HUMAN, MACHINE, PREFIX

SOURCE = {'Authorization': 'Bearer synthetic-source-machine'}
CLUSTER = {'Authorization': 'Bearer synthetic-cluster-machine'}
CLUSTER_OTHER = {'Authorization': 'Bearer synthetic-other-machine'}


class UnifiedApplicationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.control = self.h.app.state.control
        path = Path(self.h.settings.machine_identity_file)
        principals = json.loads(path.read_text())['principals']
        for subject, token, role in (
            ('code-worker', 'synthetic-source-machine', 'source'),
            ('deploy-worker', 'synthetic-cluster-machine', 'cluster'),
            ('other-worker', 'synthetic-other-machine', 'cluster')):
            principals.append({'subject': subject, 'token': token, 'scopes': ['runner', 'runner:' + role]})
        path.write_text(json.dumps({'principals': principals}))
        self.request = CreateIncident(environment='dev', service='order', reason='합성 코드 오류')
        self.origin = {'installation_id': 'synthetic-installation', 'incident_id': 42,
                       'action_id': 'synthetic-code-action', 'request_sha256': 'a' * 64}

    def linked(self, origin=None, request=None):
        return self.control.create_linked(request or self.request, {'subject': 'bridge', 'kind': 'machine'},
                                          origin=origin or self.origin)

    def claim(self, auth, kinds, worker='self-reported-label'):
        return self.h.client.post(PREFIX + '/runner/claim', headers=auth,
                                  json={'worker_id': worker, 'capabilities': kinds})

    def report(self, auth, job, result):
        return self.h.client.post(PREFIX + '/runner/jobs/' + job['id'] + '/result', headers=auth,
            json={'lease_token': job['lease_token'], 'fencing_token': job['fencing_token'], 'result': result})

    def heartbeat(self, auth, job):
        return self.h.client.post(PREFIX + '/runner/jobs/' + job['id'] + '/heartbeat', headers=auth,
            json={'lease_token': job['lease_token'], 'fencing_token': job['fencing_token']})

    def test_linked_creation_replay_and_read_preserve_one_job_and_origin(self):
        first = self.linked()
        expected = sha({key: self.origin[key] for key in ('installation_id', 'incident_id', 'action_id')})
        self.assertEqual(first['id'], expected)
        with self.h.app.state.store.read() as session:
            counts = tuple(session.scalar(select(func.count()).select_from(t)) for t in (Incident, Job, Event))
        self.assertEqual(self.linked(), first)
        self.assertEqual(self.control.detail(first['id']), first)
        with self.h.app.state.store.read() as session:
            self.assertEqual(tuple(session.scalar(select(func.count()).select_from(t)) for t in (Incident, Job, Event)), counts)
        self.assertEqual(first['data']['unified_origin'], self.origin)

    def test_concurrent_same_origin_creates_one_execution(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.linked()['id'], range(8)))
        self.assertEqual(len(set(values)), 1)
        with self.h.app.state.store.read() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Job)), 1)

    def test_conflicting_origin_hash_and_payload_cannot_rebind(self):
        original = self.linked()
        for origin, request in ((dict(self.origin, request_sha256='b' * 64), self.request),
                                (self.origin, CreateIncident(environment='dev', service='order', reason='changed'))):
            with self.assertRaises(HTTPException) as failure:
                self.linked(origin, request)
            self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(self.control.detail(original['id']), original)

    def test_unrelated_native_incident_and_different_roots_never_merge(self):
        native = self.control.create(self.request, {'subject': 'person', 'kind': 'human'})
        linked = self.linked()
        other = self.linked(dict(self.origin, incident_id=43))
        self.assertEqual(len({native['id'], linked['id'], other['id']}), 3)
        self.assertNotIn('unified_origin', self.control.detail(native['id'])['data'])

    def test_invalid_origin_or_child_id_is_rejected(self):
        for origin in (dict(self.origin, incident_id=True), dict(self.origin, installation_id=''),
                       dict(self.origin, extra='unsafe')):
            with self.assertRaises(HTTPException):
                self.linked(origin)
        with self.assertRaises(HTTPException) as failure:
            self.control.create_linked(self.request, {'subject': 'bridge'}, origin=self.origin, application_id='wrong')
        self.assertEqual(failure.exception.status_code, 409)

    def test_legacy_runner_cannot_claim_linked_job(self):
        self.linked()
        response = self.claim(MACHINE, ['collect', 'source', 'test_build', 'deploy'])
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['job'])

    def test_server_role_scopes_override_spoofed_capabilities(self):
        linked = self.linked()
        source = self.claim(SOURCE, ['collect', 'deploy', 'source', 'test_build'])
        self.assertEqual(source.status_code, 200)
        self.assertIsNone(source.json()['job'])
        job = self.claim(CLUSTER, ['collect', 'source', 'test_build']).json()['job']
        self.assertEqual(job['kind'], 'collect')
        self.assertEqual(self.report(CLUSTER, job, self.h.collect()).status_code, 200)
        self.assertIsNone(self.claim(CLUSTER, ['source']).json()['job'])
        source_job = self.claim(SOURCE, ['source', 'deploy']).json()['job']
        self.assertEqual(source_job['kind'], 'source')
        self.assertEqual(source_job['incident_id'], linked['id'])

    def test_lease_token_and_worker_label_cannot_impersonate_principal(self):
        self.linked()
        job = self.claim(CLUSTER, ['collect'], worker='identical-label').json()['job']
        before = self.control.detail(job['incident_id'])
        self.assertEqual(self.heartbeat(CLUSTER_OTHER, job).status_code, 403)
        self.assertEqual(self.report(CLUSTER_OTHER, job, self.h.collect()).status_code, 403)
        self.assertEqual(self.report(SOURCE, job, self.h.collect()).status_code, 403)
        self.assertEqual(self.control.detail(job['incident_id']), before)
        self.assertEqual(self.heartbeat(CLUSTER, job).status_code, 200)
        self.assertEqual(self.report(CLUSTER, job, self.h.collect()).status_code, 200)
        # A duplicate accepted receipt still requires the original principal.
        self.assertEqual(self.report(CLUSTER_OTHER, job, self.h.collect()).status_code, 403)

    def test_strict_mode_missing_combined_or_duplicate_roles_rejects(self):
        from dataclasses import replace
        from app.application.security import Identities
        settings = replace(self.h.settings, unified_mode=True)
        identities = Identities(settings)
        with self.assertRaises(HTTPException):
            identities.runner(MACHINE['Authorization'])
        path = Path(settings.machine_identity_file)
        values = json.loads(path.read_text())
        values['principals'][1]['scopes'].append('runner:cluster')
        path.write_text(json.dumps(values))
        with self.assertRaises(HTTPException):
            identities.runner(SOURCE['Authorization'])
        values['principals'][1]['scopes'].remove('runner:cluster')
        values['principals'].append(dict(values['principals'][1], token='synthetic-duplicate-token'))
        path.write_text(json.dumps(values))
        with self.assertRaises(HTTPException):
            identities.runner(SOURCE['Authorization'])

    def test_linked_prd_rejects_self_reported_merge_and_scope_is_preserved(self):
        linked = self.linked()
        with self.h.app.state.store.transaction() as session:
            incident = session.get(Incident, linked['id'])
            incident.state = 'awaiting_prd_approval'
            document = copy.deepcopy(incident.document)
            document['data']['merge_review'] = {'merged': True, 'human': True, 'source_tree_sha256': 'f' * 64}
            incident.document = document
        body = {'user_test_id': 'test', 'release_id': 'release', 'artifact_sha256': 'a' * 64,
                'plan_sha256': 'b' * 64, 'image': 'local/order@sha256:' + 'c' * 64}
        response = self.h.client.post(PREFIX + '/incidents/' + linked['id'] + '/approve-prd', headers=HUMAN, json=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'pull_request_merge_unobserved')
        self.assertEqual(self.h.client.post(PREFIX + '/incidents/' + linked['id'] + '/approve-prd',
                                           headers=CLUSTER, json=body).status_code, 403)
        self.assertEqual(self.control.detail(linked['id'])['state'], 'awaiting_prd_approval')

    def test_terminal_linked_incident_cannot_claim_stale_collect_or_deploy(self):
        linked = self.linked()
        with self.h.app.state.store.transaction() as session:
            incident = session.get(Incident, linked['id'])
            self.control.transition(session, incident, 'handoff')
            self.control.enqueue(session, incident, 'deploy', {'stage': 'dev'})
        response = self.claim(CLUSTER, ['collect', 'deploy'])
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['job'])
        with self.h.app.state.store.read() as session:
            jobs = list(session.scalars(select(Job).where(Job.incident_id == linked['id'])))
            self.assertTrue(all(job.status == 'cancelled' for job in jobs))
        self.assertEqual(self.control.detail(linked['id'])['state'], 'handoff')

    def test_late_read_result_cannot_revive_terminal_linked_incident(self):
        linked = self.linked()
        job = self.claim(CLUSTER, ['collect']).json()['job']
        with self.h.app.state.store.transaction() as session:
            self.control.transition(session, session.get(Incident, linked['id']), 'handoff')
        response = self.report(CLUSTER, job, self.h.collect())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'job_state_conflict')
        self.assertEqual(self.control.detail(linked['id'])['state'], 'handoff')

    def test_late_write_phase_change_requires_reconciliation_not_false_completion(self):
        from app.application.safety import text_sha
        linked = self.linked()
        with self.h.app.state.store.transaction() as session:
            incident = session.get(Incident, linked['id'])
            self.control.transition(session, incident, 'handoff')
            job = self.control.enqueue(session, incident, 'deploy', {'stage': 'dev'})
            job.status, job.lease_hash, job.lease_until, job.fence = 'leased', text_sha('synthetic-lease-only'), self.h.now + 30, 1
            job.worker_id = 'subject:' + sha('deploy-worker')
            job_id = job.id
        response = self.heartbeat(CLUSTER, {'id': job_id, 'lease_token': 'synthetic-lease-only', 'fencing_token': 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.control.detail(linked['id'])['state'], 'reconciliation_required')
        with self.h.app.state.store.read() as session:
            self.assertEqual(session.get(Job, job_id).status, 'reconciliation_required')
            self.assertEqual(session.scalar(select(func.count()).select_from(Job).where(Job.kind == 'reconcile')), 1)

    def test_cluster_poll_does_not_mask_offline_source_runner(self):
        linked = self.linked()
        status = self.control.runner_status_for_incident(linked['id'])
        self.assertEqual(status['role'], 'cluster')
        self.assertFalse(status['observed'])
        job = self.claim(CLUSTER, ['collect']).json()['job']
        self.assertEqual(self.report(CLUSTER, job, self.h.collect()).status_code, 200)
        source_status = self.control.runner_status_for_incident(linked['id'])
        self.assertEqual(source_status['role'], 'source')
        self.assertFalse(source_status['observed'])
        self.assertIsNone(source_status['last_claim_at'])
        self.assertTrue(self.control.runner_recent('cluster'))
        self.assertFalse(self.control.runner_recent('source'))
        self.assertFalse(self.control.runner_recent())  # legacy singleton not overwritten
        source_job = self.claim(SOURCE, ['source']).json()['job']
        self.assertTrue(self.control.runner_status_for_incident(linked['id'])['observed'])
        self.h.now += 61
        self.assertFalse(self.control.runner_status_for_incident(linked['id'])['observed'])
        self.assertIsNotNone(source_job)

    def test_long_job_heartbeat_is_connection_evidence_without_new_claim(self):
        linked = self.linked()
        collected = self.claim(CLUSTER, ['collect']).json()['job']
        self.assertEqual(self.report(CLUSTER, collected, self.h.collect()).status_code, 200)
        job = self.claim(SOURCE, ['source']).json()['job']
        for _ in range(4):
            self.h.now += 20
            self.assertEqual(self.heartbeat(SOURCE, job).status_code, 200)
        self.assertTrue(self.control.runner_recent('source'))
        self.assertTrue(self.control.runner_status_for_incident(linked['id'])['observed'])
        self.h.now += 31
        self.assertFalse(self.control.runner_recent('source'))
        self.assertFalse(self.control.runner_status_for_incident(linked['id'])['observed'])

    def test_strict_runner_readiness_requires_both_roles(self):
        from dataclasses import replace
        self.control.settings = replace(self.control.settings, unified_mode=True)
        self.assertFalse(self.control.runner_recent())
        self.claim(CLUSTER, ['collect'])
        self.assertFalse(self.control.runner_recent())
        self.claim(SOURCE, ['source'])
        self.assertTrue(self.control.runner_recent())

    def test_machine_cannot_request_dev_reconciliation_with_source_role(self):
        linked = self.linked()
        response = self.h.client.post(PREFIX + '/incidents/' + linked['id'] + '/review-dev-metadata', headers=SOURCE)
        self.assertEqual(response.status_code, 403)


class RunnerSeparationTests(unittest.TestCase):
    def test_source_worker_rejects_deploy_before_execution_or_disk_write(self):
        from liveprobe.application_runner.worker import Worker
        class API:
            def post(self, path, body):
                self.body = body
                return {'job': {'id': 'job', 'kind': 'deploy', 'fencing_token': 1}}
        class Executor:
            def execute(self, job):
                raise AssertionError('must not execute')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            api = API()
            worker = Worker(api, Executor(), state, 'worker', role='source')
            with self.assertRaises(ValueError):
                worker.once()
            self.assertEqual(api.body['capabilities'], ['source', 'test_build'])
            self.assertFalse(state.exists())

    def test_source_transport_cannot_patch_even_when_sdk_has_authority(self):
        from liveprobe.application_runner.deploy import KubeTransport
        class API:
            def call_api(self, *args, **kwargs):
                raise AssertionError('write must stop before transport')
        with self.assertRaisesRegex(ValueError, 'source_runner_cluster_write_forbidden'):
            KubeTransport(API(), read_only=True).patch('dev', 'order', [])

    def test_executor_role_is_enforced_without_relying_on_claim(self):
        from liveprobe.application_runner.executor import Executor
        class Target:
            pass
        for role, kind in (('source', 'deploy'), ('cluster', 'test_build')):
            executor = Executor(None, Target(), Target(), '/unused', {'runner_role': role})
            with self.assertRaisesRegex(ValueError, 'runner_role_job_forbidden'):
                executor.execute({'kind': kind, 'payload': {}})


if __name__ == '__main__':
    unittest.main()
