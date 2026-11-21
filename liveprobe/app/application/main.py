"""독립 ASGI 진입점: uvicorn app.application.main:app."""
from datetime import date
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging
import threading
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError
from json import JSONDecodeError
from .settings import ApplicationSettings
from .storage import Store
from .security import Identities
from .control import ControlPlane
from .worker import AnalysisWorker
from .contracts import (CreateIncident, Claim, Lease, Report, Progress, IncidentResponse, ClaimResponse, Receipt,
                        UserTest, Approval, Supplement, ReadyResponse, CatalogResponse, EventsResponse, ReleaseResponse)

PREFIX = '/api/application/v1'
STATIC_DIR = Path(__file__).with_name('static')
SERVICE_SYSTEMS = {
    'order': 'samsung-trade', 'inventory': 'samsung-trade', 'catalog': 'samsung-trade',
    'payment': 'samsung-trade', 'trade-member': 'samsung-trade',
    'delivery': 'samsung-delivery', 'notification': 'samsung-delivery',
    'delivery-member': 'samsung-delivery', 'business-api': 'samsung-business',
}


def create_app(settings=None, *, provider=None, clock=None):
    settings = settings or ApplicationSettings.from_env()
    @asynccontextmanager
    async def lifespan(application):
        application.state.worker_stop = threading.Event()
        application.state.worker_thread = None
        application.state.display_thread = None
        application.state.db_retry_thread = None
        application.state.worker_failures = 0
        # 허용된 Application 전용 연결만 초기화한다. 실패해도 healthz는 유지하고 readyz는 폐쇄한다.
        await asyncio.to_thread(application.state.store.bootstrap)
        store = application.state.store
        if store.retry_delay() is not None:
            # 정돈-02: 초기화 재시도는 이 백그라운드 스레드만 한다. readyz 등 HTTP 조회는 초기화를 유발하지 않는다.
            def retry():
                while not application.state.worker_stop.is_set():
                    application.state.worker_stop.wait(max(0.2, store.retry_delay() or 0))
                    if application.state.worker_stop.is_set() or store.ensure_ready() or store.retry_delay() is None:
                        return
            application.state.db_retry_thread = threading.Thread(target=retry, name='application-db-retry', daemon=True)
            application.state.db_retry_thread.start()
        if settings.worker_enabled:
            from .display_readings import start_worker
            application.state.display_thread = start_worker(application.state.control, application.state.worker_stop,
                                                            AnalysisWorker(application.state.control, provider)._provider)
            def loop():
                while not application.state.worker_stop.is_set():
                    try:
                        application.state.worker.run_once()
                    except Exception:
                        application.state.worker_failures += 1
                        # Exception contents may contain transport/source material.
                        logging.getLogger(__name__).warning('application_analysis_loop_failed')
                    application.state.worker_stop.wait(.5)
            thread = threading.Thread(target=loop, name='application-analysis', daemon=True)
            application.state.worker_thread = thread
            thread.start()
        try:
            yield
        finally:
            application.state.worker_stop.set()
            if application.state.worker_thread is not None:
                await asyncio.to_thread(application.state.worker_thread.join)
            if application.state.display_thread is not None:
                await asyncio.to_thread(application.state.display_thread.join)
            if application.state.db_retry_thread is not None:
                await asyncio.to_thread(application.state.db_retry_thread.join)

    app = FastAPI(title='LiveProbe for Application', version='1.0', lifespan=lifespan)
    app.state.settings = settings
    app.state.store = Store(settings)
    # Bearer and session authorization must resolve the same active settings.
    identities = Identities(settings, settings_provider=lambda: app.state.settings)
    control = ControlPlane(app.state.store, settings, clock)
    control.human_authorized = identities.authorized_subject
    app.state.control = control
    app.state.worker = AnalysisWorker(control, provider)
    from .browser_access import install as install_browser_access, actor as browser_actor, local_read, summary
    install_browser_access(app, identities, settings, clock)
    from .merge_policy import install_merge_policy
    install_merge_policy(app, control, identities, settings, human_actor=lambda request, scope: browser_actor(app, request, scope))

    @app.exception_handler(HTTPException)
    async def handle_http(request, exc):
        content = exc.detail if isinstance(exc.detail, dict) else {'code': 'request_rejected', 'message_ko': '요청이 거부되었습니다.'}
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(JSONDecodeError)
    async def handle_validation(request, exc):
        return JSONResponse(status_code=422, content={'code': 'request_invalid', 'message_ko': '요청 형식을 확인하십시오.'})

    @app.exception_handler(SQLAlchemyError)
    async def handle_storage(request, exc):
        return JSONResponse(status_code=503, content={'code': 'storage_unavailable', 'message_ko': '독립 저장소를 사용할 수 없습니다.'})

    @app.get(PREFIX + '/healthz')
    def health():
        return {'ok': True, 'product': 'liveprobe-application'}

    @app.get(PREFIX + '/readyz', response_model=ReadyResponse)
    def ready():
        """정돈-02/07: 읽기 전용 확인. DB 초기화·재시도를 하지 않고(lifespan 스레드 전담) 쓰기 잠금도 잡지 않는다.

        `checks.model`은 '실행 구성됨'(활성화·홈 경로·실행 파일 존재)만 뜻한다. 인증·호출 성공은 `model_status`에
        저장된 최근 역할 실행 기록으로 따로 표시하며, 기록이 없으면 '호출 성공 미관측'으로 둔다."""
        from .model_worker import cli_configured, observed
        database = app.state.store.ready()
        configured = bool(not settings.testing and cli_configured(settings))
        if settings.external_model_worker and database:
            configured = observed(app.state.store, control.clock())
        checks = {'database': database, 'human_identity': identities.configured(),
                  'machine_identity': identities.configured(machine=True),
                  'profiles': control.catalog.configured, 'runner': control.runner_recent(), 'model': configured}
        is_ready = all(checks.values())
        return JSONResponse(status_code=200 if is_ready else 503, content={
            'ready': is_ready, 'mode': 'isolated_test' if settings.testing else 'runtime',
            'code': None if is_ready else 'provisioning_required',
            'message_ko': None if is_ready else '독립 저장소·인증·실행기·모델 실행 구성을 확인해야 합니다.',
            'checks': checks, 'model_status': control.model_status(configured) if database else
            {'state': 'unknown', 'configured': configured, 'last_success_at': None, 'last_failure_at': None,
             'last_failure_code': None},
            'waiting_people': control.waiting_people() if database else None})

    @app.get(PREFIX + '/catalog', response_model=CatalogResponse)
    def catalog():
        return control.catalog.public()

    def human_create(request: Request):
        return browser_actor(app, request, 'incident:create', creator=True)

    def tester(request: Request):
        return browser_actor(app, request, 'tester')

    def approver(request: Request):
        return browser_actor(app, request, 'prd-approver')

    @app.post(PREFIX + '/incidents', response_model=IncidentResponse, status_code=201)
    def create_incident(body: CreateIncident, actor=Depends(human_create)):
        # The combined product supplies canonical intake; standalone API stays compatible.
        creator = getattr(app.state, 'incident_creator', None)
        return creator(body, actor) if callable(creator) else control.create(body, actor)

    @app.get(PREFIX + '/summaries')
    @app.get(PREFIX + '/incidents')
    def list_incidents(request: Request, environment: str | None = None, service: str | None = None, system: str | None = None,
                       state: str | None = None, group: str | None = None, work: Literal['approval', 'qa_fix', 'recovery'] | None = None,
                       search: str | None = None,
                       start: date | None = None, end: date | None = None,
                       limit: int = 50, offset: int = 0, as_of: float | None = None,
                       authorization: str | None = Header(default=None)):
        projected = request.url.path.endswith('/summaries')
        if projected:
            local_read(app, request)
        else:
            browser_actor(app, request, 'incident:read')
        # 정돈-04: 전체 이력에 서버 필터를 적용한 뒤 같은 조건으로 총건수·페이지를 계산한다.
        if not 1 <= limit <= 200 or offset < 0 or (as_of is not None and as_of < 0):
            raise HTTPException(422, {'code': 'list_page_invalid', 'message_ko': '페이지 범위를 확인하십시오.'})
        if group is not None and group not in control.LIST_STATE_GROUPS:
            raise HTTPException(422, {'code': 'list_group_invalid', 'message_ko': '상태 묶음 값을 확인하십시오.'})
        if any(v is not None and len(v) > 120 for v in (environment, service, system, state, search)):
            raise HTTPException(422, {'code': 'list_filter_invalid', 'message_ko': '검색 조건이 너무 깁니다.'})
        from ..history_dates import bounds
        try:
            lower, upper = bounds(start, end)
        except ValueError as exc:
            raise HTTPException(422, {'code': 'list_dates_invalid', 'message_ko': str(exc)}) from exc
        result = control.list_incidents(lower=lower, upper=upper, environment=environment, service=service, system=system, state=state, group=group, work=work,
                                       search=search, limit=limit, offset=offset, as_of=as_of)
        if projected:
            result['incidents'] = [summary(i) for i in result['incidents']]
        return result

    @app.get(PREFIX + '/incidents/{incident_id}/summary')
    def incident_summary(incident_id: str, request: Request):
        local_read(app, request)
        document = control.operator_detail(incident_id)
        result = summary(document)
        coordinator = getattr(app.state, 'unified_coordinator', None)
        if coordinator and result.get('root_incident_id'):
            result['root_incident_id'] = coordinator.canonical_root(document)
            result['source'] = 'unified' if result['root_incident_id'] else 'native'
        return result

    @app.get(PREFIX + '/incidents/{incident_id}', response_model=IncidentResponse)
    def detail(incident_id: str, request: Request, view: str | None = None):
        browser_actor(app, request, 'incident:read')
        document = control.operator_detail(incident_id)
        if view == 'stage':
            from .stage_readings import stage_document
            return stage_document(document)
        return document

    @app.post(PREFIX + '/incidents/{incident_id}/review-dev-metadata')
    def review_dev_metadata(incident_id: str, authorization: str | None = Header(default=None)):
        actor = identities.runner(authorization)
        if control.requires_runner_roles(control.detail(incident_id)) and actor.get('runner_role') != 'cluster':
            from .safety import fail
            fail(403, 'runner_job_kind_forbidden')
        return control.review_dev_metadata(incident_id)

    @app.get(PREFIX + '/incidents/{incident_id}/events', response_model=EventsResponse)
    def events(incident_id: str, request: Request, after: int = 0):
        browser_actor(app, request, 'incident:read')
        return {'events': control.events(incident_id, after)}

    from .accountability import routes as accountability_routes
    accountability_routes(app,control,browser_actor,PREFIX)
    from .current_version import routes as current_version_routes
    current_version_routes(app, control, browser_actor, PREFIX)

    @app.get(PREFIX + '/incidents/{incident_id}/activity')
    def work_activity(incident_id: str, request: Request, after: int = 0):
        browser_actor(app, request, 'incident:read')
        from .activity import activity
        return activity(control,incident_id,max(0,after))

    @app.get(PREFIX + '/incidents/{incident_id}/patch-change')
    def patch_change(incident_id: str, path: str, digest: str, request: Request):
        browser_actor(app, request, 'incident:read')
        from .safety import fail, safe_source_path
        from .stage_readings import file_excerpt
        patch = (control.detail(incident_id).get('data') or {}).get('patch') or {}
        if not safe_source_path(path) or not digest or patch.get('diff_sha256') != digest:
            fail(404, 'file_change_not_observed')
        return file_excerpt(patch.get('diff') or '', path)

    @app.get(PREFIX + '/incidents/{incident_id}/activity/{event_id}/change')
    def work_change(incident_id: str, event_id: int, request: Request):
        browser_actor(app,request,'incident:read')
        from .activity import change_for_event
        return change_for_event(control,incident_id,event_id)

    @app.post(PREFIX + '/runner/jobs/{job_id}/progress')
    def work_progress(job_id: str, body: Progress, authorization: str | None = Header(default=None)):
        from .activity import progress
        actor=identities.runner(authorization)
        if job_id.startswith("probe_"):
            from ..source_investigation import lease
            return lease(control,job_id,body,actor,"progress")
        return progress(control,job_id,body,actor)

    @app.post(PREFIX + '/runner/claim', response_model=ClaimResponse)
    def claim(body: Claim, authorization: str | None = Header(default=None)):
        actor = identities.runner(authorization)
        if 'investigate' in body.capabilities:
            from ..source_investigation import claim as claim_inquiry
            inquiry=claim_inquiry(control,actor)
            if inquiry: return {'job':inquiry}
        return control.claim(body, actor)

    @app.post(PREFIX + '/runner/jobs/{job_id}/heartbeat')
    def heartbeat(job_id: str, body: Lease, authorization: str | None = Header(default=None)):
        actor = identities.runner(authorization)
        if job_id.startswith('probe_'):
            from ..source_investigation import lease
            return lease(control,job_id,body,actor,'heartbeat')
        return control.heartbeat(job_id, body, actor)

    @app.post(PREFIX + '/runner/jobs/{job_id}/result', response_model=Receipt)
    def report(job_id: str, body: Report, authorization: str | None = Header(default=None)):
        actor = identities.runner(authorization)
        if job_id.startswith('probe_'):
            from ..source_investigation import lease
            return lease(control,job_id,body,actor,'result')
        return control.report(job_id, body, actor)

    @app.get(PREFIX + '/incidents/{incident_id}/release', response_model=ReleaseResponse)
    def release(incident_id: str, request: Request):
        browser_actor(app, request, 'incident:read')
        return control.release_get(incident_id)

    @app.post(PREFIX + '/incidents/{incident_id}/user-test')
    def user_test(incident_id: str, body: UserTest, actor=Depends(tester)):
        return control.user_test(incident_id, body, actor)

    @app.post(PREFIX + '/incidents/{incident_id}/approve-prd')
    def approve(incident_id: str, body: Approval, request: Request, actor=Depends(approver)):
        from ..human_sessions import COOKIE
        def apply():
            return control.approve(incident_id, body, browser_actor(app, request, 'prd-approver'))
        if request.headers.get('authorization'):
            return apply()
        return app.state.human_sessions.once(request.cookies.get(COOKIE), apply)

    def operator(request: Request):
        return browser_actor(app, request, 'incident:create')

    @app.post(PREFIX + '/incidents/{incident_id}/supplements')
    def supplement(incident_id: str, body: Supplement, request: Request, actor=Depends(operator)):
        """Operator revision/QA rerun on an open incident; a read-only follow-up note on a closed one."""
        from ..human_sessions import COOKIE
        def apply():
            return control.supplement(incident_id, body, operator(request))
        if request.headers.get('authorization'):
            return apply()
        return app.state.human_sessions.once(request.cookies.get(COOKIE), apply)

    if STATIC_DIR.is_dir():
        app.mount('/', StaticFiles(directory=STATIC_DIR, html=True), name='application-ui')
    return app


app = create_app()
