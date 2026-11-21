"""FastAPI application. Shapes follow .hermes/plans/KAN-34-api-contract.md exactly."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import OperationalError, InterfaceError
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from .observation_sources import adapter_url, bind_source_settings
from . import db
from .auth import require_user, require_webhook
from .config import get_app_config, get_settings
from .incidents import Signal
from .models import (INCIDENT_CLOSED_STATES, Action, AgentRun, AuditLog, Citation, CodeActionLink, Evidence, Incident, IncidentEvent,
                     RagChunk, RoutingDecision, Setting)
from .model_settings import ModelSettingsIn, RevisionConflict, read_settings, save_settings
from .lifecycle import transition, update_run
from .redact import redact_obj
from . import safety
from .safety import POLICY_MODES
from .unified import effective_status_expr, projection_rows, apply_projection, project_items, append_code_flow
from .sse import broker

log = logging.getLogger("liveprobe")

STATE: dict[str, Any] = {"engine": None, "watchers": None, "kube": None, "ready": False, "rag_chunks": 0,
                         "flows": None, "collector": None, "background_requested": False}
rag_ingest_stop = threading.Event()


def _start_background() -> None:
    """Heavy init (embedding model, kube client) + threads, off the request path."""
    settings, cfg = get_settings(), get_app_config()
    from .engine import Engine
    from .k8s import Kube
    from .llm import make_provider
    from .rag import RAG, FastEmbedder, index_runbooks
    from .rag_ingest import index_documents, load_public_documents, closed_incident_documents
    from .watcher import Watchers

    rag = None
    try:
        emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
        with db.session() as s:
            new = index_runbooks(s, emb, settings.runbooks_dir)
            STATE["rag_chunks"] = s.scalar(select(func.count()).select_from(RagChunk)
                                            .where(RagChunk.retired_at.is_(None)))
        log.info("RAG runbooks ready: %s new chunks, %s active", new, STATE["rag_chunks"])
        rag = RAG(emb)
        try:
            with db.session() as s:
                result = index_documents(s, emb, load_public_documents())
                STATE["rag_chunks"] = s.scalar(select(func.count()).select_from(RagChunk)
                                                .where(RagChunk.retired_at.is_(None)))
            log.info("RAG licensed public corpus ready: %s documents, %s new chunks", result.documents,
                     result.new_chunks)
        except Exception as e:  # noqa: BLE001
            log.error("RAG public corpus rejected; internal runbooks remain available: %s", type(e).__name__)

        def ingest_closed() -> None:
            while True:
                try:
                    with db.session() as s:
                        result = index_documents(s, emb, closed_incident_documents(s))
                        STATE["rag_chunks"] = s.scalar(select(func.count()).select_from(RagChunk)
                                                        .where(RagChunk.retired_at.is_(None)))
                    if result.documents:
                        log.info("RAG sanitized resolved incidents indexed: %s documents", result.documents)
                except Exception as e:  # noqa: BLE001
                    log.warning("RAG closed incident ingestion skipped: %s", type(e).__name__)
                if rag_ingest_stop.wait(60):
                    break

        threading.Thread(target=ingest_closed, name="rag-closed-ingest", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log.error("RAG init failed: %s: %s", type(e).__name__, e)

    kube = Kube(in_cluster=settings.kube_in_cluster)
    from . import policy as _policy
    prof = _policy.resolve_profile(settings.policy_profile, settings.poc_cluster_uid, kube)
    safety.set_profile(prof.name)
    STATE["profile"] = {"active": prof.name, "requested": prof.requested, "downgraded_reason": prof.reason}
    engine = Engine(cfg, settings, kube, make_provider(cfg, settings), rag)
    from .change_api import setup_changes; setup_changes(engine, STATE)
    from .command_api import recover_pending_commands
    from .commands import CommandService
    from .operator_kube import OperatorKube
    from .secret_writer import SecretWriter
    recover_pending_commands()
    STATE.update(commands=None, secret_writer=None)
    try:
        operator = OperatorKube.from_token_file(settings.operator_executor_token_file)
        writer = None
        try:
            writer = SecretWriter.from_token_file(settings.secret_writer_token_file)
        except Exception as e:  # noqa: BLE001 - Secret changes fail closed, ordinary commands remain available
            log.warning("secret writer unavailable (%s)", type(e).__name__)
        STATE.update(commands=CommandService(cfg, engine.provider, operator, secret_writer=writer), secret_writer=writer)
    except Exception as e:  # noqa: BLE001 - never borrow the read-only API or autonomous action identity
        log.warning("operator executor unavailable (%s)", type(e).__name__)
    log.info("safe-action executor identity %s",
             "enabled" if engine.attach_executor(settings.executor_token_file) else "not mounted (typed actions denied)")
    from .interpret import Interpreter, InterpretWorker
    interpret = InterpretWorker(Interpreter(cfg, engine.provider))
    interpret.start()
    engine.interpret_worker = interpret
    STATE["interpret"] = interpret
    engine.recover_on_startup()
    engine.start()
    store = None
    if settings.hubble_relay:
        from .flows import FlowStore, HubbleCollector
        store = FlowStore()
        collector = HubbleCollector(store, settings.hubble_relay)
        collector.start()
        STATE.update(flows=store, collector=collector)
    watchers = Watchers(kube, cfg, engine.on_signal, flow_store=store)
    watchers.start()
    from .app_errors import AppErrorWatcher
    app_errors = AppErrorWatcher(cfg, settings, engine.on_signal)
    app_errors.start()
    STATE["app_errors"] = app_errors
    STATE.update(engine=engine, watchers=watchers, kube=kube, ready=True)
    log.info("LiveProbe background services started (provider=%s, target=%s)", cfg.llm.provider,
             cfg.target_namespaces)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "kubernetes", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    STATE.update(background_requested=settings.background, ready=False)
    url = settings.sqlalchemy_url()
    db.run_migrations(url)
    db.init_db(url)
    bind_source_settings(settings)
    broker.bind_loop(asyncio.get_running_loop())
    if settings.background:
        rag_ingest_stop.clear()
        threading.Thread(target=_start_background, name="bootstrap", daemon=True).start()
    application = STATE.get("application")
    if application is not None:
        async with application.router.lifespan_context(application):
            STATE['unified'].start()
            try:
                yield
            finally:
                STATE['unified'].stop()
    else:
        yield
    rag_ingest_stop.set()
    if STATE.get("collector"):
        STATE["collector"].stop()
    if STATE.get("watchers"):
        STATE["watchers"].stop()
    if STATE.get("app_errors"):
        STATE["app_errors"].stop()
    if STATE.get("engine"):
        STATE["engine"].stop()
    if STATE.get("interpret") and hasattr(STATE["interpret"], "stop"):
        STATE["interpret"].stop()
    for client in (getattr(STATE.get("commands"), "kube", None), STATE.get("secret_writer")):
        if client is not None and hasattr(client, "close"):
            client.close()


app = FastAPI(title="LiveProbe", version="0.1.0", lifespan=lifespan, docs_url=None,
              openapi_url=None)
from .accountability import accountability_router
app.include_router(accountability_router())

from .command_api import command_router
from .commands import CommandError
app.include_router(command_router(STATE))
from .change_api import change_router
app.include_router(change_router(STATE))
from .observation_sources import source_router
app.include_router(source_router(STATE))
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError


def _unified_path(request):
    path = request.url.path
    return path.startswith(('/api/unified/', '/api/application/')) or '/code-action' in path


@app.exception_handler(RequestValidationError)
async def request_validation(request, exc):
    if _unified_path(request):
        return JSONResponse({'code': 'request_invalid', 'message_ko': '요청 형식을 확인하십시오.'}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(ValidationError)
async def structured_validation(request, exc):
    return JSONResponse({'code': 'request_invalid', 'message_ko': '요청 형식을 확인하십시오.'}, status_code=422)


@app.exception_handler(SQLAlchemyError)
async def unified_storage_error(request, exc):
    return JSONResponse({'code': 'storage_unavailable', 'message_ko': '저장소를 사용할 수 없습니다.'}, status_code=503)


@app.middleware('http')
async def unified_no_store(request, call_next):
    response = await call_next(request)
    if _unified_path(request):
        response.headers['Cache-Control'] = 'no-store'
    return response


from .unified_api import prepare_unified, unified_router
prepare_unified(STATE)
app.include_router(unified_router(STATE))
from .console_history import console_history_router
app.include_router(console_history_router(STATE))
from .application.browser_access import install_redirect
from .human_sessions import local_read_key
install_redirect(app, os.getenv('LIVEPROBE_LOCAL_GATEWAY_URL'), local_read_key(os.getenv('LIVEPROBE_LOCAL_READ_KEY_FILE')),
                 legacy_application_host=os.getenv('LIVEPROBE_LEGACY_APPLICATION_HOST'))
if STATE.get("application") is not None:
    # Preserve runner pull endpoints on the same origin. No host callback is added.
    from fastapi.routing import APIRoute
    for route in STATE["application"].routes:
        if isinstance(route, APIRoute):
            app.router.routes.append(route)
    app.mount("/application-history", STATE["application"], name="application-history")


@app.exception_handler(CommandError)
async def command_error(request: Request, exc: CommandError):
    return JSONResponse({"detail": exc.code}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})

@app.get("/api/openapi.json", dependencies=[Depends(require_user)], include_in_schema=False)
def authenticated_openapi():
    return app.openapi()

@app.get("/api/docs", dependencies=[Depends(require_user)], include_in_schema=False)
def authenticated_docs():
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="LiveProbe API")

@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def database_unavailable(request: Request, exc: Exception):
    log.warning("database unavailable: %s", type(exc).__name__)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


def iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ health
@app.get("/api/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/readyz")
def readyz():
    database_ok = db.db_ok()
    engine = STATE.get("engine")
    health = getattr(engine, "worker_health", None)
    worker = health() if callable(health) else None
    background = STATE["ready"]
    if STATE.get("background_requested", False):
        background = bool(background and worker and worker["ready"])
    ok = database_ok and (background or not STATE.get("background_requested", False))
    body = {"status": "ok" if ok else "unavailable", "db": database_ok, "background": background,
            "rag_chunks": STATE["rag_chunks"], "incident_worker": worker}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/api/llm/accounts")
def llm_accounts(user: str = Depends(require_user)):
    """Codex account state for the status bar. Names and availability only: no paths, no credentials."""
    eng = STATE.get("engine")
    prov = getattr(eng, "provider", None)
    if prov is None:
        return {"mode": "not_ready", "accounts": []}
    if hasattr(prov, "status"):
        return {"mode": "pool", **prov.status()}
    return {"mode": "single", "active": "primary", "accounts": [{"name": "primary", "state": "available"}],
            "note": "spare account not configured"}


@app.get("/api/policy/profile")
def policy_profile(user: str = Depends(require_user)):
    return STATE.get("profile") or {"active": safety.active_profile()}


@app.get("/api/review-ledger")
def review_ledger(limit: int = 200, offset: int = 0, reason: str | None = None, incident_id: int | None = None,
                  environment: str | None = None, user: str = Depends(require_user)):
    from . import policy
    return policy.list_entries(limit, offset, reason, incident_id, environment, envs=get_app_config().history_namespaces)


@app.get("/api/review-ledger.csv")
def review_ledger_csv(reason: str | None = None, incident_id: int | None = None, environment: str | None = None,
                      user: str = Depends(require_user)):
    """정돈-04: the export uses the same filters as the screen and contains every matching row (no fixed cap)."""
    from fastapi.responses import PlainTextResponse
    from . import policy
    rows = list(policy.iter_entries(reason, incident_id, environment, envs=get_app_config().history_namespaces))
    return PlainTextResponse(policy.to_csv(rows), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=review-ledger.csv",
                                      "X-LiveProbe-Rows": str(len(rows))})


# ------------------------------------------------------------------ run feed and steering (P5)
from pydantic import Field, field_validator  # noqa: E402 - P18 W6 MessageIn validation (kept next to its only use)


class MessageIn(BaseModel):
    body: str
    # P18 W6: stage-targeted steering. Only registered roles; the node id is display-only.
    target_role: str | None = None
    target_node: str | None = Field(default=None, max_length=80)

    @field_validator("target_role")
    @classmethod
    def _known_role(cls, v):
        from .roles_ko import ROLES
        if v is not None and v not in ROLES:
            raise ValueError("unknown target_role")
        return v


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "stop"]


@app.get("/api/incidents/{iid}/events")
def run_events(iid: int, since: int = 0, user: str = Depends(require_user)):
    from . import runlog
    with db.session() as s:
        if s.get(Incident, iid) is None:
            raise HTTPException(404, "incident not found")
    return {"events": runlog.events_since(iid, max(0, since)), "control": runlog.control_state(iid), "streaming": True}


@app.post("/api/incidents/{iid}/messages", status_code=202)
def post_message(iid: int, body: MessageIn, tasks: BackgroundTasks, user: str = Depends(require_user)):
    """Investigation steering at the next boundary; mutations and post-handoff instructions need confirmation."""
    from . import steering
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "incident not found")
        if inc.status == "closed":
            raise HTTPException(409, "incident is closed by an operator; it accepts no further instructions")
        finished = inc.status in INCIDENT_CLOSED_STATES
    if not body.body.strip():
        raise HTTPException(422, "message is empty")
    if len(body.body) > 2000:
        raise HTTPException(422, "message too long (max 2000)")
    if finished or steering.classify(body.body)["intent"] == "cluster_change":
        from .command_api import queue_command
        from .models import OperatorMessage, RunEvent
        out = queue_command(STATE, tasks, iid, body.body, user)
        # Keep the confirmed-command route intact; only carry the display target through its existing message/events.
        mid = out.get("message_id")
        targets = {"target_role": body.target_role, "target_node": body.target_node}
        if mid is not None:
            with db.session() as s:
                _guard_incident_continuation(s, iid)
                msg = s.get(OperatorMessage, mid)
                if msg is not None:
                    msg.intent_detail = {**(msg.intent_detail or {}), **targets}
                for ev in s.scalars(select(RunEvent).where(RunEvent.incident_id == iid,
                                      RunEvent.kind.in_(("operator_message", "steering_interpreted")))):
                    if (ev.detail or {}).get("message_id") == mid:
                        ev.detail = {**(ev.detail or {}), **targets}
        return {**out, **targets, "hold": False}
    out = steering.post(iid, body.body, target_role=body.target_role, target_node=body.target_node)
    return {**out, "applies": "next_node_boundary"}


@app.post("/api/incidents/{iid}/control")
def control(iid: int, body: ControlIn, user: str = Depends(require_user)):
    from . import runlog
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "incident not found")
        if inc.status in INCIDENT_CLOSED_STATES:
            raise HTTPException(409, f"incident is {inc.status}; nothing is running")
    return {"state": runlog.set_control(iid, body.action)}


# ------------------------------------------------------------------ topology (P3)
_TOPO_CACHE: dict[str, Any] = {"at": 0.0, "inventory": None}


def _inventory(kube) -> dict:
    """Cluster inventory, cached 5s so many dashboards do not each hit the Kubernetes API."""
    import time as _t
    now = _t.monotonic()
    if _TOPO_CACHE.get("inventory") is None or now - _TOPO_CACHE.get("at", 0.0) > 5:
        observed = kube.cluster_inventory()
        _TOPO_CACHE.update(inventory=observed, at=now, observed_at=datetime.now(timezone.utc).isoformat())
    return _TOPO_CACHE["inventory"]


@app.get("/api/topology")
def topology(user: str = Depends(require_user)):
    """Cluster resources (Kubernetes API) merged with observed network flows (Hubble). Structure and
    observation are separate edge lists; `freshness` says how current the flow view is."""
    from .flows import FlowStore, build_topology
    kube, store = STATE.get("kube"), STATE.get("flows")
    if kube is None:
        raise HTTPException(503, "cluster client not ready")
    store = store or FlowStore()  # collection disabled: every flow field stays empty and freshness says never_connected
    with db.session() as s:
        incidents = [{"id": i.id, "namespace": i.namespace, "workload": i.workload, "system": i.system, "environment": get_app_config().environment(i.namespace), "status": i.status,
                      "severity_level": i.severity_level, "title": i.title,
                      "claimed": bool((i.handoff or {}).get("claimed_by")),
                      "updated_ts": i.updated_at.timestamp() if i.updated_at else 0}
                     for i in s.scalars(select(Incident).where(effective_status_expr().not_in(("resolved", "failed", "closed"))))]
        incidents = project_items(s, incidents)
    return build_topology(_inventory(kube), store.snapshot(), store.freshness(), incidents, cfg=get_app_config())


# ------------------------------------------------------------------ operations overview (P15)
_OV_METRICS: dict[str, Any] = {"at": 0.0, "url": None, "values": {}, "errors": {}}
_OV_LOCK = threading.Lock()


@app.get("/api/overview")
def operations_overview(user: str = Depends(require_user)):
    from datetime import timedelta
    from time import monotonic
    from zoneinfo import ZoneInfo
    from . import overview as ov
    from .models import RunEvent
    from .narrative import stage_of
    kube = STATE.get("kube")
    if kube is None:
        raise HTTPException(503, "cluster client not ready")
    settings = get_settings()
    # Only source queries are cached: incident pins and AI stages must change on every request.
    with _OV_LOCK:
        if _OV_METRICS["url"] != adapter_url(settings, 'prometheus') or monotonic() - _OV_METRICS["at"] > 10:
            values, errors = ov.fetch_metrics(settings)
            _OV_METRICS.update(at=monotonic(), url=adapter_url(settings, 'prometheus'), values=values, errors=errors)
        values, errors = _OV_METRICS["values"], dict(_OV_METRICS["errors"])
    try:
        inv = _inventory(kube)
        inventory_observed_at = _TOPO_CACHE.get("observed_at") if _TOPO_CACHE.get("inventory") is inv else None
    except Exception as e:  # noqa: BLE001 - keep source failures visible even if inventory is unavailable
        inv = {}
        inventory_observed_at = None
        errors["inventory"] = type(e).__name__
    try:
        events = kube.cluster_warning_events()
    except Exception as e:  # noqa: BLE001
        events = []
        errors["events"] = type(e).__name__
    with db.session() as s:
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        rows = list(s.scalars(select(Incident).where(or_(
            effective_status_expr().in_(("open", "analyzing", "action_pending", "acting")),
            (effective_status_expr() == "handed_off") & (Incident.updated_at >= recent))).order_by(Incident.id.desc()).limit(200)))
        ids = [i.id for i in rows]
        routes = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id.in_(ids)).order_by(RoutingDecision.id))) if ids else []
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id.in_(ids)).order_by(AgentRun.id))) if ids else []
        evs = list(s.scalars(select(RunEvent).where(RunEvent.incident_id.in_(ids)).order_by(RunEvent.seq))) if ids else []
        incidents = [{"id": i.id, "namespace": i.namespace, "workload": i.workload, "system": i.system, "environment": get_app_config().environment(i.namespace), "status": i.status,
                      "severity_level": i.severity_level, "title": i.title, "created_at": iso(i.created_at),
                      "updated_ts": i.updated_at.timestamp() if i.updated_at else 0,
                      "claimed": bool((i.handoff or {}).get("claimed_by")),
                      "stage": stage_of(i, [r for r in routes if r.incident_id == i.id],
                                        [r for r in runs if r.incident_id == i.id], [e for e in evs if e.incident_id == i.id])}
                     for i in rows]
        day_start = now.astimezone(ZoneInfo("Asia/Seoul")).replace(hour=0, minute=0, second=0, microsecond=0)
        incidents = project_items(s, incidents)
        counts = dict(s.execute(select(effective_status_expr(), func.count()).where(Incident.created_at >= day_start)
                                .group_by(effective_status_expr())).all())
        today = {"total": sum(counts.values()), **{k: counts.get(k, 0) for k in ("resolved", "handed_off", "failed", "closed")}}
        from .incident_counts import incident_counts
        shared = incident_counts(s, get_app_config().target_namespaces)
    out = ov.assemble(inv, values, events=events, incidents=incidents, today=today,
                      errors=errors, store=STATE.get("flows"), cfg=get_app_config())
    # 정돈-05: header, overview and environment chips read the same buckets; `open` keeps feeding the map pins only.
    out["inventory_observed_at"] = inventory_observed_at
    out["incidents"]["counts"] = shared
    for env in out["cluster"].get("environments") or []:
        env["open_incidents"] = (shared["by_environment"].get(env["name"]) or {}).get("in_progress", env.get("open_incidents"))
    return redact_obj(out)


# ------------------------------------------------------------------ incidents
def _agg(s, ids: list[int]) -> dict[int, dict]:
    """정돈-07: per-incident cost through the shared accounting contract (same rule as the narrative API)."""
    from .accounting import cost_view
    if not ids:
        return {}
    rows = s.execute(select(AgentRun.incident_id, func.count(AgentRun.id),
                            func.count(AgentRun.estimated_cost_usd),
                            func.sum(AgentRun.estimated_cost_usd))
                     .where(AgentRun.incident_id.in_(ids)).group_by(AgentRun.incident_id)).all()
    result = {r[0]: {"agent_run_count": int(r[1]),
                   **cost_view(int(r[1]), int(r[2]), float(r[3]) if r[3] is not None else None)} for r in rows}
    for iid, projection in projection_rows(s, ids).items():
        result.setdefault(iid, {"agent_run_count": 0, **cost_view(0, 0, None)})['code_action'] = projection
    return result


def _summary(i: Incident, agg: dict[int, dict]) -> dict:
    from .accounting import cost_view
    from .narrative import incident_origin
    a = agg.get(i.id) or {"agent_run_count": 0, **cost_view(0, 0, None)}
    from .readings_ko import title_ko
    return apply_projection({
        "id": i.id, "title": i.title, "title_ko": title_ko(i.title, i.signal_reasons),
        "namespace": i.namespace, "workload": i.workload, "system": i.system, "environment": get_app_config().environment(i.namespace), "status": i.status,
        "severity_level": i.severity_level, "signal_count": i.signal_count, "created_at": iso(i.created_at),
        "updated_at": iso(i.updated_at), "closed_at": iso(i.closed_at), "total_cost_usd": a["total_cost_usd"],
        "known_cost_usd": a["known_cost_usd"], "cost_missing_runs": a["cost_missing_runs"], "cost_status": a["cost_status"],
        "agent_run_count": a["agent_run_count"], "origin": incident_origin(i.fingerprint),
        "recovery_kind": 'observed' if (i.handoff or {}).get('observed_recovery') else None,
    }, a.get('code_action'))


INCIDENT_ORIGINS = ("evaluation", "unconfirmed")


@app.get("/api/incidents")
def list_incidents(status: str | None = None, limit: int = 50, offset: int = 0, search: str | None = None,
                   environment: str | None = None, origin: str | None = None, system: str | None = None,
                   start: date | None = None, end: date | None = None, service: str | None = None,
                   as_of: int | None = None, work: Literal['approval', 'qa_fix', 'recovery'] | None = None,
                   user: str = Depends(require_user)):
    """정돈-04: every filter runs in the database before counting and paging, so `total` and every page describe the
    same set over the whole history. `as_of` (the max id of the first page) keeps later pages stable while new
    incidents arrive. Origin uses only the explicit `eval/` marker; everything else stays 'unconfirmed'."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    if search is not None and len(search) > 120:
        raise HTTPException(422, "search must be at most 120 characters")
    if origin is not None and origin not in INCIDENT_ORIGINS:
        raise HTTPException(422, "origin must be evaluation or unconfirmed")
    if system is not None and len(system) > 120:
        raise HTTPException(422, "system must be at most 120 characters")
    if as_of is not None and as_of < 0:
        raise HTTPException(422, "as_of must be a non-negative incident id")
    from .history_dates import bounds
    try:
        lower, upper = bounds(start, end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    envs = get_app_config().history_namespaces
    from .application.qa_release import WORK_STATES
    work_group = case(
        *((CodeActionLink.application_state.in_(states), key) for key, states in WORK_STATES.items()),
        ((CodeActionLink.incident_id.is_(None)) & (Incident.status == 'action_pending'), 'approval'))
    with db.session() as s:
        conds = []
        if lower is not None:
            conds.append(Incident.created_at >= lower)
        if upper is not None:
            conds.append(Incident.created_at < upper)
        if service:
            conds.append(Incident.workload == service)
        if status:
            conds.append(effective_status_expr() == status)
        if search and search.strip():
            term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conds.append(or_(Incident.title.ilike(f"%{term}%", escape="\\"),
                             Incident.workload.ilike(f"%{term}%", escape="\\")))
        if environment:
            # An environment is a configured target namespace; an unknown name matches nothing (never everything).
            conds.append(Incident.namespace == environment if environment in envs else Incident.id.is_(None))
        if origin == "evaluation":
            conds.append(Incident.fingerprint.like("eval/%"))
        elif origin == "unconfirmed":
            conds.append(~Incident.fingerprint.like("eval/%"))
        if system:
            conds.append(Incident.system == system)
        newest = s.scalar(select(func.max(Incident.id))) or 0
        anchor = min(as_of, newest) if as_of is not None else newest
        conds.append(Incident.id <= anchor)
        joined = Incident.__table__.outerjoin(CodeActionLink, CodeActionLink.incident_id == Incident.id)
        distribution = dict(s.execute(select(work_group, func.count()).select_from(joined)
                                      .where(*conds).group_by(work_group)).all())
        by_work = {key: distribution.get(key, 0) for key in WORK_STATES}
        if work:
            conds.append(work_group == work)
        total = by_work[work] if work else sum(distribution.values())
        incs = list(s.scalars(select(Incident).select_from(joined).where(*conds)
                              .order_by(Incident.created_at.desc(), Incident.id.desc()).offset(offset).limit(limit)))
        agg = _agg(s, [i.id for i in incs])
        newer = s.scalar(select(func.count()).select_from(Incident).where(Incident.id > anchor)) or 0
        return {"items": [_summary(i, agg) for i in incs], "total": total, "as_of": anchor, "newer_since_as_of": newer, "by_work": by_work,
                "filters": {"status": status or None, "search": (search or "").strip() or None, "environment": environment or None,
                            "origin": origin, "system": system or None, "work": work}}


def action_dict(a: Action) -> dict:
    return {"id": a.id, "type": a.type, "target": a.target, "params": a.params, "policy_mode": a.policy_mode,
            "status": a.status, "before_snapshot": a.before_snapshot, "after_state": a.after_state,
            "reason": a.reason, "executed": bool(a.executed), "created_at": iso(a.created_at),
            "updated_at": iso(a.updated_at)}


@app.get("/api/incidents/{iid}")
def get_incident(iid: int, user: str = Depends(require_user)):
    with db.session() as s:
        inc = s.get(Incident, iid)
        if not inc:
            raise HTTPException(404, "incident not found")
        incident = {**_summary(inc, _agg(s, [iid])), "summary": inc.summary, "status_reason": inc.status_reason,
                    "signal_reasons": inc.signal_reasons or [], "handoff": inc.handoff,
                    "family_hint": inc.family_hint, "remediation": (inc.handoff or {}).get("remediation")}
        evidence = [{"id": e.id, "source": e.source, "target": e.target, "collected_at": iso(e.collected_at),
                     "payload": e.payload, "error": e.error}
                    for e in s.scalars(select(Evidence).where(Evidence.incident_id == iid).order_by(Evidence.id))]
        routing = [{"id": r.id, "level": r.level, "workflow": r.workflow, "features": r.features,
                    "rationale": r.rationale, "policy": r.policy, "confidence": r.confidence, "decided_by": r.decided_by,
                    "created_at": iso(r.created_at)}
                   for r in s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)
                                      .order_by(RoutingDecision.id))]
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
        cites: dict[int, list] = {}
        if runs:
            for c, ch in s.execute(select(Citation, RagChunk).join(RagChunk, RagChunk.id == Citation.chunk_id)
                                   .where(Citation.agent_run_id.in_([r.id for r in runs])).order_by(Citation.id)):
                cites.setdefault(c.agent_run_id, []).append(
                    {"chunk_id": ch.id, "doc": ch.doc, "source": ch.source, "chunk_ix": ch.chunk_ix,
                     "score": c.score, "content": ch.content,
                     "source_uri": (ch.provenance or {}).get("source_uri") if ch.document_id else None,
                     "license": (ch.provenance or {}).get("license") if ch.document_id else None,
                     "attribution": (ch.provenance or {}).get("attribution") if ch.document_id else None,
                     "version_hash": (ch.provenance or {}).get("version_hash") if ch.document_id else None,
                     "locator": ch.locator,
                     "section": ch.section, "line_start": ch.line_start, "line_end": ch.line_end,
                     "char_start": ch.char_start, "char_end": ch.char_end})
        agent_runs = [{"id": r.id, "role": r.role, "provider": r.provider, "model": r.model,
                       "reasoning_effort": r.reasoning_effort, "input_tokens": r.input_tokens,
                       "output_tokens": r.output_tokens, "estimated_cost_usd": r.estimated_cost_usd,
                       "duration_ms": r.duration_ms, "status": r.status, "attempts": r.attempts,
                       "output": r.output, "error": r.error, "created_at": iso(r.created_at),
                       "citations": cites.get(r.id, [])} for r in runs]
        actions = [action_dict(a) for a in
                   s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id))]
        audit_rows = [{"id": a.id, "ts": iso(a.ts), "actor": a.actor, "event": a.event, "detail": a.detail}
                      for a in s.scalars(select(AuditLog).where(AuditLog.incident_id == iid).order_by(AuditLog.id))]
        return redact_obj({"incident": incident, "evidence": evidence, "routing": routing,
                           "agent_runs": agent_runs, "actions": actions, "audit": audit_rows})


