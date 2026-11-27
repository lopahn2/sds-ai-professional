"""두 화면의 개인 역할 세션과 최소 조회 투영. 기계 인증 경로는 바꾸지 않는다."""
from fastapi import HTTPException, Request
from starlette.responses import RedirectResponse
import re
from urllib.parse import urlsplit
from .. import human_sessions as H
from .. import local_operator
from .catalog import DEV_RESERVATION
from .safety import scrub


def summary(incident):
    from .qa_release import projection
    state = incident['state']
    next_ko = {'awaiting_user_test': '시험자가 dev 사용자 시험을 기록해야 합니다.',
               'awaiting_prd_approval': '승인자가 이 버전의 prd 승격을 확인해야 합니다.',
               'qa_failed': '운영자가 dev 화면 QA 실패 근거를 보고 보완 또는 재시험을 요청해야 합니다.',
               'awaiting_recovery': '배포 후 원래 업무 요청의 복구를 관측하고 있습니다.',
               'dev_infra_pending': '검증된 인프라 조치의 dev 반영 확인을 기다리고 있습니다.',
               'handoff': '담당자가 인계 사유를 확인해야 합니다.',
               'completed': '완료된 사건입니다.', 'failed': '실패 기록을 검토하세요.'}.get(state, '자동 진행 기록을 확인하세요.')
    data = incident.get('data') or {}
    origin = data.get('unified_origin') or {}
    root = origin.get('root_incident_id', origin.get('incident_id'))
    # Core incident ids are positive integers; canonical form is the decimal string. Standalone stays None.
    if type(root) is int and root > 0:
        root = str(root)
    elif not (isinstance(root, str) and root.isdigit() and root != '0' and not root.startswith('0')):
        root = None
    view = projection({'data': data}, state)
    return {**{k: incident.get(k) for k in ('id', 'state', 'created_at', 'environment', 'service', 'system')},
            'reason_ko': ((data.get('display_readings') or {}).get('signal/reason') or {}).get('ko'),
            'reason': str(scrub(incident.get('reason', '')))[:240], 'next_ko': next_ko,
            'waiting_people': state if state in ('awaiting_user_test', 'awaiting_prd_approval', 'qa_failed', 'awaiting_merge', 'recovery_review') else None,
            # A summary only names the server-recorded root; it never invents one for standalone documents.
            'root_incident_id': root,
            'source': 'unified' if origin else 'native',
            'followups': len(data.get('followups') or []),
            **view, 'projection': 'local-read/v1'}


def actor(app, request, scope, *, creator=False):
    identities = app.state.identities
    authorization = request.headers.get('authorization')
    if not authorization and request.headers.get(local_operator.HEADER):
        try:
            return local_operator.verify(request, identities.operator_key())
        except H.SessionError as exc:
            raise H.http_error(exc) from None
    if authorization or not request.cookies.get(H.COOKIE):
        return identities.authenticate_creator(authorization) if creator else identities.authenticate(authorization, scope)
    return H.session_actor(request, app.state.human_sessions, scope,
        origin_allowed=lambda r, netloc: H.same_host(r.headers.get('host'))(netloc),
        still_authorized=identities.authorized_subject)


def incident_creator(app, request, body):
    try:
        return actor(app, request, 'incident:create', creator=True)
    except HTTPException as denied:
        authorization = request.headers.get('authorization')
        if (denied.status_code != 403 or not isinstance(denied.detail, dict)
                or denied.detail.get('code') != 'scope_forbidden' or not authorization
                or body.environment != 'dev' or body.service != DEV_RESERVATION
                or body.reproduction is not None or body.signal is None or body.signal.source != 'pod_log'
                or DEV_RESERVATION not in app.state.control.catalog.dev_only):
            raise
        observer = app.state.identities.runner(authorization)
        if observer.get('runner_role') != 'cluster':
            raise denied
        return observer


def local_read(app, request):
    try:
        key = app.state.local_read_key
        if not key.configured:
            raise H.SessionError(403, 'local_gateway_required')
        key.require(request.headers)
        H.check_browser_request(request.headers, H.same_host(request.headers.get('host')), mutation=False)
    except H.SessionError as exc:
        raise H.http_error(exc) from None


def install(app, identities, settings, clock=None):
    app.state.identities = identities
    app.state.human_sessions = H.HumanSessionStore(clock)
    app.state.local_read_key = H.local_read_key(settings.local_read_key_file)
    H.install_routes(app, '/api/application/v1/human-sessions',
        resolve=lambda: (app.state.human_sessions, identities, app.state.local_read_key),
        origin_allowed=lambda r, netloc: H.same_host(r.headers.get('host'))(netloc))
    install_redirect(app, settings.local_gateway_url, app.state.local_read_key)


def install_redirect(app, gateway_url, key, *, legacy_application_host=None):
    """배포자가 지정한 루프백 주소로 문서 탐색만 연결. API·기존 ingress·기계 경로는 리다이렉트하지 않는다."""
    if not gateway_url:
        return
    if legacy_application_host and not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*\.localhost', legacy_application_host):
        raise ValueError('legacy_application_host_invalid')
    parsed = urlsplit(gateway_url)
    if (parsed.scheme != 'http' or not (parsed.hostname in ('127.0.0.1', 'localhost', '::1') or
            (parsed.hostname or '').endswith('.localhost')) or parsed.port is None or
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('local_gateway_url_invalid')

    @app.middleware('http')
    async def redirect_document(request: Request, call_next):
        if (request.method in ('GET', 'HEAD') and not request.url.path.startswith('/api') and
                'text/html' in request.headers.get('accept', '') and not key.verify(request.headers)):
            destination = request.url.path
            if (legacy_application_host and request.url.hostname == legacy_application_host and
                    destination in ('/', '/index.html')):
                destination = '/application'
            return RedirectResponse(gateway_url.rstrip('/') + destination +
                ('?' + request.url.query if request.url.query else ''), status_code=307,
                headers={'Cache-Control': 'no-store'})
        return await call_next(request)
