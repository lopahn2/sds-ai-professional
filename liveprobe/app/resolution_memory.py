"""Verified resolutions as retrievable memory, and the precedent check that lets a recurrence cost less.

Before this, only incidents the engine itself closed as `resolved` were indexed, and only a summary written in a
fixed vocabulary. A hand-off the operator fixed could only end `closed`, so the knowledge of what actually fixed
it never became memory and a recurrence paid full discovery cost again.

Here an operator (or an evaluation harness acting as one) records the verified fix when closing a hand-off. The
memory document carries a deterministic signature computed from the evidence at analysis time — finding kinds and
signals, never free text from logs — plus the operator's verified cause and steps. A later incident whose own
signature matches the precedent closely runs the lighter analysis group, and the analysts receive the precedent as
a cited reference to confirm or refute against the current evidence. A precedent is a hypothesis, never authority:
it cannot propose or approve a change by itself.

Evaluation incidents (fingerprint `eval/`) are indexed under the separate `eval_memory` kind, which production
retrieval never asks for, so benchmark memory cannot leak into live analysis.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .redact import redact

MATCH_JACCARD = 0.5
MAX_STEPS = 6
SIGNATURE_LINE = re.compile(r"^signature: (.*)$", re.M)
TEXT = re.compile(r"[^\w\s.,:;()/=+\-%'\"#<>\[\]가-힣]")


def signature(findings: list[dict] | None, dependencies: list[dict] | None, signals: list[str] | None) -> list[str]:
    """Deterministic tokens from structured evidence only."""
    tokens = {f"signal:{s}" for s in signals or [] if isinstance(s, str) and s}
    for f in findings or []:
        kind = f.get("kind")
        if kind in ("cpu_limit_binding", "namespace_default_limits", "recent_restart"):
            tokens.add(kind)
        if kind in ("cpu_limit_binding", "cpu_limit_from_admission") and f.get("limit_in_deployment_template") is False:
            tokens.add("limit_from_admission")
        # Only what was observed during the incident. Static facts about code and configuration (a latent leak in the
        # image, retry defaults, access scripts) hold for every incident on the workload and would dilute the match.
        if kind == "caller_retry_amplification":
            tokens.add(kind)
        if kind == "recent_restart" and (f.get("liveness_failures") or 0) > 0:
            tokens.add("liveness_restart")
        if kind == "db_pool_state":
            active, limit = f.get("active"), f.get("max")
            if isinstance(active, (int, float)) and isinstance(limit, (int, float)) and limit and active >= limit:
                tokens.add("pool_saturated")
            trend = f.get("active_trend") or {}
            if trend.get("never_released"):
                tokens.add("pool_never_released")
            if trend.get("resets_to_minimum"):
                tokens.add("pool_resets")
    for d in dependencies or []:
        ready = d.get("ready_endpoints")
        if isinstance(ready, int) and ready >= 2:
            tokens.add("dependency_multi_endpoint")
        if any(p.get("error_logs") for p in d.get("pods") or [] if p.get("selected_by_service", True)):
            tokens.add("dependency_errors")
        if any(p.get("error_logs") for p in d.get("pods") or [] if p.get("selected_by_service") is False):
            tokens.add("dependency_unselected_errors")
    return sorted(tokens)


def _split(tokens) -> tuple[set[str], set[str]]:
    tokens = set(tokens or [])
    signals = {t for t in tokens if t.startswith("signal:")}
    return signals, tokens - signals


def similarity(current: list[str], past: list[str]) -> float:
    cs, cf = _split(current)
    ps, pf = _split(past)
    if not cf or not pf or (cs and ps and not cs & ps):
        return 0.0
    return len(cf & pf) / len(cf | pf)


def _clean(text, limit: int) -> str:
    value = redact(str(text or "")) or ""
    return TEXT.sub("", value).strip()[:limit]


def resolution_record(cause: str, steps: list[str], verification: str, recorded_by: str,
                      signature_tokens: list[str] | None) -> dict:
    steps = [s for s in (_clean(x, 300) for x in steps or []) if s][:MAX_STEPS]
    if not steps:
        raise ValueError("a verified resolution needs at least one step")
    return {"verified": True, "cause": _clean(cause, 400), "steps": steps,
            "verification": _clean(verification, 300), "recorded_by": _clean(recorded_by, 80),
            "recorded_at": datetime.now(timezone.utc).isoformat(), "signature": list(signature_tokens or [])}


def memory_text(inc, resolution: dict) -> str:
    lines = [f"# Verified resolution of incident {inc.id} ({inc.namespace}/{inc.workload})",
             f"signature: {' '.join(resolution.get('signature') or [])}",
             f"signals: {' '.join(inc.signal_reasons or [])}",
             f"cause: {resolution.get('cause') or ''}",
             "verified fix:"]
    lines += [f"{i}. {step}" for i, step in enumerate(resolution.get("steps") or [], 1)]
    if resolution.get("verification"):
        lines.append(f"verification: {resolution['verification']}")
    return "\n".join(lines) + "\n"


def resolution_documents(s) -> list:
    """Closed hand-offs with an operator-verified resolution. Evidence rows are never read."""
    from sqlalchemy import select
    from .models import Incident
    from .rag_ingest import Document

    out = []
    for inc in s.scalars(select(Incident).where(Incident.status == "closed", Incident.closed_at.is_not(None))):
        resolution = (inc.handoff or {}).get("resolution") or {}
        if resolution.get("verified") is not True or not resolution.get("steps"):
            continue
        evaluation = isinstance(inc.fingerprint, str) and inc.fingerprint.startswith("eval/")
        out.append(Document(key=f"resolution-{inc.id}.md", title=f"Verified resolution {inc.id}",
                            kind="eval_memory" if evaluation else "past_incident", source="verified-resolution",
                            license="internal", source_uri=f"incident://{inc.id}", incident_id=inc.id,
                            text=memory_text(inc, resolution),
                            meta={"closed_at": inc.closed_at.isoformat(), "signature": resolution.get("signature") or []}))
    return out


def precedent(hits: list[dict], current: list[str]) -> dict | None:
    """The closest verified resolution among the retrieved hits, if its signature matches the current one."""
    best = None
    for hit in hits or []:
        if hit.get("source") != "verified-resolution":
            continue
        m = SIGNATURE_LINE.search(str(hit.get("content") or ""))
        past = m.group(1).split() if m else []
        score = similarity(current, past)
        if score >= MATCH_JACCARD and (best is None or score > best["similarity"]):
            best = {"ref": hit.get("ref"), "doc": hit.get("doc"), "similarity": round(score, 2), "signature": past}
    return best


def signature_from_evidence(s, inc) -> list[str]:
    """Recompute the signature from an incident's first stored collection (hand-offs recorded before signatures)."""
    from sqlalchemy import select
    from . import evidence_depth as D
    from .evidence import Bundle
    from .models import Evidence

    rows = list(s.scalars(select(Evidence).where(Evidence.incident_id == inc.id).order_by(Evidence.id)))
    first: dict = {}
    stamp = None
    deps: dict[str, dict] = {}
    for r in rows:
        if r.error is not None or r.payload is None:
            continue
        if r.source == "dependency" and isinstance(r.payload, dict) and r.payload.get("service"):
            deps.setdefault(r.payload["service"], r.payload)
        elif r.source not in first:
            first[r.source] = r.payload
            if r.source == "pod_status":
                stamp = r.collected_at
    b = Bundle(inc.namespace, inc.workload)
    b.pods = first.get("pod_status") if isinstance(first.get("pod_status"), list) else []
    b.deployment = first.get("deployment") if isinstance(first.get("deployment"), dict) else None
    b.metrics = {k: v for k, v in (first.get("prometheus") or {}).items() if not str(k).startswith("_")}
    b.events = first.get("k8s_event") if isinstance(first.get("k8s_event"), list) else []
    b.limit_ranges = first.get("limitrange") if isinstance(first.get("limitrange"), list) else []
    b.pool_trend = first.get("pool_trend") if isinstance(first.get("pool_trend"), dict) else None
    b.caller_load = first.get("caller_load") if isinstance(first.get("caller_load"), dict) else None
    b.source_view = first.get("source") if isinstance(first.get("source"), dict) else None
    b.usage_history = first.get("usage_history") if isinstance(first.get("usage_history"), dict) else None
    found = D.findings(b, (b.deployment or {}).get("probes"), stamp)
    return signature(found, list(deps.values()), list(inc.signal_reasons or []))