# ------------------------------------------------------------------ operator handoff
def _interpret_job(iid: int) -> str | None:
    """In-memory state of the display-only interpreter for one incident. Read-only: never schedules work."""
    worker = STATE.get("interpret")
    status = getattr(worker, "status", None)
    try:
        return status(iid) if callable(status) else None
    except Exception:  # noqa: BLE001 - display state only
        return None


@app.get("/api/incidents/{iid}/narrative")
def incident_narrative(iid: int, user: str = Depends(require_user)):
    """A read-only incident narrative assembled from persisted facts and public work events."""
    from .narrative import build_narrative
    with db.session() as s:
        out = build_narrative(s, iid, interpret_job=_interpret_job(iid))
        projection = projection_rows(s, [iid]).get(iid)
        if out is not None and projection:
            out['incident'] = apply_projection(out['incident'], projection)
            out['headline']['stage'] = {'key': 'code_action', 'label': projection['state_ko']}
            out['headline']['next'] = '같은 관제실의 코드 수정 조치에서 진행 상태와 다음 조치를 확인하세요.'
            out['handoff_view'] = None
            out['flow'] = append_code_flow(out['flow'], projection)
        s.rollback()  # read path: nothing is ever committed from here
    if out is None:
        raise HTTPException(404, "incident not found")
    return redact_obj(out)


