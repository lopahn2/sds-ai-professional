"""When the Korean summary pipeline started.

Records made before this moment were never summarised by it. Screens label their missing readings
'과거 데이터 없음' instead of a failure, and nothing back-fills them (no model call for history).
The constant is fixed in the release commit to the cutover time; it is not read from the environment
because a release keeps the image configuration unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Cutover of the stage card summaries (epoch seconds): the release time of the API that summarises them.
READINGS_V2_SINCE = 1795319520.0  # 2026-11-22 12:52 KST


def epoch(value) -> float | None:
    """Seconds since the epoch for a float, a datetime or an ISO string; None when unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    return None


def legacy_at(value, since: float | None = None) -> bool:
    """True when the record predates the summary pipeline. An unknown time is treated as current."""
    t = epoch(value)
    return t is not None and t < (READINGS_V2_SINCE if since is None else since)
