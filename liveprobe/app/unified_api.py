"""Same-origin unified room endpoints. Human scopes never borrow local operator authority."""
from __future__ import annotations

import os
from typing import Literal
from datetime import date
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import Field
from .auth import require_user, host_allowed
from .config import get_settings
from .application.contracts import CreateIncident, UserTest, Approval, Supplement
from .application.merge_policy import MergeReview
from .application.security import Identities
from .unified import BLOCK_KO, reject


class UnifiedCreate(CreateIncident):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')


def unified_router(state):
    def entry(request: Request, settings=Depends(get_settings)):
        # Every mutating unified handler resolves its actual human/session identity below.
        # Dedicated bearer clients and the preserved CLI session endpoints do not become local operators.
        if (request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.headers.get('x-liveprobe-operator-proof')
                and (request.headers.get('authorization') or request.cookies.get('lp_human_session')
                     or request.url.path.startswith('/api/human-sessions'))):
            if not host_allowed(settings, request.headers.get('host')):
                raise HTTPException(403, 'host not allowed')
            try:
                H.local_read_key(os.getenv('LIVEPROBE_LOCAL_READ_KEY_FILE')).require(request.headers)
                H.check_browser_request(request.headers, H.same_host(request.headers.get('host')), mutation=True)
            except H.SessionError as exc:
                raise H.http_error(exc) from None
            return
        require_user(request, settings)

    router = APIRouter(dependencies=[Depends(entry)])

    def backend():
        service = state.get('unified')
        if service is None:
            reject('feature_disabled', 503)
        return service

    from .application.browser_access import actor as browser_actor, summary as project_summary, local_read
    from . import human_sessions as H

    def resolve_session():
        application = backend().application
        return application.state.human_sessions, application.state.identities, H.local_read_key(os.getenv('LIVEPROBE_LOCAL_READ_KEY_FILE'))

    H.install_routes(router, '/api/human-sessions', resolve=resolve_session,
                     origin_allowed=lambda r, netloc: H.same_host(r.headers.get('host'))(netloc))

    def human(request, authorization, scope):
        service = backend()
        return service, browser_actor(service.application, request, scope)

    @router.get('/api/incidents/{iid}/code-action')
    def summary(iid: int):
        service = state.get('unified')
        if service is None:
            return {'enabled': False, 'linked': False, 'incident_id': iid, 'action_id': None,
                    'application_id': None, 'state': 'disabled', 'phase': 'kubernetes',
                    'state_ko': '통합 미구성', 'summary_ko': BLOCK_KO['feature_disabled'],
                    'next_ko': BLOCK_KO['feature_disabled'], 'runner_observed': None,
                    'observed_at': None, 'eligible': False, 'blocked_code': 'feature_disabled'}
        return service.summary(iid)

    @router.post('/api/incidents/{iid}/code-action')
    def connect(iid: int, *, request: Request, authorization: str | None = Header(default=None)):
        service, actor = human(request, authorization, 'incident:create')
        return service.continue_incident(iid, actor)

    @router.get('/api/incidents/{iid}/code-action/detail')
    def detail(iid: int, *, request: Request, authorization: str | None = Header(default=None)):
        service, _ = human(request, authorization, 'incident:read')
        return service.detail(iid)

    def child(service, iid):
        return service.linked_document(iid)['id']

    @router.post('/api/incidents/{iid}/code-action/supplements')
    def supplement(iid: int, body: Supplement, request: Request):
        def apply():
            service, actor = human(request, request.headers.get('authorization'), 'incident:create')
            return service.control.supplement(child(service, iid), body, actor)
        if request.headers.get('authorization'):
            return apply()
        return backend().application.state.human_sessions.once(request.cookies.get(H.COOKIE), apply)

    @router.post('/api/incidents/{iid}/code-action/user-test')
    def test(iid: int, body: UserTest, *, request: Request, authorization: str | None = Header(default=None)):
        service, actor = human(request, authorization, 'tester')
        return service.control.user_test(child(service, iid), body, actor)

    @router.post('/api/incidents/{iid}/code-action/approve-prd')
    def approve(iid: int, body: Approval, *, request: Request, authorization: str | None = Header(default=None)):
        def apply():
            service, actor = human(request, authorization, 'prd-approver')
            service.control.require_active_environment('prd')
            return service.control.approve(child(service, iid), body, actor)
        if authorization:
            return apply()
        return backend().application.state.human_sessions.once(request.cookies.get(H.COOKIE), apply)

    # MergeReview is owned by the independently authenticated release gate module.
    @router.post('/api/incidents/{iid}/code-action/review-merge')
    def merge(iid: int, body: MergeReview, *, request: Request, authorization: str | None = Header(default=None)):
        service, actor = human(request, authorization, 'prd-approver')
        return service.control.review_merge(child(service, iid), body, actor)

    @router.post('/api/unified/incidents', status_code=201)
    def create(body: UnifiedCreate, *, request: Request, authorization: str | None = Header(default=None)):
        service, actor = human(request, authorization, 'incident:create')
        request = CreateIncident.model_validate(body.model_dump(exclude={'request_id'}))
        return service.create(request, body.request_id, actor)

    @router.get('/api/unified/catalog')
    def catalog():
        service = state.get('unified')
        if service is None:
            return {'enabled': False, 'services': [], 'systems': [], 'environments': ['dev', 'prd'],
                    'historical_environments': ['dev', 'stg', 'prd'], 'profiles_configured': False}
        return {'enabled': True, **service.control.public_catalog()}

    @router.get('/api/unified/summaries')
    @router.get('/api/unified/history')
    def history(environment: str | None = None, service: str | None = None, system: str | None = None,
                state_filter: str | None = Query(default=None, alias='state'), group: str | None = None,
                work: Literal['approval', 'qa_fix', 'recovery'] | None = None,
                search: str | None = None, start: date | None = None, end: date | None = None,
                limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                as_of: float | None = Query(default=None, ge=0), *, request: Request, authorization: str | None = Header(default=None)):
        coordinator = backend()
        projected = request.url.path.endswith('/summaries')
        if projected:
            _, _, key = resolve_session()
            try:
                if not key.configured:
                    raise H.SessionError(403, 'local_gateway_required')
                key.require(request.headers)
            except H.SessionError as exc:
                raise H.http_error(exc) from None
        else:
            human(request, authorization, 'incident:read')
        live = projected and group == 'live'
        if group is not None and group not in coordinator.control.LIST_STATE_GROUPS and not live:
            reject('list_group_invalid', 422)
        if any(v is not None and len(v) > 120 for v in (environment, service, system, state_filter, search)):
            reject('list_filter_invalid', 422)
        from .history_dates import bounds
        try:
            lower, upper = bounds(start, end)
        except ValueError:
            reject('list_dates_invalid', 422)
        if live:
            if any(value is not None for value in (environment, service, system, state_filter, work, search, lower, upper)):
                reject('list_filter_invalid', 422)
            result = coordinator.control.list_live_incidents(limit=limit, offset=offset, as_of=as_of)
        else:
            result = coordinator.control.list_incidents(environment=environment, service=service, system=system,
                state=state_filter, group=group, work=work, search=search, lower=lower, upper=upper,
                limit=limit, offset=offset, as_of=as_of, summary_only=projected)
        from sqlalchemy import select
        from .models import CodeActionLink
        ids = [i['id'] for i in result['incidents']]
        roots = {}
        if ids:
            with coordinator.sessions() as session:
                documents = {item['id']: item for item in result['incidents']}
                roots = {link.application_id: link.incident_id for link in session.scalars(
                    select(CodeActionLink).where(CodeActionLink.application_id.in_(ids), CodeActionLink.state == 'linked'))
                    if coordinator.matches_origin(link, documents[link.application_id])}
        for item in result['incidents']:
            item['root_incident_id'] = roots.get(item['id'])
            item['source'] = 'unified' if item['root_incident_id'] else 'native'
        if live:
            from .application.safety import scrub

            waiting = coordinator.control.LIST_STATE_GROUPS['waiting_people']
            result['incidents'] = [{**{k: item.get(k) for k in ('id', 'state', 'environment', 'service', 'system', 'root_incident_id')},
                                    'reason': str(scrub(item.get('reason', '')))[:240],
                                    'live_group': 'waiting_people' if item['state'] in waiting else 'automatic'}
                                   for item in result['incidents']]
        elif projected:
            result['incidents'] = [{**project_summary(item), 'root_incident_id': item['root_incident_id'], 'source': item['source']}
                                   for item in result['incidents']]
        else:
            from .application.qa_release import projection
            for item in result['incidents']:
                item.update(projection(item, item['state']))
        return result

    return router


def prepare_unified(state):
    """No DB connection, secret read, model call, or worker startup at import time."""
    if os.getenv('LIVEPROBE_UNIFIED_ENABLED') != 'true':
        return
    from .application.main import create_app
    from .unified import configure
    application = create_app()
    state['application'] = application
    state['unified'] = configure(application, os.getenv('LIVEPROBE_INSTALLATION_ID'))
    state['unified'].mixed_engine = lambda: state.get('engine')
    def create_root(body, actor):
        from .application.safety import sha
        result = state['unified'].create(body, sha(body.model_dump(mode='json')), actor)
        return state['unified'].control.detail(result['code_action']['application_id'])
    application.state.incident_creator = create_root