class InterpretIn(BaseModel):
    confirm: bool = False


@app.post("/api/incidents/{iid}/interpretations", status_code=202)
def request_interpretation(iid: int, body: InterpretIn, user: str = Depends(require_user)):
    """정돈-02: explicit, operator-confirmed Korean reading. It may call a model and record cost, so it is a separate
    action from viewing. Already queued/running work is reported, never duplicated."""
    from .narrative import build_narrative
    if body.confirm is not True:
        raise HTTPException(422, "explicit confirmation is required")
    if not get_app_config().assist.interpret_enabled:
        raise HTTPException(409, "interpretation is disabled by configuration")
    worker = STATE.get("interpret")
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        if worker is None or not hasattr(worker, "request_explicit"):
            raise HTTPException(503, "interpretation worker unavailable")
        out = build_narrative(s, iid, interpret_job=_interpret_job(iid))
        if out is None:
            raise HTTPException(404, "incident not found")
        state = (out.get("interpret_state") or {}).get("status")
        if state not in ("not_requested", "unavailable", "pipeline"):
            s.rollback()
            return {"status": "running" if state == "pending" else "nothing_pending", "incident_id": iid}
        result = worker.request_explicit(iid)
        if result == "accepted":
            db.audit(s, user, "interpretation_requested", iid, pending_items=out.get("interpret_pending_count"))
    return {"status": result, "incident_id": iid}


