"""독립 검토 반례 회귀. 외부 전송만 대체하고 실제 실행 경로를 검사한다."""
import difflib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SCRATCH = str(Path(tempfile.gettempdir()).resolve())


class HarnessReviewTests(unittest.TestCase):
    def harness(self, body):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp)
            workspace = root / 'source'
            reports = root / 'reports'
            (workspace / 'tests').mkdir(parents=True)
            reports.mkdir()
            (workspace / 'tests/test_added.py').write_text(body)
            wrapper = root / 'wrapper.py'
            wrapper.write_text(
                "import runpy, sys\nfrom pathlib import Path\n"
                "original=Path\nworkspace=original(sys.argv[2]); reports=original(sys.argv[3])\n"
                "def mapped(value):\n"
                " value=str(value)\n"
                " if value=='/workspace': return workspace\n"
                " if value.startswith('/reports/'): return reports/value[len('/reports/'):]\n"
                " return original(value)\n"
                "module=runpy.run_path(sys.argv[1],run_name='review_harness')\n"
                "module['main'].__globals__['Path']=mapped\n"
                "sys.exit(module['main']())\n")
            env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
                   'TMPDIR': SCRATCH, 'APPLICATION_TEST_PHASE': 'after',
                   'REPORT_PATH': str(reports / 'unittest.json')}
            child = subprocess.run([sys.executable, '-B', str(wrapper),
                                    str(ROOT / 'liveprobe/application_runner/unittest_harness.py'),
                                    str(workspace), str(reports)], cwd=workspace, env=env,
                                   capture_output=True, text=True, timeout=15)
            path = reports / 'unittest.json'
            return child, json.loads(path.read_text()) if path.is_file() and path.stat().st_size else None

    def test_runner_replacement_fails_closed_instead_of_forging_execution(self):
        child, report = self.harness('''import unittest
from types import SimpleNamespace
class Forged:
 def __init__(self, **kwargs): self.resultclass=kwargs['resultclass']
 def run(self, suite):
  rows=[{'id':'tests/test_added.py::Added::test_regression','file':'tests/test_added.py','status':'passed','phase':'after'}]
  return SimpleNamespace(testsRun=1,failures=[],errors=[],rows=rows,wasSuccessful=lambda:True)
unittest.TextTestRunner=Forged
class Added(unittest.TestCase):
 def test_regression(self): self.fail('실제 시험은 반드시 실패')
''')
        self.assertNotEqual(child.returncode, 0)
        self.assertTrue(report is None or report.get('integrity') == 'rejected')

    def test_direct_receipt_write_even_if_caught_fails_closed(self):
        child, report = self.harness('''import os
from pathlib import Path
import unittest
try: Path(os.environ['REPORT_PATH']).write_text('{"forged":true}')
except OSError: pass
class Added(unittest.TestCase):
 def test_regression(self): self.assertTrue(True)
''')
        self.assertNotEqual(child.returncode, 0)
        self.assertTrue(report is None or report.get('integrity') == 'rejected')

    def test_subclass_assertion_framework_override_is_not_registered_test_code(self):
        child, report = self.harness('''import unittest
class Added(unittest.TestCase):
 def assertEqual(self, *args, **kwargs): pass
 def test_regression(self): self.assertEqual(1, 2)
''')
        self.assertNotEqual(child.returncode, 0)
        self.assertTrue(report is None or report.get('integrity') == 'rejected')

    def test_real_method_execution_is_not_semantic_adequacy_proof(self):
        child, report = self.harness('''import unittest
class Added(unittest.TestCase):
 def test_regression(self): self.assertEqual(2 + 2, 4)
''')
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(report['executed_tests'][0]['status'], 'passed')
        self.assertEqual(report.get('execution_proof'), 'trusted_harness_observed')
        self.assertEqual(report.get('semantic_adequacy'), 'not_assessed')


