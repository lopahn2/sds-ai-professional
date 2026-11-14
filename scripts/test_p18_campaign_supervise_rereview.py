"""감독자 재검토 S1/N1/N2/N3 회귀. 가짜 실행기와 실제 로컬 OS 파이프만 사용한다."""
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import test_p18_campaign_supervise as original

SOURCE = original.SOURCE

HELPER = r'''
import importlib.util,json,os,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
cfg=json.loads(sys.argv[1]);root=Path(cfg['root'])
if cfg.get('pre_handler_pause'):
    cfg['runner']="import pathlib,sys,time;pathlib.Path(sys.argv[1]+'.booting').write_text('1');time.sleep(2)\n"+cfg['runner']
spec=importlib.util.spec_from_file_location('supervisor_under_test',cfg['source'])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.check_plan=lambda gates:{'시험대역':True}
m.runner_command=lambda *a,**k:[sys.executable,'-u','-B','-c',cfg['runner'],str(root/'child'),'10','7']
m.sleep_guard=lambda pid:None
if cfg.get('load_stop'):
    def load(path,name):
        if name=='campaign_ledger_snapshot':os.kill(os.getpid(),signal.SIGTERM)
        return SimpleNamespace(snapshot=lambda:{},api_status=lambda timeout:{})
else:
    def load(path,name):
        def snapshot():
            if cfg.get('large_event'):
                os.set_blocking(1,False)
                try:
                    while True:os.write(1,b'x'*4096)
                except BlockingIOError:
                    pass
                finally:
                    os.set_blocking(1,True)
                (root/'snapshot.started').write_text('1')
            return {}
        return SimpleNamespace(snapshot=snapshot,api_status=lambda timeout:{})
m._load=load
if cfg.get('heartbeat_delay'):
    def api(path):
        (root/'heartbeat.started').write_text('1');time.sleep(3)
        return {'accounts':[]}
    m._api=api;m.HEARTBEAT_SECONDS=0
else:
    m._api=lambda path:{'accounts':[]}
argv=[cfg.get('mode','execute'),'--job',str(root/'job'),'--gates',str(root/'gates')]
raise SystemExit(m._finish(m.main(argv)))
'''