@app.post("/api/incidents/{iid}/handoff/claim")
def claim_handoff(iid: int, user: str = Depends(require_user)):
    """Acknowledge ownership of an existing handoff; never authorize a cluster action."""
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
        if inc is None:
            raise HTTPException(404, "incident not found")
        if inc.status != "handed_off":
            raise HTTPException(409, "incident is not handed off")
        handoff = inc.handoff or {}
        if handoff.get("claimed_by"):
            if handoff["claimed_by"] != user:
                raise HTTPException(409, "handoff already claimed by another operator")
            return redact_obj(handoff)
        handoff = {**handoff, "claimed_by": user, "claimed_at": iso(datetime.now(timezone.utc))}
        inc.handoff = handoff
        db.audit(s, user, "handoff_claimed", iid)
    broker.publish(iid, "updated", "handed_off")
    return redact_obj(handoff)


class CloseIn(BaseModel):
    reason: str
    confirm: bool = False


# Closing only records the operator's decision. It must never be the side door to a cluster change.
_CLOSABLE = ("handed_off", "action_pending")
_LIVE_COMMANDS = ("planning", "awaiting_confirmation", "awaiting_secret", "executing", "rolling_back")


@app.post("/api/incidents/{iid}/close")
def close_incident(iid: int, body: CloseIn, user: str = Depends(require_user)):
    """End an incident the AI already stopped on (hand-off, or an approval nobody will act on). No cluster change."""
    from . import runlog
    from .models import OperatorCommand
    reason = body.reason.strip()
    if not body.confirm:
        raise HTTPException(422, "explicit confirmation is required")
    if not reason or len(reason) > 500:
        raise HTTPException(422, "reason must be 1-500 characters")
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
        if inc is None:
            raise HTTPException(404, "incident not found")
        handoff = dict(inc.handoff or {})
        if inc.status == "closed":
            return redact_obj(_closed(inc, handoff))  # idempotent: the first recorded decision stands
        if inc.status not in _CLOSABLE:
            raise HTTPException(409, f"incident is {inc.status}; only a hand-off or a stale approval can be closed")
        if handoff.get("claimed_by") and handoff["claimed_by"] != user:
            raise HTTPException(409, "handoff is claimed by another operator")
        if s.scalar(select(func.count()).select_from(Action).where(
                Action.incident_id == iid, Action.status.in_(("proposed", "pending_approval", "approved", "executing")))):
            raise HTTPException(409, "an action is still pending or running; approve or reject it first")
        if s.scalar(select(func.count()).select_from(OperatorCommand).where(
                OperatorCommand.incident_id == iid, OperatorCommand.status.in_(_LIVE_COMMANDS))):
            raise HTTPException(409, "an operator command is still in progress; finish or cancel it first")
        previous = inc.status
        transition(inc, "closed")
        inc.status_reason = f"운영자 종료: {reason}"
        inc.handoff = {**handoff, "closed_by": user, "closed_at": iso(inc.closed_at), "close_reason": reason,
                       "closed_from": previous}
        update_run(s, iid, "succeeded", "closed", inc.status_reason)
        db.audit(s, user, "incident_closed", iid, previous_status=previous, reason=reason, cluster_changed=False)
        out = _closed(inc, inc.handoff)
    broker.publish(iid, "closed", "closed")
    runlog.emit(iid, "completed", f"운영자 종료: {reason}", {"outcome": "closed", "previous_status": out["closed_from"]},
                actor=user)
    return redact_obj(out)


