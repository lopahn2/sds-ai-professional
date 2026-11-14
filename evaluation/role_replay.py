"""관측된 저장 실행만 비교하며 미관측 모델·생략 결과를 만들지 않는다."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import hashlib
import json

from evaluation.accounting import _number
from evaluation.metrics.application import valid_sha


def _quality(row):
    quality = row.get("quality")
    if (not isinstance(quality, dict) or quality.get("status") != "approved"
            or not valid_sha(quality.get("definition_sha256")) or not valid_sha(quality.get("label_sha256"))):
        return None
    score = quality.get("value")
    if score is None:
        return None
    value = _number(score)
    if value is None or value > 1:
        raise ValueError("품질 점수는 0부터 1 사이여야 한다")
    return value


def _key(row):
    fields = (row.get("input_sha256"), row.get("context_sha256"), row.get("output_sha256"))
    if not all(valid_sha(value) for value in fields) or not row.get("run_id"):
        return None
    return row.get("case"), fields[0], fields[1], (row.get("quality") or {}).get("definition_sha256")


def _matches(row, spec):
    variant = spec.get("variant", "execute")
    if variant not in ("execute", "omit"):
        raise ValueError("저장 비교 유형이 올바르지 않다")
    return row.get("variant", "execute") == variant and (variant == "omit" or (
        row.get("model") == spec.get("model") and row.get("effort") == spec.get("effort")))


def compare_shadow(records: list[dict], *, role: str, baseline: dict, recommendation: dict,
                   minimum_quality: float = 0.8) -> dict:
    if _number(minimum_quality) is None or minimum_quality > 1:
        raise ValueError("품질 하한이 올바르지 않다")
    selected = [row for row in records if row.get("role") == role]
    baseline_rows, candidate_rows = {}, {}
    cases = {row.get("case") for row in selected if _matches(row, baseline)}
    for row in selected:
        key = _key(row)
        if key is None:
            continue
        if _matches(row, baseline):
            baseline_rows.setdefault(key, row)
        if _matches(row, recommendation):
            candidate_rows.setdefault(key, row)
    pairs = []
    costs_complete = True
    for key, base in sorted(baseline_rows.items()):
        candidate = candidate_rows.get(key)
        if candidate is None:
            continue
        a, b = _quality(candidate), _quality(base)
        if a is None or b is None or not isinstance(base.get("safety_pass"), bool) or not isinstance(candidate.get("safety_pass"), bool):
            continue
        ca, cb = _number(candidate.get("cost_usd")), _number(base.get("cost_usd"))
        same_basis = candidate.get("cost_basis") == base.get("cost_basis") and base.get("cost_basis") in ("observed", "estimated")
        if ca is None or cb is None or not same_basis:
            costs_complete = False
        pairs.append({"case": key[0], "baseline_run_id": base["run_id"], "candidate_run_id": candidate["run_id"],
                      "input_sha256": key[1], "context_sha256": key[2], "quality_definition_sha256": key[3],
                      "baseline_output_sha256": base["output_sha256"], "candidate_output_sha256": candidate["output_sha256"],
                      "quality_delta": round(a - b, 6), "quality_ok": a >= minimum_quality and a >= b,
                      "safety_ok": candidate["safety_pass"], "cost_baseline": cb if same_basis else None,
                      "cost_candidate": ca if same_basis else None})
    known = [pair for pair in pairs if pair["cost_baseline"] is not None and pair["cost_candidate"] is not None]
    base_cost = sum((Decimal(str(pair["cost_baseline"])) for pair in known), Decimal(0))
    candidate_cost = sum((Decimal(str(pair["cost_candidate"])) for pair in known), Decimal(0))
    savings = round(float(1 - candidate_cost / base_cost), 6) if base_cost > 0 else None
    paired_cases = {pair["case"] for pair in pairs}
    expected_rows = sum(_matches(row, baseline) for row in selected)
    complete = bool(cases) and len(pairs) == expected_rows and costs_complete and savings is not None
    failed = any(not pair["quality_ok"] or not pair["safety_ok"] for pair in pairs)
    verdict = "fail" if failed else "unknown" if not complete else "pass" if savings is not None and savings >= 0 else "fail"
    result = {"schema": "liveprobe-role-shadow/v1", "role": role, "baseline": baseline,
              "recommendation": recommendation, "expected_cases": len(cases), "paired_cases": len(paired_cases),
              "expected_baseline_rows": expected_rows, "paired_rows": len(pairs),
              "pairs": pairs, "unpaired_cases": sorted(cases - paired_cases),
              "quality_delta": round(sum(pair["quality_delta"] for pair in pairs) / len(pairs), 6) if pairs else None,
              "known_cost_savings": savings, "complete_cost_savings": savings if complete else None,
              "verdict": verdict, "operational_policy_changed": False,
              "boundary_ko": "저장 쌍의 기술통계이며 새 모델 실행·인과적 역할 제거 효과·운영 승인이 아니다"}
    result["replay_digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def role_utility(records: list[dict]) -> dict:
    groups = defaultdict(list)
    calibration = []
    for row in records:
        groups[(row.get("role"), row.get("variant", "execute"), row.get("model"), row.get("effort"))].append(row)
        value = _quality(row)
        label = row.get("quality") or {}
        confidence = row.get("confidence")
        if value is not None and isinstance(label.get("correct"), bool) and confidence is not None:
            confidence = _number(confidence)
            if confidence is None or confidence > 1:
                raise ValueError("신뢰도는 0부터 1 사이여야 한다")
            calibration.append((confidence, int(label["correct"])))
    roles = []
    for (role, variant, model, effort), rows in sorted(groups.items(), key=lambda item: repr(item[0])):
        scores = [score for row in rows if (score := _quality(row)) is not None]
        costs = [_number(row.get("cost_usd")) for row in rows]
        known = sum((Decimal(str(value)) for value in costs if value is not None), Decimal(0))
        roles.append({"role": role, "variant": variant, "model": model, "effort": effort,
                      "stored_rows": len(rows), "scored": len(scores), "mean_quality": sum(scores) / len(scores) if scores else None,
                      "known_cost_usd": float(known), "complete_cost_usd": float(known) if all(value is not None for value in costs) else None})
    bins = []
    for low, high in ((0, 0.5), (0.5, 0.8), (0.8, 1.000001)):
        sample = [(confidence, actual) for confidence, actual in calibration if low <= confidence < high]
        bins.append({"lower": low, "upper": min(high, 1), "count": len(sample),
                     "mean_confidence": sum(confidence for confidence, _ in sample) / len(sample) if sample else None,
                     "observed_accuracy": sum(actual for _, actual in sample) / len(sample) if sample else None})
    return {"schema": "liveprobe-role-utility/v1", "roles": roles, "causal_marginal_utility": None,
            "calibration": {"scored": len(calibration), "brier_score": sum((confidence - actual) ** 2 for confidence, actual in calibration) / len(calibration) if calibration else None,
                            "bins": bins},
            "boundary_ko": "승인 라벨을 가진 저장 행의 보정 입력이며 새 라우터 학습·운영 매핑 변경은 하지 않는다"}
