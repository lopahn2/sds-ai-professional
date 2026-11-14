import importlib.util
import pathlib
import sys
import threading
import unittest
import urllib.request
import json

SERVICE = pathlib.Path(__file__).resolve().parents[1]

class InitialContractTests(unittest.TestCase):

    def test_business_request_returns_json(self):
        sys.path.insert(0, str(SERVICE))
        spec = importlib.util.spec_from_file_location('target_app', SERVICE / 'app.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        server = module.create_server('127.0.0.1', 0, background=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = {'recipient': '운영팀', 'channel': 'email', 'message': '운영 알림'}
            body = json.dumps(payload).encode() if payload is not None else None
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/notifications', data=body, method='POST', headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:
                self.assertIn('notification', json.load(response))
        finally:
            server.shutdown()
            server.server_close()
            server.runtime.stop.set()
            thread.join()

    def test_running_service_serves_korean_screen(self):
        self.assertTrue((SERVICE / 'app.py').exists(), '실행 가능한 앱 파일이 필요합니다')
        sys.path.insert(0, str(SERVICE))
        spec = importlib.util.spec_from_file_location('target_app', SERVICE / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        server = module.create_server('127.0.0.1', 0, background=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                self.assertEqual(response.status, 200)
                self.assertIn('장애 실험', response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            server.runtime.stop.set()
            thread.join()
