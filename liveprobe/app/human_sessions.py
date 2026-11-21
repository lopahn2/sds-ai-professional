"""human-session/v1 (C1)와 local-read/v1 (C2)의 공통 구현.

브라우저는 bearer를 입력·보관하지 않는다. 브라우저가 대기 세션을 시작하면 로컬 CLI가 사람 자격 파일의
bearer로 user_code를 승인하고, 그 뒤에만 쿠키 세션이 활성 권한을 갖는다. 서버는 쿠키 값의 SHA-256만
보관하며, 저장소는 API 프로세스 메모리(1 replica, 재시작 시 재로그인)다.

이 모듈은 FastAPI에 의존하지 않는 순수 로직(저장소·요청 검사·키 파일 검증)이다. HTTP 응답 변환은
호출하는 앱이 맡는다(`SessionError.status`, `.code`).
"""

import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

COOKIE = 'lp_human_session'
COOKIE_PATH = '/api'
CLIENT_HEADER = 'x-liveprobe-client'
CLIENT_VALUE = 'dashboard'
CSRF_HEADER = 'x-csrf-token'
LOCAL_READ_HEADER = 'x-liveprobe-local-read'
SCOPES = ('incident:read', 'incident:create', 'tester', 'prd-approver')
OPERATOR = 'operator'  # 로그인 모드이며 동작 권한이 아니다.
PENDING_SECONDS = 300
ACTIVE_SECONDS = 900
APPROVER_SECONDS = 600
MAX_PENDING = 200
# 기록 보존(만료 표시용): 만료 후 이 시간 동안은 'expired'로 답하고 그 뒤에는 'none'이다.
EXPIRED_GRACE_SECONDS = 900
# 0/O, 1/I/L 같은 혼동 문자를 뺀 31자.
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
CODE_PATTERN = re.compile(r'^[%s]{4}-?[%s]{4}$' % (CODE_ALPHABET, CODE_ALPHABET))
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')

MESSAGES_KO = {
    'operator_gateway_unconfigured': '로컬 운영자 게이트웨이의 서명 키를 확인하세요.',
    'local_operator_gateway_required': '운영자용 로컬 관제 주소가 필요합니다.',
    'human_session_required': '역할 확인이 필요합니다. 역할 확인을 시작하고 로컬 CLI로 승인하세요.',
    'human_session_pending': '역할 확인이 아직 승인되지 않았습니다. 로컬 CLI로 승인하세요.',
    'human_session_expired': '역할 확인이 만료되었습니다. 다시 시작하세요.',
    'human_session_code_invalid': '확인 코드를 찾을 수 없거나 만료되었습니다.',
    'human_session_scope_mismatch': '확인 코드가 요청한 역할과 다릅니다.',
    'human_session_scope_invalid': '지원하지 않는 역할입니다.',
    'browser_request_forbidden': '역할 승인은 로컬 CLI에서만 할 수 있습니다.',
    'csrf_invalid': '요청 확인 값이 맞지 않습니다. 화면을 새로 고친 뒤 다시 시도하세요.',
    'cross_origin_denied': '다른 출처의 요청은 허용하지 않습니다.',
    'client_header_required': '대시보드 요청 표식이 필요합니다.',
    'local_gateway_required': '로컬 게이트웨이(127.0.0.1)를 통한 요청만 허용합니다.',
    'local_read_key_unavailable': '로컬 읽기 키 파일을 확인할 수 없습니다.',
    'scope_forbidden': '이 작업의 권한이 없습니다.',
}


