#!/usr/bin/env python3
"""기존 ingress를 끊지 않는 루프백 전용 관제 게이트웨이. 자격은 브라우저에 노출하지 않는다."""
import argparse
import http.client
import json
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'liveprobe'))
from application_runner.human_login import token_file
from app import local_operator as operator

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
       'transfer-encoding', 'upgrade'}


def legacy_route(method, path):
    """전환 전 원본 사건의 조회와 개인 읽기 인증에 사용하는 경로."""
    route = urlsplit(path)
    if route.netloc or route.scheme:
        return None
    native = '/api/application/v1'
    reads = {'/api/unified/summaries': native + '/summaries',
             native + '/summaries': native + '/summaries'}
    match = re.fullmatch(native + r'/incidents/[a-zA-Z0-9_-]{1,128}(?:/summary|/events|/release)?', route.path)
    destination = reads.get(route.path, route.path if match else None)
    if destination and method in ('GET', 'HEAD'):
        return destination + ('?' + route.query if route.query else '')
    if method == 'POST' and not route.query and re.fullmatch(native + r'/incidents/[a-zA-Z0-9_-]{1,128}/user-test', route.path):
        return route.path
    sessions = native + '/human-sessions'
    if (method, route.path) in {('POST', sessions), ('GET', sessions + '/current'),
                                ('DELETE', sessions + '/current'), ('POST', sessions + '/approve'),
                                ('POST', sessions + '/revoke')} and not route.query:
        return route.path
    return None


