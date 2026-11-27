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

Incidents the engine itself resolved are memory too. An allowlisted action succeeded and the accountability
check explained the recovery. These become `observed-resolution` documents built from fixed vocabulary only:
signature, signal names, one sentence per action type and the fact of observed recovery, with no summary,
log or model text. The detection family stays in metadata only (prompts never see label-like keys). They inform analysis, but only an operator-verified resolution lets a
precedent lower the analysis group.

Continuity: a follow-up incident on the same workload, after an operator acted on the previous one, has a
different signature by construction (the action changed the state). It inherits the previous incident's
precedent at a lower similarity bar, without lowering the group. The precedent's verified steps travel as
check items that analysts must confirm or refute against current evidence, never as instructions.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .redact import redact

MATCH_JACCARD = 0.5
CONTINUED_JACCARD = 0.25         # inherited precedent: same workload, recent incident that used it
CONTINUITY_MINUTES = 120
MAX_STEPS = 6
PRECEDENT_SOURCES = ("verified-resolution", "observed-resolution")
# One fixed sentence per allowlisted action type; parameters (images, values) never enter memory text.
ACTION_TEXT = {
    "rollout_undo": "roll the Deployment back to its previous revision",
    "rollout_restart": "restart the Deployment rollout",
    "set_memory_limit": "raise the container memory limit",
    "configmap_restore": "restore the previous ConfigMap content",
    "pod_delete": "delete the failing Pod so its controller recreates it",
    "scale": "scale the Deployment replicas",
}
_SIGNAL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_STEP_LINE = re.compile(r"^\d+\. (.+)$", re.M)
CHECK_NOTE = ("Steps of a verified precedent. Confirm or refute each against current evidence; when the evidence "
              "that would decide a step was not collected, list it as a verification step. A precedent alone "
              "never justifies an action.")
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


def precedent(hits: list[dict], current: list[str], threshold: float = MATCH_JACCARD) -> dict | None:
    """The closest resolution memory among the retrieved hits, if its signature matches the current one.

    Operator-verified resolutions win ties over observed ones; `verified` says which kind matched."""
    best = None
    for hit in hits or []:
        if hit.get("source") not in PRECEDENT_SOURCES:
            continue
        m = SIGNATURE_LINE.search(str(hit.get("content") or ""))
        past = m.group(1).split() if m else []
        score = similarity(current, past)
        verified = hit.get("source") == "verified-resolution"
        if score >= threshold and (best is None or (score, verified) > (best["similarity"], best["verified"])):
            best = {"ref": hit.get("ref"), "doc": hit.get("doc"), "similarity": round(score, 2), "signature": past,
                    "verified": verified}
    return best


def steps_of(content: str) -> list[str]:
    """Verified steps of a memory document (the numbered lines under `verified fix:`)."""
    text = str(content or "")
    tail = text.split("verified fix:", 1)[1] if "verified fix:" in text else ""
    return [s.strip() for s in _STEP_LINE.findall(tail)][:MAX_STEPS]


def precedent_checks(match: dict | None, hits: list[dict]) -> dict | None:
    """Check items for the analysts and the hand-off, from the matched precedent's verified steps."""
    if not match:
        return None
    hit = next((h for h in hits or [] if h.get("ref") == match.get("ref")), None)
    steps = steps_of(hit.get("content") if hit else "")
    if not steps:
        return None
    return {"ref": match["ref"], "doc": match.get("doc"), "similarity": match.get("similarity"),
            "inherited": bool(match.get("inherited")), "note": CHECK_NOTE, "items": steps}


def inherited_precedent(previous: list[dict], current: list[str], allowed_kinds: list[str] | None,
                        threshold: float = CONTINUED_JACCARD) -> dict | None:
    """Precedent carried over from a recent incident on the same workload.

    `previous` holds {"incident_id", "precedent": {...doc, signature}, "kind"} for recent incidents, newest first.
    Only a document of a kind the current search may see is inherited (production never sees `eval_memory`)."""
    for prev in previous or []:
        p = prev.get("precedent") or {}
        kind = prev.get("kind")
        if not p.get("doc") or (allowed_kinds is not None and kind not in allowed_kinds):
            continue
        score = similarity(current, p.get("signature") or [])
        if score >= threshold:
            return {"doc": p["doc"], "similarity": round(score, 2), "signature": list(p.get("signature") or []),
                    "verified": bool(p.get("verified", True)), "inherited": True,
                    "from_incident": prev.get("incident_id")}
    return None


def signature_from_evidence(s, inc) -> list[str]:
    """Recompute the signature from an incident's first stored collection (hand-offs recorded before signatures)."""
    found, deps = observations_from_evidence(s, inc)
    return signature(found, deps, list(inc.signal_reasons or []))


def observations_from_evidence(s, inc) -> tuple[list[dict], list[dict]]:
    """(findings, dependencies) recomputed from the incident's first stored collection."""
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
    return found, list(deps.values())


_OBSERVED_CACHE: dict[int, tuple] = {}


def observed_memory_text(inc, action_type: str, signature_tokens: list[str]) -> str:
    signals = [x for x in (inc.signal_reasons or []) if isinstance(x, str) and _SIGNAL_NAME.match(x)]
    # Signature and steps first: if the text ever spans two chunks, the precedent line and its steps stay together.
    lines = [f"# Observed resolution of incident {inc.id} ({inc.namespace}/{inc.workload})",
             f"signature: {' '.join(signature_tokens)}",
             "verified fix:",
             f"1. {ACTION_TEXT[action_type]}",
             f"signals: {' '.join(signals)}",
             "cause: not recorded (automatic action, observed recovery)",
             "verification: original symptom cleared after the action"]
    return "\n".join(lines) + "\n"


def observed_resolution_documents(s) -> list:
    """Engine-resolved production incidents whose recovery followed a succeeded allowlisted action."""
    from sqlalchemy import select
    from .models import Action, Incident
    from .rag_ingest import Document

    out = []
    incidents = s.scalars(select(Incident).where(Incident.status == "resolved", Incident.closed_at.is_not(None),
                                                 ~Incident.fingerprint.like("eval/%")))
    for inc in incidents:
        act = s.scalars(select(Action).where(Action.incident_id == inc.id, Action.executed.is_(True),
                                             Action.status == "succeeded").order_by(Action.id.desc())).first()
        if act is None or act.type not in ACTION_TEXT:
            continue
        cached = _OBSERVED_CACHE.get(inc.id)
        if cached is None:
            # A resolved incident's evidence never changes, so the signature is computed once per process.
            cached = (signature_from_evidence(s, inc),)
            _OBSERVED_CACHE[inc.id] = cached
        tokens = cached[0]
        out.append(Document(key=f"observed-{inc.id}.md", title=f"Observed resolution {inc.id}", kind="past_incident",
                            source="observed-resolution", license="internal", source_uri=f"incident://{inc.id}",
                            incident_id=inc.id, text=observed_memory_text(inc, act.type, tokens),
                            meta={"closed_at": inc.closed_at.isoformat(), "signature": tokens,
                                  "family": [inc.family_hint] if inc.family_hint else [],
                                  "action_type": act.type}))
    return out
