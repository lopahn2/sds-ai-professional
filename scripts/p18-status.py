#!/usr/bin/env python3
"""P18 캠페인·검증 상태를 수 초 안에 조회한다(읽기 전용, 표준 라이브러리만 사용).

전체 로그를 읽지 않는다. 영수증(json)과 PID 생존, 로그 끝 4KB만 확인하고,
네트워크·kubectl 호출은 각각 짧은 시간 상한을 둔다. 호출 하나가 응답하지 않아도
그 항목만 "확인불가"로 표시하고 나머지 결과는 그대로 출력한다.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / 'docs/evidence/P18/campaign'
EVIDENCE = CAMPAIGN / 'continuation/resume-20261026/worker-db-recovery-fix'
API = 'http://liveprobe.localhost'
ACTIVE = ('open', 'analyzing', 'action_pending', 'acting', 'verifying')
RECEIPTS = ('child-return.json', 'receipt.json', 'verdict.json', 'launch.json')
PROCESS_PATTERN = re.compile(
    r'pytest|p18-campaign-run|run_p2_postgres|run_isolated_suite|run_final\.py|reproduce\.py')
STALE_MINUTES = 15
TAIL_BYTES = 4096
_SECRET_PATTERNS = (
    (re.compile(r'postgres(?:ql)?(?:\+\w+)?://\S+'), 'postgresql://[생략]'),
    (re.compile(r'(?i)\b(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)\S+'), r'\1\2[생략]'),
)


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail_lines(path: Path, lines: int = 3, max_bytes: int = TAIL_BYTES) -> list[str]:
    """파일 크기와 무관하게 끝 max_bytes만 읽는다."""
    with path.open('rb') as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        chunk = handle.read().decode('utf-8', 'replace')
    rows = [row.strip() for row in chunk.splitlines() if row.strip()]
    if size > max_bytes and rows:
        rows = rows[1:]  # 잘린 첫 줄은 버린다
    return [redact(row)[:200] for row in rows[-lines:]]


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def run_record(folder: Path) -> dict:
    """실행 디렉터리 하나를 영수증만으로 요약한다."""
    files = {name: _load(folder / name) for name in RECEIPTS if (folder / name).exists()}
    launch = files.get('launch.json') or {}
    receipt = files.get('receipt.json') or {}
    child = files.get('child-return.json') or {}
    verdict = files.get('verdict.json') or {}
    record = {'path': str(folder), 'started_at': launch.get('started_at') or receipt.get('started_at')}
    exit_code = child.get('actual_exit', receipt.get('actual_exit', verdict.get('실제종료코드')))
    finished = child.get('finished_at') or receipt.get('finished_at')
    for key in ('tests_run', 'failure_counts', 'junit'):
        if key in receipt:
            record[key] = receipt[key]
    for source, target in (('시험수', 'tests'), ('실패수', 'failures'), ('오류수', 'errors'),
                           ('gate', 'gate'), ('verdict', 'verdict'), ('passed', 'passed')):
        if source in verdict:
            record[target] = verdict[source]
    review_verdict = next((verdict.get(key) for key in ('판정', 'verdict', 'gate')
                           if verdict.get(key) in ('PASS', 'BLOCK', 'HOLD')), None)
    if review_verdict:
        record['verdict'] = review_verdict
        record['review_recorded_at'] = verdict.get('기록시각') or verdict.get('recorded_at_utc')
        if '검사항목수' in verdict:
            record['review_checks'] = verdict['검사항목수']
    pid = launch.get('parent_pid') or launch.get('pid')
    if exit_code is not None or finished:
        record.update(state='종료', actual_exit=exit_code, finished_at=finished)
    elif pid is not None and pid_alive(pid):
        record.update(state='실행중', pid=pid)
    elif review_verdict:
        record['state'] = '판정기록'  # 검토 시각은 프로세스 종료·시험 exit의 증거가 아니다.
    elif launch:
        record.update(state='종료영수증없음', pid=pid)
    else:
        record['state'] = '판정자료없음'
    if record['state'] != '종료':
        logs = sorted(folder.glob('*.log'), key=lambda p: p.stat().st_mtime)
        if logs:
            record['log_tail'] = tail_lines(logs[-1])
    return record


def recent_runs(root: Path, limit: int, max_depth: int = 3) -> list[dict]:
    """root 아래 max_depth 이내에서 영수증이 있는 디렉터리를 최근 수정 순으로 고른다."""
    found: dict[Path, float] = {}
    base_depth = len(root.parts)
    for current, dirs, names in os.walk(root):
        depth = len(Path(current).parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        stamps = [os.stat(os.path.join(current, n)).st_mtime for n in names if n in RECEIPTS]
        if stamps:
            found[Path(current)] = max(stamps)
    chosen = sorted(found, key=lambda folder: found[folder], reverse=True)[:limit]
    return [run_record(folder) for folder in chosen]


def runner_lock(path: Path) -> str:
    if not path.exists():
        return '잠금파일없음(러너 미실행)'
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return '잠금보유(러너 실행중)'
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return '잠금없음(러너 미실행)'


def processes() -> list[str]:
    out = subprocess.run(['ps', '-axo', 'pid=,etime=,command='], text=True,
                         capture_output=True, timeout=5).stdout
    me = str(os.getpid())
    rows = []
    for row in out.splitlines():
        row = row.strip()
        if PROCESS_PATTERN.search(row) and not row.startswith(me + ' ') and 'p18-status' not in row:
            rows.append(redact(row)[:180])
    return rows[:10]


def http_json(path: str, timeout: float):
    request = urllib.request.Request(API + path, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:  # 503도 본문을 읽는다
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, None


def _age_minutes(stamp, now: datetime):
    try:
        value = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return round((now - value).total_seconds() / 60, 1)


def api_status(timeout: float) -> dict:
    now = datetime.now(timezone.utc)
    jobs = {'readyz': '/api/readyz', 'analyzing_items': '/api/incidents?limit=5&status=analyzing'}
    jobs.update({s: f'/api/incidents?limit=1&status={s}' for s in ACTIVE})
    result: dict = {'counts': {}}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {key: pool.submit(http_json, path, timeout) for key, path in jobs.items()}
        for key, future in futures.items():
            try:
                code, body = future.result(timeout=timeout + 1)
            except Exception as error:  # 개별 실패는 그 항목만 확인불가
                if key in ACTIVE:
                    result['counts'][key] = f'확인불가:{type(error).__name__}'
                else:
                    result[key] = f'확인불가:{type(error).__name__}'
                continue
            if key == 'readyz':
                body = body or {}
                result['readyz'] = {'http': code, 'status': body.get('status'), 'db': body.get('db'),
                                    'background': body.get('background'),
                                    'incident_worker': body.get('incident_worker', '필드없음')}
            elif key == 'analyzing_items':
                stale = []
                for item in (body or {}).get('items', []):
                    age = _age_minutes(item.get('updated_at'), now)
                    stale.append({'id': item.get('id'), 'updated_at': item.get('updated_at'),
                                  'age_min': age, 'stale': age is not None and age > STALE_MINUTES})
                result['analyzing_items'] = stale
            else:
                result['counts'][key] = (body or {}).get('total', f'HTTP{code}')
    return result


def kube_status(timeout: float) -> dict:
    command = ['kubectl', f'--request-timeout={int(timeout)}s', '-n', 'liveprobe-system', 'get', 'pod',
               '-l', 'app=liveprobe-api', '-o',
               'jsonpath={range .items[*]}{.metadata.name}|{.status.phase}|'
               '{.status.containerStatuses[0].restartCount}|{.status.containerStatuses[0].imageID}{"\\n"}{end}']
    out = subprocess.run(command, text=True, capture_output=True, timeout=timeout + 2)
    if out.returncode:
        return {'error': redact(out.stderr.strip())[:200]}
    pods = []
    for row in out.stdout.splitlines():
        name, phase, restarts, image = (row.split('|') + ['', '', '', ''])[:4]
        pods.append({'pod': name, 'phase': phase, 'restarts': restarts, 'image': image.split('@')[-1][:19]})
    return {'api_pods': pods}


def campaign_status() -> dict:
    checkpoint = _load(CAMPAIGN / 'checkpoint.json') or {}
    summary = _load(CAMPAIGN / 'report-live/summary.json') or {}
    payment = checkpoint.get('payment04_retry') or {}
    return {'phase': checkpoint.get('phase'),
            'worker_recovery': (checkpoint.get('incident_worker_recovery') or {}).get('state'),
            'coverage': summary.get('coverage'),
            'payment04_original_state': payment.get('original_state'),
            'runner_lock': runner_lock(CAMPAIGN / 'runs-r2/.lock')}


def timed(name: str, func, *args):
    started = time.monotonic()
    try:
        value = func(*args)
    except Exception as error:
        value = {'error': f'확인불가:{type(error).__name__}'}
    return name, value, round(time.monotonic() - started, 2)


def collect(args) -> dict:
    tasks = [('campaign', campaign_status), ('processes', processes),
             ('runs', recent_runs, Path(args.root), args.runs)]
    if not args.no_api:
        tasks.append(('api', api_status, args.timeout))
    if not args.no_kube:
        tasks.append(('kube', kube_status, args.timeout))
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'elapsed_s': {}}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        for name, value, seconds in pool.map(lambda task: timed(*task), tasks):
            report[name] = value
            report['elapsed_s'][name] = seconds
    return report


def render(report: dict) -> str:
    lines = [f"[조회 시각] {report['checked_at']}  (항목별 소요초 {report['elapsed_s']})"]
    campaign = report.get('campaign', {})
    lines.append(f"[캠페인] phase={campaign.get('phase')} worker복구={campaign.get('worker_recovery')}")
    lines.append(f"  러너={campaign.get('runner_lock')} 보고서범위={campaign.get('coverage')}"
                 f" payment04원본={campaign.get('payment04_original_state')}")
    api = report.get('api')
    if api is not None:
        lines.append(f"[API] readyz={api.get('readyz')}")
        lines.append(f"  활성건수={api.get('counts')}")
        items = api.get('analyzing_items')
        if isinstance(items, str):
            lines.append(f'  analyzing 목록 확인불가: {items}')
            items = []
        for item in items or []:
            mark = '  ※ 정체 의심' if item.get('stale') else ''
            lines.append(f"  analyzing #{item['id']} 마지막갱신 {item['updated_at']} ({item['age_min']}분 전){mark}")
        readyz = api.get('readyz')
        if isinstance(readyz, dict) and readyz.get('incident_worker') == '필드없음':
            lines.append('  주의: readyz에 incident_worker 필드가 없다. 200이어도 worker 생존을 뜻하지 않는다.')
    if 'kube' in report:
        lines.append(f"[kube] {report['kube']}")
    lines.append(f"[프로세스] {len(report.get('processes') or [])}개")
    for row in report.get('processes') or []:
        lines.append(f'  {row}')
    lines.append('[최근 실행 영수증]')
    for run in report.get('runs') or []:
        rel = os.path.relpath(run['path'], ROOT)
        detail = {k: run[k] for k in ('actual_exit', 'tests_run', 'failure_counts', 'junit', 'tests',
                                      'failures', 'errors', 'gate', 'pid', 'verdict', 'review_checks') if k in run}
        lines.append(f"  {run['state']:<8} {rel} {detail}")
        for row in run.get('log_tail', []):
            lines.append(f'      | {row}')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(EVIDENCE), help='실행 영수증을 찾을 디렉터리')
    parser.add_argument('--runs', type=int, default=8, help='표시할 최근 실행 수')
    parser.add_argument('--timeout', type=float, default=3.0, help='HTTP·kubectl 개별 상한(초)')
    parser.add_argument('--no-api', action='store_true')
    parser.add_argument('--no-kube', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--tail', metavar='LOG', help='지정 로그의 끝 몇 줄만 출력하고 종료')
    parser.add_argument('--lines', type=int, default=20)
    args = parser.parse_args(argv)
    if args.tail:
        print('\n'.join(tail_lines(Path(args.tail), args.lines, max_bytes=max(TAIL_BYTES, args.lines * 400))))
        return 0
    report = collect(args)
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
