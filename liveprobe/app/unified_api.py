"""Same-origin unified room endpoints. Human scopes never borrow local operator authority."""
from __future__ import annotations

import os
from datetime import date
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import Field
from .auth import require_user
from .application.contracts import CreateIncident, UserTest, Approval
from .application.merge_policy import MergeReview
from .application.security import Identities
from .unified import BLOCK_KO, reject


class UnifiedCreate(CreateIncident):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')


def unified_router(state):
    router = APIRouter(dependencies=[Depends(require_user)])

    def backend():
        service = state.get('unified')
        if service is None:
            reject('feature_disabled', 503)
        return service

    def human(authorization, scope):
        service = backend()
        actor = Identities(service.application.state.settings).authenticate(authorization, scope)
        return service, actor

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
    def connect(iid: int, authorization: str | None = Header(default=None)):
        service, actor = human(authorization, 'incident:create')
        return service.continue_incident(iid, actor)

    @router.get('/api/incidents/{iid}/code-action/detail')
    def detail(iid: int, authorization: str | None = Header(default=None)):
        service, _ = human(authorization, 'incident:read')
        return service.detail(iid)

    def child(service, iid):
        summary = service.summary(iid)
        if not summary['linked']:
            reject('code_action_not_found', 404)
        return summary['application_id']

    @router.post('/api/incidents/{iid}/code-action/user-test')
    def test(iid: int, body: UserTest, authorization: str | None = Header(default=None)):
        service, actor = human(authorization, 'tester')
        return service.control.user_test(child(service, iid), body, actor)

    @router.post('/api/incidents/{iid}/code-action/approve-prd')
    def approve(iid: int, body: Approval, authorization: str | None = Header(default=None)):
        service, actor = human(authorization, 'prd-approver')
        return service.control.approve(child(service, iid), body, actor)

    # MergeReview is owned by the independently authenticated release gate module.
    @router.post('/api/incidents/{iid}/code-action/review-merge')
    def merge(iid: int, body: MergeReview, authorization: str | None = Header(default=None)):
        service, actor = human(authorization, 'prd-approver')
        return service.control.review_merge(child(service, iid), body, actor)

    @router.post('/api/unified/incidents', status_code=201)
    def create(body: UnifiedCreate, authorization: str | None = Header(default=None)):
        service, actor = human(authorization, 'incident:create')
        request = CreateIncident.model_validate(body.model_dump(exclude={'request_id'}))
        return service.create(request, body.request_id, actor)

    @router.get('/api/unified/catalog')
    def catalog():
        service = state.get('unified')
        if service is None:
            return {'enabled': False, 'services': [], 'systems': [], 'environments': ['dev', 'prd'],
                    'historical_environments': ['dev', 'stg', 'prd'], 'profiles_configured': False}
        return {'enabled': True, **service.control.catalog.public()}

    @router.get('/api/unified/history')
    def history(environment: str | None = None, service: str | None = None, system: str | None = None,
                state_filter: str | None = Query(default=None, alias='state'), group: str | None = None,
                search: str | None = None, start: date | None = None, end: date | None = None,
                limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                as_of: float | None = Query(default=None, ge=0), authorization: str | None = Header(default=None)):
        coordinator, _ = human(authorization, 'incident:read')
        if group is not None and group not in coordinator.control.LIST_STATE_GROUPS:
            reject('list_group_invalid', 422)
        if any(v is not None and len(v) > 120 for v in (environment, service, system, state_filter, search)):
            reject('list_filter_invalid', 422)
        from .history_dates import bounds
        try:
            lower, upper = bounds(start, end)
        except ValueError:
            reject('list_dates_invalid', 422)
        result = coordinator.control.list_incidents(environment=environment, service=service, system=system,
            state=state_filter, group=group, search=search, lower=lower, upper=upper,
            limit=limit, offset=offset, as_of=as_of)
        from sqlalchemy import select
        from .models import CodeActionLink
        ids = [i['id'] for i in result['incidents']]
        with coordinator.sessions() as session:
            roots = {link.application_id: link.incident_id for link in session.scalars(
                select(CodeActionLink).where(CodeActionLink.application_id.in_(ids)))}
        for item in result['incidents']:
            item['root_incident_id'] = roots.get(item['id'])
            item['source'] = 'application'
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
    def create_root(body, actor):
        from .application.safety import sha
        result = state['unified'].create(body, sha(body.model_dump(mode='json')), actor)
        return state['unified'].control.detail(result['code_action']['application_id'])
    application.state.incident_creator = create_root
