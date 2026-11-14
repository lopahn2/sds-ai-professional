"""실제 HTTP API→화면의 실패 시험 원장 표시. 합성 provider·폐기 SQLite만 사용한다."""
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest
import urllib.request
import uvicorn

from test_core_support import Harness
from test_core_analysis import SyntheticTransport
import test_core_release as release_tests

ROOT = Path(__file__).resolve().parents[2]


class FailureApiBrowserTests(unittest.TestCase):
    def test_failed_build_receipt_from_actual_api_is_visible(self):
        out = Path(os.environ.get('APPLICATION_UI_EVIDENCE_DIR', ROOT / 'docs/evidence/application/integration/parent-ui-failure'))
        out.mkdir(parents=True, exist_ok=True)
        h = Harness(SyntheticTransport())
        flow = release_tests.CoreReleaseTests()
        flow.h = h
        incident, job, result = flow.built()
        result['ok'] = False
        result['tests'].update(after_passed=False, failed=1, report_sha256='e' * 64)
        result['tests']['executed_tests'][1]['status'] = 'failed'
        result['error'] = {'code': 'after_not_passed'}
        self.assertEqual(h.report(job, result).status_code, 200)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(h.app, host='127.0.0.1', port=port, log_level='critical', access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(server.started, '폐기 API 서버 시작 실패')
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/application/v1/healthz', timeout=3) as response:
                self.assertTrue(json.load(response)['ok'])
            command = ['node', 'liveprobe/application_tests/test_ui_failure_browser.mjs',
                       f'http://127.0.0.1:{port}', incident, str(out)]
            observed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(observed.returncode, 0, observed.stdout + observed.stderr)
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
            h.close()
            (out / 'cleanup.json').write_text(json.dumps({'server_thread_closed': not thread.is_alive(),
                'socket_closed': sock.fileno() == -1, 'database_scope': '시험 장치 소유 폐기 SQLite'}, ensure_ascii=False))
            self.assertFalse(thread.is_alive(), '폐기 API 서버 종료 실패')


if __name__ == '__main__':
    unittest.main()
