"""세 서비스에 독립 복제된 캠페인 계약 시험(외부 의존성 없음)."""
from test_contract import HttpCase, app, SYSTEM, SERVICE
import json
import os
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit


class CampaignRuntimeTests(HttpCase):
    def test_memory_disable_wins_against_inflight_allocation(self):
        runtime = self.server.runtime
        self.chaos('memory_leak', params={'mib_per_second': 1})
        entered, resume = threading.Event(), threading.Event()
        original = runtime.params_of
        def paused(name):
            options = original(name)
            if name == 'memory_leak':
                entered.set()
                resume.wait(2)
            return options
        runtime.params_of = paused
        thread = threading.Thread(target=runtime.tick)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            runtime.clear()
        finally:
            resume.set()
            thread.join(2)
            runtime.params_of = original
        self.assertEqual(len(runtime.allocations), 0)

    def test_crash_waits_three_seconds_and_can_be_cancelled(self):
        runtime = self.server.runtime
        exits = []
        runtime.exit = exits.append
        self.chaos('crash')
        runtime.tick()
        self.assertEqual(exits, [])
        self.assertIsNotNone(runtime.crash_at)
        runtime.crash_at = time.monotonic() - 0.1
        runtime.tick()
        self.assertEqual(exits, [1])
        runtime.clear()

    def test_release_controls_are_disabled_in_served_script(self):
        script = self.request('/static/app.js')[1]
        self.assertIn("fault.source === 'release'", script)
        self.assertIn('button.disabled', script)
        self.assertIn('배포 결함', script)

    def test_dependency_uses_business_query_and_propagates_real_5xx(self):
        seen = []
        class Dependency(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200 if len(seen) == 1 else 503)
                self.end_headers()
                self.wfile.write(b'{}')
        dependency = ThreadingHTTPServer(('127.0.0.1', 0), Dependency)
        thread = threading.Thread(target=dependency.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        calls = {'payment': ('POST', '/api/payments', {'order_id': '구매자-시험', 'amount': 9000}),
                 'catalog': ('GET', '/api/products/1', None),
                 'notification': ('POST', '/api/notifications', {'recipient': '수신자-시험', 'channel': 'sms', 'message': '조회'})}
        method, path, data = calls[SYSTEM]
        key = 'INVENTORY_URL' if SYSTEM == 'catalog' else 'MEMBER_URL'
        try:
            with patch.dict(os.environ, {key: f'http://127.0.0.1:{dependency.server_port}'}):
                self.assertIn(self.request(path, method, data)[0], (200, 201, 202))
                if SYSTEM == 'catalog':
                    self.assertEqual(seen[0], '/api/stock/1')
                else:
                    expected = data['order_id'] if SYSTEM == 'payment' else data['recipient']
                    self.assertEqual(parse_qs(urlsplit(seen[0]).query).get('search'), [expected])
                self.assertEqual(self.request(path, method, data)[0], 502)
                self.assertIn('HTTPError', self.output.getvalue())
        finally:
            dependency.shutdown()
            dependency.server_close()
            thread.join(2)

    def test_real_self_traffic_cycles_all_three_exception_paths(self):
        runtime = self.server.runtime
        for name in ('code_bug', 'code_bug_validation', 'code_bug_serialization'):
            self.chaos(name)
        expected = {'payment': {'ZeroDivisionError', 'KeyError', 'ValueError'},
                    'catalog': {'KeyError', 'TypeError', 'ValueError'},
                    'notification': {'AttributeError', 'TypeError', 'KeyError'}}[SYSTEM]
        with patch.dict(os.environ, {'SELF_TRAFFIC_INTERVAL_SECONDS': '0.02'}):
            thread = threading.Thread(target=runtime.self_traffic, args=(self.server.server_port,))
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not expected.issubset(runtime.errors) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(expected.issubset(runtime.errors), repr(runtime.errors))
                metrics = self.request('/metrics')[1]
                self.assertIn('status="500"', metrics)
            finally:
                runtime.stop.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_ttl_validation_persistence_restart_and_expiry(self):
        runtime = self.server.runtime
        for ttl in (0, 3601, 1.5, True, '1'):
            self.assertEqual(self.request('/api/chaos', 'POST', {'fault': 'not_ready', 'ttl_seconds': ttl})[0], 400)
        with tempfile.TemporaryDirectory() as directory:
            runtime.state_dir = directory
            self.request('/api/chaos', 'POST', {'fault': 'not_ready', 'ttl_seconds': 1})
            state = json.loads((Path(directory) / 'faults.json').read_text())
            self.assertIn('not_ready', state)
            with patch.dict(os.environ, {'FAULT_STATE_DIR': directory}):
                server = app.create_server('127.0.0.1', 0, background=False)
            try:
                self.assertTrue(server.runtime.enabled('not_ready'))
                time.sleep(1.05)
                self.assertNotIn('not_ready', server.runtime.chaos()['active'])
                self.assertFalse((Path(directory) / 'faults.json').exists())
            finally:
                server.runtime.stop.set()
                server.server_close()

    def test_worker_exhaustion_is_real_slot_timeout_then_recovers(self):
        runtime = self.server.runtime
        runtime.worker_wait = 0.04
        self.chaos('worker_exhaustion')
        deadline = time.monotonic() + 1
        while runtime.slots._value and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(runtime.slots._value, 0)
        status, body, _ = self.request(runtime.business.api_prefix)
        self.assertEqual(status, 503)
        self.assertEqual(body['exception'], 'WorkerPoolExhausted')
        retired = list(runtime.holders)
        self.request('/api/chaos', 'DELETE')
        for thread in retired:
            thread.join(1)
        self.assertEqual(runtime.slots._value, 8)
        self.assertEqual(self.request(runtime.business.api_prefix)[0], 200)

    def test_intermittent_exception_latency_spike_and_liveness(self):
        prefix = self.server.runtime.business.api_prefix
        self.chaos('intermittent_exception', params={'ratio': 1})
        self.assertEqual(self.request(prefix)[0], 500)
        self.assertIn('ConnectionResetError', self.output.getvalue())
        self.chaos('intermittent_exception', False)
        self.chaos('latency_spike', params={'ratio': 1, 'seconds': 0.03})
        started = time.monotonic()
        self.assertEqual(self.request(prefix)[0], 200)
        self.assertGreaterEqual(time.monotonic() - started, 0.025)
        self.chaos('liveness_fail')
        self.assertEqual(self.request('/healthz/live')[0], 503)
        self.assertEqual(self.request('/healthz/ready')[0], 200)
        self.request('/api/chaos', 'DELETE')
        self.assertEqual(self.request('/healthz/live')[0], 200)

    def test_background_timer_repeats_failure_without_manual_business_requests(self):
        runtime = self.server.runtime
        expected = {'payment': 'SettlementError', 'catalog': 'IndexRebuildError', 'notification': 'DispatchJobError'}[SYSTEM]
        runtime.tick()  # 정상 배치를 먼저 실행해 기존 예약 시각을 만든다.
        self.chaos('background_job_failure', params={'interval_seconds': 1})
        runtime.start(self.server.server_port)
        try:
            deadline = time.monotonic() + 4
            while runtime.errors[expected] < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(runtime.errors[expected], 2)
            self.assertEqual(self.request(runtime.business.api_prefix)[0], 200)
        finally:
            runtime.stop.set()
            for thread in runtime.threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_cpu_generation_stops_and_releases_on_expiry(self):
        runtime = self.server.runtime
        self.request('/api/chaos', 'POST', {'fault': 'cpu_spin', 'ttl_seconds': 1, 'params': {'threads': 1}})
        retired = runtime.spin_stop
        old = list(runtime.spinners)
        self.chaos('cpu_spin', False)
        self.chaos('cpu_spin', params={'threads': 1})
        self.assertIsNot(runtime.spin_stop, retired)
        self.assertTrue(retired.is_set())
        for thread in old:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        current = list(runtime.spinners)
        self.request('/api/chaos', 'DELETE')
        for thread in current:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.request('/api/chaos', 'POST', {'fault': 'cpu_spin', 'ttl_seconds': 1, 'params': {'threads': 1}})
        current = list(runtime.spinners)
        time.sleep(1.05)
        self.assertNotIn('cpu_spin', self.request('/api/chaos')[1]['active'])
        for thread in current:
            thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_release_precedence_and_runtime_delete_preserves_image_fault(self):
        release = {'faults': {'latency': {'params': {'seconds': 0.01}}, 'not_ready': {}}}
        with patch.dict(os.environ, {'RELEASE_FAULTS': json.dumps(release)}):
            server = app.create_server('127.0.0.1', 0, background=False)
        try:
            runtime = server.runtime
            runtime.set_fault({'fault': 'latency', 'params': {'seconds': 0}})
            row = next(row for row in runtime.chaos()['faults'] if row['name'] == 'latency')
            self.assertEqual(row['source'], 'release')
            self.assertEqual(runtime.params_of('latency')['seconds'], 0.01)
            self.assertIsNone(row['expires_at'])
            self.assertEqual(set(runtime.clear()['active']), {'latency', 'not_ready'})
        finally:
            server.runtime.stop.set()
            server.server_close()

    def test_hang_can_be_cleared_without_blocking_control_api(self):
        runtime = self.server.runtime
        self.chaos('hang', params={'seconds': 1})
        result = []
        thread = threading.Thread(target=lambda: result.append(self.request(runtime.business.api_prefix)[0]))
        thread.start()
        time.sleep(0.05)
        self.assertEqual(self.request('/api/chaos', 'DELETE')[0], 200)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [200])

    def test_corrupt_persisted_expiry_cannot_create_an_immortal_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'faults.json').write_text(json.dumps({'not_ready': {'params': {}, 'expires_at': float('inf')}}))
            with patch.dict(os.environ, {'FAULT_STATE_DIR': directory}):
                server = app.create_server('127.0.0.1', 0, background=False)
            try:
                self.assertFalse(server.runtime.enabled('not_ready'))
                self.assertEqual(server.runtime.chaos()['active'], [])
            finally:
                server.runtime.stop.set()
                server.server_close()

    def test_probes_execute_each_business_defect_then_recover(self):
        business = self.server.runtime.business
        probes = business.probes()
        for fault in ('code_bug', 'code_bug_validation', 'code_bug_serialization'):
            with self.subTest(fault=fault):
                self.chaos(fault)
                method, path, data = probes[fault]
                status, body, _ = self.request(path, method, data)
                self.assertEqual(status, 500)
                self.assertIn('exception', body)
                self.assertIn('app.py', self.output.getvalue())
                self.chaos(fault, False)
                self.assertIn(self.request(path, method, data)[0], (200, 201, 202))
        templates = {business.template(business.traffic()[1].split('?')[0]) for _ in range(12)}
        for method, path, data in probes.values():
            self.assertIn(business.template(path.split('?')[0]), templates)

    def test_release_code_bug_does_not_break_seed_or_delete(self):
        with patch.dict(os.environ, {'RELEASE_FAULTS': json.dumps({'faults': {'code_bug': {}}})}):
            server = app.create_server('127.0.0.1', 0, background=False)
        try:
            self.assertIn('code_bug', server.runtime.clear()['release_active'])
        finally:
            server.runtime.stop.set()
            server.server_close()

    def test_release_startup_error_exits_one_with_business_settings_stack(self):
        env = dict(os.environ, RELEASE_FAULTS=json.dumps({'faults': {'startup_config_error': {}}}), SELF_TRAFFIC='0')
        command = [sys.executable, '-c', "import app; from runtime import serve; serve(lambda: app.create_server('127.0.0.1', 0, background=False))"]
        try:
            result = subprocess.run(command, cwd=str(SERVICE), env=env, capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail('시작 설정 오류가 프로세스를 종료하지 않았습니다')
        self.assertEqual(result.returncode, 1)
        self.assertIn('in load_settings', result.stdout)
        self.assertIn('Traceback (most recent call last):', result.stdout)
