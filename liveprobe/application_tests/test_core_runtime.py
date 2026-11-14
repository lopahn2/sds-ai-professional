import copy
import json
from pathlib import Path
import unittest
from test_core_support import Harness, HUMAN, MACHINE, PREFIX, NEW_IMAGE
from test_core_analysis import SyntheticTransport
from test_core_release import CoreReleaseTests


class CoreRuntimeTests(unittest.TestCase):
    def test_ready_names_actual_missing_checks_and_runner_observation(self):
        h = Harness(SyntheticTransport())
        self.addCleanup(h.close)
        before = h.client.get(PREFIX + '/readyz').json()
        self.assertFalse(before['ready'])
        self.assertFalse(before['checks']['runner'])
        h.claim(['collect'])
        after = h.client.get(PREFIX + '/readyz').json()
        self.assertTrue(after['checks']['runner'])
        self.assertTrue(after['checks']['human_identity'])
        self.assertTrue(after['checks']['machine_identity'])
        self.assertFalse(after['checks']['model'])
        self.assertEqual(after['mode'], 'isolated_test')
        Path(h.settings.identity_file).unlink()
        self.assertFalse(h.client.get(PREFIX + '/readyz').json()['checks']['human_identity'])

    def test_runner_readiness_expires_after_two_leases_and_survives_restart(self):
        from app.application.main import create_app
        from fastapi.testclient import TestClient
        h = Harness()
        self.addCleanup(h.close)
        h.claim(['collect'])
        other = create_app(h.settings, clock=lambda: h.now)
        self.addCleanup(other.state.store.engine.dispose)
        with TestClient(other) as client:
            self.assertTrue(client.get(PREFIX + '/readyz').json()['checks']['runner'])
            h.now += h.settings.lease_seconds * 2 + 1
            ready = client.get(PREFIX + '/readyz')
            self.assertEqual(ready.status_code, 503)
            self.assertFalse(ready.json()['checks']['runner'])

    def test_openapi_documents_human_inputs_and_all_route_shapes(self):
        h = Harness()
        self.addCleanup(h.close)
        schema = h.client.get('/openapi.json').json()
        for endpoint in ('/incidents', '/incidents/{incident_id}/user-test', '/incidents/{incident_id}/approve-prd'):
            self.assertTrue('requestBody' in schema['paths'][PREFIX + endpoint]['post'])
        for endpoint in ('/readyz', '/catalog', '/incidents/{incident_id}/events', '/incidents/{incident_id}/release'):
            self.assertNotEqual(schema['paths'][PREFIX + endpoint]['get']['responses']['200']['content']['application/json']['schema'], {})

    def test_sensitive_source_path_is_rejected_even_if_hash_matches(self):
        h = Harness(SyntheticTransport())
        self.addCleanup(h.close)
        incident = h.create()
        h.report(h.claim(['collect']), h.collect())
        source = h.source()
        source['source_snapshot']['files']['.env'] = 'ordinary-looking-content'
        from test_core_support import digest
        source['source_snapshot']['file_sha256']['.env'] = digest('ordinary-looking-content')
        h.report(h.claim(['source']), source)
        self.assertEqual(h.detail(incident)['state'], 'handoff')
        self.assertTrue(h.claim(['test_build']) is None)
        self.assertNotIn('ordinary-looking-content', json.dumps(h.detail(incident)))

    def test_sensitive_and_traversing_source_paths_never_reach_worker(self):
        from test_core_support import digest
        for path in ('.env.local', 'config/x.env', 'google_oauth.json', 'auth.py',
                     'private/key.toml', 'secret/a.sql', 'tests/conftest.py', 'dump.sql',
                     'x.pem', '../x.py', '/x.py', 'x\\y.py', 'src/.git/config'):
            with self.subTest(path=path):
                h = Harness(SyntheticTransport())
                self.addCleanup(h.close)
                incident = h.create()
                h.report(h.claim(['collect']), h.collect())
                source = h.source()
                source['source_snapshot']['files'][path] = 'sensitive-path-marker'
                source['source_snapshot']['file_sha256'][path] = digest('sensitive-path-marker')
                self.assertEqual(h.report(h.claim(['source']), source).status_code, 200)
                self.assertEqual(h.detail(incident)['state'], 'handoff')
                self.assertNotIn('sensitive-path-marker', json.dumps(h.detail(incident)))
                self.assertFalse(h.app.state.worker.run_once())

    def test_collect_timestamps_and_log_environment_follow_runner_contract(self):
        h = Harness()
        self.addCleanup(h.close)
        incident = h.create()
        job = h.claim(['collect'])
        self.assertTrue(job['payload']['since'].endswith('Z'))
        collection = h.collect()
        collection['logs'][0]['environment'] = 'dev'
        collection['logs'][0]['text'] = 'x' * 2100
        collection['evidence'][0]['text'] = 'x' * 2100
        self.assertEqual(h.report(job, collection).status_code, 200)
        saved = h.detail(incident)['data']['collection']
        self.assertEqual(saved['logs'][0]['environment'], 'dev')
        self.assertEqual(len(saved['logs'][0]['text']), 2000)
        self.assertEqual(len(saved['evidence'][0]['text']), 2000)
        self.assertEqual(saved['evidence'][0]['quality'], 'truncated')

    def test_collect_never_mixes_another_environment_or_unregistered_service(self):
        for field, value in (('environment', 'prd'), ('service', 'not-registered')):
            h = Harness()
            self.addCleanup(h.close)
            incident = h.create()
            collection = h.collect()
            collection['logs'][0]['environment'] = 'dev'
            collection['logs'][0][field] = value
            self.assertEqual(h.report(h.claim(['collect']), collection).status_code, 200)
            self.assertEqual(h.detail(incident)['state'], 'handoff')
            self.assertTrue(h.claim(['source']) is None)

    def test_enabled_lifespan_worker_retries_transient_loop_exception_then_stops(self):
        import threading
        import time
        from fastapi.testclient import TestClient
        provider = SyntheticTransport()
        h = Harness(provider, worker_enabled=True)
        self.addCleanup(h.close)
        class FlakyClock:
            failed = False
            def __call__(self):
                if threading.current_thread().name == 'application-analysis' and not self.failed:
                    self.failed = True
                    raise RuntimeError('synthetic-transient-loop-fault')
                return h.now
        clock = FlakyClock()
        h.app.state.control.clock = clock
        with TestClient(h.app):
            incident = h.seed_source()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and h.detail(incident)['state'] != 'test_build_queued':
                time.sleep(.025)
            self.assertEqual(h.detail(incident)['state'], 'test_build_queued')
            self.assertTrue(clock.failed)
            thread = h.app.state.worker_thread
            self.assertTrue(thread.is_alive())
            self.assertGreaterEqual(h.app.state.worker_failures, 1)
            self.assertEqual(len(provider.calls), 5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(h.app.state.worker_stop.is_set())

    def test_disabled_worker_never_starts_thread(self):
        from fastapi.testclient import TestClient
        h = Harness(SyntheticTransport())
        self.addCleanup(h.close)
        with TestClient(h.app):
            incident = h.seed_source()
            self.assertEqual(h.detail(incident)['state'], 'analysis_queued')
            self.assertTrue(getattr(h.app.state, 'worker_thread', None) is None)
            self.assertEqual(len(h.app.state.worker.provider.calls), 0)

    def test_static_root_mount_is_optional_and_api_routes_win(self):
        import importlib
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app.application.settings import ApplicationSettings
        main = importlib.import_module('app.application.main')
        h = Harness()
        self.addCleanup(h.close)
        root = h.path / 'synthetic-ui'
        root.mkdir()
        (root / 'index.html').write_text('<html><body>synthetic-mount-marker</body></html>')
        # Do not create/change the UI owner's static directory.
        with patch.object(main, 'STATIC_DIR', root, create=True):
            app = main.create_app(ApplicationSettings())
        with TestClient(app) as client:
            self.assertEqual(client.get('/').status_code, 200)
            self.assertIn('synthetic-mount-marker', client.get('/').text)
            self.assertEqual(client.get(PREFIX + '/healthz').json()['product'], 'liveprobe-application')
        with patch.object(main, 'STATIC_DIR', root / 'absent', create=True):
            app = main.create_app(ApplicationSettings())
        with TestClient(app) as client:
            self.assertEqual(client.get('/').status_code, 404)
            self.assertEqual(client.get(PREFIX + '/healthz').status_code, 200)

    def test_app_provider_uses_application_preamble_without_transport_call(self):
        from app.application.settings import ApplicationSettings
        from app.application.main import create_app
        from app.application.worker import APP_PREAMBLE
        settings = ApplicationSettings(worker_enabled=True, codex_home='/synthetic/not-a-real-credential-directory')
        app = create_app(settings)
        provider = app.state.worker._provider()
        self.assertEqual(provider.system_preamble, APP_PREAMBLE)
        self.assertIn('source-code', provider.system_preamble)
        command = provider._cmd('synthetic-model', 'low', '/synthetic/schema', '/synthetic/work')
        self.assertIn('shell_tool', command)
        self.assertIn('--ignore-user-config', command)


class CoreNativeReceiptTests(CoreReleaseTests):
    def test_native_executed_test_rows_are_validated_not_crash(self):
        incident, job, result = self.built()
        result['tests']['executed_tests'] = [
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'error', 'phase': 'before'},
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'passed', 'phase': 'after'}]
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'dev_targets_collecting')

    def test_malformed_test_receipt_objects_never_crash_or_enqueue_deploy(self):
        for key, value in (('tests', []), ('tests', 'not-an-object'), ('artifact', []), ('artifact', 'not-an-object')):
            with self.subTest(key=key, shape=type(value).__name__):
                self.h = Harness(SyntheticTransport())
                self.addCleanup(self.h.close)
                incident, job, result = self.built()
                result[key] = value
                self.assertEqual(self.h.report(job, result).status_code, 200)
                self.assertEqual(self.h.detail(incident)['state'], 'handoff')
                self.assertTrue(self.h.claim(['collect', 'deploy']) is None)

    def test_identical_mode_rows_normalize_without_losing_test_proof(self):
        incident, job, result = self.built()
        result['tests']['executed_tests'] *= 3  # shared domain's three registered modes
        result['tests']['total'] = 6
        self.assertEqual(self.h.report(job, result).status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'dev_targets_collecting')
        release = self.release(incident)
        self.assertEqual(len(release['tests']['executed_tests']), 2)

    def test_test_receipt_legacy_missing_phase_skipped_and_wrong_method_fail_closed(self):
        good = [
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'failed', 'phase': 'before'},
            {'id': 'tests/test_main.py::test_run', 'file': 'tests/test_main.py', 'status': 'passed', 'phase': 'after'}]
        cases = [['tests/test_main.py'], ['tests/test_main.py::test_run'], [{}], [42],
                 [dict(good[1], phase='before'), good[1]],
                 [good[0], dict(good[1], status='skipped')],
                 [good[0], dict(good[1], id='tests/test_main.py::test_other')],
                 [good[0], dict(good[1], file='tests/test_other.py')],
                 [{k: v for k, v in good[0].items() if k != 'phase'}, good[1]],
                 [good[0], good[1], dict(good[1], status='error')]]
        for index, rows in enumerate(cases):
            with self.subTest(case=index):
                self.h = Harness(SyntheticTransport())
                self.addCleanup(self.h.close)
                incident, job, result = self.built()
                result['tests']['executed_tests'] = rows
                self.assertEqual(self.h.report(job, result).status_code, 200)
                self.assertEqual(self.h.detail(incident)['state'], 'handoff')
                self.assertTrue(self.h.claim(['collect', 'deploy']) is None)

    def test_partial_written_failure_preserves_observation_and_handoffs_without_promotion(self):
        incident, build, result = self.built()
        self.h.report(build, result)
        self.collect_stage('dev')
        deploy = self.h.claim(['deploy'])
        targets = [dict(target, image=NEW_IMAGE, resource_version='11', write_outcome='written')
                   for target in deploy['payload']['targets']]
        self.assertEqual(self.h.report(deploy, {'ok': False, 'write_outcome': 'written', 'targets': targets,
                                              'error': {'code': 'partial_write'}}).status_code, 200)
        detail = self.h.detail(incident)
        self.assertEqual(detail['state'], 'handoff')
        saved = detail['data']['release']['stage_results']['dev']
        self.assertEqual(saved['write_outcome'], 'written')
        self.assertEqual(saved['targets'][0]['image'], NEW_IMAGE)
        self.assertFalse(saved['verified'])
        self.assertTrue(self.h.claim(['collect', 'deploy', 'verify']) is None)

    def test_unknown_write_reconciles_original_payload_then_separate_verify_without_deploy_replay(self):
        incident, build, result = self.built()
        self.h.report(build, result)
        self.collect_stage('dev')
        deploy = self.h.claim(['deploy'])
        self.assertEqual(self.h.report(deploy, {'ok': False, 'write_outcome': 'unknown'}).status_code, 200)
        self.assertTrue(self.h.claim(['deploy']) is None)
        reconcile = self.h.claim(['reconcile'])
        self.assertEqual(reconcile['payload']['original_payload'], deploy['payload'])
        self.assertEqual(reconcile['payload']['idempotency_key'], deploy['idempotency_key'])
        observed = [dict(target, image=NEW_IMAGE, resource_version='11', write_outcome='written')
                    for target in deploy['payload']['targets']]
        receipt = self.h.report(reconcile, {'ok': True, 'write_outcome': 'written', 'targets': observed})
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'dev_verifying')
        self.assertTrue(self.h.claim(['deploy']) is None)
        self.verified('dev')
        self.assertEqual(self.h.detail(incident)['state'], 'prd_targets_collecting')

    def test_verification_requires_every_target_exact_image_profile_and_business_receipt(self):
        for mutation in ('empty', 'profile', 'hash', 'image', 'environment', 'duplicate', 'status', 'business', 'object'):
            with self.subTest(case=mutation):
                self.h = Harness(SyntheticTransport())
                self.addCleanup(self.h.close)
                incident, job, result = self.built()
                self.h.report(job, result)
                self.collect_stage('dev')
                self.deployed('dev')
                verify = self.h.claim(['verify'])
                rows = [{'environment': 'dev', 'service': 'order', 'image': NEW_IMAGE,
                         'business': {'ok': True, 'profile_id': 'order', 'body_sha256': 'a' * 64, 'status_code': 200}}]
                if mutation == 'empty': rows = []
                elif mutation == 'object': rows = {'ok': True}
                elif mutation == 'duplicate': rows.append(copy.deepcopy(rows[0]))
                elif mutation == 'profile': rows[0]['business']['profile_id'] = 'other'
                elif mutation == 'hash': rows[0]['business']['body_sha256'] = ''
                elif mutation == 'image': rows[0]['image'] = 'other@sha256:' + 'e' * 64
                elif mutation == 'environment': rows[0]['environment'] = 'stg'
                elif mutation == 'status': rows[0]['business']['status_code'] = 500
                else: rows[0]['business']['ok'] = False
                self.assertEqual(self.h.report(verify, {'ok': True, 'write_outcome': 'not_written', 'verification': rows}).status_code, 200)
                self.assertEqual(self.h.detail(incident)['state'], 'failed')
                self.assertTrue(self.h.claim(['collect', 'deploy']) is None)

    def test_release_explains_exact_approval_artifact_targets_and_cas_rollback(self):
        incident = self.awaiting()
        release = self.release(incident)
        self.assertEqual(release['artifact']['image'], NEW_IMAGE)
        self.assertEqual(release['artifact']['source_tree_sha256'], '5' * 64)
        self.assertEqual(release['artifact']['diff_sha256'], self.h.detail(incident)['data']['patch']['diff_sha256'])
        self.assertEqual(set(release['targets']), {'dev', 'prd'})
        self.assertTrue(release['summary_ko'] and release['risk_ko'])
        self.assertIn('CAS', release['rollback_ko'])
        self.assertIn('이전', release['rollback_ko'])
        self.assertIn('digest', release['rollback_ko'])

    def test_runner_native_verification_receipt_is_supported(self):
        incident, job, result = self.built()
        self.h.report(job, result)
        self.collect_stage('dev')
        job = self.h.claim(['deploy'])
        targets = [{'environment': 'dev', 'service': 'order', 'uid': 'synthetic-dev', 'resource_version': '11', 'image': NEW_IMAGE, 'write_outcome': 'written'}]
        result = {'ok': True, 'write_outcome': 'written', 'targets': targets,
                  'verification': [{'environment': 'dev', 'service': 'order', 'image': NEW_IMAGE,
                    'business': {'ok': True, 'body_sha256': 'a' * 64, 'profile_id': 'order', 'status_code': 200}}], 'error': None}
        self.assertEqual(self.h.report(job, result).status_code, 200)
        verify = self.h.claim(['verify'])
        self.assertIsNotNone(verify)
        self.assertEqual(self.h.report(verify, {'ok': True, 'write_outcome': 'not_written', 'verification': result['verification'], 'error': None}).status_code, 200)
        self.assertEqual(self.h.detail(incident)['state'], 'prd_targets_collecting')
