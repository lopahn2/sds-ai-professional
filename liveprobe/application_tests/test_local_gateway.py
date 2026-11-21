from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading
import urllib.request
import urllib.error
from application_runner import human_login


def test_gateway_exact_host_fixed_upstream_document_exception_and_cli(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('gateway', Path(__file__).resolve().parents[2] / 'scripts/liveprobe-local-gateway.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    seen = []
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.headers, self.path))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"state":"active","secret":"never-print"}')
        do_POST = do_GET
        def log_message(self, *args): pass
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    t = threading.Thread(target=upstream.serve_forever, daemon=True); t.start()
    gateway = module.server(f'http://127.0.0.1:{upstream.server_port}', 'synthetic-key-' + 'x'*40, 0)
    # 실제 OS 할당 포트로 고정 front Host 서버를 다시 생성한다.
    port = gateway.server_port; gateway.server_close()
    front = f'application.liveprobe.localhost:{port}'
    gateway = module.server(f'http://127.0.0.1:{upstream.server_port}', 'synthetic-key-'+'x'*40, port, front_host=front, upstream_host='application.liveprobe.localhost')
    worker = threading.Thread(target=gateway.serve_forever, daemon=True); worker.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path, *, cross=False, host=front):
        request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Host': host, 'Accept': 'text/html', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Site': 'cross-site' if cross else 'same-origin'})
        try:
            with opener.open(request) as response: return response.status
        except urllib.error.HTTPError as e: return e.code
    try:
        assert call('/', cross=True) == 200
        assert call('/index.html', cross=True) == 200
        assert call('/api/human-sessions/current', cross=True) == 403
        assert call('/private', cross=True) == 403
        assert call('/%61pi', cross=True) == 403
        assert call('/', host=f'127.0.0.1:{port}') == 403
        assert seen[0][0]['Host'] == 'application.liveprobe.localhost'
        path=tmp_path/'personal'; path.write_text('synthetic-human-token-only'); path.chmod(0o600)
        assert human_login.main(['approve','--url',f'http://{front}/api','--token-file',str(path),'--scope','tester','--code','AAAA-BBBB']) == 0
        output=capsys.readouterr().out
        assert 'never-print' not in output and 'synthetic-human-token-only' not in output
        assert seen[-1][0]['Host'] == 'application.liveprobe.localhost'
        assert seen[-1][1] == '/api/human-sessions/approve'
    finally:
        gateway.shutdown(); gateway.server_close(); upstream.shutdown(); upstream.server_close()


def test_legacy_projection_only_before_cutover():
    spec = importlib.util.spec_from_file_location('gateway', Path(__file__).resolve().parents[2] / 'scripts/liveprobe-local-gateway.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen, state = [], {'enabled': False}
    list_response = {'total': 1476, 'items': []}
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Host'), self.headers.get('X-LiveProbe-Local-Read'), self.headers.get('Cookie')))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(state if self.path.endswith('/catalog') else list_response).encode())
        def log_message(self, *args): pass
    with ThreadingHTTPServer(('127.0.0.1', 0), Upstream) as upstream:
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{upstream.server_port}'
        with module.server(url, 'k'*40, 0) as reservation:
            port = reservation.server_port
        with module.server(url, 'k'*40, port, upstream_host='liveprobe.localhost',
                           legacy_application_host='application.liveprobe.localhost') as gateway:
            threading.Thread(target=gateway.serve_forever, daemon=True).start()
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def call(path):
                req = urllib.request.Request(f'http://127.0.0.1:{port}'+path, headers={'Cookie':'lp=test'})
                try:
                    with client.open(req) as response: return response.status
                except urllib.error.HTTPError as e: return e.code
            try:
                assert call('/api/unified/summaries?group=waiting_people') == 200
                assert seen[-1] == ('/api/application/v1/summaries?group=waiting_people', 'application.liveprobe.localhost', 'k'*40, 'lp=test')
                assert call('/api/overview') == 200
                assert seen[-1] == ('/api/overview','liveprobe.localhost',None,None)
                for route in ('/api/incidents', '/api/unified/summaries'):
                    assert call(route + '?work=qa_fix&limit=200') == 503
                    assert seen[-1][0].endswith('?work=qa_fix&limit=1')
                list_response['by_work'] = {'qa_fix': 0}
                assert call('/api/incidents?work=qa_fix') == 503
                list_response['total'] = 0
                assert call('/api/incidents?work=qa_fix&limit=200') == 200
                assert seen[-1] == ('/api/incidents?work=qa_fix&limit=200','liveprobe.localhost',None,None)
                assert call('/api/unified/summaries?work=qa_fix&limit=50') == 200
                assert seen[-1] == ('/api/application/v1/summaries?work=qa_fix&limit=50','application.liveprobe.localhost','k'*40,'lp=test')
                assert call('/api/incidents?work=unknown') == 422
                assert module.legacy_route('POST','/api/application/v1/incidents/test/approve-prd') is None
                assert module.legacy_route('POST','/api/application/v1/runner/claim') is None
                state['enabled'] = True
                assert call('/api/unified/summaries') == 503
                assert seen[-1][0] == '/api/unified/catalog'
            finally:
                gateway.shutdown()
                upstream.shutdown()
