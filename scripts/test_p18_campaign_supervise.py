"""p18-campaign-supervise.py 회귀. 상위 출력 단절·감독자 종료가 실행기를 끊지 않는지 확인한다.

클러스터·API·DB·모델을 쓰지 않는다. 실행기 대신 짧은 python 자식 프로세스를 쓴다.
독립 검토(deleg_e6c7afd6)가 찾은 경계는 실제 프로세스·신호·파이프로 재현한다.
"""
import ast
from datetime import datetime, timedelta, timezone
import hashlib
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

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'p18-campaign-supervise.py'

# 가짜 실행기: 받은 신호를 기록하고, 신호를 받으면 70+신호번호로, 아니면 지정 코드로 끝난다.
FAKE_RUNNER = r'''
import json, pathlib, signal, sys, time
base = pathlib.Path(sys.argv[1]); seconds = float(sys.argv[2]); code = int(sys.argv[3])
got = []
for s in (signal.SIGTERM, signal.SIGINT):
    signal.signal(s, lambda signum, frame: got.append(signum))
pathlib.Path(str(base) + '.ready').write_text('1')
end = time.monotonic() + seconds
while time.monotonic() < end and not got:
    time.sleep(0.01)
time.sleep(0.3)
pathlib.Path(str(base) + '.signals.json').write_text(json.dumps(got))
sys.exit(70 + got[0] if got else code)
'''

# 감독자 main을 가짜 경계로 실행하는 보조 프로세스(실제 API·원장·실행기 없음).
MAIN_HELPER = r'''
import importlib.util, json, sys, time
from pathlib import Path
from types import SimpleNamespace
cfg = json.loads(sys.argv[1])
spec = importlib.util.spec_from_file_location('supervise_under_test', cfg['source'])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
root = Path(cfg['root'])
def plan(gates):
    if cfg.get('plan_delay'):
        (root / 'plan.started').write_text('1'); time.sleep(cfg['plan_delay'])
    return {'시험대역': True}
m.check_plan = plan
m.runner_command = lambda r, max_attempts: [sys.executable, '-c', cfg['runner'], str(root / 'child'),
                                            str(cfg.get('child_seconds', 0.6)), str(cfg.get('child_exit', 7))]
def guard(pid):
    if cfg.get('guard_delay'):
        # 전달 지연이 사라졌으므로 가짜 자식의 신호 처리기 준비를 먼저 확인한다.
        # 준비 전 신호의 실제 음수 종료코드는 별도 회귀로 검증한다.
        end = time.monotonic() + 5
        while not (root / 'child.ready').exists():
            if time.monotonic() >= end: raise TimeoutError('가짜 자식 준비 실패')
            time.sleep(0.01)
        (root / 'guard.started').write_text('1'); time.sleep(cfg['guard_delay'])
    return None
m.sleep_guard = guard
def snapshot():
    if cfg.get('snapshot') == 'fail':
        raise PermissionError('격리 원장 조회 실패')
    return {'시험대역': True}
observer = SimpleNamespace(snapshot=snapshot)
status = SimpleNamespace(api_status=lambda timeout: {'시험대역': True})
m._load = lambda path, name: observer if name == 'campaign_ledger_snapshot' else status
def no_api(path):
    raise TimeoutError('격리 조회 실패')
m._api = no_api
m.HEARTBEAT_SECONDS = cfg.get('heartbeat_seconds', 900)
raise SystemExit(m.main(['execute', '--job', str(root / 'job'), '--gates', str(root / 'gates')]))
'''

