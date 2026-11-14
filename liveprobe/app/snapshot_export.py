"""정돈-08: read-only, hash-based snapshot of stored K8s incident history for offline analysis.

* Reads only. Every session is rolled back; nothing in the database changes and no model, cluster or job is touched.
* No raw trace, log, evidence payload, prompt, model output text, error text or credential leaves the database:
  those are represented by SHA-256 of their canonical JSON. Identifiers, models, statuses, timings and stored
  usage/cost numbers are copied as recorded (never re-priced with today's price table).
* Provenance comes only from the explicit `eval/` fingerprint marker (`narrative.incident_origin`); everything else
  is `unconfirmed`. Nothing is inferred from titles, dates or names.
* Missing values stay null/unknown. `records` is an `evaluation.offline utility` input; it has no input/context
  hashes and no quality labels because the database does not store comparable inputs or approved labels, so shadow
  comparisons correctly find no pairs instead of inventing them.
* `content_sha256` covers everything except `exported_at`: the same database state and the same `as_of` give the same
  hash, which is how a re-export proves the stored history did not change.

Usage: python -m app.snapshot_export --out <new file> [--as-of <max incident id>] [--origin evaluation|unconfirmed]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "liveprobe-k8s-history-snapshot/v1"


def sha(value) -> str | None:
    """SHA-256 of canonical JSON; None stays None (absent is not the same as an empty value)."""
    if value is None:
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def build_snapshot(s, *, as_of: int | None = None, origin: str | None = None, environment_of=None) -> dict:
    """Pure read over an open session. `environment_of(namespace)` maps a namespace to its configured environment."""
    from sqlalchemy import func, select
    from .accounting import cost_of, export_runs, summarize_runs
    from .models import AgentRun, Evidence, Incident, RoutingDecision
    from .narrative import incident_origin
    if origin not in (None, "evaluation", "unconfirmed"):
        raise ValueError("origin must be evaluation or unconfirmed")
    newest = s.scalar(select(func.max(Incident.id))) or 0
    anchor = newest if as_of is None else min(int(as_of), newest)
    q = select(Incident).where(Incident.id <= anchor)
    if origin == "evaluation":
        q = q.where(Incident.fingerprint.like("eval/%"))
    elif origin == "unconfirmed":
        q = q.where(~Incident.fingerprint.like("eval/%"))
    incidents = list(s.scalars(q.order_by(Incident.id)))
    ids = [i.id for i in incidents]
    runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id.in_(ids)).order_by(AgentRun.id))) if ids else []
    routes = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id.in_(ids)).order_by(RoutingDecision.id))) if ids else []
    evidence = list(s.scalars(select(Evidence).where(Evidence.incident_id.in_(ids)).order_by(Evidence.id))) if ids else []
    by = lambda rows: {iid: [r for r in rows if r.incident_id == iid] for iid in ids}  # noqa: E731
    runs_by, routes_by, ev_by = by(runs), by(routes), by(evidence)
    env = environment_of or (lambda ns: None)
    out_incidents, records = [], []
    for i in incidents:
        prov = incident_origin(i.fingerprint)
        i_runs = runs_by[i.id]
        exported = export_runs(i_runs)
        out_incidents.append({
            "id": i.id, "origin": prov, "namespace": i.namespace, "environment": env(i.namespace),
            "system": i.system, "workload": i.workload, "status": i.status, "severity_level": i.severity_level,
            "signal_count": i.signal_count, "created_at": _iso(i.created_at), "closed_at": _iso(i.closed_at),
            "fingerprint_sha256": sha(i.fingerprint), "title_sha256": sha(i.title),
            "signal_reasons_sha256": sha(i.signal_reasons), "handoff_sha256": sha(i.handoff),
            "routing": [{"id": r.id, "level": r.level, "workflow": r.workflow, "decided_by": r.decided_by,
                         "confidence": r.confidence, "features_sha256": sha(r.features), "rationale_sha256": sha(r.rationale),
                         "policy_sha256": sha(r.policy), "created_at": _iso(r.created_at)} for r in routes_by[i.id]],
            "evidence": [{"id": e.id, "source": e.source, "target_sha256": sha(e.target), "collected_at": _iso(e.collected_at),
                          "payload_sha256": sha(e.payload), "failed": e.error is not None} for e in ev_by[i.id]],
            "runs": [{"id": r.id, "role": r.role, "provider": r.provider, "model": r.model, "effort": r.reasoning_effort,
                      "status": r.status, "attempts": r.attempts, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                      "cost_usd": r.estimated_cost_usd, "duration_ms": r.duration_ms, "output_sha256": sha(r.output),
                      "error_sha256": sha(r.error), "created_at": _iso(r.created_at)} for r in i_runs],
            "cost": cost_of([r.estimated_cost_usd for r in i_runs]),
            "accounting": summarize_runs(exported),
        })
        for r in i_runs:
            records.append({"case": f"k8s:{i.id}", "run_id": f"k8s-run:{r.id}", "role": r.role, "variant": "execute",
                            "model": r.model, "effort": r.reasoning_effort, "status": r.status, "origin": prov,
                            "environment": env(i.namespace), "cost_usd": r.estimated_cost_usd,
                            "cost_basis": "estimated" if r.estimated_cost_usd is not None else None,
                            "output_sha256": sha(r.output), "input_sha256": None, "context_sha256": None, "quality": None})
    body = {"schema": SCHEMA, "product": "liveprobe-k8s", "as_of_incident_id": anchor,
            "filters": {"origin": origin}, "counts": {"incidents": len(out_incidents), "runs": len(runs),
                                                      "routing_decisions": len(routes), "evidence": len(evidence)},
            "definition": {"origin": "eval/ fingerprint marker only; otherwise unconfirmed",
                           "hashes": "sha256 of canonical JSON (sort_keys, compact, utf-8); null stays null",
                           "cost": "stored estimated_cost_usd as recorded; never re-priced",
                           "records": "evaluation.offline utility rows; input/context hashes and quality labels are not stored, so they are null"},
            "incidents": out_incidents, "records": records}
    return {**body, "content_sha256": sha(body)}


def export(path: Path, *, as_of: int | None = None, origin: str | None = None) -> dict:
    """Write a new snapshot file (never overwrites). Returns a small receipt without any record content."""
    from . import db
    from .config import get_app_config
    cfg = get_app_config()
    with db.session() as s:
        try:
            if s.get_bind().dialect.name == "postgresql":
                from sqlalchemy import text
                s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            snap = build_snapshot(s, as_of=as_of, origin=origin, environment_of=cfg.environment)
        finally:
            s.rollback()
    snap = {**snap, "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        json.dump(snap, fh, sort_keys=True, ensure_ascii=False, indent=2)
        fh.write("\n")
    return {"path": str(path), "schema": SCHEMA, "content_sha256": snap["content_sha256"], **snap["counts"],
            "as_of_incident_id": snap["as_of_incident_id"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only hashed snapshot of stored K8s incident history.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--as-of", type=int)
    parser.add_argument("--origin", choices=("evaluation", "unconfirmed"))
    args = parser.parse_args(argv)
    try:
        from . import db
        from .config import get_settings
        db.init_db(get_settings().sqlalchemy_url())
        receipt = export(args.out, as_of=args.as_of, origin=args.origin)
    except FileExistsError:
        print(json.dumps({"ok": False, "code": "output_exists"}))
        return 2
    except Exception:  # noqa: BLE001 - never print connection strings or stored content
        print(json.dumps({"ok": False, "code": "export_failed"}))
        return 1
    print(json.dumps({"ok": True, **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