class SessionError(Exception):
    """고정 코드만 담는다. 쿠키·토큰·CSRF 값은 넣지 않는다."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code

    @property
    def message_ko(self) -> str:
        return MESSAGES_KO.get(self.code, '요청을 처리할 수 없습니다.')


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(value) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().upper()
    if not CODE_PATTERN.fullmatch(raw):
        return None
    raw = raw.replace('-', '')
    return raw[:4] + '-' + raw[4:]


@dataclass
class Session:
    key: str                      # SHA-256(쿠키 값)
    scope: str
    user_code: str
    created_at: float
    expires_at: float
    state: str = 'pending'        # pending | active
    subject: str | None = None
    scopes: tuple[str, ...] = ()
    csrf_token: str | None = field(default=None, repr=False)
    approved_at: float | None = None
    recheck: Callable[[str], list | tuple] | None = field(default=None, repr=False)


class HumanSessionStore:
    """스레드 안전 메모리 저장소. clock은 시험에서 주입한다."""

    def __init__(self, clock=None, *, max_pending: int = MAX_PENDING):
        self.clock = clock or time.time
        self.max_pending = max_pending
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._expired: dict[str, float] = {}     # 키 → 만료 표시를 끝낼 시각

    # ---------------------------------------------------------------- 내부
    def _purge(self, now: float) -> None:
        for key, item in list(self._sessions.items()):
            if item.expires_at <= now:
                del self._sessions[key]
                self._expired[key] = now + EXPIRED_GRACE_SECONDS
        for key, until in list(self._expired.items()):
            if until <= now:
                del self._expired[key]

    def _code(self) -> str:
        taken = {s.user_code for s in self._sessions.values() if s.state == 'pending'}
        while True:
            raw = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            code = raw[:4] + '-' + raw[4:]
            if code not in taken:
                return code

    # ---------------------------------------------------------------- 브라우저
    def start(self, scope: str, previous_cookie: str | None = None) -> tuple[str, dict]:
        if scope not in (*SCOPES, OPERATOR):
            raise SessionError(422, 'human_session_scope_invalid')
        now = self.clock()
        cookie = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            if previous_cookie:
                # 같은 브라우저의 이전 세션은 새 역할 확인으로 대체한다.
                self._sessions.pop(digest(previous_cookie), None)
                self._expired.pop(digest(previous_cookie), None)
            pending = sorted((s for s in self._sessions.values() if s.state == 'pending'), key=lambda s: s.created_at)
            while len(pending) >= self.max_pending:
                oldest = pending.pop(0)
                self._sessions.pop(oldest.key, None)
            item = Session(key=digest(cookie), scope=scope, user_code=self._code(), created_at=now,
                           expires_at=now + PENDING_SECONDS)
            self._sessions[item.key] = item
            return cookie, self._public(item)

    def view(self, cookie: str | None) -> dict:
        """GET current 응답. 활성 세션만 csrf_token을 포함한다."""
        if not cookie:
            return {'state': 'none'}
        now = self.clock()
        key = digest(cookie)
        with self._lock:
            self._purge(now)
            item = self._sessions.get(key)
            if item is None:
                return {'state': 'expired'} if key in self._expired else {'state': 'none'}
            if item.state == 'active' and not self._refresh_scopes(item):
                return {'state': 'expired'}
            return self._public(item, csrf=True)

    def remaining(self, cookie: str | None) -> int:
        """쿠키 Max-Age로 쓸 남은 초."""
        if not cookie:
            return 0
        with self._lock:
            item = self._sessions.get(digest(cookie))
            return max(0, int(item.expires_at - self.clock())) if item else 0

    def end(self, cookie: str | None, csrf: str | None) -> None:
        if not cookie:
            return
        key = digest(cookie)
        with self._lock:
            item = self._sessions.get(key)
            if item is not None and item.state == 'active':
                if not csrf or not hmac.compare_digest(csrf.encode(), (item.csrf_token or '').encode()):
                    raise SessionError(403, 'csrf_invalid')
            self._sessions.pop(key, None)
            self._expired.pop(key, None)

    # ---------------------------------------------------------------- CLI
    def approve(self, user_code, scope: str, subject: str, principal_scopes, *, recheck=None) -> dict:
        code = normalize_code(user_code)
        if scope not in (*SCOPES, OPERATOR):
            raise SessionError(422, 'human_session_scope_invalid')
        registered = set(SCOPES).intersection(principal_scopes or ())
        if scope == OPERATOR:
            if not registered or not callable(recheck):
                raise SessionError(403, 'scope_forbidden')
            scopes = registered
        else:
            if scope not in registered:
                raise SessionError(403, 'scope_forbidden')
            scopes = {scope} | ({'incident:read'} & registered)
        if code is None:
            raise SessionError(404, 'human_session_code_invalid')
        now = self.clock()
        with self._lock:
            self._purge(now)
            item = next((s for s in self._sessions.values() if s.state == 'pending'
                         and hmac.compare_digest(s.user_code, code)), None)
            if item is None:
                raise SessionError(404, 'human_session_code_invalid')
            if item.scope != scope:
                raise SessionError(409, 'human_session_scope_mismatch')
            item.state, item.subject = 'active', subject
            item.scopes = tuple(s for s in SCOPES if s in scopes)
            item.recheck = recheck if scope == OPERATOR else None
            item.csrf_token = secrets.token_urlsafe(32)
            item.approved_at = now
            item.expires_at = now + (APPROVER_SECONDS if 'prd-approver' in scopes else ACTIVE_SECONDS)
            return {'state': 'active', 'scope': item.scope, 'scopes': list(item.scopes),
                    'subject': subject, 'expires_at': item.expires_at}

    def revoke_subject(self, subject: str) -> int:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s.subject == subject]
            for key in keys:
                del self._sessions[key]
                self._expired[key] = self.clock() + EXPIRED_GRACE_SECONDS
            return len(keys)

    # ---------------------------------------------------------------- 동작 인증
    def _refresh_scopes(self, item):
        if item.recheck is not None:
            # A different credential with the same subject cannot extend or retain this grant.
            observed = set(item.recheck(item.subject))
            item.scopes = tuple(scope for scope in item.scopes if scope in observed)
            if not item.scopes:
                self._sessions.pop(item.key, None)
                self._expired[item.key] = self.clock() + EXPIRED_GRACE_SECONDS
        return bool(item.scopes)

    def actor(self, cookie: str | None, scope: str, *, csrf: str | None = None, mutation: bool = False,
              still_authorized=None) -> dict:
        """활성·미만료·scope 포함 세션의 주체. 변경 요청은 CSRF가 일치해야 한다."""
        if not cookie:
            raise SessionError(401, 'human_session_required')
        now = self.clock()
        key = digest(cookie)
        with self._lock:
            self._purge(now)
            item = self._sessions.get(key)
            if item is None:
                raise SessionError(401, 'human_session_expired' if key in self._expired else 'human_session_required')
            if item.state != 'active':
                raise SessionError(401, 'human_session_pending')
            if not self._refresh_scopes(item):
                raise SessionError(403, 'scope_forbidden')
            if scope not in item.scopes:
                raise SessionError(403, 'scope_forbidden')
            if mutation and (not csrf or not hmac.compare_digest(csrf.encode(), (item.csrf_token or '').encode())):
                raise SessionError(403, 'csrf_invalid')
            subject = item.subject
        # 승인 이후 신원 파일에서 권한이 빠졌으면 세션도 즉시 쓸 수 없다(bearer와 같은 재확인).
        if still_authorized is not None and not still_authorized(subject, scope):
            raise SessionError(403, 'scope_forbidden')
        return {'subject': subject, 'kind': 'human', 'via': 'session'}

    def consume(self, cookie: str | None) -> None:
        """prd-approver 세션은 approve-prd 1회 성공 뒤 소멸한다."""
        if not cookie:
            return
        key = digest(cookie)
        with self._lock:
            if self._sessions.pop(key, None) is not None:
                self._expired[key] = self.clock() + EXPIRED_GRACE_SECONDS

    def once(self, cookie, action):
        """승인 동작을 직렬화하고 성공한 경우에만 세션을 소멸시킨다. action 안에서 actor를 재확인한다."""
        with self._lock:
            result = action()
            self.consume(cookie)
            return result

    def _public(self, item: Session, *, csrf: bool = False) -> dict:
        if item.state == 'pending':
            return {'state': 'pending', 'scope': item.scope, 'user_code': item.user_code, 'expires_at': item.expires_at}
        result = {'state': 'active', 'scope': item.scope, 'scopes': list(item.scopes), 'subject': item.subject,
                  'expires_at': item.expires_at}
        if csrf:
            result['csrf_token'] = item.csrf_token
        return result


# -------------------------------------------------------------------- 요청 검사(헤더만 사용)
def lower_headers(headers) -> dict:
    return {str(k).lower(): v for k, v in headers.items()}


def browser_marked(headers) -> bool:
    """CLI 승인 요청에 브라우저 표식(Origin·Sec-Fetch-*)이 있으면 거부한다."""
    h = lower_headers(headers)
    return 'origin' in h or any(k.startswith('sec-fetch-') for k in h)


def check_browser_request(headers, origin_allowed, *, mutation: bool) -> None:
    """같은 출처·대시보드 표식 검사. origin_allowed(netloc) -> bool."""
    h = lower_headers(headers)
    site = h.get('sec-fetch-site')
    if site is not None and site not in ('same-origin', 'none'):
        raise SessionError(403, 'cross_origin_denied')
    origin = h.get('origin')
    if origin is not None:
        parts = urlsplit(origin)
        if origin == 'null' or parts.scheme not in ('http', 'https') or not origin_allowed(parts.netloc):
            raise SessionError(403, 'cross_origin_denied')
    if mutation and h.get(CLIENT_HEADER) != CLIENT_VALUE:
        raise SessionError(403, 'client_header_required')


def same_host(host_header):
    """독립 앱용: Origin의 host:port가 요청 Host와 같아야 한다."""
    host = (host_header or '').strip().lower()
    return lambda netloc: bool(host) and netloc.lower() == host


def set_cookie_header(value: str, max_age: int) -> str:
    return f'{COOKIE}={value}; HttpOnly; SameSite=Strict; Path={COOKIE_PATH}; Max-Age={max(0, int(max_age))}'


def clear_cookie_header() -> str:
    return f'{COOKIE}=; HttpOnly; SameSite=Strict; Path={COOKIE_PATH}; Max-Age=0'


# -------------------------------------------------------------------- C2 로컬 읽기 키
class LocalReadKey:
    """키 파일은 처음 읽을 때 일반 파일·symlink 아님·소유자 본인·권한 0600 이하를 확인한다.

    값은 메모리에만 두고 출력하지 않는다. 구성됐지만 파일이 부적합하면 모든 요청을 거부한다(폐쇄 실패).
    """

    def __init__(self, path: str | None):
        self.path = path or None
        self._value: bytes | None = None
        self._error: str | None = None
        self._loaded = False
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.path)

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            path = self.path or ''
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError('not_regular')
                if info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                    raise ValueError('permissions')
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                try:
                    raw = os.read(fd, 4096)
                finally:
                    os.close(fd)
                value = raw.strip()
                if len(value) < 32 or any(c in value for c in b' \t\r\n'):
                    raise ValueError('weak')
                self._value = value
            except (OSError, ValueError):
                self._value, self._error = None, 'local_read_key_unavailable'

    def verify(self, headers) -> bool:
        """구성되고 헤더 값이 일치하면 True. 미구성이면 항상 False."""
        if not self.configured:
            return False
        self._load()
        if self._value is None:
            return False
        supplied = lower_headers(headers).get(LOCAL_READ_HEADER)
        if not supplied:
            return False
        return hmac.compare_digest(supplied.encode(), self._value)

    def require(self, headers) -> None:
        """구성된 경우에만 키를 요구한다."""
        if not self.configured:
            return
        self._load()
        if self._value is None:
            raise SessionError(503, 'local_read_key_unavailable')
        if not self.verify(headers):
            raise SessionError(403, 'local_gateway_required')


_KEYS: dict[str, LocalReadKey] = {}
_KEYS_LOCK = threading.Lock()


def local_read_key(path: str | None) -> LocalReadKey:
    """경로별로 한 번만 읽는다. 경로가 없으면 미구성 객체."""
    if not path:
        return LocalReadKey(None)
    with _KEYS_LOCK:
        key = _KEYS.get(path)
        if key is None:
            key = _KEYS[path] = LocalReadKey(path)
        return key


LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '[::1]', '::1')


def loopback_netloc(netloc: str) -> bool:
    try:
        host = urlsplit('//' + netloc).hostname
    except ValueError:
        return False
    return host in ('127.0.0.1', 'localhost', '::1')


# -------------------------------------------------------------------- HTTP 연결(FastAPI는 호출 시에만 import)
def error_body(exc: SessionError) -> dict:
    return {'code': exc.code, 'message_ko': exc.message_ko}


def http_error(exc: SessionError):
    from fastapi import HTTPException
    return HTTPException(exc.status, detail=error_body(exc))


def session_actor(request, store: HumanSessionStore, scope: str, *, origin_allowed, still_authorized=None) -> dict:
    """쿠키 세션 주체. 변경 요청은 같은 출처·대시보드 표식·CSRF가 모두 맞아야 한다."""
    mutation = request.method not in SAFE_METHODS
    try:
        check_browser_request(request.headers, lambda netloc: origin_allowed(request, netloc), mutation=mutation)
        return store.actor(request.cookies.get(COOKIE), scope, csrf=request.headers.get(CSRF_HEADER),
                           mutation=mutation, still_authorized=still_authorized)
    except SessionError as exc:
        raise http_error(exc) from None


def install_routes(router, prefix: str, *, resolve, origin_allowed, browser_guard=None, cli_guard=None) -> None:
    """두 앱이 같은 구현을 쓴다.

    resolve() -> (store, identities, local_key)  (통합 기능이 꺼져 있으면 SessionError(503, 'feature_disabled'))
    origin_allowed(request, netloc) -> bool       (Origin 헤더 허용 판단)
    browser_guard(request)                         (앱별 추가 검사; 예: 코어 require_user)
    cli_guard(request)                             (CLI 승인·폐기 앞단 검사; 예: Host 허용 목록)
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from app.application.contracts import HumanSessionApprove, HumanSessionStart

    def reply(body, status=200, cookie=None):
        response = JSONResponse(body, status_code=status, headers={'Cache-Control': 'no-store'})
        if cookie is not None:
            response.headers.append('set-cookie', cookie)
        return response

    def rejected(exc: SessionError, *, clear=False):
        return reply(error_body(exc), exc.status, clear_cookie_header() if clear else None)

    def browser(request, mutation):
        if browser_guard is not None:
            browser_guard(request)
        check_browser_request(request.headers, lambda netloc: origin_allowed(request, netloc), mutation=mutation)

    def cli(request):
        if cli_guard is not None:
            cli_guard(request)
        if browser_marked(request.headers):
            raise SessionError(403, 'browser_request_forbidden')

    @router.post(prefix, status_code=201)
    def start(body: HumanSessionStart, request: Request):
        try:
            store, _, key = resolve()
            browser(request, True)
            if key.configured:
                # 외부 페이지가 대기 세션을 만들어 사람에게 승인을 유도하는 기기 코드 피싱을 막는다.
                key.require(request.headers)
            cookie, view = store.start(body.scope, request.cookies.get(COOKIE))
        except SessionError as exc:
            return rejected(exc)
        return reply(view, 201, set_cookie_header(cookie, PENDING_SECONDS))

    @router.get(prefix + '/current')
    def current(request: Request):
        try:
            store, _, _ = resolve()
            browser(request, False)
        except SessionError as exc:
            return rejected(exc)
        cookie = request.cookies.get(COOKIE)
        view = {**store.view(cookie), 'session_modes': [OPERATOR]}
        if view['state'] in ('none', 'expired'):
            return reply(view, cookie=clear_cookie_header() if cookie else None)
        # 활성화되면 쿠키 수명도 활성 만료에 맞춘다.
        return reply(view, cookie=set_cookie_header(cookie, store.remaining(cookie)))

    @router.delete(prefix + '/current')
    def end(request: Request):
        try:
            store, _, _ = resolve()
            browser(request, True)
            store.end(request.cookies.get(COOKIE), request.headers.get(CSRF_HEADER))
        except SessionError as exc:
            return rejected(exc)
        return reply({'state': 'none'}, cookie=clear_cookie_header())

    @router.post(prefix + '/approve')
    def approve(body: HumanSessionApprove, request: Request):
        try:
            store, identities, _ = resolve()
            cli(request)
        except SessionError as exc:
            return rejected(exc)
        # 사람 파일 주체만 승인한다. 실행기 자격은 identity_kind_forbidden(403)이다.
        principal = identities.authenticate_human(request.headers.get('authorization'))
        recheck = None
        if body.scope == OPERATOR:
            fingerprint = digest(request.headers['authorization'][7:])
            def recheck(subject):
                return identities.credential_scopes(subject, fingerprint)
        try:
            result = store.approve(body.user_code, body.scope, principal['subject'], principal['scopes'], recheck=recheck)
        except SessionError as exc:
            return rejected(exc)
        return reply(result)

    @router.post(prefix + '/revoke')
    def revoke(request: Request):
        try:
            store, identities, _ = resolve()
            cli(request)
        except SessionError as exc:
            return rejected(exc)
        principal = identities.authenticate_human(request.headers.get('authorization'))
        return reply({'subject': principal['subject'], 'revoked': store.revoke_subject(principal['subject'])})