def _closed(inc: Incident, handoff: dict) -> dict:
    return {"id": inc.id, "status": inc.status, "closed_by": handoff.get("closed_by"), "closed_at": handoff.get("closed_at"),
            "close_reason": handoff.get("close_reason"), "closed_from": handoff.get("closed_from"), "cluster_changed": False}

class ReassessIn(BaseModel):
    reason: str
    confirm: bool = False
    read_only: Literal[True] = True
    observed_recovery_only: Literal[True] = True


@app.post('/api/incidents/{iid}/reassess')
def reassess_incident(iid: int, body: ReassessIn, user: str = Depends(require_user)):
    """Narrow correction for premature router handoffs, never a replay of mutation authority."""
    from . import router as routing
    from .models import OperatorCommand, OperatorMessage, RunControl, WorkflowRun
    reason = body.reason.strip()
    if not body.confirm or not reason or len(reason) > 500:
        raise HTTPException(422, 'confirmation and a 1-500 character reason are required')
    engine = STATE.get('engine')
    if engine is None:
        raise HTTPException(503, 'engine not ready')
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
        if inc is None:
            raise HTTPException(404, 'incident not found')
        handoff = dict(inc.handoff or {})
        if inc.status != 'handed_off' or inc.namespace not in engine.cfg.target_namespaces:
            raise HTTPException(409, 'only active-environment router handoffs may be reassessed')
        if handoff.get('claimed_by') and handoff['claimed_by'] != user:
            raise HTTPException(409, 'handoff claimed by another operator')
        history = list(handoff.get('reassessments') or [])
        if len(history) >= 2:
            raise HTTPException(409, 'reassessment budget exhausted')
        if s.scalar(select(Action.id).where(Action.incident_id == iid).limit(1)) is not None:
            raise HTTPException(409, 'incident has action history; reassessment cannot replay authority')
        if s.scalar(select(WorkflowRun.id).where(WorkflowRun.incident_id == iid,
                WorkflowRun.status.in_(('running', 'resume_pending', 'paused', 'waiting_quota'))).limit(1)) is not None:
            raise HTTPException(409, 'live workflow conflict')
        if s.scalar(select(OperatorCommand.id).where(OperatorCommand.incident_id == iid).limit(1)) is not None:
            # A failed command can still carry an indeterminate write after process restart.
            raise HTTPException(409, 'operator command history conflict; no authority replay')
        if (handoff.get('handoff_classification') or {}).get('unknown_write'):
            raise HTTPException(409, 'unknown previous write outcome')
        control = s.get(RunControl, iid)
        if control and control.state != 'running':
            raise HTTPException(409, 'run control conflict')
        if s.scalar(select(OperatorMessage.id).where(OperatorMessage.incident_id == iid,
                OperatorMessage.applied_at.is_(None)).limit(1)) is not None:
            raise HTTPException(409, 'pending operator instruction conflict')
        previous = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid)
                            .order_by(RoutingDecision.id.desc()).limit(1))
        # Subsequent read-only diagnostic attempts still refer to the original eligible routing row.
        if history:
            previous = s.get(RoutingDecision, history[0]['routing_id'])
        if previous is None:
            raise HTTPException(409, 'no original router evidence')
        f = dict(previous.features or {})
        later_rulings = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid,
            RoutingDecision.id > previous.id, RoutingDecision.level == 'HIGH_RISK',
            RoutingDecision.decided_by != 'router_failure')))
        if later_rulings:
            raise HTTPException(409, 'a later rules authority boundary prohibits reassessment')
        needed = {'protected_resource', 'service_selector_matches_zero_pods', 'service_has_no_endpoints',
                  'dependent_callers_failing', 'image_pull_failing', 'crashloop', 'recent_rollout',
                  'oom_killed', 'latency_signal', 'affected_workload_count', 'ready_pods',
                  'current_revision_age_min', 'waiting_reasons', 'current_revision'}
        f['signal_reasons'] = list(set(f.get('signal_reasons') or []) | set(inc.signal_reasons))
        if not needed.issubset(f) or routing.rules_decide(f)[0] == 'HIGH_RISK':
            raise HTTPException(409, 'original rules authority boundary or unprovable original evidence')
        eligible = previous.decided_by == 'evidence_state' and f.get('evidence_state') in ('currently_healthy', 'insufficient')
        if previous.decided_by == 'router_failure':
            eligible = any(text in previous.rationale for text in (
                'critical evidence unavailable', 'deployment/pod evidence missing', 'no routing evidence',
                'LLM classifier unavailable', 'LLM classifier confidence below threshold'))
        if not eligible or not (previous.policy or {}).get('handoff_only'):
            raise HTTPException(409, 'not a premature router handoff')
        if s.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid,
                AgentRun.role != 'router').limit(1)) is not None:
            raise HTTPException(409, 'incident already ran diagnostic roles')
        old = {'status': inc.status, 'status_reason': inc.status_reason, 'closed_at': iso(inc.closed_at),
               'handoff': {k: v for k, v in handoff.items() if k != 'reassessments'}, 'routing_id': previous.id}
        run = WorkflowRun(incident_id=iid, status='running', phase='read_only_reassessment',
                          snapshot={'read_only_reassessment': True})
        s.add(run)
        s.flush()
        entry = {**old, 'reason': reason, 'actor': user, 'at': iso(datetime.now(timezone.utc)), 'run_id': run.id}
        inc.handoff = {**handoff, 'reassessments': [*history, entry]}
        # This exceptional transition is only available under the eligibility/row-lock gate above.
        inc.status, inc.closed_at = 'analyzing', None
        inc.status_reason = '읽기 전용 재평가 중 · 변경 권한 없음'
        db.audit(s, user, 'incident_reassessment_started', iid, reassessment=entry, read_only=True)
        run_id = run.id
    engine.reassess_read_only(iid, run_id)
    with db.session() as s:
        _guard_incident_continuation(s, iid)
        inc = s.get(Incident, iid)
        return redact_obj({'id': iid, 'status': inc.status, 'run_id': run_id, 'read_only': True,
                           'cluster_changed': False, 'handoff': inc.handoff})


