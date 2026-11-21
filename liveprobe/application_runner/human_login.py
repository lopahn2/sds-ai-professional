"""로컬 개인 자격으로 브라우저의 일회 코드를 승인한다. 원본 bearer는 출력하지 않는다."""
import argparse
import json
import os
import stat
import sys
import urllib.request
from urllib.parse import urlsplit


def token_file(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError('token_file_permissions')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        value = os.read(fd, 8193).decode().strip()
    finally:
        os.close(fd)
    if not 16 <= len(value) <= 8192 or any(c.isspace() for c in value):
        raise ValueError('token_file_invalid')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='LiveProbe 개인 역할 세션 승인·취소 (bearer 출력 없음)')
    parser.add_argument('action', choices=['approve', 'revoke'])
    parser.add_argument('--url', required=True, help='API 기준 주소: http://127.0.0.1:포트/api/application/v1 또는 /api')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--scope', choices=['operator', 'incident:read', 'incident:create', 'tester', 'prd-approver'],
                        help='operator는 이 개인 자격에 등록된 작업만 확인합니다.')
    parser.add_argument('--code')
    args = parser.parse_args(argv)
    parsed = urlsplit(args.url)
    if (parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or parsed.query or parsed.fragment or
        (parsed.scheme == 'http' and not (parsed.hostname in ('127.0.0.1', 'localhost', '::1') or
             (parsed.hostname or '').endswith('.localhost')))):
        parser.error('HTTPS 또는 루프백 HTTP 주소만 허용합니다.')
    if args.action == 'approve' and (not args.code or not args.scope):
        parser.error('approve에는 --code와 --scope가 필요합니다.')
    try:
        payload = {'user_code': args.code, 'scope': args.scope} if args.action == 'approve' else {}
        base = args.url.rstrip('/')
        if parsed.scheme == 'http' and (parsed.hostname or '').endswith('.localhost'):
            # macOS DNS에 의존하지 않는다. 목적지는 loopback으로 고정하고 Host만 검증한 원래 값으로 둔다.
            base = f'http://127.0.0.1:{parsed.port or 80}' + parsed.path.rstrip('/')
        request = urllib.request.Request(base + '/human-sessions/' + args.action,
            data=json.dumps(payload).encode(), method='POST', headers={
                'Host': parsed.netloc, 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token_file(args.token_file)})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=15) as response:
            result = json.loads(response.read(16384))
        # whitelist: 응답에 쿠키·CSRF·원본 자격이 있더라도 출력하지 않는다.
        summary = {k: result[k] for k in ('state', 'scope', 'subject', 'expires_at', 'revoked') if k in result}
        if result.get('scope') == 'operator':
            summary['scopes'] = [v for v in ('incident:read', 'incident:create', 'tester', 'prd-approver')
                                 if v in (result.get('scopes') or [])]
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception:
        print('역할 세션 요청 실패. 주소·개인 자격 파일·코드·역할을 확인하세요.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
