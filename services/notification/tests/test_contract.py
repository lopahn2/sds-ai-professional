"""네트워크 외부 접근 없이 루프백에서 공통 HTTP 계약을 검증한다.

의도적인 code_bug 경로의 회귀 시험은 포함하지 않는다.
표준 traceback 계약은 별도의 예외를 삽입해 검증한다.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

SERVICE = Path(__file__).resolve().parents[1]
SYSTEM = 'notification'
sys.path.insert(0, str(SERVICE))
spec = importlib.util.spec_from_file_location('contract_app', SERVICE / 'app.py')
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class HttpCase(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'APP_ENV': 'stg', 'APP_VERSION': '시험-1', 'SELF_TRAFFIC': '0'})
        self.env.start()
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.server = app.create_server('127.0.0.1', 0, background=False)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.runtime.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.capture.__exit__(None, None, None)
        self.env.stop()

    def request(self, path, method='GET', data=None, raw=None):
        payload = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req = urllib.request.Request(self.base + path, data=payload, method=method, headers={'Content-Type': 'application/json'})
        try:
            response = self.opener.open(req, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read().decode()
            status, headers = response.status, response.headers
        return status, (json.loads(body) if 'application/json' in headers.get('Content-Type', '') else body), headers

    def chaos(self, fault, on=True, params=None):
        return self.request('/api/chaos', 'POST', {'fault': fault, 'on': on, 'params': params or {}})

class HttpContractTests(HttpCase):
    def test_self_traffic_url_is_ascii_and_executes_business(self):
        method, path, data = self.server.runtime.business.traffic()
        self.assertTrue(path.isascii(), '내부 HTTP 요청 주소의 한글은 퍼센트 인코딩해야 합니다')
        self.assertIn(self.request(path, method, data)[0], (200, 201, 202))

    def test_assets_health_identity(self):
        for path in ('/', '/chaos', '/static/app.js', '/static/styles.css', '/healthz/live', '/healthz/ready'):
            with self.subTest(path=path):
                status, _, headers = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        _, config, _ = self.request('/api/config')
        self.assertEqual(config['environment'], 'stg')
        self.assertEqual(config['version'], '시험-1')
        self.assertFalse(config['self_traffic'])

    def test_chaos_readiness_and_clear(self):
        status, state, _ = self.chaos('not_ready')
        self.assertEqual(status, 200)
        self.assertIn('not_ready', state['active'])
        self.assertEqual(self.request('/healthz/ready')[0], 503)
        self.assertEqual(self.request('/healthz/live')[0], 200)
        self.assertEqual(self.request('/api/chaos')[0], 200)
        self.assertEqual(self.request('/api/chaos', 'DELETE')[1]['active'], [])
        self.assertEqual(self.request('/healthz/ready')[0], 200)

    def test_chaos_off_switch(self):
        self.chaos('not_ready')
        self.assertEqual(self.chaos('not_ready', False)[1]['active'], [])
        self.chaos('not_ready')
        self.assertEqual(self.request('/api/chaos', 'POST', {'fault': 'not_ready', 'off': True})[1]['active'], [])

    def test_unknown_fault_and_invalid_parameters_are_rejected(self):
        self.assertEqual(self.chaos('unknown')[0], 400)
        for fault, params in [('latency', {'seconds': -1}), ('error_rate', {'ratio': 2}),
                              ('memory_leak', {'mib_per_second': 0}), ('latency', {'seconds': float('nan')})]:
            with self.subTest(fault=fault, params=params):
                self.assertEqual(self.chaos(fault, params=params)[0], 400)
        self.assertEqual(self.request('/api/chaos', 'POST', {'fault': 'latency', 'on': 'yes'})[0], 400)
        self.assertEqual(self.request('/api/chaos', 'POST', {'fault': 'latency', 'params': []})[0], 400)

    def test_invalid_json_and_oversize_body_are_rejected(self):
        self.assertEqual(self.request('/api/chaos', 'POST', raw=b'{bad')[0], 400)
        self.assertEqual(self.request('/api/chaos', 'POST', raw=b'[]')[0], 400)
        self.assertEqual(self.request('/api/chaos', 'POST', raw=b'x'*65537)[0], 413)

    def test_error_rate_is_business_only_and_recovers(self):
        self.chaos('error_rate', params={'ratio': 1})
        self.assertEqual(self.request(self.server.runtime.business.api_prefix)[0], 503)
        for path in ('/api/config', '/api/chaos', '/metrics', '/', '/healthz/ready'):
            self.assertEqual(self.request(path)[0], 200)
        self.chaos('error_rate', False)
        self.assertEqual(self.request(self.server.runtime.business.api_prefix)[0], 200)

    def test_latency_delays_business_not_readiness(self):
        self.chaos('latency', params={'seconds': 0.06})
        start = time.perf_counter()
        self.request(self.server.runtime.business.api_prefix)
        self.assertGreaterEqual(time.perf_counter()-start, 0.055)
        self.assertEqual(self.request('/healthz/ready')[0], 200)
        self.chaos('latency', False)
        self.assertFalse(self.server.runtime.enabled('latency'))

    def test_metrics_have_cumulative_histogram_and_application_identity(self):
        self.request(self.server.runtime.business.api_prefix)
        status, metrics, headers = self.request('/metrics')
        self.assertEqual(status, 200)
        self.assertIn('version=0.0.4', headers['Content-Type'])
        self.assertIn('language="python"', metrics)
        self.assertRegex(metrics, r'app_info\{[^\n]+\} 1\n')
        self.assertIn('# TYPE http_server_requests_seconds histogram\n', metrics)
        prefix = 'http_server_requests_seconds_'
        self.assertIn(prefix+'count{', metrics)
        self.assertIn(prefix+'sum{', metrics)
        counts = []
        for boundary in ('0.05', '0.1', '0.25', '0.5', '1', '2.5', '5', '+Inf'):
            matches = re.findall(r'^'+prefix+r'bucket\{[^\n]*le="'+re.escape(boundary)+r'"\} (\d+)$', metrics, re.M)
            self.assertTrue(matches, boundary)
            counts.append(int(matches[0]))
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(counts[-1], 0)
        self.assertIn('app_errors_total{exception="none"} 0\n', metrics)
        self.assertIn('# TYPE '+SYSTEM+'_', metrics)

    def test_unhandled_exception_emits_standard_traceback_and_error_metric(self):
        def broken(method, path, query, data):
            raise RuntimeError('표준 예외 형식 검사')
        original = self.server.runtime.business.handle
        self.server.runtime.business.handle = broken
        status, response, _ = self.request(self.server.runtime.business.api_prefix)
        self.server.runtime.business.handle = original
        self.assertEqual(status, 500)
        self.assertEqual(response['exception'], 'RuntimeError')
        log = self.output.getvalue()
        self.assertIn('level=ERROR system='+SYSTEM+' service='+SYSTEM, log)
        self.assertIn('Traceback (most recent call last):\n', log)
        self.assertRegex(log, r'  File "[^\n]+runtime.py", line \d+, in (dispatch|route)')
        self.assertIn('RuntimeError: 표준 예외 형식 검사', log)
        self.assertIn('app_errors_total{exception="RuntimeError"} 1', self.request('/metrics')[1])

    def test_access_log_is_single_line_json_without_request_body(self):
        self.request('/healthz/live')
        self.server.shutdown()
        logs = [json.loads(line) for line in self.output.getvalue().splitlines()]
        access = [entry for entry in logs if entry['event'] == 'http_access']
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0]['uri'], '/healthz/live')
        self.assertEqual(access[0]['system'], SYSTEM)
        self.assertEqual(access[0]['status'], 200)
        self.assertNotIn('body', access[0])

    def test_memory_allocation_is_released_when_disabled(self):
        self.chaos('memory_leak', params={'mib_per_second': 1})
        self.server.runtime.tick()
        self.assertEqual(sum(map(len, self.server.runtime.allocations)), 1024*1024)
        self.chaos('memory_leak', False)
        self.assertEqual(self.server.runtime.allocations, [])

    def test_unknown_path_and_path_traversal_do_not_expose_source(self):
        for path in ('/app.py', '/static/../app.py', '/absent'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertIn('uri="/unknown"', self.request('/metrics')[1])