class SourceReviewTests(unittest.TestCase):
    def inputs(self):
        from liveprobe.application_runner.profiles import Registry, digest
        profile = Registry(ROOT).get('catalog')
        files = {'app.py': 'value = 1\n', 'Dockerfile': 'FROM scratch\n'}
        source = {'id': 's', 'provenance_verified': True, 'profile_id': profile['id'],
                  'files': files, 'file_sha256': {p: digest(t.encode()) for p, t in files.items()}}
        diff = ''.join(difflib.unified_diff(['value = 1\n'], ['value = 2\n'], fromfile='a/app.py', tofile='b/app.py'))
        diff += ''.join(difflib.unified_diff([], ['def test_fix(): assert True\n'], fromfile='/dev/null', tofile='b/tests/test_fix.py'))
        patch = {'source_snapshot_id': 's', 'diff': diff, 'diff_sha256': digest(diff.encode()),
                 'tests_added': ['tests/test_fix.py::test_fix']}
        return profile, source, patch

    def transport(self, mutation=None, mutate_phase='after'):
        class Transport:
            builds = 0
            workspace = None
            def tests(self, workspace, profile, phase, added):
                self.workspace = workspace
                if mutation and phase == mutate_phase:
                    path = workspace / mutation
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('변조\n')
                return {'exit_code': 1 if phase == 'before' else 0, 'total': 1,
                        'failed': 1 if phase == 'before' else 0,
                        'executed_tests': [{'id': added[0], 'file': 'tests/test_fix.py',
                                            'status': 'failed' if phase == 'before' else 'passed', 'phase': phase}]}
            def build(self, workspace, profile, tree):
                self.builds += 1
                return {'image': profile['image_repository'] + '@sha256:' + 'b' * 64,
                        'manifest_observed': True, 'source_tree_sha256': tree}
        return Transport()

    def test_any_test_mutation_prevents_all_builds_and_cleans_failed_workspace(self):
        from liveprobe.application_runner.build import Builder
        for phase in ('before', 'after'):
            for name in ('Dockerfile', 'app.py', 'tests/test_fix.py', 'extra.py', '.dockerignore'):
                with self.subTest(phase=phase, name=name), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                    transport = self.transport(name, phase)
                    with self.assertRaisesRegex(ValueError, 'source_tree_changed'):
                        Builder(tmp, transport).run(*self.inputs()[1:], self.inputs()[0])
                    self.assertEqual(transport.builds, 0)
                    self.assertFalse(transport.workspace.exists())
                    receipts = list((Path(tmp) / 'cleanup').glob('*.json'))
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(json.loads(receipts[0].read_text())['workspace']['status'], 'removed')

    def test_provenance_source_is_retained_with_reason_and_full_expected_hashes(self):
        from liveprobe.application_runner.build import Builder
        profile, source, patch = self.inputs()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            transport = self.transport()
            Builder(tmp, transport).run(source, patch, profile)
            receipt = json.loads(next((Path(tmp) / 'cleanup').glob('*.json')).read_text())
            self.assertEqual(receipt['workspace']['status'], 'retained')
            self.assertEqual(receipt['workspace']['reason'], 'provenance_source_reference')
            self.assertEqual(Path(receipt['workspace']['path']), transport.workspace)
            self.assertTrue(transport.workspace.exists())
            local = json.loads((transport.workspace / '.application-receipt.json').read_text())
            self.assertEqual(local['source_integrity']['status'], 'fixed_source_plus_exact_patch')
            self.assertIn('Dockerfile', local['source_integrity']['after_file_sha256'])
            self.assertEqual(local['semantic_adequacy'], 'not_assessed')

    def test_source_mount_is_readonly_and_temporary_reports_are_cleaned(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult
        from liveprobe.application_runner.profiles import Registry
        calls = []
        report_paths = []
        def run(argv, timeout=0):
            calls.append(argv)
            if '--rm' in argv:
                mounts = [argv[i+1] for i, x in enumerate(argv[:-1]) if x == '--mount']
                source = next(x for x in mounts if 'dst=/workspace' in x and 'target' not in x)
                self.assertTrue(source.endswith(',readonly'))
                report = next(x for x in mounts if x.endswith('dst=/reports'))
                directory = Path(report.split('src=', 1)[1].split(',dst=')[0])
                report_paths.append(directory)
                (directory / 'pytest.xml').write_text('<testsuite><testcase file="tests/test_fix.py" classname="tests.test_fix" name="test_fix"/></testsuite>')
                return ProcessResult(0, '', '')
            return ProcessResult(1, '[]', '')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp) / 'source'
            root.mkdir()
            report = DockerHost({'test_images': {'python': 'test/image@sha256:' + 'a' * 64}}, run).tests(
                root, Registry(ROOT).get('catalog'), 'after', [])
            self.assertEqual(report['total'], 1)
            self.assertTrue(all(not p.exists() for p in report_paths))
            cleanup = json.loads(next((root.parent / 'cleanup').glob('*.json')).read_text())
            self.assertEqual(cleanup['reports']['status'], 'removed')
            self.assertEqual(cleanup['containers'][0]['status'], 'absent')

    def test_dockerfile_cannot_opt_into_network_secrets_or_privilege(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult
        profile = self.inputs()[0]
        for instruction in ('RUN --network=host true', 'RUN --mount=type=secret,id=x true',
                            'RUN --security=insecure true', 'ADD https://invalid.example/a /a'):
            with self.subTest(instruction=instruction), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                root = Path(tmp)
                (root / 'Dockerfile').write_text('FROM scratch\n' + instruction + '\n')
                calls = []
                host = DockerHost({}, lambda argv, timeout=0: calls.append(argv) or ProcessResult(0, '', ''))
                with self.assertRaisesRegex(ValueError, 'unsafe_build_recipe'):
                    host.build(root, profile, 'a' * 64)
                self.assertEqual(calls, [])


class LeaseReviewTests(unittest.TestCase):
    def execute_case(self, *, mode='valid', delay=0, expire_transport=False, repeat=False):
        from test_runner_fencing import KubeHTTP
        from liveprobe.application_runner.worker import Worker, durable_write
        from liveprobe.application_runner.executor import Executor
        class Clock:
            wall = 1000.0
            mono = 10.0
            def time(self): return self.wall
            def monotonic(self): return self.mono
        clock = Clock()
        class Raw(KubeHTTP):
            timeouts = []
            def call_api(self, path, method, **kwargs):
                if method == 'PATCH':
                    self.timeouts.append(kwargs['_request_timeout'])
                    if expire_transport: clock.mono += 6
                return super().call_api(path, method, **kwargs)
        raw = Raw()
        request = raw.payload()
        request['artifact'].update({key: 'a' * 64 for key in ('source_snapshot_id', 'diff_sha256', 'source_tree_sha256', 'build_receipt_sha256')})
        job = {'id': 'j', 'kind': 'deploy', 'payload': request, 'fencing_token': 1,
               'lease_token': 'synthetic-lease', 'idempotency_key': 'lease-key', 'lease_expires_at': 1005.0}
        class API:
            heartbeats = 0
            results = []
            def post(self, path, body, timeout=20):
                if path.endswith('/claim'): return {'job': job}
                if path.endswith('/heartbeat'):
                    self.heartbeats += 1
                    if self.heartbeats > 1:
                        if mode == 'cancelled': raise OSError('승인 취소')
                        if mode == 'missing': return {'ok': True}
                        if mode == 'expired': return {'lease_expires_at': 999.0}
                        clock.mono += delay
                    return {'lease_expires_at': clock.wall + 5}
                if path.endswith('/result'):
                    self.results.append(body['result'])
                    return {'ok': True}
                raise AssertionError('허용되지 않은 시험 경로')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp)
            executor = Executor(raw.registry, raw.transport(), type('Host', (), {})(), root / 'execution', {})
            durable_write(executor.state_root / 'artifacts' / (executor.artifact_key(request['artifact']) + '.json'), {'ok': True})
            api = API()
            worker = Worker(api, executor, root / 'jobs', 'w', heartbeat_seconds=100,
                            wall_clock=clock.time, monotonic=clock.monotonic)
            try: worker.once()
            except ValueError: pass
            if repeat:
                clock.mono = 10
                try: worker.once()
                except ValueError: pass
            journals = [json.loads(p.read_text()) for p in (root / 'jobs').glob('*.json')]
            return raw, api, journals

    def test_expired_missing_cancelled_or_delayed_sync_confirmation_has_zero_patches(self):
        for mode, delay in (('expired', 0), ('missing', 0), ('cancelled', 0), ('valid', 6)):
            with self.subTest(mode=mode, delay=delay):
                raw, api, journals = self.execute_case(mode=mode, delay=delay)
                self.assertEqual(raw.patches, [])
                self.assertGreaterEqual(api.heartbeats, 2)

    def test_short_valid_window_limits_timeout_and_resume_never_redeploys(self):
        raw, api, journals = self.execute_case(repeat=True)
        self.assertEqual(len(raw.patches), 2)
        self.assertTrue(all(0 < timeout <= 5 for timeout in raw.timeouts))
        self.assertEqual(len(journals), 1)
        self.assertEqual(journals[0]['result']['write_outcome'], 'written')

    def test_expiry_during_response_stops_second_patch_and_keeps_unknown(self):
        raw, api, journals = self.execute_case(expire_transport=True)
        self.assertEqual(len(raw.patches), 1)
        self.assertEqual(journals[0]['result']['write_outcome'], 'unknown')

    def test_blocked_background_heartbeat_cannot_mask_local_deadline(self):
        from liveprobe.application_runner.worker import LeaseGuard
        import threading
        clock = {'wall': 1000.0, 'mono': 10.0}
        entered = threading.Event()
        released = threading.Event()
        class API:
            def post(self, path, body, timeout=20):
                entered.set()
                released.wait(2)
                return {'lease_expires_at': 1010.0}
        guard = LeaseGuard(API(), '/heartbeat', {}, {'lease_expires_at': 1005.0},
                           wall_clock=lambda: clock['wall'], monotonic=lambda: clock['mono'])
        thread = threading.Thread(target=guard.refresh)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            clock['mono'] = 16
            clock['wall'] = 900.0
            with self.assertRaisesRegex(ValueError, 'lease_lost'): guard()
        finally:
            released.set()
            thread.join(3)
        with self.assertRaisesRegex(ValueError, 'lease_lost'): guard()


