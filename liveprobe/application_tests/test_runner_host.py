"""호스트 argv와 컨테이너 격리 회귀. subprocess transport만 대체한다."""
from pathlib import Path
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry


class HostTests(unittest.TestCase):
    def test_reservation_jar_is_readable_by_the_image_user_with_a_private_umask(self):
        from liveprobe.app.application.catalog import DEV_RESERVATION_PROFILE
        from liveprobe.application_runner.host import DockerHost, ProcessResult

        def process(argv, cwd=None, timeout=0):
            if 'mvn' in argv:
                mount = next(arg for arg in argv if arg.endswith(',dst=/workspace/target'))
                output = Path(mount.split(',')[1].removeprefix('src='))
                (output / 'reservation-1.0.0.jar').write_bytes(b'x' * 100_001)
                report = output / 'surefire-reports' / 'TEST-example.RegressionTest.xml'
                report.parent.mkdir()
                report.write_text('<testsuite><testcase classname="example.RegressionTest" name="fault"/></testsuite>')
            return ProcessResult(0, '[]', '')

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'Dockerfile').write_text('COPY target/reservation-1.0.0.jar /app.jar\n')
            host = DockerHost({
                'test_images': {'java': 'fixture/java@sha256:' + 'a' * 64},
                'reservation_maven_cache': tmp,
            }, process)

            def build_image(context, profile, tree_sha, release):
                archive = context / 'target' / 'reservation-1.0.0.jar'
                self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o644)
                self.assertEqual(archive.stat().st_size, 100_001)

            old_umask = os.umask(0o077)
            try:
                with patch.object(host, '_build_image', side_effect=build_image) as build:
                    host.build(workspace, DEV_RESERVATION_PROFILE, 'a' * 64)
                    build.assert_called_once()
            finally:
                os.umask(old_umask)

    def test_node_before_runs_only_declared_files_and_after_runs_the_entire_suite(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult

        invocations = []

        def process(argv, cwd=None, timeout=0):
            if argv[:3] == ['docker', 'run', '--rm']:
                patterns = tuple(arg for arg in argv if arg.startswith('--test-name-pattern='))
                invocations.append((argv[-1], patterns))
                if 'APPLICATION_TEST_PHASE=before' in argv:
                    if argv[-1] == 'test/first.js' and patterns:
                        return ProcessResult(0, 'TAP version 13\nok 1 - actual test\n# tests 1\n# fail 0\n', '')
                    names = ['actual test', 'another test (v1.2)?'] if argv[-1] == 'test/first.js' else ['actual test']
                    tap = '\n'.join(f'not ok {i} - {name}' for i, name in enumerate(names, 1))
                    return ProcessResult(1, f'TAP version 13\n{tap}\n# tests {len(names)}\n# fail {len(names)}\n', '')
                return ProcessResult(0, 'TAP version 13\nok 1 - actual test\n# tests 1\n# fail 0\n', '')
            return ProcessResult(0, '[]', '')

        config = {'test_images': {'node': 'fixture/node@sha256:'+'a'*64}}
        profile = Registry(ROOT).get('delivery')
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            (workspace / 'test').mkdir(parents=True)
            for name in ('baseline.js', 'first.js', 'second.js'):
                (workspace / 'test' / name).write_text("require('node:test')('actual test',()=>{});\n")
            host = DockerHost(config, process)
            ids = ['test/second.js::actual test', 'test/first.js::actual test', 'test/first.js::another test (v1.2)?']
            before = host.tests(workspace, profile, 'before', ids)
            self.assertEqual(invocations, [
                ('test/first.js', ('--test-name-pattern=^actual test$', r'--test-name-pattern=^another test \(v1\.2\)\?$')),
                ('test/first.js', ()),
                ('test/second.js', ('--test-name-pattern=^actual test$',)),
            ])
            self.assertEqual(before['total'], 3)
            self.assertEqual(before['failed'], 3)
            invocations.clear()
            after = host.tests(workspace, profile, 'after', ids)
            self.assertEqual(invocations, [('test/baseline.js', ()), ('test/first.js', ()), ('test/second.js', ())])
            self.assertEqual(after['total'], 3)
            invocations.clear()
            with self.assertRaisesRegex(ValueError, '^added_regression_required$'):
                host.tests(workspace, profile, 'before', ['test/missing.js::actual test'])
            self.assertEqual(invocations, [])

    def test_java_compile_failure_is_not_reported_as_a_missing_test_receipt(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult

        def process(argv, cwd=None, timeout=0):
            if argv[1] == 'run':
                return ProcessResult(1, '[ERROR] Failed to execute goal '
                                     'org.apache.maven.plugins:maven-compiler-plugin:3.14.1:testCompile '
                                     '(default-testCompile) on project order: Compilation failure', '')
            return ProcessResult(0, '[]', '')

        config = {'test_images': {'java': 'fixture/java@sha256:'+'a'*64}}
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            source = workspace / 'src/test/java/example/RegressionTest.java'
            source.parent.mkdir(parents=True)
            source.touch()
            host = DockerHost(config, process)
            with self.assertRaisesRegex(ValueError, '^java_test_compile_failed$'):
                host.tests(workspace, Registry(ROOT).get('order'), 'before', ['example.RegressionTest#reproducesFault'])

    def test_java_before_selects_exact_classes_and_after_runs_the_entire_suite(self):
        from liveprobe.application_runner.host import DockerHost, ProcessResult

        commands = []

        def process(argv, cwd=None, timeout=0):
            if argv[:3] == ['docker', 'run', '--rm']:
                commands.append(argv[argv.index('mvn'):])
                return ProcessResult(1, '', '')
            return ProcessResult(0, '[]', '')

        config = {'test_images': {'java': 'fixture/java@sha256:'+'a'*64}}
        profile = Registry(ROOT).get('order')
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            for package in ('first', 'second', 'unrelated'):
                source = workspace / 'src/test/java' / package / 'RegressionTest.java'
                source.parent.mkdir(parents=True)
                source.touch()
            host = DockerHost(config, process)
            ids = ['second.RegressionTest#fault', 'first.RegressionTest#fault', 'first.RegressionTest#anotherFault']
            host.tests(workspace, profile, 'before', ids)
            host.tests(workspace, profile, 'after', ids)
            self.assertEqual(commands, [
                ['mvn', '-o', '-B', '-Dstyle.color=never',
                 '-Dtest=first/RegressionTest.java,second/RegressionTest.java', 'test'],
                ['mvn', '-o', '-B', '-Dstyle.color=never', 'test'],
            ])
            with self.assertRaisesRegex(ValueError, '^added_regression_required$'):
                host.tests(workspace, profile, 'before', ['missing.RegressionTest#fault'])
            with self.assertRaisesRegex(ValueError, '^test_id_required$'):
                host.tests(workspace, profile, 'before', ['first.*#fault'])
            self.assertEqual(len(commands), 2)

    def test_manifest_observation_rejects_config_only_inspection(self):
        from liveprobe.application_runner.host import DockerHost,ProcessResult
        image='liveprobe/payment@sha256:'+'a'*64
        def process(argv,cwd=None,timeout=0):
            return ProcessResult(0,json.dumps([{'Id':'sha256:'+'a'*64,'RepoDigests':[]}]),'')
        host=DockerHost({},process)
        self.assertTrue(hasattr(host,'observe_manifest'),'소스 manifest 관측 함수가 없습니다')
        with self.assertRaisesRegex(ValueError,'manifest_digest_not_observed'):
            host.observe_manifest(image)

    def test_node_argv_no_socket_and_domain_own_network(self):
        try:
            from liveprobe.application_runner.host import DockerHost, ProcessResult
        except ImportError:
            self.fail('실제 호스트 transport 구현이 없습니다')
        calls=[]
        def process(argv,cwd=None,timeout=0):
            calls.append(argv)
            if argv[:3]==['docker','network','inspect']:
                owner=argv[-1]
                return ProcessResult(0,json.dumps([{'Labels':{'liveprobe.application.owner':owner}}]),'')
            if argv[:3]==['docker','run','--rm']:
                return ProcessResult(0,'TAP version 13\n# Subtest: 회귀\nok 1 - 회귀\n# tests 1\n# pass 1\n# fail 0\n','')
            return ProcessResult(0,'[]','')
        config={'test_images':{'node':'fixture/node@sha256:'+'a'*64,'domain':'fixture/domain@sha256:'+'b'*64},'database_image':'fixture/postgres@sha256:'+'c'*64}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'test').mkdir();(root/'test/fix.js').write_text("require('node:test')('회귀',()=>{});\n")
            host=DockerHost(config,process)
            report=host.tests(root,Registry(ROOT).get('delivery'),'after',['test/fix.js'])
            self.assertEqual(report['total'],1)
            self.assertEqual(report['executed_tests'][0]['file'],'test/fix.js')
            command=next(c for c in calls if c[:3]==['docker','run','--rm'])
            self.assertIn('--network=none',command)
            self.assertIn('--cap-drop=ALL',command)
            self.assertNotIn('/var/run/docker.sock',' '.join(command))
            self.assertEqual(command[-4:],['node','--test','--test-reporter=tap','test/fix.js'])
            (root/'tests').mkdir();(root/'tests/test_fix.py').write_text('import unittest\n')
            # DB fixture나 실제 실행 보고서가 없으면 성공을 합성하지 않는다.
            with self.assertRaises(ValueError):
                host.tests(root,Registry(ROOT).get('trade-member'),'after',['tests/test_fix.py'])
            self.assertTrue(any(c[:3]==['docker','network','create'] for c in calls))
            cleanup=[c for c in calls if c[:3]==['docker','network','rm']]
            self.assertTrue(cleanup)
            self.assertTrue(all(c[-1].startswith('application-') for c in cleanup))

if __name__=='__main__':unittest.main()
