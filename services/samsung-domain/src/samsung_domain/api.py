"""8080 공개 표면과 8443 TLS 원장을 서로 다른 ASGI 앱으로 분리한다."""
import hmac
from pathlib import Path
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.encoders import jsonable_encoder
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field

from .config import DISPLAY_NAMES
from .store import DomainError, visible


class MemberInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=40)


class LedgerInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    name: str | None = None
    title: str | None = None
    department: str | None = None
    employee_id: str | None = None
    status: str | None = None
    target_systems: list[str] | None = None
    active: bool | None = None
    content: str | None = Field(default=None, max_length=1048576)


def create_apps(settings, store, business_client=None):
    public = FastAPI(title='삼성 공개 서비스', docs_url=None, redoc_url=None, openapi_url=None)
    protected = FastAPI(title='삼성 TLS 원장', docs_url=None, redoc_url=None, openapi_url=None)

    async def domain_error(_request, exc):
        return JSONResponse({'error': exc.message}, status_code=exc.status)

    async def validation_error(_request, _exc):
        return JSONResponse({'error': '입력 형식 또는 필수 필드를 확인해 주세요'}, status_code=422)

    async def dependency_error(_request, _exc):
        return JSONResponse({'error': '의존 서비스가 준비되지 않았습니다'}, status_code=503)

    for app in (public, protected):
        app.add_exception_handler(DomainError, domain_error)
        app.add_exception_handler(RequestValidationError, validation_error)
        app.add_exception_handler(DatabaseError, dependency_error)
        app.add_exception_handler(PoolTimeout, dependency_error)

    @protected.middleware('http')
    async def tls_only(request, call_next):
        if request.scope['scheme'] != 'https':
            return JSONResponse({'error': 'TLS 보호 연결만 허용합니다'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def authorize(request: Request):
        supplied = request.headers.get('authorization', '')
        principal = None
        for candidate in settings.principals:
            if hmac.compare_digest(supplied.encode(), ('Bearer ' + candidate['token']).encode()):
                principal = candidate
        if principal is None:
            raise DomainError(401, '서비스 인증이 필요합니다')
        if principal['environment'] != settings.environment:
            raise DomainError(403, '다른 환경에는 접근할 수 없습니다')
        if settings.mode != 'business' and (principal['target_system'] != settings.system or principal['role'] != f'{settings.mode}-reader'):
            raise DomainError(403, '자기 조직의 읽기 주체만 허용합니다')
        for key, expected in (('environment', settings.environment), ('target_system', principal['target_system'])):
            if key in request.query_params and request.query_params[key] != expected:
                raise DomainError(403, '요청으로 주체 범위를 변경할 수 없습니다')
        return principal

    def writable(principal):
        if settings.mode != 'business' or principal['role'] != 'business-admin':
            raise DomainError(403, '원장 변경은 비즈니스 관리자만 허용합니다')
        if settings.read_only:
            raise DomainError(503, '읽기 전용 안전 모드입니다')

    @public.get('/healthz/live')
    def live():
        return {'alive': True}

    @public.get('/healthz/ready')
    def ready():
        return {'ready': store.ready()}

    @public.get('/api/info')
    def info():
        return dict(system=settings.system, service=settings.service, environment=settings.environment,
            display_name=DISPLAY_NAMES[settings.mode], version=settings.version, synthetic=True)

    @public.get('/api/status')
    def status():
        return jsonable_encoder(dict(read_only=settings.read_only, db_ready=store.ready(), sync=store.sync_status(),
            synthetic=True, sensitive_actions='비즈니스 원장 최종 확인 필요'))

    @public.get('/metrics', response_class=PlainTextResponse)
    def metrics():
        sync = store.sync_status()
        result = ['# TYPE samsung_domain_db_ready gauge', f'samsung_domain_db_ready {int(store.ready())}',
            '# TYPE samsung_domain_read_only gauge', f'samsung_domain_read_only {int(settings.read_only)}',
            '# TYPE samsung_domain_sync_fresh gauge', f'samsung_domain_sync_fresh {int(sync["state"] == "fresh")}',
            '# TYPE samsung_domain_quarantine_unresolved gauge', f'samsung_domain_quarantine_unresolved {sync.get("unresolved_quarantine", 0)}',
            '# TYPE samsung_domain_outbox_pending gauge', f'samsung_domain_outbox_pending {sync.get("pending_outbox", 0)}']
        return '\n'.join(result) + '\n'

    @public.get('/', response_class=HTMLResponse)
    def home():
        return Path(__file__).with_name('index.html').read_text()

    if settings.mode != 'business':
        @public.get('/api/members')
        def members(search: str = ''):
            return store.members(search)

        @public.post('/api/members', status_code=201)
        def create_member(body: MemberInput):
            if settings.read_only:
                raise DomainError(503, '읽기 전용 안전 모드에서는 회원을 생성할 수 없습니다')
            return store.create_member(body.name)

        @public.post('/api/login')
        def demo_login(body: MemberInput):
            member = next((m for m in store.members(body.name)['members'] if m['name'] == body.name), None)
            if not member:
                raise DomainError(404, '가입된 합성 고객이 없습니다')
            return dict(member=member, displayName=member['name'], message='합성 예제 확인이며 실제 인증이나 원장 권한을 부여하지 않습니다')

        @public.get('/api/members/{member_id}/profile')
        def profile(member_id: str):
            member = next((m for m in store.members()['members'] if m['id'] == member_id), None)
            if not member:
                raise DomainError(404, '고객 회원이 없습니다')
            return member

    @protected.get('/api/v1/sync/status')
    def sync_status(principal=Depends(authorize)):
        return jsonable_encoder(store.sync_status())

    def list_rows(kind, principal):
        rows = store.list_entities(kind)
        if settings.mode == 'business' and principal['role'] != 'business-admin':
            rows = [{k: v for k, v in row.items() if k != 'target_systems'} for row in rows
                if principal['target_system'] in row['target_systems'] and visible(kind, row)]
        return rows

    @protected.get('/api/v1/employees')
    def employees(principal=Depends(authorize)):
        rows = list_rows('employee', principal)
        return {'employees': rows, 'total': len(rows)}

    @protected.get('/api/v1/contracts')
    def contracts(principal=Depends(authorize)):
        rows = list_rows('contract', principal)
        return {'contracts': rows, 'total': len(rows)}

    @protected.get('/api/v1/employees/{entity_id}')
    def employee(entity_id: str, principal=Depends(authorize)):
        row = next((row for row in list_rows('employee', principal) if row['id'] == entity_id), None)
        if row is None:
            raise DomainError(404, '허용된 직원이 없습니다')
        return row

    @protected.get('/api/v1/contracts/{entity_id}')
    def contract(entity_id: str, principal=Depends(authorize)):
        row = next((row for row in list_rows('contract', principal) if row['id'] == entity_id), None)
        if row is None:
            raise DomainError(404, '허용된 계약이 없습니다')
        return row

    def write(kind, body, principal, entity_id=None):
        writable(principal)
        values = body.model_dump(exclude_unset=True)
        supplied_id = values.pop('id', None)
        expected = values.pop('expected_version', None)
        content = values.pop('content', None)
        if entity_id and supplied_id is not None:
            raise DomainError(422, '변경 요청에 식별자를 중복 지정할 수 없습니다')
        if not entity_id and expected is not None:
            raise DomainError(422, '생성 요청에 expected_version을 지정할 수 없습니다')
        return store.write(kind, entity_id or supplied_id or str(uuid.uuid4()), values,
            create=entity_id is None, expected_version=expected, content=content)

    @protected.post('/api/v1/employees', status_code=201)
    def create_employee(body: LedgerInput, principal=Depends(authorize)):
        return write('employee', body, principal)

    @protected.patch('/api/v1/employees/{entity_id}')
    def update_employee(entity_id: str, body: LedgerInput, principal=Depends(authorize)):
        return write('employee', body, principal, entity_id)

    @protected.delete('/api/v1/employees/{entity_id}')
    def delete_employee(entity_id: str, expected_version: int, principal=Depends(authorize)):
        writable(principal)
        return store.write('employee', entity_id, {'active': False}, expected_version=expected_version)

    @protected.post('/api/v1/contracts', status_code=201)
    def create_contract(body: LedgerInput, principal=Depends(authorize)):
        return write('contract', body, principal)

    @protected.patch('/api/v1/contracts/{entity_id}')
    def update_contract(entity_id: str, body: LedgerInput, principal=Depends(authorize)):
        return write('contract', body, principal, entity_id)

    @protected.delete('/api/v1/contracts/{entity_id}')
    def delete_contract(entity_id: str, expected_version: int, principal=Depends(authorize)):
        writable(principal)
        return store.write('contract', entity_id, {'active': False, 'status': 'revoked'}, expected_version=expected_version)

    def sensitive(kind, entity_id, principal, content=False):
        if settings.mode == 'business':
            return store.confirm(kind, entity_id, principal['target_system'], include_content=content)
        if store.sync_status()['state'] != 'fresh' or business_client is None:
            raise DomainError(503, '민감 동작은 동기화와 원장 확인이 모두 필요합니다')
        # 복제본의 허용 결과를 사용하지 않고 원장의 현행 범위 검사를 매번 받는다.
        suffix = 'content' if content else 'verify'
        return business_client.get(f'/api/v1/{"employees" if kind == "employee" else "contracts"}/{entity_id}/{suffix}')

    @protected.get('/api/v1/contracts/{entity_id}/content')
    def contract_content(entity_id: str, principal=Depends(authorize)):
        return sensitive('contract', entity_id, principal, True)

    @protected.get('/api/v1/contracts/{entity_id}/verify')
    def contract_verify(entity_id: str, principal=Depends(authorize)):
        return sensitive('contract', entity_id, principal)

    @protected.get('/api/v1/employees/{entity_id}/verify')
    def employee_verify(entity_id: str, principal=Depends(authorize)):
        return sensitive('employee', entity_id, principal)

    @protected.post('/api/v1/sync/exports', status_code=201)
    async def create_export(request: Request, principal=Depends(authorize)):
        if settings.mode != 'business' or principal['target_system'] is None:
            raise DomainError(403, '자기 조직 reader만 export할 수 있습니다')
        import json
        raw = await request.body()
        if len(raw) > 4096:
            raise DomainError(422, 'export 요청 크기 제한을 초과했습니다')
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise DomainError(422, 'export 요청 형식이 올바르지 않습니다') from None
        if not isinstance(body, dict) or set(body) - {'environment', 'target_system'}:
            raise DomainError(422, 'export 요청 필드를 확인해 주세요')
        if body.get('environment', settings.environment) != settings.environment or body.get('target_system', principal['target_system']) != principal['target_system']:
            raise DomainError(403, '본문으로 주체 범위를 변경할 수 없습니다')
        from starlette.concurrency import run_in_threadpool
        export = await run_in_threadpool(store.create_export, principal['target_system'])
        return await run_in_threadpool(store.export_page, export['export_id'], principal['target_system'])

    @protected.get('/api/v1/sync/exports/{export_id}')
    def get_export(export_id: str, page: int = 0, principal=Depends(authorize)):
        if settings.mode != 'business' or principal['target_system'] is None:
            raise DomainError(403, '자기 조직 reader만 export할 수 있습니다')
        return store.export_page(export_id, principal['target_system'], page)

    return public, protected