# ------------------------------------------------------------------ actions
class RejectIn(BaseModel):
    reason: str | None = None


@app.post("/api/actions/{aid}/approve")
def approve(aid: int, user: str = Depends(require_user)):
    with db.session() as s:
        act = s.get(Action, aid)
        if not act:
            raise HTTPException(404, "action not found")
        if act.status != "pending_approval":
            raise HTTPException(409, f"action is {act.status}, not pending_approval")
        inc = s.get(Incident, act.incident_id)
        iid, level = inc.id, inc.severity_level
        if level == "HIGH_RISK" and not safety.high_risk_may_execute():
            act.status, act.reason = "denied", "HIGH_RISK incidents never execute actions"
            db.audit(s, user, "action_denied", iid, action_id=aid, reason=act.reason)
            denied_reason = act.reason
        else:
            denied_reason = None
            if STATE.get("engine") is None:
                raise HTTPException(503, "engine not ready")
            db.audit(s, user, "action_approved", iid, action_id=aid)
    if denied_reason:
        broker.publish(iid, "action")
        raise HTTPException(403, denied_reason)
    engine = STATE.get("engine")
    if engine is None:
        raise HTTPException(503, "engine not ready")
    res = engine.run_action(iid, aid, actor=user, level=level)  # re-runs all checks server-side
    with db.session() as s:
        act = s.get(Action, aid)
        out = action_dict(act)
    if res.status == "denied":
        raise HTTPException(403, res.reason or "denied by policy")
    return redact_obj(out)