class MetadataReviewTests(unittest.TestCase):
    def test_acquire_response_loss_is_not_not_written_and_readonly_reconcile_allows_exact_release(self):
        from test_runner_fencing import KubeHTTP, KEYS
        from liveprobe.application_runner.deploy import Deployer
        class Raw(KubeHTTP):
            lose = True
            def call_api(self, path, method, body=None, **kwargs):
                result = super().call_api(path, method, body, **kwargs)
                if method == 'PATCH' and self.lose:
                    self.lose = False
                    raise OSError('획득 응답 유실')
                return result
        raw = Raw()
        payload = raw.payload()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            deployer = Deployer(raw.registry, raw.transport(), tmp)
            self.assertEqual(deployer.deploy(payload, 3)['write_outcome'], 'unknown')
            result = deployer.reconcile(payload, 99)
            self.assertEqual(result['write_outcome'], 'unknown')
            self.assertEqual(result['error'], 'metadata_only_observed')
            self.assertEqual(len(raw.patches), 1)
            self.assertTrue(all(k in raw.doc['metadata']['annotations'] for k in KEYS))
            deployer.release_confirmed(payload)
            self.assertEqual(len(raw.patches), 2)
            self.assertEqual(raw.doc['spec']['template']['spec']['containers'][0]['image'], raw.old)
            self.assertFalse(any(k in raw.doc['metadata']['annotations'] for k in KEYS))
            self.assertFalse(any(op['op'] == 'replace' for op in raw.patches[-1]))
            self.assertEqual(deployer.deploy(payload, 99)['write_outcome'], 'unknown')
            self.assertEqual(len(raw.patches), 2)

    def test_cas_after_acquire_keeps_metadata_fact_and_foreign_or_changed_source_cannot_release(self):
        from test_runner_fencing import KubeHTTP, KEYS
        from liveprobe.application_runner.deploy import Deployer, Conflict
        class Raw(KubeHTTP):
            def call_api(self, path, method, body=None, **kwargs):
                if method == 'PATCH' and len(self.patches) == 1:
                    raise Conflict('cas_conflict')
                return super().call_api(path, method, body, **kwargs)
        for change in ('foreign', 'image', 'template', 'payload'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                raw = Raw()
                payload = raw.payload()
                deployer = Deployer(raw.registry, raw.transport(), tmp)
                result = deployer.deploy(payload, 3)
                self.assertEqual(result['write_outcome'], 'unknown')
                self.assertEqual(deployer.reconcile(payload, 99)['error'], 'metadata_only_observed')
                if change == 'foreign': raw.doc['metadata']['annotations'][KEYS[-1]] = '4'
                elif change == 'image': raw.doc['spec']['template']['spec']['containers'][0]['image'] = raw.new
                elif change == 'template': raw.doc['spec']['template']['spec']['containers'][0]['name'] = '다른대상'
                else: payload = dict(payload, attempt_id='다른시도')
                with self.assertRaises(ValueError): deployer.release_confirmed(payload)
                self.assertEqual(len(raw.patches), 1)
                self.assertTrue(all(k in raw.doc['metadata']['annotations'] for k in KEYS))


class CleanupReviewTests(unittest.TestCase):
    def test_network_creation_response_loss_is_reconciled_and_foreign_labels_are_preserved(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult
        from liveprobe.application_runner.profiles import Registry
        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                calls = []
                resources = {}
                def run(argv, timeout=0):
                    calls.append(argv)
                    if argv[1:3] == ['network', 'create']:
                        resources[argv[-1]] = True
                        raise OSError('network 생성 응답 유실')
                    if argv[1:3] == ['network', 'inspect']:
                        docs = [{'Labels': {'liveprobe.application.owner': '다른소유' if foreign else argv[-1]}}] if resources.get(argv[-1]) else []
                        return ProcessResult(0, json.dumps(docs), '')
                    if argv[1:3] == ['network', 'rm']:
                        resources[argv[-1]] = False
                        return ProcessResult(0, '', '')
                    return ProcessResult(1, '[]', '')
                root = Path(tmp) / 'source'
                root.mkdir()
                config = {'test_images': {'domain': 'test/domain@sha256:' + 'a' * 64},
                          'database_image': 'test/db@sha256:' + 'b' * 64}
                with self.assertRaises(OSError):
                    DockerHost(config, run).tests(root, Registry(ROOT).get('trade-member'), 'before', [])
                cleanup = json.loads(next((root.parent / 'cleanup').glob('*.json')).read_text())
                self.assertEqual(cleanup['network']['status'], 'retained_foreign' if foreign else 'removed')
                self.assertEqual(cleanup['reports']['status'], 'removed')
                self.assertEqual(any(argv[1:3] == ['network', 'rm'] for argv in calls), not foreign)

    def test_container_without_labels_is_preserved_and_report_cleanup_still_runs(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult
        from liveprobe.application_runner.profiles import Registry
        calls = []
        def run(argv, timeout=0):
            calls.append(argv)
            if argv[1:3] == ['container', 'inspect']:
                return ProcessResult(0, json.dumps([{'Config': {'Labels': None}}]), '')
            return ProcessResult(0, '잘못된 시험 출력', '')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp) / 'source'
            (root / 'test').mkdir(parents=True)
            (root / 'test/a.js').write_text('')
            with self.assertRaises(ValueError):
                DockerHost({'test_images': {'node': 'test/node@sha256:' + 'a' * 64}}, run).tests(
                    root, Registry(ROOT).get('delivery'), 'after', [])
            cleanup = json.loads(next((root.parent / 'cleanup').glob('*.json')).read_text())
            self.assertEqual(cleanup['containers'][0]['status'], 'retained_foreign')
            self.assertEqual(cleanup['reports']['status'], 'removed')
            self.assertFalse(any(argv[1] == 'rm' for argv in calls))

    def test_provenance_receipt_response_loss_never_deletes_referenced_source(self):
        from unittest.mock import patch
        from liveprobe.application_runner import worker
        from liveprobe.application_runner.build import Builder
        profile, source, request = SourceReviewTests().inputs()
        transport = SourceReviewTests().transport()
        original = worker.durable_write
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            def lost(path, value):
                original(path, value)
                if Path(path).parent.name == 'provenance': raise OSError('영수증 동기화 응답 유실')
            with patch.object(worker, 'durable_write', lost):
                with self.assertRaises(OSError): Builder(tmp, transport).run(source, request, profile)
            proof = json.loads(next((Path(tmp) / 'provenance').glob('*.json')).read_text())
            self.assertTrue(Path(proof['source_root']).exists())
            receipt = json.loads(next((Path(tmp) / 'cleanup').glob('*.json')).read_text())
            self.assertEqual(receipt['workspace']['status'], 'retained')

    def test_fixture_source_retention_receipt_names_provenance_reference(self):
        from liveprobe.application_runner.fixture import FixtureBuilder
        from liveprobe.application_runner.profiles import Registry
        class Host:
            def build(self, workspace, profile, tree):
                return {'image': profile['image_repository'] + '@sha256:' + 'e' * 64,
                        'manifest_observed': True, 'source_tree_sha256': tree}
            def observe_manifest(self, image): return image
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = FixtureBuilder(Registry(ROOT), Host(), tmp).run('catalog-title-key', 'dev')
            proof = json.loads(Path(result['proof_file']).read_text())
            cleanup = json.loads(next((Path(tmp) / 'workspaces/cleanup').glob('*.json')).read_text())
            self.assertEqual(cleanup['workspace']['status'], 'retained')
            self.assertEqual(cleanup['workspace']['reason'], 'provenance_source_reference')
            self.assertEqual(cleanup['workspace']['path'], proof['source_root'])


if __name__ == '__main__':
    unittest.main()
