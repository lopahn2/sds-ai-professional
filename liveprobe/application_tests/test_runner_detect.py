"""detect는 로그 transport만 대체, 상태·서명·API 요청은 실제 경로."""
import json
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from liveprobe.application_runner.profiles import Registry,digest

class DetectTests(unittest.TestCase):
    def test_watch_leaves_paused_environment_checkpoint_and_logs_alone(self):
        from liveprobe.application_runner.detect import Detector

        registry = Registry(ROOT)
        kube = Mock()
        kube.logs.return_value = []
        api = Mock()
        api.active_environments.return_value = {'dev'}
        first_since = '2026-11-23T00:00:00Z'
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            checkpoints = state / 'watch'
            checkpoints.mkdir()
            prd_path = checkpoints / 'prd-catalog.json'
            saved = '{"since":"2026-11-22T15:00:00Z","reported":1}\n'
            prd_path.write_text(saved)
            detector = Detector(registry, kube, api, state / 'detect')
            detector.watch_once(checkpoints, ['catalog'], first_since)
            kube.logs.assert_called_once_with('dev', 'catalog', first_since)
            api.post.assert_not_called()
            self.assertEqual(prd_path.read_text(), saved)
            dev_path = checkpoints / 'dev-catalog.json'
            dev_checkpoint = dev_path.read_bytes()

            api.active_environments.side_effect = OSError('unavailable')
            with self.assertRaises(OSError):
                detector.watch_once(checkpoints, ['catalog'], first_since)
            kube.logs.assert_called_once()
            self.assertEqual(dev_path.read_bytes(), dev_checkpoint)
            self.assertEqual(prd_path.read_text(), saved)

            api.active_environments.side_effect = None
            api.active_environments.return_value = {'dev', 'prd'}
            kube.logs.reset_mock()
            detector.watch_once(checkpoints, ['catalog'], first_since)
            self.assertIn(unittest.mock.call('prd', 'catalog', '2026-11-22T15:00:00Z'), kube.logs.call_args_list)

    def test_public_catalog_environment_lookup_uses_no_machine_credentials(self):
        from liveprobe.application_runner.worker import API

        api = API('http://127.0.0.1:4175', Path('/missing-test-credentials'))
        api.opener = Mock()
        for document, expected in (({'environments': ['dev'], 'historical_environments': ['prd']}, {'dev'}),
                                   ({'environments': []}, set())):
            api.opener.open.return_value = BytesIO(json.dumps(document).encode())
            self.assertEqual(api.active_environments(), expected)
            request = api.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:4175/api/application/v1/catalog')
            self.assertEqual(request.get_method(), 'GET')
            self.assertFalse(request.has_header('Authorization'))
        api.opener.open.return_value = BytesIO(b'{"environments":["dev","stg"]}')
        with self.assertRaisesRegex(OSError, 'invalid_application_catalog_environments'):
            api.active_environments()

    def test_first_sanitized_signal_and_durable_signature_dedup(self):
        from liveprobe.application_runner.detect import Detector
        class Logs:
            calls=[]
            def logs(self,env,service,since):
                self.calls.append((env,service,since))
                return [{'observed_at':'2026-11-02T01:00:00Z','text':'2026-11-02T01:00:00Z ERROR\n2026-11-02T01:00:00Z Traceback (most recent call last):\n2026-11-02T01:00:00Z   File "/app/app.py", line 8, in handle\n2026-11-02T01:00:00Z KeyError: product_title token=never-export'}]
        class API:
            calls=[]
            def post(self,path,body):self.calls.append((path,body));return {'id':'incident-1','state':'collecting'}
        registry=Registry(ROOT);logs=Logs();api=API()
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'detect-state'
            detector=Detector(registry,logs,api,state)
            out=detector.run('dev',['catalog'],'2026-11-02T00:00:00Z')
            self.assertEqual(out['reported'],1);self.assertEqual(len(api.calls),1)
            path,body=api.calls[0];self.assertEqual(path,'/incidents')
            self.assertEqual(set(body),{'environment','service','reason','signal'})
            signal=body['signal'];self.assertEqual(set(signal),{'source','observed_at','sample','sha256'})
            self.assertEqual(signal['source'],'pod_log');self.assertEqual(signal['observed_at'],'2026-11-02T01:00:00Z')
            self.assertEqual(signal['sha256'],digest(signal['sample'].encode()))
            self.assertNotIn('never-export',json.dumps(body))
            self.assertTrue(state.exists())
            out=Detector(registry,logs,api,state).run('dev',['catalog'],'2026-11-02T00:01:00Z')
            self.assertEqual(out['reported'],0);self.assertEqual(out['duplicates'],1);self.assertEqual(len(api.calls),1)
            out=detector.run('prd',['catalog'],'2026-11-02T00:00:00Z')
            self.assertEqual(out['reported'],1);self.assertEqual(len(api.calls),2)
    def test_signature_ignores_following_access_records_and_timestamp_changes(self):
        from liveprobe.application_runner.detect import exception_sample,signature
        first='2026-11-02T01:00:00Z ERROR\n2026-11-02T01:00:00Z Traceback (most recent call last):\n2026-11-02T01:00:00Z   File "/app/app.py", line 8, in handle\n2026-11-02T01:00:00Z KeyError: display_name\n2026-11-02T01:00:00Z INFO access duration=0.1'
        second=first.replace('01:00:00Z','01:01:00Z').replace('duration=0.1','duration=0.7')
        self.assertNotIn('INFO access',exception_sample(first))
        self.assertEqual(signature(exception_sample(first)),signature(exception_sample(second)))

    def test_detector_dry_run_zero_io_and_no_plain_error_signature(self):
        from liveprobe.application_runner.detect import Detector,exception_sample
        self.assertIsNone(exception_sample('ERROR http status=500\nINFO healthy'))
        self.assertIsNotNone(exception_sample('java.lang.IllegalStateException: broken\n\tat demo.Main.run(Main.java:4)'))
        class Forbidden:
            def __getattr__(self,name):raise AssertionError('dry run touched transport')
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'absent'
            out=Detector(Registry(ROOT),Forbidden(),Forbidden(),state).run('dev',['catalog'],'2026-11-02T00:00:00Z',dry_run=True)
            self.assertEqual(out['writes'],0);self.assertFalse(state.exists())
    def test_cli_detect_and_fixture_dry_run_need_no_runtime_or_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            for command in (['detect','--environment','dev','--service','catalog'],['build-fixture','--fixture','catalog-title-key','--environment','dev']):
                state=Path(tmp)/'absent'
                proc=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/liveprobe-application-runner.py'),*command,'--dry-run','--state-dir',str(state)],capture_output=True,text=True,cwd=ROOT)
                self.assertEqual(proc.returncode,0,proc.stderr)
                self.assertEqual(json.loads(proc.stdout)['writes'],0);self.assertFalse(state.exists())

if __name__=='__main__':unittest.main()
