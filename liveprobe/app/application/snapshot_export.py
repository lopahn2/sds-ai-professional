"""정돈-08 Application: read-only, hash-based snapshot of the isolated Application store for offline analysis.

Same rules as the K8s exporter (app/snapshot_export.py): read-only session, no raw reason/log/source/diff/model
output/credential text (SHA-256 only), stored values copied as recorded, missing values stay null. The Application
store has no explicit evaluation marker, so every incident is `unconfirmed`. Role rows become
`evaluation.offline utility` records with null cost (the store does not record a price) and null quality labels.

Usage: python -m app.application.snapshot_export --out <new file> [--as-of <created_at epoch seconds>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "liveprobe-application-history-snapshot/v1"


def sha(value) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def _tests(t):
    t = t if isinstance(t, dict) else {}
    return {k: t.get(k) for k in ("before_failed", "after_passed", "regression_executed", "total", "failed", "report_sha256")}


def _stage_results(release):
    out = {}
    for env, s in ((release or {}).get("stage_results") or {}).items():
        s = s if isinstance(s, dict) else {}
        err = s.get("error")
        out[env] = {"deployed": s.get("deployed"), "verified": s.get("verified"), "write_outcome": s.get("write_outcome"),
                    "error_code": err.get("code") if isinstance(err, dict) else err if isinstance(err, str) else None}
    return out


def build_snapshot(session, *, as_of: float | None = None) -> dict:
    from sqlalchemy import func, select
    from .storage import Event, Incident
    newest = session.scalar(select(func.max(Incident.created_at)))
    anchor = newest if as_of is None or newest is None else min(float(as_of), newest)
    q = select(Incident).order_by(Incident.created_at, Incident.id)
    if anchor is not None:
        q = q.where(Incident.created_at <= anchor)
    rows = list(session.scalars(q))
    ids = [r.id for r in rows]
    events = dict(session.execute(select(Event.incident_id, func.count()).where(Event.incident_id.in_(ids))
                                  .group_by(Event.incident_id)).all()) if ids else {}
    incidents, records = [], []
    for row in rows:
        doc = row.document if isinstance(row.document, dict) else {}
        data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        release, patch = data.get("release") or {}, data.get("patch") or {}
        roles = ((data.get("analysis") or {}).get("roles")) or []
        error = doc.get("error") if isinstance(doc.get("error"), dict) else {}
        incidents.append({
            "id": row.id, "origin": "unconfirmed", "state": row.state, "created_at": row.created_at,
            "environment": doc.get("environment"), "service": doc.get("service"), "system": doc.get("system"),
            "reason_sha256": sha(doc.get("reason")), "catalog_sha256": data.get("catalog_sha256"),
            "stages": [{"state": st.get("state"), "occurred_at": st.get("occurred_at"),
                        "code": (st.get("details") or {}).get("code")} for st in doc.get("stages") or [] if isinstance(st, dict)],
            "error_code": error.get("code"), "event_count": events.get(row.id, 0),
            "collection_sha256": sha(data.get("collection")), "source_snapshot_sha256": sha(data.get("source_snapshot")),
            "roles": [{"role": r.get("role"), "model": r.get("model"), "effort": r.get("effort"), "provider": r.get("provider"),
                       "usage": r.get("usage"), "occurred_at": r.get("occurred_at"), "output_sha256": r.get("output_sha256")}
                      for r in roles if isinstance(r, dict)],
            "patch": {"diff_sha256": patch.get("diff_sha256"), "tests_added": len(patch.get("tests_added") or [])} if patch else None,
            "tests": _tests(release.get("tests") or (data.get("test_build") or {}).get("tests")),
            "release": {"release_id": release.get("release_id"), "artifact_sha256": release.get("artifact_sha256"),
                        "plan_sha256": release.get("plan_sha256"), "valid": release.get("valid"),
                        "invalidation_reason": release.get("invalidation_reason"),
                        "user_test_passed": (release.get("user_test") or {}).get("passed"),
                        "approved": bool(release.get("approval")) if release else None,
                        "stage_results": _stage_results(release)} if release else None,
        })
        for n, r in enumerate(x for x in roles if isinstance(x, dict)):
            records.append({"case": f"app:{row.id}", "run_id": f"app-role:{row.id}:{n}", "role": r.get("role"),
                            "variant": "execute", "model": r.get("model"), "effort": r.get("effort"), "origin": "unconfirmed",
                            "environment": doc.get("environment"), "cost_usd": None, "cost_basis": None,
                            "output_sha256": r.get("output_sha256"), "input_sha256": None, "context_sha256": None, "quality": None})
    body = {"schema": SCHEMA, "product": "liveprobe-application", "as_of_created_at": anchor,
            "counts": {"incidents": len(incidents), "role_rows": len(records)},
            "definition": {"origin": "no explicit evaluation marker in the Application store; all unconfirmed",
                           "hashes": "sha256 of canonical JSON (sort_keys, compact, utf-8); null stays null",
                           "cost": "not stored by the Application store; null (unknown), never 0"},
            "incidents": incidents, "records": records}
    return {**body, "content_sha256": sha(body)}


def export(store, path: Path, *, as_of: float | None = None) -> dict:
    if not store.ready():
        raise RuntimeError("store_not_ready")
    with store.read() as session:
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        snap = build_snapshot(session, as_of=as_of)
    snap = {**snap, "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        json.dump(snap, fh, sort_keys=True, ensure_ascii=False, indent=2)
        fh.write("\n")
    return {"path": str(path), "schema": SCHEMA, "content_sha256": snap["content_sha256"], **snap["counts"]}


def main(argv=None, *, settings=None) -> int:
    from .settings import ApplicationSettings
    from .storage import Store
    parser = argparse.ArgumentParser(description="Read-only hashed snapshot of the Application store.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--as-of", type=float)
    args = parser.parse_args(argv)
    store = Store(settings or ApplicationSettings.from_env())
    try:
        receipt = export(store, args.out, as_of=args.as_of)
    except FileExistsError:
        print(json.dumps({"ok": False, "code": "output_exists"}))
        return 2
    except Exception:  # noqa: BLE001 - never print connection strings or stored content
        print(json.dumps({"ok": False, "code": "export_failed"}))
        return 1
    finally:
        if store.engine is not None:
            store.engine.dispose()
    print(json.dumps({"ok": True, **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
