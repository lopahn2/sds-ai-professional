"""정규 시험 ID·before/after 상태는 native report에서만 얻는다."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.build import Builder,test_command
from liveprobe.application_runner.profiles import Registry,digest

class ReportTests(unittest.TestCase):
    def test_pytest_node_and_surefire_native_ids_status_and_phase(self):
        from liveprobe.application_runner.reports import junit_rows,tap_rows
        xml='<testsuites><testsuite><testcase classname="tests.test_fix.Check" name="test_fix" file="tests/test_fix.py"><failure/></testcase><testcase classname="tests.test_fix" name="test_skip" file="tests/test_fix.py"><skipped/></testcase></testsuite></testsuites>'
        rows=junit_rows(xml,'python','before')
        self.assertEqual(rows,[{'id':'tests/test_fix.py::Check::test_fix','file':'tests/test_fix.py','status':'failed','phase':'before'},{'id':'tests/test_fix.py::test_skip','file':'tests/test_fix.py','status':'skipped','phase':'before'}])
        rows=junit_rows('<testsuite><testcase classname="io.demo.CheckTest" name="testFix"><error/></testcase></testsuite>','java','after')
        self.assertEqual(rows,[{'id':'io.demo.CheckTest#testFix','file':'src/test/java/io/demo/CheckTest.java','status':'error','phase':'after'}])
        rows=tap_rows('TAP version 13\n# Subtest: works\nnot ok 1 - works\nok 2 - later # SKIP disabled\n# tests 2\n# fail 1\n','test/fix.js','before')
        self.assertEqual(rows,[{'id':'test/fix.js::works','file':'test/fix.js','status':'failed','phase':'before'},{'id':'test/fix.js::later','file':'test/fix.js','status':'skipped','phase':'before'}])
    def test_every_added_id_fails_before_then_passes_after_and_both_are_returned(self):
        p=Registry(ROOT).get('catalog')
        source={'id':'s','provenance_verified':True,'profile_id':p['id'],'files':{'app.py':'value = 1\n'},'file_sha256':{'app.py':digest(b'value = 1\n')}}
        diff='--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n--- /dev/null\n+++ b/tests/test_fix.py\n@@ -0,0 +1,3 @@\n+from app import value\n+def test_a(): assert value == 2\n+def test_b(): assert value != 1\n'
        added=['tests/test_fix.py::test_a','tests/test_fix.py::test_b']
        patch={'source_snapshot_id':'s','diff':diff,'diff_sha256':digest(diff.encode()),'tests_added':added}
        class Transport:
            bad=None
            def tests(self,workspace,profile,phase,paths):
                rows=[{'id':id,'file':'tests/test_fix.py','status':'failed' if phase=='before' else 'passed','phase':phase} for id in added]
                if self.bad:rows[1]['status']=self.bad
                return {'total':2,'failed':sum(r['status']=='failed' for r in rows),'errors':0,'exit_code':1 if phase=='before' else 0,'executed_tests':rows}
            def build(self,workspace,profile,tree):return {'image':'liveprobe/catalog@sha256:'+'b'*64,'manifest_observed':True,'source_tree_sha256':tree}
        with tempfile.TemporaryDirectory() as tmp:
            t=Transport();out=Builder(tmp,t).run(source,patch,p)
            rows=out['tests']['executed_tests'];self.assertEqual(len(rows),4)
            self.assertEqual({r['phase'] for r in rows},{'before','after'})
            self.assertTrue(all(set(r)=={'id','file','status','phase'} for r in rows))
            proof_path=Path(tmp)/'provenance'/('catalog-'+'b'*64+'.json')
            self.assertTrue(proof_path.is_file(),'final tested image needs a durable exact-source receipt')
            from liveprobe.application_runner.source import snapshot
            proof=json.loads(proof_path.read_text())
            self.assertTrue(snapshot(proof['source_root'],p,out['artifact']['image'],proof)['provenance_verified'])
            for bad in ('passed','skipped'):
                t.bad=bad
                with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'regression_not_failed|regression_not_executed'):
                    Builder(tmp,t).run(source,patch,p)
    def test_python_host_reads_pytest_xml_not_unittest_filename_counts(self):
        from liveprobe.application_runner.host import DockerHost,ProcessResult
        calls=[]
        def run(argv,timeout=0):
            calls.append(argv)
            if 'run' in argv and '--rm' in argv:
                report=next(x for x in argv if x.startswith('type=bind,src=') and x.endswith('dst=/reports')).split('src=')[1].split(',dst=')[0]
                Path(report,'pytest.xml').write_text('<testsuite><testcase classname="tests.test_fix" file="tests/test_fix.py" name="test_fix"/></testsuite>')
            return ProcessResult(0,'[]','')
        with tempfile.TemporaryDirectory() as tmp:
            report=DockerHost({'test_images':{'python':'fixture/pytest@sha256:'+'a'*64}},run).tests(Path(tmp),Registry(ROOT).get('catalog'),'after',['tests/test_fix.py'])
            self.assertEqual(report['executed_tests'],[{'id':'tests/test_fix.py::test_fix','file':'tests/test_fix.py','status':'passed','phase':'after'}])
            command=next(c for c in calls if 'run' in c)
            self.assertIn('pytest',command);self.assertIn('--junitxml=/reports/pytest.xml',command)
            self.assertNotIn('/runner/unittest_harness.py',command)

if __name__=='__main__':unittest.main()
