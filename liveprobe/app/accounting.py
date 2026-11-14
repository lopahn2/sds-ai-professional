"""저장된 역할 실행의 완전합계와 알려진 부분합을 분리한다(운영 이미지에 포함되는 순수 구현).

모델 등급 정의는 평가 정의(evaluation/metrics/snapshots)에 있고 운영 이미지에는 없다. 정의가 주어지지 않으면
고성능 등급 집계를 추정하지 않고 null로 둔다. 호스트 평가는 evaluation.accounting이 과거 정의를 넘겨 계산한다.
"""
from __future__ import annotations

from decimal import Decimal
import math


def _number(value, *, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("계측값은 유한한 음이 아닌 수 또는 null이어야 한다")
    if integer and not isinstance(value, int):
        raise ValueError("토큰·시도 횟수는 정수여야 한다")
    return value


def _sum(rows, field, known_field=None, *, integer=False):
    total = Decimal(0)
    missing = 0
    for row in rows:
        value = _number(row.get(field), integer=integer)
        if value is None:
            missing += 1
            value = _number((row.get("usage_summary") or {}).get(known_field), integer=integer) if known_field else None
        if value is not None:
            total += Decimal(str(value))
    known = int(total) if integer else float(total)
    return {"total": None if missing else known, "known_subtotal": known, "missing_rows": missing}


def summarize_runs(rows: list[dict], *, escalation_events=None, model_class=None, model_class_source=None) -> dict:
    if model_class is None:
        high_rows = unknown = None
        high_attempts = {"total": None, "known_subtotal": None, "missing_rows": None}
    else:
        high = [row for row in rows if model_class.get(row.get("model")) == "high"]
        unknown = sum(row.get("model") not in model_class for row in rows)
        high_attempts = _sum(high, "attempts", integer=True)
        if unknown:
            high_attempts["total"] = None
        high_rows = None if unknown else len(high)
    return {"schema": "liveprobe-accounting/v2", "role_execution_rows": len(rows),
            "provider_attempts": _sum(rows, "attempts", integer=True),
            "high_class_role_rows": high_rows,
            "high_class_provider_attempts": high_attempts, "unmapped_model_rows": unknown,
            "model_class_source": model_class_source if model_class is not None else None,
            "escalations": None if escalation_events is None else len(escalation_events),
            "input_tokens": _sum(rows, "input_tokens", "known_input_tokens", integer=True),
            "output_tokens": _sum(rows, "output_tokens", "known_output_tokens", integer=True),
            "cost_usd": _sum(rows, "cost", "known_cost_usd"),
            "agent_ms": _sum(rows, "duration_ms", integer=True)}


COST_STATUSES = ("observed", "partial", "unobserved", "no_runs", "invalid")


def cost_view(role_rows: int, observed_rows: int, observed_sum) -> dict:
    """정돈-07: one display contract for incident cost, shared by list, detail and narrative APIs.

    * observed  — every role execution has a cost; `total_cost_usd` is the full total (a real 0 stays 0).
    * partial   — some rows have no cost; the total is unknown (None) and only the known subtotal is shown.
    * unobserved— no row has a cost; total and subtotal are unknown.
    * no_runs   — no role execution was recorded; nothing was spent through a recorded run (0).
    Historical costs are reported as stored; they are never re-priced with the current price table.
    """
    if role_rows == 0:
        return {"total_cost_usd": 0.0, "known_cost_usd": 0.0, "cost_missing_runs": 0, "cost_status": "no_runs"}
    try:
        subtotal = _number(observed_sum) if observed_rows else 0
    except ValueError:
        return {"total_cost_usd": None, "known_cost_usd": None, "cost_missing_runs": None, "cost_status": "invalid"}
    missing = role_rows - observed_rows
    known = round(float(subtotal or 0), 6)
    status = "observed" if missing == 0 else "unobserved" if observed_rows == 0 else "partial"
    return {"total_cost_usd": known if missing == 0 else None, "known_cost_usd": known if observed_rows else None,
            "cost_missing_runs": missing, "cost_status": status}


def cost_of(values: list) -> dict:
    """Same contract as `cost_view`, computed with the shared `_sum` rules from per-run stored costs."""
    try:
        summed = _sum([{"cost": v} for v in values], "cost")
    except ValueError:
        return {"total_cost_usd": None, "known_cost_usd": None, "cost_missing_runs": None, "cost_status": "invalid"}
    observed = len(values) - summed["missing_rows"]
    return cost_view(len(values), observed, summed["known_subtotal"] if observed else None)


def export_runs(runs, usage_summaries=None) -> list[dict]:
    summaries = usage_summaries or {}
    return [dict(id=r.id, role=r.role, model=r.model, effort=r.reasoning_effort,
                 attempts=summaries.get(r.id, {}).get("provider_attempts"),
                 validation_attempts=summaries.get(r.id, {}).get("validation_rounds", r.attempts), status=r.status,
                 input_tokens=r.input_tokens, output_tokens=r.output_tokens, cost=r.estimated_cost_usd,
                 duration_ms=r.duration_ms, output=r.output, error=r.error, usage_summary=summaries.get(r.id, {}))
            for r in runs]
