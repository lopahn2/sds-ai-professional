#!/usr/bin/env python3
"""P18 순차 캠페인 감독자. 상위 출력이 끊기거나 감독자가 죽어도 실행기를 끊지 않는다.

r1 감독자는 실행기 출력을 자기 stdout으로 중계하다 Hermes 쪽 파이프가 닫히자
BrokenPipeError로 종료하면서 실행기에 SIGTERM을 보냈다. 여기서는
- 실행기 출력은 파일로만 쓴다(감독자 파이프를 거치지 않음),
- 실행기는 별도 세션으로 띄워 감독자 그룹 종료·SIGHUP이 전달되지 않게 한다,
- 감독자 이벤트는 JSONL 파일에 먼저 쓰고, 화면 출력이 끊기면 그 뒤로는 버린다,
- 맥 유휴/시스템 잠자기를 실행기 생존 동안만 막는다(caffeinate -w).
중단은 운영자가 감독자에 SIGTERM/SIGINT를 보낼 때만 실행기 PID 하나에 한 번 전달한다.
단일회 티켓을 소비한 짧은 신호 전달은 동기 조회·화면 출력과 독립적으로 진행한다.
화면은 비차단 복사이며 전체 이벤트는 JSONL에 보존한다. 시작 전 접수된 중단은 생성 권한을 취소한다.
안전 검사는 assert가 아닌 명시적 예외로 하며, 최적화(-O) 인터프리터는 실행을 거부한다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Optional
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'docs/evidence/P18/campaign/runs-r2'
EXPECTED_RUNNER = '715d85152095f08271571acf06893cf4e35091d312eec40ce20ebdeeab750a6d'
EXPECTED_GATE_ISSUER = '9ed62f5a2aaf3e507263e80f2f60866edf93c15b0bdd7f56bbda78c046c64a49'
GATE_SCHEMA = 'liveprobe_campaign_gate/v2'
GATE_SERVICES = ('order', 'inventory', 'payment', 'member', 'catalog', 'delivery', 'notification')
CAFFEINATE = Path('/usr/bin/caffeinate')
HEARTBEAT_SECONDS = 900
GATE_MAX_AGE_SECONDS = 1800        # gate 영수증과 실제 모델 성공 근거의 최대 경과
CLOCK_SKEW_SECONDS = 60            # 이보다 앞선 시각은 미래 값으로 거부


class GateError(RuntimeError):
    """사전 조건 거부. -O에서도 사라지지 않는 명시적 예외."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _discard_stream(stream) -> None:
    """끊긴 출력 기술자를 /dev/null로 바꿔 종료 시 flush가 종료코드를 덮지 않게 한다."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except OSError:
        pass


def _write_display(stream, line: str) -> bool:
    """화면 출력은 손실 가능 비차단 복사다. 전체 이벤트의 원본은 JSONL 파일이다."""
    if stream is None:
        return False
    try:
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            stream.write(line)
            stream.flush()
            return True
        os.set_blocking(fd, False)
        data = line.encode(getattr(stream, 'encoding', None) or 'utf-8', errors='replace')
        if len(data) > 4096:  # 파이프의 부분 행 대신 작은 안내만 복사한다
            data = ('{"event":"화면 출력 생략: 전체 내용은 감독 JSONL 확인"}\n').encode('utf-8')
        return os.write(fd, data) == len(data)
    except BlockingIOError:
        return False  # 읽지 않는 상위 파이프는 파일 기록·중단 전달을 막지 않는다
    except (AttributeError, BrokenPipeError, OSError, ValueError):
        _discard_stream(stream)
        return False


class Relay:
    """이벤트를 파일에 먼저 기록하고, 화면 출력은 실패해도 감독을 멈추지 않는다."""

    def __init__(self, path: Path, stream=None):
        self.path = Path(path)
        self.stream = sys.stdout if stream is None else stream
        self.stream_ok = True

    def event(self, name: str, **fields) -> dict:
        row = {'event': name, 'at': now(), **fields}
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self.path.open('a') as handle:
            handle.write(line + '\n')
            handle.flush()
        if self.stream_ok:
            self.stream_ok = _write_display(self.stream, line + '\n')
        return row


class StopRequests:
    """중단 전달은 화면·조회와 분리한다. 원자적 단일회 티켓으로 신호 재진입을 막는다."""

    def __init__(self):
        self.signum: Optional[int] = None
        self.count = 0
        self.forwarded = False
        self.child: Optional[subprocess.Popen] = None
        self._request = {}
        self._send_ticket = {'send': True}
        self._event_ticket = {'event': True}
        self._launch_ticket = {'launch': True}

    def request(self, signum, frame) -> None:
        self.count += 1
        self.signum = self._request.setdefault('signal', int(signum))
        self._launch_ticket.pop('launch', None)
        self._send_once()  # 파일·화면·조회 없이 짧은 PID 신호 전달만 한다

    def commit_launch(self) -> bool:
        # CPython dict.pop은 단일 C 연산이다. 먼저 접수된 중단은 생성 권한을 소비한다.
        # 이 연산 뒤의 신호는 시작 중 중단이며, bind 즉시 자식에 전달한다.
        return bool(self._launch_ticket.pop('launch', False))

    def bind(self, child: subprocess.Popen) -> None:
        self.child = child
        self._send_once()

    def _send_once(self) -> bool:
        child = self.child
        if self.signum is None or child is None or child.returncode is not None:
            return False
        if not self._send_ticket.pop('send', False):  # 검사/대입 사이 신호 재진입 공백 없음
            return False
        try:
            child.send_signal(self.signum)
        except ProcessLookupError:
            return False  # 이미 종료된 PID에는 재전송하지 않는다
        self.forwarded = True
        return True

    def forward(self, child: subprocess.Popen, relay: Relay) -> bool:
        if self.child is None:
            self.bind(child)
        if not self.forwarded or not self._event_ticket.pop('event', False):
            return False
        relay.event('안전 중단 전달', signal=self.signum, runner_pid=child.pid, requests=self.count)
        return True


def launch(command: list[str], log: Path, cwd: Path) -> subprocess.Popen:
    """실행기를 별도 세션으로 시작하고 출력은 로그 파일로만 보낸다. 최적화 모드는 물려주지 않는다."""
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONOPTIMIZE'}
    with Path(log).open('ab') as handle:
        return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=handle,
                                stderr=subprocess.STDOUT, start_new_session=True)


def sleep_guard(pid: int):
    """실행기 PID가 살아 있는 동안만 맥 잠자기를 막는다. 없으면 None."""
    if not CAFFEINATE.exists():
        return None
    return subprocess.Popen([str(CAFFEINATE), '-i', '-s', '-w', str(pid)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def ignore_hangup() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def supervise(child: subprocess.Popen, relay: Relay, heartbeat_seconds: float,
              heartbeat: Callable[[], dict], poll: float = 1.0, stops: Optional[StopRequests] = None) -> int:
    """실행기 종료까지 기다리며 주기 점검을 남긴다. 점검 실패는 기록만 하고 계속한다."""
    due = time.monotonic() + heartbeat_seconds
    while child.poll() is None:
        if stops is not None:
            stops.forward(child, relay)
        time.sleep(poll)
        if time.monotonic() >= due and child.poll() is None:
            due = time.monotonic() + heartbeat_seconds
            try:
                relay.event('15분 실제 실행 점검', runner_pid=child.pid, **heartbeat())
            except Exception as error:  # 조회 하나의 실패로 감독을 멈추지 않는다
                relay.event('점검 일부 확인불가', runner_pid=child.pid, error_type=type(error).__name__)
    if stops is not None:
        stops.forward(child, relay)  # 조회 중 자식이 먼저 종료해도 실제 전달 기록은 남긴다
    return child.returncode


def runner_command(root: Path, max_attempts: int) -> list[str]:
    return [str(root / 'liveprobe/.venv/bin/python'), '-u', '-B', str(root / 'scripts/p18-campaign-run.py'),
            '--execute', '--build-manifest', str(root / 'docs/evidence/P18/campaign/build-r2/build-summary.json'),
            '--output', str(root / 'docs/evidence/P18/campaign/runs-r2'), '--context', 'rancher-desktop',
            '--namespace', 'dev', '--max-attempts', str(max_attempts)]


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'불러올 수 없음: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _api(path: str):
    with urlopen('http://liveprobe.localhost' + path, timeout=3) as response:
        return json.load(response)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise GateError(f'{label} 시각 없음')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise GateError(f'{label} 시각 형식 오류') from error
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _require_recent(value: Any, label: str) -> float:
    """실행 시점 기준으로 미래가 아니고 GATE_MAX_AGE_SECONDS 이내인지 확인한다."""
    age = (datetime.now(timezone.utc) - _parse_time(value, label)).total_seconds()
    _require(age >= -CLOCK_SKEW_SECONDS, f'{label} 시각이 미래임')
    _require(age <= GATE_MAX_AGE_SECONDS, f'{label} 오래됨(새 검증 필요)')
    return age


def _check_gate_origin(row: dict) -> None:
    issuer = row.get('issuer') or {}
    _require(row.get('schema') == GATE_SCHEMA, '검증된 새 기준선 발급 계약 필요')
    _require(issuer.get('path') == 'scripts/p18-campaign-gate.py'
             and issuer.get('sha256') == EXPECTED_GATE_ISSUER
             and type(issuer.get('optimize')) is int and issuer['optimize'] == 0,
             '기준선 발급기 지문/최적화 정보 불일치')
    _require(_sha(ROOT / 'scripts/p18-campaign-gate.py') == EXPECTED_GATE_ISSUER, '발급기 현재 지문 불일치')
    _require(row.get('runner_sha256') == EXPECTED_RUNNER
             and row.get('read_only') is True and row.get('new_injection_performed') is False,
             '영수증 실행기/읽기 전용 범위 불일치')


def _check_baseline_targets(row: dict) -> None:
    targets = row.get('deployments')
    if not isinstance(targets, list) or len(targets) != 21 or not all(isinstance(t, dict) for t in targets):
        raise GateError('기준선 21대상 관측 결측/형식 오류')
    expected = {(ns, name) for ns in ('dev', 'stg', 'prd') for name in GATE_SERVICES}
    _require({(r.get('namespace'), r.get('name')) for r in targets} == expected, '기준선 대상 중복/누락')
    for target in targets:
        _require(target.get('ready') is True and target.get('baseline_fields_equal') is True
                 and target.get('chaos_active') == [] and target.get('release_active') == []
                 and target.get('health_http_status') == 200 and target.get('business_verified') is True
                 and bool(target.get('business_http_statuses'))
                 and all(x == 200 for x in target['business_http_statuses']), '기준선 건강/원복 관측 실패')


def check_plan(gates: Path) -> dict:
    """새 주입 전 조건. 하나라도 어긋나면 GateError로 실행기를 시작하지 않는다."""
    _require(_sha(ROOT / 'scripts/p18-campaign-run.py') == EXPECTED_RUNNER, '실행기 지문 불일치')
    with (RUNS / '.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 다른 러너가 있으면 즉시 실패
        except BlockingIOError as error:
            raise GateError('다른 러너가 원장 잠금을 보유함') from error
        fcntl.flock(lock, fcntl.LOCK_UN)
    baseline = json.loads((gates / 'campaign-readonly-gate.json').read_text())
    _check_gate_origin(baseline)
    _check_baseline_targets(baseline)
    _require(baseline.get('gate') == 'pass', '기준선 gate 실패')
    _require_recent(baseline.get('at'), '기준선 gate 영수증')
    model = json.loads((gates / 'campaign-model-gate.json').read_text())
    _check_gate_origin(model)
    _require(model.get('actual_model_success') is True, '실제 모델 성공 근거 없음')
    _require_recent(model.get('at'), '모델 gate 영수증')
    model_age = _require_recent(model.get('latest_success_at'), '실제 모델 성공')
    ready = _api('/api/readyz')
    _require(bool(ready.get('background'))
             and ready.get('incident_worker') == {'alive': True, 'cleanup_pending': False, 'ready': True},
             'API worker 미준비')
    for state in ('open', 'analyzing', 'action_pending', 'acting', 'verifying'):
        page = _api('/api/incidents?limit=100&status=' + state)
        items = page.get('items') or []
        _require(page.get('total') == len(items), f'{state} 목록 일부만 조회됨')
        _require(not [i for i in items if i.get('namespace') == 'dev'], 'dev 활성 사건 존재')
    unfinished = sorted(p.parent.name for p in RUNS.glob('*/progress.json') if not (p.parent / 'result.json').exists())
    _require(not unfinished, '결과 없는 진행 원장 존재: ' + ','.join(unfinished))
    return {'runner_sha256': EXPECTED_RUNNER, 'new_attempts_per_case_this_run': 1,
            'model_success_age_seconds_at_check': round(model_age, 1),
            'scope': 'dev만 순차 실행. stg/prd 변경 금지. 원복/drain 미확정 시 즉시 중단.'}


def _safe_snapshot(observer) -> dict:
    """원장 조회 실패는 감독·영수증을 막지 않고 확인불가로만 남긴다."""
    try:
        return observer.snapshot()
    except Exception as error:
        return {'확인불가': type(error).__name__}


class _Unavailable:
    """관측 모듈을 못 불러온 경우의 대역. 호출 시 원래 오류를 다시 던진다."""

    def __init__(self, error: Exception):
        self.error = error

    def snapshot(self):
        raise self.error

    def api_status(self, timeout):
        raise self.error


def _load_or_unavailable(path: Path, name: str):
    try:
        return _load(path, name)
    except Exception as error:
        return _Unavailable(error)


def main(argv=None) -> int:
    if sys.flags.optimize:
        sys.stderr.write('최적화(-O/PYTHONOPTIMIZE) 실행 거부: 안전 검사가 생략될 수 있음\n')
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('check-plan', 'execute'))
    parser.add_argument('--job', type=Path, required=True, help='새 감독 기록 디렉터리(기존이면 거부)')
    parser.add_argument('--gates', type=Path, required=True, help='기준선·모델 gate 영수증 디렉터리')
    args = parser.parse_args(argv)
    stops = StopRequests()
    if args.mode == 'execute':
        ignore_hangup()
        for signum in (signal.SIGTERM, signal.SIGINT):  # 사전 검사 전부터 요청을 받는다
            signal.signal(signum, stops.request)
    try:
        plan = check_plan(args.gates)
    except (GateError, OSError, ValueError) as error:
        sys.stderr.write(f'사전 조건 거부: {type(error).__name__}: {error}\n')
        return 3
    if args.mode == 'check-plan':
        print(json.dumps({'gate': 'pass', **plan, 'injection_performed': False}, ensure_ascii=False))
        return 0
    args.job.mkdir(parents=True, exist_ok=False)
    relay = Relay(args.job / 'campaign-supervisor.jsonl')
    def stopped_before_launch() -> int:
        signum = stops.signum
        if signum is None:
            raise RuntimeError('중단 요청 없이 시작 전 중단 기록 불가')
        receipt = {'started_at': None, 'finished_at': now(), 'actual_exit': None, 'child_actual_exit': None,
                   'stopped_before_launch': True, 'signal': stops.signum,
                   'scope': '실행기 시작 전 운영자 중단. 주입 없음.'}
        (args.job / 'child-return.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        relay.event('시작 전 중단 요청', **receipt)
        return 128 + signum

    if stops.signum is not None:
        return stopped_before_launch()
    observer = _load_or_unavailable(
        ROOT / 'docs/evidence/P18/campaign/queued-stop-fix/p1-fixes/parent-verification/run_campaign_resume.py',
        'campaign_ledger_snapshot')
    status = _load_or_unavailable(ROOT / 'scripts/p18-status.py', 'campaign_fast_status')
    command = runner_command(ROOT, max_attempts=1)
    if not stops.commit_launch():
        return stopped_before_launch()
    child = launch(command, args.job / 'campaign.log', cwd=ROOT)
    stops.bind(child)
    started_at = now()
    try:
        guard = sleep_guard(child.pid)
        guard_pid: Any = guard.pid if guard else None
    except Exception as error:  # 잠자기 방지 실패는 기록만 하고 감독을 계속한다
        guard_pid = f'확인불가:{type(error).__name__}'
    launch_row = {'started_at': started_at, 'parent_pid': os.getpid(), 'runner_pid': child.pid, 'pid': os.getpid(),
                  'sleep_guard_pid': guard_pid, 'command': command, 'plan': plan,
                  'heartbeat_seconds': HEARTBEAT_SECONDS, 'actual_exit': None}
    (args.job / 'launch.json').write_text(json.dumps(launch_row, ensure_ascii=False, indent=2))
    relay.event('캠페인 실제 재개', supervisor_pid=os.getpid(), runner_pid=child.pid, ledger=_safe_snapshot(observer))

    def heartbeat() -> dict:
        accounts = [{'name': a['name'], 'state': a['state']} for a in _api('/api/llm/accounts')['accounts']]
        return {'ledger': _safe_snapshot(observer), 'api': status.api_status(3), 'model_accounts': accounts}

    code = supervise(child, relay, HEARTBEAT_SECONDS, heartbeat, stops=stops)
    receipt = {'started_at': started_at, 'finished_at': now(), 'actual_exit': code, 'child_actual_exit': code,
               'runner_pid': child.pid, 'stop_signal_forwarded': stops.signum if stops.forwarded else None,
               'scope': '실행기 실제 종료코드. 원복·완료는 개별 원장과 별도 대조 필요.'}
    (args.job / 'child-return.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    relay.event('캠페인 실행 종료', **receipt, ledger=_safe_snapshot(observer))
    return code


def _finish(code: int) -> int:
    """닫힌 stdout의 종료 시 flush가 실제 종료코드를 덮지 않게 한다."""
    if sys.stdout is None:
        return code
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError):
        _discard_stream(sys.stdout)
    return code


if __name__ == '__main__':
    raise SystemExit(_finish(main()))