def core_has_no_unified_writer(connection_type, target, host):
    conn = connection_type(target.hostname, target.port, timeout=3)
    try:
        conn.request('GET', '/api/unified/catalog', headers={'Host': host, 'Accept': 'application/json'})
        response = conn.getresponse()
        if response.status == 404:
            return True
        if response.status != 200:
            return False
        body = json.loads(response.read(16385))
        return isinstance(body, dict) and body.get('enabled') is False
    except (ValueError, OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def legacy_work_filter_status(connection_type, target, path, headers):
    """Old list handlers silently ignore work. Check a single row before forwarding a filtered list."""
    route = urlsplit(path)
    if route.path not in ('/api/incidents', '/api/application/v1/summaries'):
        return 200
    values = parse_qs(route.query, keep_blank_values=True).get('work')
    if values is None:
        return 200
    if len(values) != 1 or values[0] not in ('approval', 'qa_fix', 'recovery'):
        return 422
    work = values[0]
    probe_headers = {k: v for k, v in headers.items() if k.lower() in
                     ('host', 'cookie', 'authorization', 'x-liveprobe-client', 'x-liveprobe-local-read')}
    conn = connection_type(target.hostname, target.port, timeout=3)
    try:
        conn.request('GET', f'{route.path}?work={work}&limit=1', headers=probe_headers)
        response = conn.getresponse()
        if response.status != 200:
            return response.status if response.status in (401, 403) else 503
        sample = json.loads(response.read(262145))
        counts = sample.get('by_work') if isinstance(sample, dict) else None
        total = sample.get('total') if isinstance(sample, dict) else None
        if isinstance(counts, dict) and type(counts.get(work)) is int and type(total) is int and total >= 0 and total == counts[work]:
            return 200
        return 503
    except (ValueError, OSError, http.client.HTTPException):
        return 503
    finally:
        conn.close()


def server(upstream, key, port, *, operator_key=None, front_host=None, upstream_host=None, legacy_application_host=None, document_url=None):
    target = urlsplit(upstream)
    if (target.scheme not in ('http', 'https') or not target.hostname or target.username or target.password or
            target.path not in ('', '/') or target.query or target.fragment):
        raise ValueError('upstream_invalid')
    front = urlsplit('http://' + (front_host or f'127.0.0.1:{port}'))
    route_host = upstream_host or target.netloc
    if (front.username or front.password or front.path or front.query or front.fragment or front.port != port or
            not (front.hostname == '127.0.0.1' or (front.hostname or '').endswith('.localhost')) or
            any(c in route_host for c in '/\\ \t\r\n?#@')):
        raise ValueError('gateway_host_invalid')
    if operator_key is not None:
        if (front.hostname != '127.0.0.1' or legacy_application_host or document_url or
                len(operator_key) < 32 or hmac.compare_digest(operator_key, key) or
                (target.scheme == 'http' and target.hostname not in ('127.0.0.1', 'localhost', '::1'))):
            raise ValueError('operator_gateway_invalid')
        operator_key = operator_key.encode()
    if len(key) < 32:
        raise ValueError('local_key_invalid')
    if legacy_application_host and (target.hostname not in ('127.0.0.1', 'localhost', '::1')
            or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*\.localhost', legacy_application_host)
            or legacy_application_host == route_host):
        raise ValueError('legacy_application_host_invalid')
    if document_url:
        destination = urlsplit(document_url)
        if (destination.scheme != 'http' or destination.hostname != '127.0.0.1' or not destination.port
                or destination.path != '/application' or destination.query or destination.fragment
                or destination.username or destination.password):
            raise ValueError('local_dashboard_url_invalid')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'
        def log_message(self, *args):
            pass  # URL·헤더·쿠키·기기 코드를 출력하지 않는다.

        def forward(self):
            allowed = {front.netloc}
            if (len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host', '').lower() not in allowed
                    or not self.path.startswith('/') or self.path.startswith('//')):
                self.send_error(403); return
            origin = self.headers.get('Origin')
            document_path = urlsplit(self.path).path
            frontend_entry = (document_path in ('/', '/index.html', '/incidents', '/application', '/settings', '/ledger', '/map')
                              or re.fullmatch(r'/incidents/(?:[0-9]+|(?:app|application)/[A-Za-z0-9_-]{1,128})', document_path) is not None)
            document_entry = (self.command in ('GET', 'HEAD') and frontend_entry and
                              'text/html' in self.headers.get('Accept', '') and
                              self.headers.get('Sec-Fetch-Dest') in (None, 'document'))
            if not document_entry and origin and origin not in {f'http://{host}' for host in allowed}:
                self.send_error(403); return
            if not document_entry and self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                self.send_error(403); return
            if document_entry and document_url and document_path in ('/', '/index.html'):
                ids = parse_qs(urlsplit(self.path).query).get('incident', [])
                location = document_url
                if len(ids) == 1 and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ids[0]):
                    location += '?incident=' + ids[0]
                self.send_response(307)
                self.send_header('Location', location)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            # The signed double-submit cookie is readable by same-origin UI. The signing key never is.
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except CookieError:
                self.send_error(400); return
            nonce = cookie[operator.COOKIE].value if operator.COOKIE in cookie else None
            operator_call = operator_key and operator.human_path(self.path) and not self.headers.get('Authorization')
            if operator_call and self.command not in operator.SAFE:
                if (origin != 'http://' + front.netloc or self.headers.get('X-LiveProbe-Client') != 'dashboard'
                        or not operator.valid_token(operator_key, nonce)
                        or not hmac.compare_digest(nonce.encode(), self.headers.get(operator.CSRF, '').encode())):
                    self.send_error(403); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self.send_error(400); return
            if not 0 <= length <= 2_000_000 or self.headers.get('Transfer-Encoding'):
                self.send_error(413); return
            hop = HOP | {p.strip().lower() for p in self.headers.get('Connection', '').split(',')}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in hop | {'host', 'x-liveprobe-local-read', 'x-liveprobe-local-view', operator.HEADER}
                       and not k.lower().startswith(('x-forwarded-', 'forwarded'))}
            native_path = legacy_route(self.command, self.path) if legacy_application_host else None
            if legacy_application_host and not native_path and urlsplit(self.path).path.startswith('/api/application/v1/'):
                self.send_error(503, 'Unified execution is required for this operation')
                return
            host = legacy_application_host if native_path else route_host
            headers['Host'] = host
            if (operator_call and self.headers.get('X-LiveProbe-Client') == 'dashboard' and
                    operator.valid_token(operator_key, nonce)):
                headers[operator.HEADER] = operator.proof(operator_key, self.command, self.path, host, nonce)
            if self.command in ('GET', 'HEAD') and not document_path.startswith('/api/') and 'text/html' in self.headers.get('Accept', ''):
                headers['X-LiveProbe-Local-View'] = 'dashboard'
            if not legacy_application_host or native_path:
                headers['X-LiveProbe-Local-Read'] = key
            else:
                # 이전 K8s API와 nginx에는 Application의 개인 세션을 전달하지 않는다.
                headers = {k:v for k,v in headers.items() if k.lower() != 'cookie'}
            if origin:
                headers['Origin'] = f'{target.scheme}://{host}'
            cls = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
            if native_path and not core_has_no_unified_writer(cls, target, route_host):
                self.send_error(503, 'Check unified gateway configuration')
                return
            if legacy_application_host and self.command in ('GET', 'HEAD'):
                status = legacy_work_filter_status(cls, target, native_path or self.path, headers)
                if status != 200:
                    self.send_error(status, 'Filtered incident counts unavailable')
                    return
            conn = cls(target.hostname, target.port, timeout=60)
            try:
                conn.request(self.command, native_path or self.path, body=self.rfile.read(length) if length else None, headers=headers)
                response = conn.getresponse()
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in HOP | {'content-length', 'server', 'date'}:
                        self.send_header(k, v)
                if operator_key and document_entry and response.status == 200:
                    if not operator.valid_token(operator_key, nonce):
                        nonce = operator.new_token(operator_key)
                    self.send_header('Set-Cookie', f'{operator.COOKIE}={nonce}; Path=/; SameSite=Strict')
                    self.send_header('Cache-Control', 'no-store')
                if operator_key and frontend_entry:
                    self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Connection', 'close')
                self.end_headers()
                if self.command != 'HEAD':
                    while data := response.read1(16384):
                        self.wfile.write(data); self.wfile.flush()
            except (OSError, http.client.HTTPException):
                self.close_connection = True
            finally:
                conn.close()
        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = forward

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--operator-key-file', help='루프백 운영자 요청 서명용 별도 0600 키 파일')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--front-host', help='허용 프런트 Host:port 한 개 (예: application.liveprobe.localhost:4186)')
    parser.add_argument('--upstream-host', help='배포자가 고정한 upstream Host (예: application.liveprobe.localhost)')
    parser.add_argument('--legacy-application-host', help='통합 writer 활성화 전, 기존 Application 읽기 API의 고정 .localhost ingress Host')
    parser.add_argument('--document-url', help='기존 Application 화면 북마크가 열 통합 로컬 관제 주소')
    args = parser.parse_args()
    server(args.upstream, token_file(args.key_file), args.port, operator_key=token_file(args.operator_key_file) if args.operator_key_file else None, front_host=args.front_host,
           upstream_host=args.upstream_host, legacy_application_host=args.legacy_application_host,
           document_url=args.document_url).serve_forever()


if __name__ == '__main__':
    main()
