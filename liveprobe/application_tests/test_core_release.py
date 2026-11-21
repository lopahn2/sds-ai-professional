"""실제 API/worker/SQLite에서 승격과 사람 승인 경계를 검증한다."""
import copy
import unittest
from test_core_analysis import SyntheticTransport
from test_core_support import Harness, HUMAN, MACHINE, PREFIX, NEW_IMAGE, target


class CoreReleaseTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(SyntheticTransport())
        self.addCleanup(self.h.close)

    def built(self):
        incident = self.h.seed_source()
        self.h.app.state.worker.run_once()
        job = self.h.claim(['test_build'])
        patch = job['payload']['patch']
        result = {'ok': True, 'tests': {'before_failed': True, 'after_passed': True,
            'regression_executed': True, 'total': 2, 'failed': 0, 'executed_tests': [
                {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed', 'phase': 'before'},
                {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'passed', 'phase': 'after'}],
            'report_sha256': '4' * 64}, 'artifact': {'image': NEW_IMAGE,
            'source_snapshot_id': 'snapshot-one', 'diff_sha256': patch['diff_sha256'],
            'source_tree_sha256': '5' * 64, 'build_receipt_sha256': '6' * 64}, 'error': None}
        return incident, job, result

    def collect_stage(self, stage):
        job = self.h.claim(['collect'])
        self.assertIsNotNone(job)
        self.assertEqual(job['payload']['environment'], stage)
        result = self.h.collect()
        result['target'] = target(stage)
        result['evidence'][0]['environment'] = stage
        self.assertEqual(self.h.report(job, result).status_code, 200)
        return job

    def deployed(self, stage, ok=True, unknown=False):
        job = self.h.claim(['deploy'])
        self.assertIsNotNone(job)
        self.assertEqual(job['payload']['stage'], stage)
        self.assertEqual(job['payload']['artifact']['image'], NEW_IMAGE)
        targets = [dict(t, image=NEW_IMAGE, resource_version='11') for t in job['payload']['targets']]
        result = {'ok': ok, 'write_outcome': 'unknown' if unknown else 'written' if ok else 'not_written',
                  'targets': targets if ok else [], 'verification': None, 'error': None if ok else {'code': 'write_failed'}}
        self.assertEqual(self.h.report(job, result).status_code, 200)
        return job

    def verified(self, stage):
        job = self.h.claim(['verify'])
        self.assertIsNotNone(job)
        self.assertEqual(job['payload']['stage'], stage)
        result = {'ok': True, 'write_outcome': 'not_written',
                  'targets': [dict(t, image=NEW_IMAGE, resource_version='11') for t in job['payload']['targets']],
                  'verification': [{'environment': stage, 'service': 'order', 'image': NEW_IMAGE,
                                    'business': {'ok': True, 'profile_id': 'order', 'body_sha256': 'a' * 64, 'status_code': 200}}], 'error': None}
        self.assertEqual(self.h.report(job, result).status_code, 200)

    def awaiting(self):
        incident, job, result = self.built()
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.collect_stage('dev')
        self.deployed('dev')
        self.verified('dev')
        self.collect_stage('prd')
        self.assertEqual(self.h.detail(incident)['state'], 'awaiting_user_test')
        self.assertTrue(self.h.claim(['deploy']) is None)
        return incident

    def release(self, incident):
        response = self.h.client.get(PREFIX + f'/incidents/{incident}/release', headers=HUMAN)
        self.assertEqual(response.status_code, 200)
        return response.json()['release']

    def attest(self, incident):
        release = self.release(incident)
        body = {k: release[k] for k in ('release_id', 'artifact_sha256', 'plan_sha256')}
        body.update(image=release['artifact']['image'], passed=True, notes_ko='합성 dev 사용자 기능 시험 통과')
        response = self.h.client.post(PREFIX + f'/incidents/{incident}/user-test', headers=HUMAN, json=body)
        self.assertEqual(response.status_code, 200)
        return response.json()['user_test'], body

    def approval_body(self, incident):
        test, body = self.attest(incident)
        body.pop('passed')
        body.pop('notes_ko')
        body['user_test_id'] = test['id']
        return body

    def test_build_dev_human_test_then_exact_manifest_prd(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        response = self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'prd_deploy_queued')
        self.deployed('prd')
        self.verified('prd')
        self.assertEqual(self.h.detail(incident)['state'], 'recovery_review')
        url = PREFIX + f'/incidents/{incident}/accountability'
        review = self.h.client.get(url, headers=HUMAN).json()
        self.assertEqual(review['status'], 'pending_operator')
        decision = {'revision': review['revision'], 'note': '원래 요청의 관측 공백을 확인했습니다.'}
        self.assertEqual(self.h.client.post(url + '/confirm', headers=HUMAN, json=decision).status_code, 200)
        self.assertEqual(self.h.client.post(url + '/confirm', headers=HUMAN, json=decision).status_code, 409)
        self.assertEqual(self.h.detail(incident)['state'], 'completed')
        self.assertTrue(self.h.claim(['deploy']) is None)

    def test_failed_dev_never_enqueues_promotion(self):
        incident, job, result = self.built()
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.collect_stage('dev')
        self.deployed('dev', False)
        self.assertEqual(self.h.detail(incident)['state'], 'failed')
        self.assertTrue(self.h.claim(['collect', 'deploy', 'verify']) is None)

    def test_test_build_requires_real_regression_receipt(self):
        for field, value in [('before_failed', False), ('executed_tests', []), ('failed', 1)]:
            incident, job, result = self.built()
            result['tests'][field] = value
            self.assertEqual(self.h.report(job, result).status_code, 200)
            self.assertEqual(self.h.detail(incident)['state'], 'handoff')
            self.assertTrue(self.h.claim(['deploy', 'collect']) is None)

    def test_expired_deploy_is_reconcile_only_not_deploy_again(self):
        incident, job, result = self.built()
        self.h.report(job, result)
        self.collect_stage('dev')
        deploy = self.h.claim(['deploy'])
        self.h.now += 31
        self.assertTrue(self.h.claim(['deploy']) is None)
        reconcile = self.h.claim(['reconcile'])
        self.assertEqual(reconcile['payload']['original_job_id'], deploy['id'])
        self.assertEqual(self.h.detail(incident)['state'], 'reconciliation_required')
        self.assertEqual(self.h.report(deploy, {'ok': True}).status_code, 409)
        self.assertTrue(self.h.claim(['deploy']) is None)

    def test_wrong_image_expired_approval_and_target_change_block_prd(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        wrong = dict(body, image='other@sha256:' + 'a' * 64)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=wrong).status_code, 409)
        self.assertTrue(self.h.claim(['deploy']) is None)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 200)
        self.h.now += self.h.settings.approval_seconds + 1
        self.assertTrue(self.h.claim(['deploy']) is None)
        self.assertEqual(self.h.detail(incident)['state'], 'handoff')

    def test_rebuild_invalidates_user_test_and_approval(self):
        incident = self.awaiting()
        body = self.approval_body(incident)
        self.assertEqual(self.h.client.post(PREFIX + f'/incidents/{incident}/approve-prd', headers=HUMAN, json=body).status_code, 200)
        self.h.app.state.control.invalidate_release(incident, 'artifact_rebuilt')
        self.assertTrue(self.h.claim(['deploy']) is None)
        release = self.release(incident)
        self.assertFalse(release['valid'])
        self.assertTrue(release['user_test']['invalidated'])
        self.assertTrue(release['approval']['invalidated'])


if __name__ == '__main__':
    unittest.main()