@app.post("/api/actions/{aid}/reject")
def reject(aid: int, body: RejectIn | None = Body(default=None), user: str = Depends(require_user)):
    reason = (body.reason if body else None) or f"rejected by {user}"
    with db.session() as s:
        act = s.get(Action, aid)
        if not act:
            raise HTTPException(404, "action not found")
        if act.status != "pending_approval":
            raise HTTPException(409, f"action is {act.status}, not pending_approval")
        act.status, act.reason = "rejected", reason[:500]
        inc = s.get(Incident, act.incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        transition(inc, "handed_off")
        inc.status_reason = f"action rejected by {user}"
        update_run(s, inc.id, "succeeded", "handed_off", inc.status_reason)
        db.audit(s, user, "action_rejected", inc.id, action_id=aid, reason=reason[:500])
        s.flush()
        out, iid = action_dict(act), inc.id
    broker.publish(iid, "handed_off", "handed_off")
    return redact_obj(out)


# ------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    automation_mode: Literal["auto", "approval", "suggest_only"]


def _mode(s) -> str:
    row = s.get(Setting, "policy_mode")
    v = (row.value or {}).get("mode") if row else None
    return v if v in POLICY_MODES else "auto"


@app.get("/api/settings")
def get_settings_api(user: str = Depends(require_user)):
    with db.session() as s:
        return {"automation_mode": _mode(s)}


@app.put("/api/settings")
def put_settings_api(body: SettingsIn, user: str = Depends(require_user)):
    with db.session() as s:
        row = s.get(Setting, "policy_mode")
        if row:
            row.value = {"mode": body.automation_mode}
        else:
            s.add(Setting(key="policy_mode", value={"mode": body.automation_mode}))
        db.audit(s, user, "automation_mode_changed", mode=body.automation_mode)
    return {"automation_mode": body.automation_mode}


# Role placement is stored separately from policy mode and ConfigMap safety configuration.
@app.get("/api/model-settings")
def get_model_settings(user: str = Depends(require_user)):
    with db.session() as s:
        return read_settings(s, get_app_config())


@app.put("/api/model-settings")
def put_model_settings(body: ModelSettingsIn, user: str = Depends(require_user)):
    try:
        with db.session() as s:
            return save_settings(s, get_app_config(), body, user)
    except RevisionConflict as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# Operational extra (not used by the dashboard): runtime status of watchers/RAG/LLM mapping.
@app.get("/api/status")
def status(user: str = Depends(require_user)):
    cfg = get_app_config()
    w = STATE.get("watchers")
    return {"ready": STATE["ready"], "rag_chunks": STATE["rag_chunks"], "watchers": w.status if w else {},
            "llm": {"provider": cfg.llm.provider,
                    "roles": {k: v.model_dump() for k, v in cfg.llm.roles.items()}},
            "target_namespace": cfg.target_namespace, "target_namespaces": cfg.target_namespaces, "allowlist": cfg.allowlist}


# ------------------------------------------------------------------ webhook
ALERT_KIND = {
    "TargetWorkloadImagePullFailing": "pod",
    "TargetWorkloadCrashLooping": "pod",
    "TargetWorkloadOOMKilled": "pod",
    "TargetServiceNoEndpoints": "endpoint",
    "OrderHigh5xxRate": "app",
    "InventoryHigh5xxRate": "app",
    "TargetHigh5xxRate": "app",
    "High5xx": "app",
    "TargetHighLatency": "app",
    "TargetConnectionPoolExhausted": "app",
}


def alert_signal(alert: dict, cfg, kube) -> Signal | None:
    labels = alert.get("labels") or {}
    name, ns = labels.get("alertname"), labels.get("namespace")
    if ns not in cfg.target_namespaces or name not in ALERT_KIND:
        return None
    kind = ALERT_KIND[name]
    if kind == "endpoint" and labels.get("endpoint"):
        from .service_health import read_backend
        if kube is None:
            return None
        svc = kube.get_service(ns, labels["endpoint"])
        if not svc or read_backend(kube, ns, svc)["backend_state"] != "missing":
            return None
        return Signal(ns, "Service", labels["endpoint"], name, "alertmanager", {"labels": labels})
    if kind == "app" and labels.get("app"):
        return Signal(ns, "App", labels["app"], name, "alertmanager", {"labels": labels})
    if kind == "pod" and labels.get("pod") and kube is not None:
        from .k8s import deployment_for_pod
        pod = kube.get_pod(ns, labels["pod"])
        if not pod or (pod.get("metadata") or {}).get("deletionTimestamp"):
            return None
        dep = deployment_for_pod(kube, ns, pod)
        if dep:
            return Signal(ns, "Deployment", dep, name, "alertmanager", {"labels": labels})
    return None


@app.post("/api/alerts", dependencies=[Depends(require_webhook)])
@app.post("/api/hooks/alertmanager", dependencies=[Depends(require_webhook)])
async def alertmanager_hook(request: Request):
    payload = await request.json()
    cfg = get_app_config()
    engine = STATE.get("engine")
    accepted = 0
    for alert in payload.get("alerts") or []:
        if alert.get("status") != "firing":
            continue
        sig = await asyncio.to_thread(alert_signal, alert, cfg, STATE.get("kube"))
        if sig and engine:
            await asyncio.to_thread(engine.on_signal, sig)
            accepted += 1
    return {"accepted": accepted}


# ------------------------------------------------------------------ SSE
def _guard_incident_continuation(session, iid):
    from .unified import require_no_code_action
    require_no_code_action(session, session.get(Incident, iid))


def _current_status(iid: int) -> str | None:
    with db.session() as s:
        inc = s.get(Incident, iid)
        return inc.status if inc else None


@app.get("/api/incidents/{iid}/events/stream")
async def run_event_stream(iid: int, request: Request, user: str = Depends(require_user)):
    from . import runlog
    raw = request.headers.get("last-event-id") or request.query_params.get("since", "0")
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
        raise HTTPException(400, "invalid event cursor")
    cursor = int(raw)
    if cursor > 2**63 - 1:
        raise HTTPException(400, "invalid event cursor")
    with db.session() as session:
        if session.get(Incident, iid) is None:
            raise HTTPException(404, "incident not found")

    async def gen():
        nonlocal cursor
        while not await request.is_disconnected():
            events = await asyncio.to_thread(runlog.events_since, iid, cursor, 200)
            for event in events:
                cursor = event["seq"]
                yield ServerSentEvent(id=str(cursor), event="run_event", data=broker.encode(event))
            await asyncio.sleep(0 if len(events) == 200 else 0.5)

    return EventSourceResponse(gen(), ping=15, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stream")
async def stream(request: Request, user: str = Depends(require_user)):
    raw = request.headers.get("last-event-id") or request.query_params.get("since")
    if raw is not None and (not raw.isascii() or not raw.isdecimal() or len(raw) > 19):
        raise HTTPException(400, "invalid event cursor")
    with db.session() as s:
        cursor = int(raw) if raw is not None else (s.scalar(select(func.max(IncidentEvent.id))) or 0)
        latest = s.scalar(select(func.max(IncidentEvent.id))) or 0
        oldest = s.scalar(select(func.min(IncidentEvent.id))) or 0
    if cursor > latest:
        raise HTTPException(409, "event cursor ahead of server; refetch snapshot")
    if raw is not None and oldest and cursor < oldest - 1:
        raise HTTPException(409, "event history expired; refetch snapshot")

    async def gen():
        nonlocal cursor
        last_ping = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            def fetch():
                with db.session() as s:
                    return [(r.id, r.incident_id, r.status, r.event) for r in s.scalars(
                        select(IncidentEvent).where(IncidentEvent.id > cursor).order_by(IncidentEvent.id).limit(200))]
            rows = await asyncio.to_thread(fetch)
            for rid, iid, status, event in rows:
                cursor = rid
                yield ServerSentEvent(id=str(rid), event="incident", data=broker.encode(
                    {"incident_id": iid, "status": status, "event": event}))
            now = asyncio.get_running_loop().time()
            if now - last_ping >= 15:
                yield ServerSentEvent(comment="ping")
                last_ping = now
            await asyncio.sleep(0.5 if rows else 1)

    return EventSourceResponse(gen(), ping=3600)
