"""실제 dispatch와 CLI. 외부 transport만 바꾼 독립 시험."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry,digest


class ExecutorTests(unittest.TestCase):
    def test_collect_source_dispatch_and_cli_dry_run(self):
        try:
            from liveprobe.application_runner.executor import Executor
            from liveprobe.application_runner.deploy import summarize
        except ImportError:self.fail('실행 dispatch 구현이 없습니다')
        registry=Registry(ROOT);p=registry.get('payment');image=registry.images['payment']
        doc={'metadata':{'uid':'u','resourceVersion':'1'},'spec':{'template':{'spec':{'containers':[{'name':'payment','image':image}]}}}}
        class Kube:
            def read(self,env,service):
                from copy import deepcopy
                observed=deepcopy(doc)
                observed['spec']['template']['spec']['containers']=[{'name':service,'image':registry.images[service]}]
                return observed
            def logs(self,env,service,since):return [{'service':service,'text':'Authorization: Bearer should-not-escape','since':since}]
        class Host:
            def observe_manifest(self,expected):return expected
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);provenance=root/'proof';provenance.mkdir();sources=root/'source';sources.mkdir()
            (sources/'app.py').write_text('value = 1\n')
            proof={'image':image,'profile_id':p['id'],'base_commit':None,'file_sha256':{'app.py':digest(b'value = 1\n')},'source_root':str(sources),'provenance_kind':'local_build_receipt'}
            proof_path=provenance/('payment-'+image.split(':')[-1]+'.json');proof_path.write_text(json.dumps(proof));proof_path.chmod(0o600)
            executor=Executor(registry,Kube(),Host(),root/'state',{'provenance_dir':str(provenance)})
            result=executor.execute({'kind':'collect','payload':{'environment':'dev','service':'payment','since':'2026-11-01T00:00:00Z','catalog_sha256':registry.catalog_sha256}})
            self.assertTrue(result['ok']);self.assertNotIn('should-not-escape',json.dumps(result))
            target=summarize(doc,'dev','payment',registry)
            out=executor.execute({'kind':'source','payload':{'target':target,'profile_id':p['id']}})
            self.assertTrue(out['source_snapshot']['provenance_verified'])
            # The same deployed manifest and verified file hash bind this read-only K8s question.
            reader=Executor(registry,Kube(),Host(),root/'state',{'provenance_dir':str(provenance),'runner_role':'source'})
            observations=[]
            reader.observe=lambda phase, file=None: observations.append((phase,file))
            question={'image':image,'profile_id':p['id'],'service':'payment','environment':'dev',
                      'frames':[{'file':p['runtime_root']+'/app.py','line':1}]}
            findings=reader.execute({'kind':'investigate','payload':question})['findings']
            self.assertEqual(findings,[{'file':'app.py','line':1,'file_sha256':proof['file_sha256']['app.py'],'excerpt':'value = 1'}])
            self.assertEqual(observations,[('pin_source',None),('read_file','app.py')])
            (sources/'app.py').write_text('value = 2\n')
            with self.assertRaises(ValueError):executor.execute({'kind':'source','payload':{'target':target,'profile_id':p['id']}})
            state=root/'dry-state'
            command=[sys.executable,'-B',str(ROOT/'scripts/liveprobe-application-runner.py'),'--dry-run','--state-dir',str(state)]
            completed=subprocess.run(command,cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertFalse(state.exists())
            self.assertFalse(json.loads(completed.stdout)['claimed'])

if __name__=='__main__':unittest.main()
