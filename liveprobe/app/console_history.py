"""Read-only, chronologically merged history across the two incident stores."""
from datetime import date, datetime, timezone
from time import time_ns
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Text, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .auth import require_user
from .history_dates import bounds
from .models import CodeActionLink, Incident
from .unified import effective_status_expr, projected_status


def _core_kind():
    from .main import ALERT_KIND
    from .watcher import SIGNAL_REASONS as pod_reasons

    # These are ingested signal reasons, not evidence collected during a routine investigation.
    app_reasons = {'ApplicationException', 'ApplicationSignal'} | {
        reason for reason, target in ALERT_KIND.items() if target == 'app'
    }
    infra_reasons = pod_reasons | {'NoEndpoints', 'NetworkDrop'} | {
        reason for reason, target in ALERT_KIND.items() if target in ('pod', 'endpoint')
    }

    def observed(reasons):
        return Incident.signal_reasons.has_any(literal(sorted(reasons), type_=ARRAY(Text)))

    app_intake = Incident.fingerprint.like('application:%')
    app_signal = observed(app_reasons)
    return case(
        (or_(app_intake, app_signal) & observed(infra_reasons), 'mixed'),
        (app_intake, 'application'),
        (CodeActionLink.incident_id.is_not(None), 'mixed'),
        (app_signal, 'application'),
        else_='kubernetes',
    )


