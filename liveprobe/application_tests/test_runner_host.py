"""호스트 argv와 컨테이너 격리 회귀. subprocess transport만 대체한다."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry


class HostTests(unittest.TestCase):
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
