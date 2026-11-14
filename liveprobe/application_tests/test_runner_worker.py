"""claim/heartbeat/result·dry-run·지속 결과 영수증 회귀."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


class WorkerTests(unittest.TestCase):
    def test_dry_run_zero_claims_and_result_contract(self):
        try:
            from liveprobe.application_runner.worker import Worker, redact
        except ImportError:
            self.fail('호스트 worker 구현이 없습니다')
        class API:
            def __init__(self):self.calls=[]
            def post(self,path,body,timeout=20):
                self.calls.append((path,body))
                if path.endswith('/claim'):
                    return {'job':{'id':'job-1','kind':'collect','incident_id':'i','payload':{},'lease_token':'synthetic-lease','fencing_token':4,'idempotency_key':'job-key','lease_expires_at':time.time()+120}}
                return {'ok':True,'lease_expires_at':time.time()+120}
        class Executor:
            def __init__(self):self.calls=[]
            def execute(self,job):self.calls.append(job);return {'ok':True,'evidence':[]}
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state';api=API();executor=Executor()
            worker=Worker(api,executor,state,'worker-1',heartbeat_seconds=0.01)
            self.assertEqual(worker.once(dry_run=True)['claimed'],False)
            self.assertFalse(state.exists());self.assertEqual(api.calls,[]);self.assertEqual(executor.calls,[])
            worker.once()
            paths=[c[0] for c in api.calls]
            self.assertEqual(paths[0],'/runner/claim')
            self.assertIn('/runner/jobs/job-1/heartbeat',paths)
            result=next(body for path,body in api.calls if path.endswith('/result'))
            self.assertEqual(set(result),{'lease_token','fencing_token','result'})
            self.assertEqual(result['fencing_token'],4)
            self.assertTrue(result['result']['ok'])
            worker.once()
            self.assertEqual(len(executor.calls),1)
            self.assertEqual(redact('Authorization: Bearer sensitive-value password=hidden-value'), '[정제] [정제]')

    def test_json_log_credentials_are_redacted_before_evidence_and_signal(self):
        from liveprobe.application_runner.worker import redact
        raw='{"password":"synthetic-do-not-export", "token": "synthetic-do-not-export", "Authorization":"Bearer synthetic-do-not-export"}'
        clean=redact(raw)
        self.assertTrue('synthetic-do-not-export' not in clean,'JSON credential value survived redaction')
        self.assertEqual(clean.count('[정제]'),3)

    def test_result_delivery_failure_never_reexecutes_job(self):
        try:
            from liveprobe.application_runner.worker import Worker
        except ImportError:self.fail('호스트 worker 구현이 없습니다')
        class API:
            fail=True
            def post(self,path,body,timeout=20):
                if path.endswith('claim'):return {'job':{'id':'j','kind':'deploy','payload':{},'lease_token':'lease','fencing_token':1,'idempotency_key':'key','lease_expires_at':time.time()+120}}
                if path.endswith('result') and self.fail:raise OSError('전송 실패')
                return {'ok':True,'lease_expires_at':time.time()+120}
        class Executor:
            count=0
            def execute(self,job):self.count+=1;return {'ok':False,'write_outcome':'unknown'}
        with tempfile.TemporaryDirectory() as tmp:
            api=API();executor=Executor();worker=Worker(api,executor,Path(tmp),'w')
            with self.assertRaises(OSError):worker.once()
            api.fail=False;worker.once()
            self.assertEqual(executor.count,1)

if __name__=='__main__':unittest.main()