def console_history_router(state):
    router = APIRouter(dependencies=[Depends(require_user)])

    @router.get('/api/console/incidents')
    def history(request: Request, kind: Literal['all', 'kubernetes', 'application', 'mixed'] = 'all',
                environment: str = '', system: str = '', service: str = '', status: str = '',
                search: str = Query('', max_length=120), origin: Literal['evaluation','unconfirmed'] | None = None,
                start: date | None = None, end: date | None = None, as_of: int | None = Query(None, ge=0, le=253402300799000000),
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        from .main import _agg, _summary
        from .application.storage import Incident as App
        from .application.browser_access import local_read
        from .application.safety import scrub
        from .application.control import ControlPlane

        coordinator = state.get('unified')
        now = time_ns() // 1000
        # Pre-unification bookmarks used a core id as their anchor.
        old_anchor = as_of if as_of is not None and as_of < 10**12 else None
        anchor = min(now, as_of) if as_of is not None and old_anchor is None else now
        cutoff = datetime.fromtimestamp(anchor / 1e6, timezone.utc)
        try:
            lower, upper = bounds(start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        pattern = search.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        def contains(column):
            return column.ilike('%' + pattern + '%', escape='\\')
        def range_for(column, is_epoch=False):
            return ([column >= (lower.timestamp() if is_epoch else lower)] if lower else []) + (
                [column < (upper.timestamp() if is_epoch else upper)] if upper else [])

        root_kind = _core_kind()
        root_filters = range_for(Incident.created_at)
        if old_anchor is not None:
            root_filters.append(Incident.id <= old_anchor)
        for key, value in ((Incident.workload,service),(Incident.namespace,environment),(Incident.system,system),
                           (root_kind,kind if kind != 'all' else ''),(effective_status_expr(),status)):
            if value:
                root_filters.append(key == value)
        if pattern:
            root_filters.append(or_(contains(Incident.title),contains(Incident.workload),contains(CodeActionLink.application_id),
                                    contains(CodeActionLink.request['reason'].as_string())))
        if origin:
            ev = Incident.fingerprint.like('eval/%')
            root_filters.append(ev if origin == 'evaluation' else ~ev)
        with db.session() as session:
            if old_anchor is not None:
                legacy_time = session.scalar(select(func.max(Incident.created_at)).where(Incident.id <= old_anchor))
                cutoff = legacy_time or datetime.fromtimestamp(0, timezone.utc)
                anchor = int(cutoff.timestamp()*1e6)
            joined = Incident.__table__.outerjoin(CodeActionLink, CodeActionLink.incident_id == Incident.id)
            def core_query(*cols):
                return select(*cols).select_from(joined).where(*root_filters)
            total = session.scalar(core_query(func.count()).where(Incident.created_at <= cutoff)) or 0
            newer = session.scalar(core_query(func.count()).where(Incident.created_at > cutoff)) or 0
            candidates = [(stamp.timestamp() if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc).timestamp(), 1, iid, typ)
                          for iid,stamp,typ in session.execute(core_query(Incident.id,Incident.created_at,root_kind)
                          .where(Incident.created_at <= cutoff).order_by(Incident.created_at.desc(),Incident.id.desc()).limit(offset + limit))]
            apps = {}
            coverage = 'ready' if coordinator else 'not_configured'
            if kind in ('all','application') and origin != 'evaluation' and coordinator:
                local_read(coordinator.application, request)
                # Only linked child ids belong to the core row. A native incident keeps its original identity.
                links = {link.application_id:link for link in session.scalars(select(CodeActionLink))}
                c = coordinator.control
                if not c.store.ready():
                    coverage = 'unavailable'
                else:
                    doc = App.document
                    conds = range_for(App.created_at, True)
                    for key, value in (('environment',environment),('system',system),('service',service)):
                        if value:
                            conds.append(doc[key].as_string() == value)
                    if pattern:
                        conds.append(or_(contains(App.id),contains(doc['reason'].as_string()),contains(doc['service'].as_string())))
                    if status:
                        states = set().union(*ControlPlane.LIST_STATE_GROUPS.values(), {'awaiting_merge','source_queued','recovery_review'})
                        conds.append(App.state.in_([s for s in states if projected_status(s) == status]))
                    root_total, root_newer, root_candidates = total, newer, len(candidates)
                    try:
                        with c.store.read() as other:
                            if links:
                                identities = other.execute(select(App.id,doc['data']['unified_origin'])
                                                           .where(App.id.in_(links)))
                                linked_ids = [iid for iid, origin_doc in identities
                                              if coordinator.matches_origin(links[iid], {'id':iid,'data':{'unified_origin':origin_doc}})]
                                if linked_ids:
                                    conds.append(App.id.not_in(linked_ids))
                            base = select(func.count()).select_from(App).where(*conds)
                            total += other.scalar(base.where(App.created_at <= anchor / 1e6)) or 0
                            newer += other.scalar(base.where(App.created_at > anchor / 1e6)) or 0
                            ids = other.execute(select(App.id,App.created_at).where(*conds,App.created_at <= anchor / 1e6)
                                                .order_by(App.created_at.desc(),App.id.desc()).limit(offset + limit))
                            candidates.extend((ts,0,iid,'application') for iid,ts in ids)
                            selected = sorted(candidates, reverse=True)[offset:offset+limit]
                            native_ids = [i for _,side,i,_ in selected if not side]
                            for i in other.scalars(select(App).where(App.id.in_(native_ids))):
                                d=i.document
                                last=next((s.get('occurred_at') for s in reversed(d.get('stages') or []) if s.get('state')==i.state),None)
                                stamp=datetime.fromtimestamp(i.created_at,timezone.utc).isoformat()
                                apps[i.id]=dict(id=i.id, native_id=i.id, kind='application', title=str(scrub(d.get('reason') or ''))[:240],
                                    workload=d.get('service'), system=d.get('system'), namespace=d.get('environment'),environment=d.get('environment'),
                                    created_at=stamp, closed_at=datetime.fromtimestamp(last,timezone.utc).isoformat() if last and i.state in ('completed','failed') else None,
                                    status=projected_status(i.state), application_state=i.state, severity_level=None)
                    except SQLAlchemyError:
                        # A connection can disappear after the readiness probe. Keep the core page usable.
                        total, newer = root_total, root_newer
                        del candidates[root_candidates:]
                        apps.clear()
                        coverage = 'unavailable'
            page = sorted(candidates,reverse=True)[offset:offset+limit]
            root_ids = [i for _,side,i,_ in page if side]
            agg = _agg(session,root_ids)
            roots = {i.id:_summary(i,agg) for i in session.scalars(select(Incident).where(Incident.id.in_(root_ids)))}
            items = [dict(roots[i],kind=typ) if side else apps[i] for _,side,i,typ in page]
        return dict(items=items,total=total,as_of=old_anchor if old_anchor is not None else anchor,newer_since_as_of=newer,application_coverage=coverage)
    return router
