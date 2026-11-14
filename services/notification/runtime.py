"""Standalone standard-library HTTP runtime; copied with each service repository."""
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json
import math
import os
import random
import re
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, float('inf'))
COMMON_FAULTS = {
    'code_bug': ('업무 코드 결함', '특정 업무 요청에서 실제 예외와 500 응답'),
    'code_bug_validation': ('입력 처리 결함', '조회·입력 처리 중 실제 예외와 500 응답'),
    'code_bug_serialization': ('응답 변환 결함', '응답 변환 중 실제 예외와 500 응답'),
    'intermittent_exception': ('간헐적 예외', '업무 요청 일부(기본 15%)에서 처리되지 않은 예외'),
    'latency': ('응답 지연', '업무 응답 지연, 기본 2초'),
    'latency_spike': ('간헐적 지연', '업무 요청 일부(기본 30%)가 기본 3초 지연'),
    'hang': ('요청 멈춤', '업무 요청이 기본 40초 동안 멈춰 호출자 시간 초과'),
    'error_rate': ('오류율 증가', '업무 요청에 503 응답, 기본 50%'),
    'crash': ('프로세스 종료', '종료 코드 1, 재시작해도 파드가 바뀌기 전까지 반복'),
    'startup_config_error': ('시작 설정 오류', '설정 검증 예외 후 종료, 재시작해도 반복'),
    'memory_leak': ('메모리 누수', '초당 메모리 증가, 제한 도달 시 강제 종료'),
    'not_ready': ('준비 상태 실패', '준비 상태 503, 준비된 엔드포인트 0'),
    'liveness_fail': ('생존 확인 실패', '생존 확인 503, 컨테이너 재시작 반복'),
    'cpu_spin': ('CPU 과점유', '바쁜 반복 스레드로 CPU 포화와 응답 지연'),
    'worker_exhaustion': ('처리 슬롯 고갈', '업무 처리 슬롯 8개 점유, 10초 대기 후 503'),
    'background_job_failure': ('배치 작업 실패', '주기 작업이 예외로 실패, 화면 응답은 정상'),
    'business_invariant': ('업무 정합성 위반', '정합성 검사 예외 로그, 응답은 정상'),
    'dependency_failure': ('연동 실패', '연동 호출이 연결 거부로 실패하고 502 응답'),
}
# name -> {param: (default, low, high, integer_only)}
PARAMS = {
    'latency': {'seconds': (2, 0, 30, False)},
    'latency_spike': {'ratio': (0.3, 0, 1, False), 'seconds': (3, 0, 30, False)},
    'hang': {'seconds': (40, 1, 120, False)},
    'error_rate': {'ratio': (0.5, 0, 1, False)},
    'intermittent_exception': {'ratio': (0.15, 0, 1, False)},
    'memory_leak': {'mib_per_second': (8, 1, 64, False)},
    'cpu_spin': {'threads': (2, 1, 4, True)},
    'background_job_failure': {'interval_seconds': (3, 1, 60, False)},
}
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 3600
WORKER_SLOTS = 8
STATE_FILE = 'faults.json'


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class DependencyError(Exception):
    """An outbound integration call failed (connection, timeout or 5xx)."""


class WorkerPoolExhausted(Exception):
    """No business worker slot became free in time."""


class TransientStateError(Exception):
    """Reading cached business state failed transiently."""


