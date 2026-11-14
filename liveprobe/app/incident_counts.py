"""정돈-05: one definition for incident counts shown in the header, overview, map and lists.

* automatic          — the engine is working on it: open / analyzing / acting
* awaiting_approval  — an action waits for an operator: action_pending
* unclaimed_handoff  — handed off and nobody has claimed it yet (no time window)
* closed             — history: resolved / failed / closed (never counted as live work)

`in_progress` = automatic + awaiting_approval, the same set the header alert and the incident list status filter use.
The map raises a pin for an unclaimed hand-off only for `MAP_HANDOFF_WINDOW_HOURS`; that window is reported next to
the count instead of silently making the numbers differ. Counting never closes or edits an incident.
"""
from __future__ import annotations

from sqlalchemy import func, select

AUTOMATIC = ("open", "analyzing", "acting")
AWAITING_APPROVAL = ("action_pending",)
IN_PROGRESS = AUTOMATIC + AWAITING_APPROVAL
CLOSED = ("resolved", "failed", "closed")


def map_handoff_window_hours() -> float:
    from .flows import HANDOFF_FRESH_S
    return HANDOFF_FRESH_S / 3600


def bucket_of(status: str | None, claimed: bool = False) -> str:
    if status in AUTOMATIC:
        return "automatic"
    if status in AWAITING_APPROVAL:
        return "awaiting_approval"
    if status == "handed_off":
        return "claimed_handoff" if claimed else "unclaimed_handoff"
    return "closed"


def incident_counts(s, namespaces: list[str] | None = None) -> dict:
    """Read-only counts over the whole history (not a recent window). `by_environment` uses the same buckets."""
    from .models import Incident
    rows = s.execute(select(Incident.namespace, Incident.status, Incident.handoff)
                     .where(Incident.status.in_(IN_PROGRESS + ("handed_off",)))).all()
    total = {"automatic": 0, "awaiting_approval": 0, "unclaimed_handoff": 0}
    by_env: dict[str, dict] = {ns: dict(total) for ns in namespaces or []}
    for namespace, status, handoff in rows:
        bucket = bucket_of(status, bool((handoff or {}).get("claimed_by")))
        if bucket not in total:
            continue
        total[bucket] += 1
        if namespace in by_env:
            by_env[namespace][bucket] += 1
    closed = s.scalar(select(func.count()).select_from(Incident).where(Incident.status.in_(CLOSED))) or 0
    return {**total, "in_progress": total["automatic"] + total["awaiting_approval"], "closed_history": closed,
            "map_handoff_window_hours": map_handoff_window_hours(),
            "by_environment": {ns: {**c, "in_progress": c["automatic"] + c["awaiting_approval"]} for ns, c in by_env.items()}}