# check_plan을 나쁜 조건으로 실행하는 보조 프로세스(-O로 실행해 assert 제거 여부를 본다).
GATE_HELPER = r'''
import importlib.util, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
cfg = json.loads(sys.argv[1])
spec = importlib.util.spec_from_file_location('supervise_gate_under_test', cfg['source'])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
root = Path(cfg['root'])
m.ROOT = root / 'repo'; m.RUNS = root / 'runs'
(m.ROOT / 'scripts').mkdir(parents=True); m.RUNS.mkdir()
(m.ROOT / 'scripts/p18-campaign-run.py').write_text('# 지문이 다른 가짜 실행기\n')
(m.RUNS / 'unfinished').mkdir(); (m.RUNS / 'unfinished/progress.json').write_text('{}')
gates = root / 'gates'; gates.mkdir()
old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
(gates / 'campaign-readonly-gate.json').write_text(json.dumps({'at': old, 'gate': 'fail'}))
(gates / 'campaign-model-gate.json').write_text(json.dumps({'at': old, 'actual_model_success': False}))
m._api = lambda path: ({'background': False, 'incident_worker': {}} if path == '/api/readyz'
                       else {'total': 1, 'items': [{'namespace': 'dev', 'id': 1}]})
try:
    m.check_plan(gates)
except Exception as error:
    print('거부:' + type(error).__name__)
    raise SystemExit(3)
print('통과')
raise SystemExit(0)
'''