def text(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RequestError(400, f'{name}을(를) 올바르게 입력하세요')
    return value.strip()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def label(value):
    return json.dumps(str(value), ensure_ascii=False)


def validate_params(fault, params):
    """Return validated options for a fault; raise RequestError(400) on any bad value or unknown key."""
    if not isinstance(params, dict):
        raise RequestError(400, 'params는 객체여야 합니다')
    spec = PARAMS.get(fault, {})
    unknown = [key for key in params if key not in spec]
    if unknown:
        raise RequestError(400, f'지원하지 않는 매개변수입니다: {unknown[0]}')
    options = {}
    for key, (default, low, high, integer) in spec.items():
        value = params.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high \
                or (integer and type(value) is not int):
            raise RequestError(400, f'{key} 값은 {low}~{high} 범위의 수여야 합니다')
        options[key] = value
    return options


class Runtime:
    def __init__(self, name, title, extra_fault):
        self.system = os.getenv('SYSTEM_NAME', name)
        self.service = os.getenv('SERVICE_NAME', name)
        self.environment = os.getenv('APP_ENV', 'dev')
        if self.environment not in ('dev', 'stg', 'prd'):
            raise ValueError('APP_ENV 값은 dev, stg, prd 중 하나여야 합니다')
        self.version = os.getenv('APP_VERSION', 'p18-dev')
        self.title = title
        self.definitions = dict(COMMON_FAULTS, **extra_fault)
        self.lock = threading.RLock()
        self.log_lock = threading.Lock()
        self.stop = threading.Event()
        self.faults = {}          # runtime faults: name -> {'params': {...}, 'expires_at': epoch}
        self.release = {}         # release faults baked into the image: name -> params (cannot be cleared by API)
        self.samples = {}
        self.errors = Counter()
        self.allocations = []
        self.threads = []
        self.business = None
        self.exit = os._exit      # tests replace this
        self.state_dir = os.getenv('FAULT_STATE_DIR') or None
        self.worker_wait = float(os.getenv('WORKER_WAIT_SECONDS', '10'))
        self.slots = threading.BoundedSemaphore(WORKER_SLOTS)
        self.holders, self.holder_stop = [], threading.Event()
        self.spinners, self.spin_stop = [], threading.Event()
        self.next_job = 0.0
        self.next_check = 0.0
        self.crash_at = None
        self._load_release()
        self._restore_state()
        if self.enabled('crash'):
            self.crash_at = time.monotonic() + 3
        for fault in self.definitions:
            if os.getenv('FAULT_' + fault.upper()) == '1':
                self.set_fault({'fault': fault, 'on': True}, persistence_reason='startup')

    # ------------------------------------------------------------------ fault state
    def _load_release(self):
        raw = os.getenv('RELEASE_FAULTS')
        if not raw:
            return
        try:
            data = json.loads(raw)
            faults = data.get('faults') if isinstance(data, dict) else None
            if not isinstance(faults, dict):
                raise ValueError('faults')
            release = {}
            for name, entry in faults.items():
                if name not in self.definitions or not isinstance(entry, dict):
                    raise ValueError(name)
                release[name] = validate_params(name, entry.get('params', {}))
        except (ValueError, RequestError, AttributeError):
            self.log('WARN', 'release_faults_invalid')
            return
        self.release = release
        self.log('WARN', 'release_faults_changed', active=sorted(release))

    def _restore_state(self):
        if not self.state_dir:
            return
        path = Path(self.state_dir) / STATE_FILE
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.log('WARN', 'chaos_state_unreadable')
            return
        restored = {}
        entries = data.get('faults', data) if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            self.log('WARN', 'chaos_state_unreadable')
            return
        for name, entry in entries.items():
            try:
                expires = float(entry['expires_at'])
                if name in self.definitions and math.isfinite(expires) and time.time() < expires <= time.time() + MAX_TTL_SECONDS:
                    restored[name] = {'params': validate_params(name, entry.get('params', {})), 'expires_at': expires}
            except (KeyError, TypeError, ValueError, RequestError):
                continue
        self.faults.update(restored)
        if restored:
            self.log('WARN', 'chaos_restored', active=sorted(restored))

    def _persist(self, faults=None, *, reason='request'):
        """Write the candidate state before publishing requested changes."""
        if not self.state_dir:
            return
        path = Path(self.state_dir) / STATE_FILE
        with self.lock:
            data = {name: dict(entry) for name, entry in
                    (self.faults if faults is None else faults).items()}
            try:
                if not data:
                    path.unlink(missing_ok=True)
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(data))
                os.replace(tmp, path)
            except OSError as exc:
                self.log('WARN', 'chaos_state_write_failed', reason=reason, error=type(exc).__name__)
                raise RequestError(503, '장애 상태를 저장하지 못했습니다') from exc

    def _expire(self):
        expired = []
        with self.lock:
            moment = time.time()
            for name, entry in list(self.faults.items()):
                if entry['expires_at'] <= moment:
                    self.faults.pop(name)
                    expired.append(name)
            if 'memory_leak' in expired and 'memory_leak' not in self.release:
                self.allocations.clear()
            if expired:
                # TTL is authoritative even when storage is unavailable. Never
                # let a failed cleanup write abort the tick or retain workers.
                try:
                    self._persist(reason='expiry')
                except RequestError:
                    self.log('WARN', 'chaos_expiry_persist_failed', expired=sorted(expired))
                self._sync_workers()
        for name in expired:
            self.log('INFO', 'chaos_expired', fault=name)

    def enabled(self, fault):
        with self.lock:
            entry = self.faults.get(fault)
            if entry is not None and entry['expires_at'] > time.time():
                return True
            return fault in self.release

    def params_of(self, fault):
        with self.lock:
            if fault in self.release:
                return dict(self.release[fault])
            entry = self.faults.get(fault)
            if entry is not None and entry['expires_at'] > time.time():
                return dict(entry['params'])
        return validate_params(fault, {})

    def log(self, level, event, **fields):
        with self.log_lock:
            print(json.dumps(dict(timestamp=now(), level=level, system=self.system,
                                  service=self.service, environment=self.environment,
                                  event=event, **fields), ensure_ascii=False), flush=True)

    def exception(self, exc):
        with self.lock:
            self.errors[type(exc).__name__] += 1
        with self.log_lock:
            print(f'level=ERROR system={self.system} service={self.service} environment={self.environment}', flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()

    def set_fault(self, data, *, persistence_reason='request'):
        fault = data.get('fault')
        if not isinstance(fault, str) or fault not in self.definitions:
            raise RequestError(400, '지원하지 않는 장애입니다')
        on = data.get('on', not data.get('off', False))
        if type(on) is not bool or ('off' in data and type(data['off']) is not bool):
            raise RequestError(400, 'on 또는 off 값은 참·거짓이어야 합니다')
        options = validate_params(fault, data.get('params', {}))
        ttl = data.get('ttl_seconds', DEFAULT_TTL_SECONDS)
        if type(ttl) is not int or not 1 <= ttl <= MAX_TTL_SECONDS:
            raise RequestError(400, f'ttl_seconds 값은 1~{MAX_TTL_SECONDS} 범위의 정수여야 합니다')
        with self.lock:
            candidate = dict(self.faults)
            if on:
                candidate[fault] = {'params': options, 'expires_at': time.time() + ttl}
            else:
                candidate.pop(fault, None)
            self._persist(candidate, reason=persistence_reason)
            self.faults = candidate
            if on:
                if fault == 'background_job_failure':
                    self.next_job = 0.0
                if fault == 'crash':
                    self.crash_at = time.monotonic() + 3
            elif fault == 'memory_leak' and fault not in self.release:
                self.allocations.clear()
            self._sync_workers()
        self.log('WARN' if on else 'INFO', 'chaos_changed', fault=fault, on=on, params=options,
                 ttl_seconds=ttl if on else None)
        return self.chaos()

    def clear(self):
        with self.lock:
            self._persist({})
            self.faults = {}
            if 'memory_leak' not in self.release:
                self.allocations.clear()
            self._sync_workers()
        self.log('INFO', 'chaos_cleared', release_active=sorted(self.release))
        return self.chaos()

    def chaos(self):
        self._expire()
        with self.lock:
            moment = time.time()
            active = [n for n, e in self.faults.items() if e['expires_at'] > moment]
            active += [n for n in self.release if n not in active]
            faults = []
            for name, (title, signal_text) in self.definitions.items():
                entry = self.faults.get(name)
                source = 'release' if name in self.release else ('runtime' if entry else None)
                params = self.release[name] if name in self.release else (entry['params'] if entry else {})
                faults.append({'name': name, 'label': title, 'signal': signal_text, 'on': source is not None,
                               'source': source, 'params': dict(params),
                               'expires_at': datetime.fromtimestamp(entry['expires_at'], timezone.utc).isoformat(timespec='seconds')
                               if entry and source == 'runtime' else None})
            return {'active': active, 'release_active': sorted(self.release), 'faults': faults,
                    'worker_slots': WORKER_SLOTS, 'allocated_mib': round(sum(map(len, self.allocations)) / 1048576, 1)}

    # ------------------------------------------------------------------ background workers
    def _sync_workers(self):
        # Each generation owns an immutable stop event; rapid off/on must not revive old workers.
        with self.lock:
            want_holders = self.enabled('worker_exhaustion') and not self.stop.is_set()
            if want_holders and not self.holders:
                self.holder_stop = threading.Event()
                for i in range(WORKER_SLOTS):
                    thread = threading.Thread(target=self._hold_slot, args=(self.holder_stop,),
                                              name=f'slot-holder-{i}', daemon=True)
                    self.holders.append(thread)
                    thread.start()
            elif not want_holders and self.holders:
                self.holder_stop.set()
                self.holders = []
            count = self.params_of('cpu_spin')['threads'] if self.enabled('cpu_spin') and not self.stop.is_set() else 0
            if len(self.spinners) != count:
                self.spin_stop.set()
                self.spinners = []
                self.spin_stop = threading.Event()
                for i in range(count):
                    thread = threading.Thread(target=self._spin, args=(self.spin_stop,),
                                              name=f'cpu-spin-{i}', daemon=True)
                    self.spinners.append(thread)
                    thread.start()

    def _hold_slot(self, stop):
        while not stop.is_set() and not self.stop.is_set():
            if self.slots.acquire(timeout=0.2):
                try:
                    while not stop.is_set() and not self.stop.is_set():
                        stop.wait(0.2)
                finally:
                    self.slots.release()
                return

    def _spin(self, stop):
        while not stop.is_set() and not self.stop.is_set():
            sum(i * i for i in range(20000))

    # ------------------------------------------------------------------ request hooks
    def acquire_slot(self):
        if not self.slots.acquire(timeout=self.worker_wait):
            raise WorkerPoolExhausted(f'업무 처리 슬롯 {WORKER_SLOTS}개가 모두 사용 중입니다 ({self.worker_wait:g}초 대기 초과)')

    def before_business(self):
        if self.enabled('hang'):
            end = time.monotonic() + self.params_of('hang')['seconds']
            while time.monotonic() < end and self.enabled('hang') and not self.stop.is_set():
                time.sleep(0.2)
        if self.enabled('latency'):
            time.sleep(self.params_of('latency')['seconds'])
        if self.enabled('latency_spike'):
            spike = self.params_of('latency_spike')
            if random.random() < spike['ratio']:
                time.sleep(spike['seconds'])
        if self.enabled('error_rate') and random.random() < self.params_of('error_rate')['ratio']:
            raise RequestError(503, '오류율 증가 실험으로 업무 요청을 거절했습니다')
        if self.enabled('intermittent_exception') and random.random() < self.params_of('intermittent_exception')['ratio']:
            self.read_session_state()

    def read_session_state(self):
        try:
            raise ConnectionResetError('세션 저장소 연결이 상대방에 의해 초기화되었습니다')
        except ConnectionResetError as exc:
            raise TransientStateError('세션 상태를 읽는 중 일시적 오류가 발생했습니다') from exc

    def call_dependency(self, env_name, path, target):
        """GET a JSON resource from a same-environment dependency. Returns (status, body) below 500, None when unset."""
        base = os.getenv(env_name, '').rstrip('/')
        if self.enabled('dependency_failure'):
            # Misconfigured integration: the discard port refuses every connection.
            base = 'http://127.0.0.1:9'
        if not base:
            return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(base + path, headers={'Accept': 'application/json', 'X-Caller': self.service})
        try:
            with opener.open(request, timeout=2) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status >= 500:
                raise DependencyError(f'{target} 연동 호출이 {status} 응답으로 실패했습니다') from exc
            return status, {}
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            raise DependencyError(f'{target} 연동 호출에 실패했습니다: {type(exc).__name__}') from exc

    # ------------------------------------------------------------------ metrics
    def observe(self, method, uri, status, elapsed):
        with self.lock:
            sample = self.samples.setdefault((method, uri, str(status)), [0, 0.0, [0]*len(BUCKETS)])
            sample[0] += 1
            sample[1] += elapsed
            for i, upper in enumerate(BUCKETS):
                if elapsed <= upper:
                    sample[2][i] += 1

    def metrics(self):
        output = ['# HELP http_server_requests_seconds HTTP request duration in seconds.',
                  '# TYPE http_server_requests_seconds histogram',
                  '# HELP app_info Application identity.', '# TYPE app_info gauge',
                  f'app_info{{system={label(self.system)},service={label(self.service)},language="python",version={label(self.version)}}} 1',
                  '# HELP app_errors_total Unhandled application exceptions.', '# TYPE app_errors_total counter']
        with self.lock:
            for exception, count in sorted(self.errors.items()):
                output.append(f'app_errors_total{{exception={label(exception)}}} {count}')
            if not self.errors:
                output.append('app_errors_total{exception="none"} 0')
            for (method, uri, status), (count, duration, buckets) in sorted(self.samples.items()):
                labels = f'method={label(method)},uri={label(uri)},status={label(status)}'
                output.extend([f'http_server_requests_seconds_count{{{labels}}} {count}',
                               f'http_server_requests_seconds_sum{{{labels}}} {duration:.9f}'])
                for upper, value in zip(BUCKETS, buckets):
                    bound = '+Inf' if math.isinf(upper) else str(upper)
                    output.append(f'http_server_requests_seconds_bucket{{{labels},le="{bound}"}} {value}')
            active = len(self.faults) + len([n for n in self.release if n not in self.faults])
        output.extend(['# HELP app_faults_active Injected faults currently active.', '# TYPE app_faults_active gauge',
                       f'app_faults_active {active}'])
        for name, kind, value in self.business.metrics():
            output.extend([f'# HELP {name} Business metric.', f'# TYPE {name} {kind}', f'{name} {value}'])
        return '\n'.join(output) + '\n'

    # ------------------------------------------------------------------ periodic work
    def boot_check(self):
        """Fail fast (stack trace + exit 1) when the service configuration does not load."""
        if self.enabled('startup_config_error'):
            try:
                self.business.load_settings()
            except Exception as exc:  # noqa: BLE001 - the stack trace is the evidence
                self.exception(exc)
                self.log('ERROR', 'startup_failed', exit_code=1)
                self.exit(1)

    def tick(self):
        self._expire()
        self._sync_workers()
        if self.enabled('crash'):
            if self.crash_at is None:
                self.crash_at = time.monotonic() + 3
            if time.monotonic() >= self.crash_at:
                self.log('ERROR', 'crash_exit', exit_code=1)
                self.exit(1)
                return
        else:
            self.crash_at = None
        self.boot_check()
        if self.enabled('memory_leak'):
            size = int(self.params_of('memory_leak')['mib_per_second'] * 1024 * 1024)
            with self.lock:
                # No growth cap: the container memory limit must produce the OOM signal.
                if self.enabled('memory_leak'):
                    self.allocations.append(bytearray(size))
        moment = time.monotonic()
        if moment >= self.next_job:
            failing = self.enabled('background_job_failure')
            self.next_job = moment + (self.params_of('background_job_failure')['interval_seconds'] if failing else 15)
            try:
                self.business.background_job()
            except Exception as exc:  # noqa: BLE001
                self.exception(exc)
        if moment >= self.next_check:
            self.next_check = moment + 5
            try:
                self.business.check_invariants()
            except Exception as exc:  # noqa: BLE001
                self.exception(exc)
        self.business.tick()

    def background_tick(self):
        while not self.stop.wait(1):
            try:
                self.tick()
            except Exception as exc:
                self.exception(exc)

    def self_traffic(self, port):
        # Ignore proxy environment variables: traffic never leaves loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        interval = float(os.getenv('SELF_TRAFFIC_INTERVAL_SECONDS', '4'))
        while not self.stop.wait(interval):
            method, path, data = self.business.traffic()
            body = json.dumps(data).encode() if data is not None else None
            request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=body, method=method,
                                             headers={'Content-Type': 'application/json', 'X-Self-Traffic': '1'})
            try:
                with opener.open(request, timeout=35) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                exc.close()  # The HTTP handler already recorded status, metric and traceback.
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                self.log('WARN', 'self_traffic_transport', error=type(exc).__name__)

    def start(self, port):
        functions = [(self.background_tick, ())]
        if os.getenv('SELF_TRAFFIC', '1') != '0':
            functions.append((self.self_traffic, (port,)))
        for function, args in functions:
            thread = threading.Thread(target=function, args=args, daemon=True)
            self.threads.append(thread)
            thread.start()


class ServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, runtime):
        self.runtime = runtime
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'LiveProbeTarget/1'

    def log_message(self, format, *args):
        # Access logs are emitted once per request, as JSON, by dispatch().
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(130)

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise RequestError(400, '잘못된 본문 길이입니다') from None
        if length < 0 or length > 65536:
            raise RequestError(413, '요청 본문은 64KiB 이하여야 합니다')
        try:
            data = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, UnicodeDecodeError):
            raise RequestError(400, '올바른 JSON 본문이 필요합니다') from None
        if not isinstance(data, dict):
            raise RequestError(400, 'JSON 객체가 필요합니다')
        return data

    def route(self, method, path, query):
        runtime = self.server.runtime
        if method == 'GET' and path in ('/', '/chaos', '/static/styles.css', '/static/app.js'):
            name = {'/': 'index.html', '/chaos': 'index.html', '/static/styles.css': 'styles.css', '/static/app.js': 'app.js'}[path]
            content_type = {'index.html': 'text/html; charset=utf-8', 'styles.css': 'text/css; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8'}[name]
            return 200, (Path(__file__).parent / 'static' / name).read_bytes(), content_type
        if method == 'GET' and path == '/metrics':
            return 200, runtime.metrics().encode(), 'text/plain; version=0.0.4; charset=utf-8'
        if method == 'GET' and path == '/healthz/live':
            live = not runtime.enabled('liveness_fail')
            return (200 if live else 503), {'status': 'ok' if live else 'liveness_failed'}, None
        if method == 'GET' and path == '/healthz/ready':
            ready = not runtime.enabled('not_ready')
            return (200 if ready else 503), {'status': 'ok' if ready else 'not_ready'}, None
        if method == 'GET' and path == '/api/config':
            return 200, {'system': runtime.system, 'service': runtime.service, 'environment': runtime.environment,
                         'version': runtime.version, 'title': runtime.title, 'self_traffic': os.getenv('SELF_TRAFFIC', '1') != '0'}, None
        if path == '/api/chaos':
            if method == 'GET':
                return 200, runtime.chaos(), None
            if method == 'POST':
                return 200, runtime.set_fault(self.read_json()), None
            if method == 'DELETE':
                return 200, runtime.clear(), None
            raise RequestError(405, '지원하지 않는 요청 방식입니다')
        if path.startswith(runtime.business.api_prefix):
            runtime.acquire_slot()
            try:
                runtime.before_business()
                return (*runtime.business.handle(method, path, query, self.read_json() if method in ('POST', 'PUT') else {}), None)
            finally:
                runtime.slots.release()
        raise RequestError(404, '요청한 주소를 찾을 수 없습니다')

    def dispatch(self):
        start = time.perf_counter()
        runtime = self.server.runtime
        status = 500
        path = urlsplit(self.path).path
        uri = runtime.business.template(path)
        try:
            status, result, content_type = self.route(self.command, path, parse_qs(urlsplit(self.path).query))
        except RequestError as exc:
            status, result, content_type = exc.status, {'error': str(exc)}, None
        except DependencyError as exc:
            runtime.exception(exc)
            status, result, content_type = 502, {'error': '연동 서비스 호출에 실패했습니다', 'exception': type(exc).__name__}, None
        except WorkerPoolExhausted as exc:
            runtime.exception(exc)
            status, result, content_type = 503, {'error': '처리 가능한 업무 슬롯이 없습니다', 'exception': type(exc).__name__}, None
        except Exception as exc:
            runtime.exception(exc)
            status, result, content_type = 500, {'error': '업무 처리 중 오류가 발생했습니다', 'exception': type(exc).__name__}, None
        payload = result if isinstance(result, bytes) else json.dumps(result, ensure_ascii=False).encode()
        elapsed = time.perf_counter() - start
        runtime.observe(self.command, uri, status, elapsed)
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type or 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError) as exc:
            runtime.log('WARN', 'client_disconnected', error=type(exc).__name__)
        finally:
            runtime.log('ERROR' if status >= 500 else 'INFO', 'http_access', method=self.command,
                        uri=uri, status=status, duration_seconds=round(elapsed, 6))

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch
    do_PUT = dispatch
    do_PATCH = dispatch


def serve(factory):
    try:
        server = factory()
    except Exception:  # noqa: BLE001 - a configuration that does not load is a startup failure, with its stack trace
        print(f'level=ERROR system={os.getenv("SYSTEM_NAME", "")} service={os.getenv("SERVICE_NAME", "")} '
              f'environment={os.getenv("APP_ENV", "")}', flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        sys.exit(1)
    def stop(signum, frame):
        server.runtime.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.runtime.boot_check()
    server.runtime.log('INFO', 'server_started', port=server.server_port, release_active=sorted(server.runtime.release))
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.runtime.stop.set()
        server.server_close()