class Rereview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, *, closed_fd=False, **cfg):
        cfg.update(root=str(self.root), source=str(SOURCE), runner=original.FAKE_RUNNER)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        command = [sys.executable, '-u', '-B', '-c', HELPER, json.dumps(cfg)]
        return subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True,
                                preexec_fn=(lambda: os.close(1)) if closed_fd else None)

    def read_receipt(self):
        return json.loads((self.root / 'job/child-return.json').read_text())

    def cleanup(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        launch = self.root / 'job/launch.json'
        if launch.exists():
            pid = json.loads(launch.read_text())['runner_pid']
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        if proc.stdout: proc.stdout.close()
        if proc.stderr: proc.stderr.close()

    def test_stop_accepted_in_observer_load_never_creates_child(self):
        proc = self.start(load_stop=True)
        try:
            _, error = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 143, error.decode())
            self.assertFalse((self.root / 'job/launch.json').exists())
            self.assertTrue(self.read_receipt()['stopped_before_launch'])
        finally:
            self.cleanup(proc)

    def test_missing_stdout_does_not_change_check_plan_exit(self):
        proc = self.start(closed_fd=True, mode='check-plan')
        try:
            _, error = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, error.decode())
        finally:
            self.cleanup(proc)

    def test_missing_stdout_keeps_execute_receipt(self):
        # 시작된 가짜 실행기를 중단해 자연 종료10초를 기다리지 않는다.
        proc = self.start(closed_fd=True)
        try:
            original.wait_file(self.root / 'child.ready')
            os.kill(proc.pid, signal.SIGTERM)
            _, error = proc.communicate(timeout=6)
            self.assertEqual(proc.returncode, 85, error.decode())
            self.assertEqual(self.read_receipt()['actual_exit'], 85)
        finally:
            self.cleanup(proc)

    def delivery_probe(self, *, slow_heartbeat=False, unread_stdout=False):
        proc = self.start(heartbeat_delay=slow_heartbeat, large_event=unread_stdout)
        try:
            original.wait_file(self.root / 'child.ready')
            if slow_heartbeat: original.wait_file(self.root / 'heartbeat.started')
            if unread_stdout:
                original.wait_file(self.root / 'snapshot.started')
                time.sleep(0.1)
            started = time.monotonic()
            os.kill(proc.pid, signal.SIGTERM)
            # stdout 읽기 끝을 열어 둔 채 전혀 소비하지 않는다.
            try:
                original.wait_file(self.root / 'child.signals.json', timeout=1.5)
                delivered = True
            except TimeoutError:
                delivered = False
            self.assertTrue(delivered, '출력/조회 때문에 1.5초 이내 중단이 전달되지 않음')
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(json.loads((self.root / 'child.signals.json').read_text()), [signal.SIGTERM])
            _, error = proc.communicate(timeout=6)
            self.assertEqual(proc.returncode, 85, error.decode())
            self.assertEqual(self.read_receipt()['actual_exit'], 85)
        finally:
            self.cleanup(proc)

    def test_unread_live_stdout_cannot_block_signal_delivery(self):
        self.delivery_probe(unread_stdout=True)

    def test_stop_before_child_handler_keeps_actual_negative_exit(self):
        proc = self.start(pre_handler_pause=True)
        try:
            original.wait_file(self.root / 'child.booting')
            os.kill(proc.pid, signal.SIGTERM)
            _, error = proc.communicate(timeout=6)
            self.assertEqual(proc.returncode, (-signal.SIGTERM) & 255, error.decode())
            self.assertEqual(self.read_receipt()['child_actual_exit'], -signal.SIGTERM)
            self.assertFalse((self.root / 'child.ready').exists())
        finally:
            self.cleanup(proc)

    def test_legacy_unbound_baseline_receipt_is_refused(self):
        fixture = original.Supervise()
        fixture.setUp()
        try:
            from datetime import datetime, timezone
            gates = fixture._good_plan_env({'actual_model_success': True, 'latest_success_at': datetime.now(timezone.utc).isoformat()})
            baseline = json.loads((gates / 'campaign-readonly-gate.json').read_text())
            baseline.pop('schema', None)
            baseline.pop('issuer', None)
            (gates / 'campaign-readonly-gate.json').write_text(json.dumps(baseline))
            with self.assertRaises(fixture.m.GateError):
                fixture.m.check_plan(gates)
        finally:
            fixture.tearDown()

    def test_v2_receipt_refuses_each_provenance_and_health_gap(self):
        from datetime import datetime, timezone
        for flaw in ('issuer_hash', 'optimize', 'optimized_bool', 'read_only', 'missing_target', 'duplicate_target', 'unhealthy_target', 'model_origin'):
            with self.subTest(flaw=flaw):
                fixture = original.Supervise()
                fixture.setUp()
                try:
                    gates = fixture._good_plan_env({'actual_model_success': True, 'latest_success_at': datetime.now(timezone.utc).isoformat()})
                    target = gates / ('campaign-model-gate.json' if flaw == 'model_origin' else 'campaign-readonly-gate.json')
                    row = json.loads(target.read_text())
                    if flaw in ('issuer_hash', 'model_origin'): row['issuer']['sha256'] = '0' * 64
                    elif flaw == 'optimize': row['issuer']['optimize'] = 1
                    elif flaw == 'optimized_bool': row['issuer']['optimize'] = False
                    elif flaw == 'read_only': row['read_only'] = False
                    elif flaw == 'missing_target': row['deployments'].pop()
                    elif flaw == 'duplicate_target': row['deployments'][-1] = row['deployments'][0]
                    else: row['deployments'][0]['business_verified'] = False
                    target.write_text(json.dumps(row))
                    with self.assertRaises(fixture.m.GateError): fixture.m.check_plan(gates)
                finally:
                    fixture.tearDown()

    def test_slow_heartbeat_cannot_block_signal_delivery(self):
        self.delivery_probe(slow_heartbeat=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