def module():
    spec = importlib.util.spec_from_file_location('p18_campaign_supervise', SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError('감독자 모듈을 불러올 수 없음')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class BrokenStream(io.StringIO):
    def write(self, text):
        raise BrokenPipeError(32, 'Broken pipe')


def wait_file(path: Path, timeout: float = 6.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > end:
            raise TimeoutError(str(path))
        time.sleep(0.01)


class Supervise(unittest.TestCase):
    def setUp(self):
        self.m = module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *, buffered=False, close_stdout=False, signal_after=None, **cfg):
        """보조 프로세스로 main을 실행하고 (실제 종료코드, 종료 영수증, 이벤트, stderr)를 돌려준다."""
        cfg.update(source=str(SOURCE), root=str(self.root), runner=FAKE_RUNNER)
        command = [sys.executable, '-B'] + ([] if buffered else ['-u']) + ['-c', MAIN_HELPER, json.dumps(cfg)]
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONUNBUFFERED'}
        proc = subprocess.Popen(command, stdout=subprocess.PIPE if close_stdout else subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
        if close_stdout:
            proc.stdout.close()
        if signal_after:
            marker, signum = signal_after
            wait_file(self.root / marker)
            os.kill(proc.pid, signum)
        _, err = proc.communicate(timeout=20)
        job = self.root / 'job'
        receipt = json.loads((job / 'child-return.json').read_text()) if (job / 'child-return.json').exists() else None
        events = ([json.loads(x) for x in (job / 'campaign-supervisor.jsonl').read_text().splitlines()]
                  if (job / 'campaign-supervisor.jsonl').exists() else [])
        return proc.returncode, receipt, events, err.decode('utf-8', 'replace')

    def child_signals(self):
        path = self.root / 'child.signals.json'
        wait_file(path)
        return json.loads(path.read_text())

    # ---- 기존 경계 ------------------------------------------------------------

    def test_relay_survives_closed_parent_output_and_keeps_file_record(self):
        relay = self.m.Relay(self.root / 'events.jsonl', BrokenStream())
        for index in range(3):
            relay.event('점검', index=index)
        rows = [json.loads(x) for x in (self.root / 'events.jsonl').read_text().splitlines()]
        self.assertEqual([r['index'] for r in rows], [0, 1, 2])
        self.assertFalse(relay.stream_ok)

    def test_child_output_goes_to_file_and_child_runs_in_own_session(self):
        log = self.root / 'campaign.log'
        child = self.m.launch([sys.executable, '-c',
                               "import time;print('첫줄',flush=True);time.sleep(0.6);print('끝줄');raise SystemExit(3)"],
                              log, cwd=self.root)
        try:
            self.assertNotEqual(os.getsid(child.pid), os.getsid(0))
        finally:
            relay = self.m.Relay(self.root / 'events.jsonl', BrokenStream())
            code = self.m.supervise(child, relay, heartbeat_seconds=60, heartbeat=lambda: {}, poll=0.05)
        self.assertEqual(code, 3)
        self.assertEqual(log.read_text().split(), ['첫줄', '끝줄'])

    def test_heartbeat_failure_is_recorded_without_stopping_supervision(self):
        calls = []

        def heartbeat():
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise TimeoutError('조회 지연')
            return {'ok': True}

        child = self.m.launch([sys.executable, '-c', 'import time;time.sleep(0.8)'], self.root / 'c.log', cwd=self.root)
        relay = self.m.Relay(self.root / 'events.jsonl', io.StringIO())
        code = self.m.supervise(child, relay, heartbeat_seconds=0.2, heartbeat=heartbeat, poll=0.05)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(calls), 2)
        events = [json.loads(x) for x in (self.root / 'events.jsonl').read_text().splitlines()]
        self.assertIn('점검 일부 확인불가', [e['event'] for e in events])
        self.assertIn('15분 실제 실행 점검', [e['event'] for e in events])

    def test_runner_survives_supervisor_group_kill(self):
        log = self.root / 'runner.log'
        pid_file = self.root / 'runner.pid'
        helper = ("import importlib.util,sys,time\n"
                  "spec=importlib.util.spec_from_file_location('s',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
                  "child=m.launch([sys.executable,'-c',\"import time;time.sleep(0.8);print('기록완료',flush=True)\"],sys.argv[2],cwd=sys.argv[4])\n"
                  "open(sys.argv[3],'w').write(str(child.pid))\n"
                  "time.sleep(30)\n")
        supervisor = subprocess.Popen([sys.executable, '-c', helper, str(SOURCE), str(log), str(pid_file), str(self.root)],
                                      start_new_session=True)
        wait_file(pid_file)
        os.killpg(supervisor.pid, signal.SIGKILL)  # 감독자와 그 프로세스 그룹 전체 강제 종료
        supervisor.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and '기록완료' not in log.read_text():
            time.sleep(0.05)
        self.assertIn('기록완료', log.read_text())

    def test_runner_command_keeps_single_attempt(self):
        command = self.m.runner_command(Path('/repo'), max_attempts=1)
        self.assertEqual(command[command.index('--max-attempts') + 1], '1')
        self.assertEqual(command.count('--execute'), 1)
        self.assertIn('/repo/docs/evidence/P18/campaign/runs-r2', command)
        self.assertEqual(command[2:4], ['-B', '/repo/scripts/p18-campaign-run.py'])
        self.assertNotIn('-O', command)

    def test_sleep_guard_is_bound_to_runner_pid(self):
        child = self.m.launch([sys.executable, '-c', 'import time;time.sleep(0.3)'], self.root / 'g.log', cwd=self.root)
        guard = self.m.sleep_guard(child.pid)
        if guard is None:
            self.skipTest('caffeinate 없음')
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(guard.wait(timeout=5), 0)

    def test_hangup_is_ignored_by_supervisor(self):
        previous = signal.getsignal(signal.SIGHUP)
        try:
            self.m.ignore_hangup()
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, previous)

    def test_nominal_main_returns_child_exit_and_receipt(self):
        code, receipt, events, _ = self.run_main(child_exit=7)
        self.assertEqual(code, 7)
        self.assertEqual(receipt['actual_exit'], 7)
        self.assertEqual(events[0]['event'], '캠페인 실제 재개')
        self.assertEqual(events[-1]['event'], '캠페인 실행 종료')

    # ---- 독립 검토 지적 회귀 ------------------------------------------------------

    def test_stop_requests_forward_exactly_once_from_loop(self):
        child = self.m.launch([sys.executable, '-c', FAKE_RUNNER, str(self.root / 'child'), '5', '0'],
                              self.root / 's.log', cwd=self.root)
        wait_file(self.root / 'child.ready')
        stops = self.m.StopRequests()
        stops.request(signal.SIGTERM, None)
        stops.request(signal.SIGINT, None)
        stops.request(signal.SIGTERM, None)
        relay = self.m.Relay(self.root / 'events.jsonl', io.StringIO())
        code = self.m.supervise(child, relay, heartbeat_seconds=60, heartbeat=lambda: {}, poll=0.02, stops=stops)
        self.assertEqual(code, 70 + signal.SIGTERM)
        self.assertEqual(self.child_signals(), [signal.SIGTERM])
        self.assertEqual((self.root / 'events.jsonl').read_text().count('안전 중단 전달'), 1)

    def test_signal_arriving_inside_forward_does_not_send_twice(self):
        """검토 P2: 전달 도중 다른 신호가 재진입해도 실행기에는 한 번만 보낸다."""
        child = self.m.launch([sys.executable, '-c', FAKE_RUNNER, str(self.root / 'child'), '5', '0'],
                              self.root / 's.log', cwd=self.root)
        wait_file(self.root / 'child.ready')
        stops = self.m.StopRequests()
        previous = signal.signal(signal.SIGINT, stops.request)
        fired = []
        forward_code = self.m.StopRequests.forward.__code__

        def trace(frame, event, arg):
            if event == 'line' and frame.f_code is forward_code and not fired:
                fired.append(True)
                signal.raise_signal(signal.SIGINT)
            return trace
        try:
            stops.request(signal.SIGTERM, None)
            relay = self.m.Relay(self.root / 'events.jsonl', io.StringIO())
            sys.settrace(trace)
            try:
                code = self.m.supervise(child, relay, heartbeat_seconds=60, heartbeat=lambda: {}, poll=0.02, stops=stops)
            finally:
                sys.settrace(None)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertTrue(fired)
        self.assertEqual(code, 70 + signal.SIGTERM)
        self.assertEqual(self.child_signals(), [signal.SIGTERM])
        self.assertEqual((self.root / 'events.jsonl').read_text().count('안전 중단 전달'), 1)

    def test_stop_during_launch_window_reaches_runner(self):
        """검토 P1: 자식 시작 직후·감독 루프 진입 전 SIGTERM도 실행기에 한 번 전달하고 영수증을 남긴다."""
        code, receipt, events, _ = self.run_main(guard_delay=1.0, child_seconds=5,
                                                 signal_after=('guard.started', signal.SIGTERM))
        self.assertEqual(self.child_signals(), [signal.SIGTERM])
        self.assertEqual(code, 70 + signal.SIGTERM)
        self.assertEqual(receipt['actual_exit'], 70 + signal.SIGTERM)
        self.assertEqual([e['event'] for e in events].count('안전 중단 전달'), 1)

    def test_stop_before_launch_does_not_start_runner(self):
        code, receipt, events, _ = self.run_main(plan_delay=1.0, signal_after=('plan.started', signal.SIGTERM))
        self.assertFalse((self.root / 'child.ready').exists())
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertTrue(receipt['stopped_before_launch'])
        self.assertIsNone(receipt['actual_exit'])
        self.assertIn('시작 전 중단 요청', [e['event'] for e in events])

    def test_ledger_snapshot_failure_keeps_supervision_and_receipt(self):
        """검토 P1: 시작·종료 원장 조회 실패가 감독·영수증·종료코드를 잃게 하지 않는다."""
        code, receipt, events, _ = self.run_main(snapshot='fail', child_exit=7)
        self.assertEqual(code, 7)
        self.assertEqual(receipt['actual_exit'], 7)
        names = [e['event'] for e in events]
        self.assertEqual(names[0], '캠페인 실제 재개')
        self.assertEqual(names[-1], '캠페인 실행 종료')
        self.assertEqual(events[0]['ledger'], {'확인불가': 'PermissionError'})
        self.assertEqual(events[-1]['ledger'], {'확인불가': 'PermissionError'})

    def test_closed_buffered_stdout_does_not_override_exit(self):
        """검토 P2: 닫힌 OS 파이프의 종료 시 flush가 실제 자식 종료코드(7)를 120으로 덮지 않는다."""
        code, receipt, _, err = self.run_main(buffered=True, close_stdout=True, child_exit=7)
        self.assertEqual(receipt['actual_exit'], 7)
        self.assertEqual(code, 7, err[-400:])

    def test_check_plan_rejects_bad_conditions_even_with_optimize(self):
        """검토 P1: -O(assert 제거)에서도 실행기 지문·gate·worker·활성사건·미완료 원장을 거부한다."""
        cfg = json.dumps({'source': str(SOURCE), 'root': str(self.root)})
        proc = subprocess.run([sys.executable, '-O', '-B', '-c', GATE_HELPER, cfg], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr[-400:])
        self.assertIn('거부:GateError', proc.stdout)

    def test_main_refuses_optimized_interpreter(self):
        proc = subprocess.run([sys.executable, '-O', '-B', str(SOURCE), 'check-plan', '--job', str(self.root / 'j'),
                               '--gates', str(self.root / 'g')], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('최적화', proc.stderr)

    def test_runner_does_not_inherit_python_optimize(self):
        previous = os.environ.get('PYTHONOPTIMIZE')
        os.environ['PYTHONOPTIMIZE'] = '2'
        try:
            child = self.m.launch([sys.executable, '-c', 'import sys;print(sys.flags.optimize)'], self.root / 'o.log', cwd=self.root)
        finally:
            if previous is None:
                os.environ.pop('PYTHONOPTIMIZE', None)
            else:
                os.environ['PYTHONOPTIMIZE'] = previous
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual((self.root / 'o.log').read_text().strip(), '0')

    def _good_plan_env(self, model_row):
        m = self.m
        m.ROOT = self.root / 'repo'
        m.RUNS = self.root / 'runs'
        (m.ROOT / 'scripts').mkdir(parents=True)
        m.RUNS.mkdir()
        runner = m.ROOT / 'scripts/p18-campaign-run.py'
        runner.write_text('# 가짜 실행기\n')
        m.EXPECTED_RUNNER = hashlib.sha256(runner.read_bytes()).hexdigest()
        issuer = m.ROOT / 'scripts/p18-campaign-gate.py'
        issuer.write_text('# 가짜 발급기\n')
        issuer_sha = hashlib.sha256(issuer.read_bytes()).hexdigest()
        setattr(m, 'EXPECTED_GATE_ISSUER', issuer_sha)
        m._api = lambda path: ({'background': True, 'incident_worker': {'alive': True, 'cleanup_pending': False, 'ready': True}}
                               if path == '/api/readyz' else {'total': 0, 'items': []})
        gates = self.root / 'gates'
        gates.mkdir()
        now = datetime.now(timezone.utc)
        common = {'schema': 'liveprobe_campaign_gate/v2', 'at': (now - timedelta(minutes=1)).isoformat(),
                  'issuer': {'path': 'scripts/p18-campaign-gate.py', 'sha256': issuer_sha, 'optimize': 0},
                  'runner_sha256': m.EXPECTED_RUNNER, 'read_only': True, 'new_injection_performed': False}
        targets = [{'namespace': ns, 'name': name, 'ready': True, 'baseline_fields_equal': True,
                    'chaos_active': [], 'release_active': [], 'health_http_status': 200,
                    'business_verified': True, 'business_http_statuses': [200]}
                   for ns in ('dev', 'stg', 'prd') for name in ('order', 'inventory', 'payment', 'member', 'catalog', 'delivery', 'notification')]
        (gates / 'campaign-readonly-gate.json').write_text(json.dumps({**common, 'gate': 'pass', 'deployments': targets}))
        (gates / 'campaign-model-gate.json').write_text(json.dumps({**common, **model_row}))
        return gates

    def test_check_plan_accepts_recent_model_success(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
        gates = self._good_plan_env({'actual_model_success': True, 'latest_success_at': recent})
        self.assertEqual(self.m.check_plan(gates)['new_attempts_per_case_this_run'], 1)

    def test_check_plan_rejects_stale_model_success_at_launch(self):
        """검토 P2: 영수증 발급은 최근이어도 실제 성공이 실행 시점에 30분을 넘으면 거부한다."""
        stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        gates = self._good_plan_env({'actual_model_success': True, 'latest_success_at': stale})
        with self.assertRaises(self.m.GateError):
            self.m.check_plan(gates)

    def test_check_plan_rejects_future_model_success(self):
        """검토 P2: 미래 시각 성공 근거를 실제 모델 성공으로 받지 않는다."""
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        gates = self._good_plan_env({'actual_model_success': True, 'latest_success_at': future})
        with self.assertRaises(self.m.GateError):
            self.m.check_plan(gates)

    def test_check_plan_rejects_missing_model_success_time(self):
        gates = self._good_plan_env({'actual_model_success': True})
        with self.assertRaises(self.m.GateError):
            self.m.check_plan(gates)


class NoAssertGuards(unittest.TestCase):
    def test_supervisor_safety_checks_do_not_use_assert(self):
        """-O에서 사라지는 assert를 안전 판정에 쓰지 않는다."""
        tree = ast.parse(SOURCE.read_text())
        asserts = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]
        self.assertEqual(asserts, [], f'assert 행: {asserts}')


if __name__ == '__main__':
    unittest.main()
