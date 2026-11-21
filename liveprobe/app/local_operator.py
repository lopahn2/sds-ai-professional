"""Loopback dashboard operator. Only the gateway signs requests; the browser receives a CSRF nonce."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import time

HEADER = 'x-liveprobe-operator-proof'
COOKIE = 'lp_operator_csrf'
CSRF = 'x-liveprobe-operator-csrf'
SUBJECT = 'local-operator'
SAFE = ('GET', 'HEAD')
TOKEN = re.compile(r'[a-f0-9]{64}\.[a-f0-9]{64}')


def private_key(path):
    if not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            key = source.read(4097).strip()
        if not 32 <= len(key) <= 4096 or not key.isascii() or any(c <= 32 for c in key):
            return None
        return key
    except (OSError, ValueError):
        return None


def operator_key(path, read_path):
    key, read = private_key(path), private_key(read_path)
    return key if key and read and not hmac.compare_digest(key, read) else None


def _sign(key, *fields):
    return hmac.new(key, '\0'.join(fields).encode(), hashlib.sha256).hexdigest()


def new_token(key):
    nonce = secrets.token_hex(32)
    return nonce + '.' + _sign(key, 'csrf', nonce)


def valid_token(key, token):
    if not token or not TOKEN.fullmatch(token):
        return False
    nonce, tag = token.split('.')
    return hmac.compare_digest(tag, _sign(key, 'csrf', nonce))


def proof(key, method, path, host, token, timestamp=None):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return stamp + '.' + _sign(key, 'request', stamp, method, path, host.lower(), token)


def human_path(path):
    # Do not forward operator credentials to identities, webhooks, docs or execution workers.
    clean = path.partition('?')[0]
    if '%' in clean or '//' in clean or any(part in ('.', '..') for part in clean.split('/')):
        return False
    if clean.startswith('/api/application/v1/'):
        return (clean.startswith('/api/application/v1/incidents') and
                not clean.endswith('/review-dev-metadata'))
    return clean.startswith(('/api/incidents', '/api/unified/', '/api/actions/', '/api/settings', '/api/account',
                             '/api/operator/', '/api/model-settings', '/api/commands', '/api/changes', '/api/codex',
                             '/api/config', '/api/reviews', '/api/ledger'))


def verify(request, key):
    from .human_sessions import SessionError, check_browser_request, same_host
    if not key:
        raise SessionError(503, 'operator_gateway_unconfigured')
    token = request.cookies.get(COOKIE)
    host = request.headers.get('host', '')
    path = request.scope['raw_path'].decode('ascii')
    if request.scope.get('query_string'):
        path += '?' + request.scope['query_string'].decode('ascii')
    supplied = request.headers.get(HEADER, '')
    stamp, _, tag = supplied.partition('.')
    if (not valid_token(key, token) or not human_path(path) or not re.fullmatch(r'[0-9]{1,12}', stamp)
            or abs(time.time()-int(stamp)) > 60 or not re.fullmatch(r'[a-f0-9]{64}', tag)
            or not hmac.compare_digest(supplied, proof(key, request.method, path, host, token, stamp))):
        raise SessionError(403, 'local_operator_gateway_required')
    mutation = request.method not in SAFE
    check_browser_request(request.headers, same_host(host), mutation=mutation)
    if mutation and (not request.headers.get('origin') or not hmac.compare_digest(
            request.headers.get(CSRF, '').encode(), token.encode())):
        raise SessionError(403, 'csrf_invalid')
    return {'subject': SUBJECT, 'kind': 'human', 'via': 'local_gateway'}
