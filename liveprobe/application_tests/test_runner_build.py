"""실행 transport만 대체하여 등록 argv·전후시험·추가 회귀 증거를 검사한다."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry, digest


class BuildTests(unittest.TestCase):
    def test_workspace_recheck_uses_exact_digest_and_never_reads_unexpected_files(self):
        import os
        from liveprobe.application_runner.build import Builder
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            expected_file = workspace / "app.py"
            expected_file.write_bytes(b"value = 1\n")
            expected = {"app.py": digest(expected_file.read_bytes())}
            Builder.assert_tree(workspace, expected)

            extra = workspace / "extra.txt"
            extra.touch()
            open_file = os.open

            def approved_open(path, *args, **kwargs):
                self.assertNotEqual(Path(path).name, extra.name)
                return open_file(path, *args, **kwargs)

            with patch.object(os, "open", approved_open):
                with self.assertRaisesRegex(ValueError, "^source_tree_changed$"):
                    Builder.assert_tree(workspace, expected)
            extra.unlink()
            linked_directory = workspace / "linked"
            linked_directory.symlink_to(workspace, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "^source_tree_changed$"):
                Builder.assert_tree(workspace, expected)
            linked_directory.unlink()
            expected_file.write_bytes(b"value = 2\n")
            with self.assertRaisesRegex(ValueError, "^source_tree_changed$"):
                Builder.assert_tree(workspace, expected)
            expected_file.unlink()
            with self.assertRaisesRegex(ValueError, "^source_tree_changed$"):
                Builder.assert_tree(workspace, expected)

    def test_skipped_added_method_cannot_reuse_existing_method_result(self):
        from liveprobe.application_runner.build import Builder
        report={'exit_code':0,'total':1,'failed':0,'executed_tests':[{'id':'test_fix.Old.test_old','file':'tests/test_fix.py','status':'passed'}]}
        expected={'tests/test_fix.py':['Added.test_regression']}
        with self.assertRaisesRegex(ValueError,'regression_id_not_executed'):
            Builder.validate_report(report,['tests/test_fix.py'],phase='after',regression_ids=expected)

    def test_unrelated_failure_cannot_be_regression_evidence(self):
        from liveprobe.application_runner.build import Builder
        report={'exit_code':1,'total':2,'failed':1,'executed_tests':[{'id':'test_old','file':'tests/test_old.py','status':'failed'},{'id':'test_fix','file':'tests/test_fix.py','status':'passed'}]}
        with self.assertRaisesRegex(ValueError,'regression_not_failed'):
            Builder.validate_report(report,['tests/test_fix.py'],phase='before')

    def test_real_pipeline_requires_before_failure_after_pass_and_added_execution(self):
        try:
            from liveprobe.application_runner.build import Builder, test_command, final_manifest
        except ImportError:
            self.fail('격리 시험 빌드 구현이 없습니다')
        p = Registry(ROOT).get('payment')
        class Transport:
            def __init__(self): self.calls=[]; self.skip=False; self.fail_build=False; self.fail_regression=False
            def tests(self, workspace, profile, phase, added):
                self.calls.append((phase, (workspace/'app.py').read_text(), added))
                failed = phase=='before'
                rows = [{'id':'tests/test_fix.py::Added::test_fix','file':'tests/test_fix.py',
                         'status':'failed' if failed else 'passed','phase':phase}]
                if phase=='after' and self.fail_regression:
                    failed = True
                    rows.append({'id':'tests/test_existing.py::test_other','file':'tests/test_existing.py',
                                 'status':'failed','phase':'after'})
                return {'exit_code':int(failed),'total':len(rows),'failed':int(failed),'executed_tests':[] if self.skip else rows}
            def build(self,workspace,profile,tree_sha):
                if self.fail_build:
                    raise ValueError('final_build_failed')
                return {'image':'liveprobe/payment@sha256:'+'b'*64,'source_tree_sha256':tree_sha,'manifest_observed':True}
        source = {'id':'snapshot','base_image':'liveprobe/payment@sha256:'+'a'*64,'provenance_verified':True,'profile_id':p['id'],'files':{'app.py':'value = 1\n'},'file_sha256':{'app.py':digest(b'value = 1\n')}}
        diff = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n--- /dev/null\n+++ b/tests/test_fix.py\n@@ -0,0 +1,4 @@\n+import unittest\n+from app import value\n+class Added(unittest.TestCase):\n+ def test_fix(self): self.assertEqual(value,2)\n'
        patch = {'source_snapshot_id':'snapshot','diff':diff,'diff_sha256':digest(diff.encode()),'tests_added':['tests/test_fix.py::Added::test_fix']}
        with tempfile.TemporaryDirectory() as tmp:
            transport=Transport()
            builder=Builder(Path(tmp),transport)
            out=builder.run(source,patch,p)
            self.assertTrue(out['ok'])
            self.assertEqual([x[1] for x in transport.calls], ['value = 1\n','value = 2\n'])
            self.assertTrue(out['tests']['regression_executed'])
            self.assertTrue(out['artifact']['image'].startswith('liveprobe/payment@sha256:'))
            from types import SimpleNamespace
            from liveprobe.application_runner.executor import Executor
            from liveprobe.application_runner.worker import durable_write

            executor=Executor(Registry(ROOT),SimpleNamespace(),transport,Path(tmp)/'failed',{'runner_role':'source'})
            transport.fail_build=True
            frozen={**source,'id':'a'*64}
            durable_write(executor.state_root/'snapshots'/(frozen['id']+'.json'),frozen)
            job={'kind':'test_build','payload':{'profile_id':p['id'],'source_snapshot':frozen,
                'patch':{**patch,'source_snapshot_id':frozen['id']}}}
            refused=executor.execute(job)
            self.assertEqual(refused,{'ok':False,'write_outcome':'not_written','error':'final_build_failed','tests':out['tests']})
            transport.fail_regression=True
            refused=executor.execute(job)
            self.assertEqual(refused['error'],'after_not_passed')
            self.assertFalse(refused['tests']['after_passed'])
            self.assertEqual(refused['tests']['failed'],1)
            self.assertEqual(refused['tests']['executed_tests'][-1]['id'],'tests/test_existing.py::test_other')
            self.assertFalse((executor.state_root/'artifacts').exists())
            transport.skip=True
            with self.assertRaisesRegex(ValueError,'regression_not_executed'):
                builder.run(source,patch,p)
        self.assertEqual(test_command(Registry(ROOT).get('delivery'), ['test/fix.js']), ['node','--test','--test-reporter=tap','test/fix.js'])
        command=test_command(Registry(ROOT).get('order'),[])
        self.assertIn('test',command)
        self.assertNotIn('shell',command)
        with self.assertRaises(ValueError):
            final_manifest({'Id':'sha256:'+'a'*64,'RepoDigests':[]}, 'liveprobe/payment')
        self.assertEqual(final_manifest({'Id':'sha256:'+'c'*64,'RepoDigests':['liveprobe/payment@sha256:'+'b'*64]},'liveprobe/payment'),'liveprobe/payment@sha256:'+'b'*64)

if __name__=='__main__': unittest.main()
