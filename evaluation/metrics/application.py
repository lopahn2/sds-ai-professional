"""저장 출력만으로 독립 선택 정확도와 완전한 쌍 비용을 채점한다."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from evaluation.accounting import summarize_runs, _number
from .contracts import definition_hash, legacy_definition


def definition() -> dict:
    return json.loads((Path(__file__).parent / "snapshots/liveprobe-evaluation-v2.json").read_text("utf-8"))


def valid_sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _score(value, reason=""):
    return {"value": value, "verdict": "unknown" if value is None else "pass" if value else "fail",
            "reason_ko": reason}


def _minimum(run, label):
    ref = label.get("minimum_cost")
    if not isinstance(ref, dict) or ref.get("status") != "approved" or not valid_sha(ref.get("provenance_sha256")):
        return _score(None, "승인된 최소비용 비교 정답이 없다")
    if not valid_sha(run.get("input_sha256")) or ref.get("input_sha256") != run.get("input_sha256"):
        return _score(None, "비교 입력 지문이 다르거나 없다")
    candidates = ref.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return _score(None, "저장된 비교 조합이 없다")
    eligible = []
    selected = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return _score(None, "비교 조합 계약이 올바르지 않다")
        quality, safety = candidate.get("quality_pass"), candidate.get("safety_pass")
        if not isinstance(quality, bool) or not isinstance(safety, bool):
            return _score(None, "미관측 품질·안전 후보가 있다")
        if not quality or not safety:
            continue
        cost = _number(candidate.get("cost_usd"))
        if cost is None or candidate.get("cost_basis") not in ("observed", "estimated"):
            return _score(None, "비교 조합 비용이 미관측이다")
        if candidate.get("cost_basis") != run.get("cost_basis"):
            return _score(None, "서로 다른 비용 기준이다")
        eligible.append(Decimal(str(cost)))
        if candidate.get("selection") == run.get("selection"):
            selected = Decimal(str(cost))
    if selected is None or not eligible:
        return _score(None, "선택 조합의 품질·안전·비용 관측이 없다")
    actual = _number(run.get("cost_usd"))
    if actual is None or Decimal(str(actual)) != selected:
        return _score(None, "선택 조합 비용이 비교 정답과 일치하지 않는다")
    return _score(selected == min(eligible), "승인된 관측 비교 집합 안에서만 판정한다")


def score_selection(run: dict, label: dict | None) -> dict:
    names = definition()["selection_dimensions"]
    if not isinstance(label, dict) or label.get("status") != "approved" or not label.get("label_version") or not valid_sha(label.get("provenance_sha256")):
        return {name: _score(None, "승인된 정답 또는 버전·출처 지문이 없다") for name in names}
    levels = legacy_definition()["workflow_level"]
    actual, expected = run.get("selected_workflow"), label.get("level")
    level = _score(levels[actual] == levels[expected]) if actual in levels and expected in levels else _score(None, "수준 정답 또는 관측값이 없다")
    selection = run.get("selection")
    roles = label.get("roles")
    result = {"level": level}
    result["roles"] = (_score(set(selection) == set(roles))
                       if isinstance(selection, dict) and isinstance(roles, list)
                       else _score(None, "역할 정답 또는 선택 기록이 없다"))
    for name, field in (("models", "model"), ("efforts", "effort")):
        expected_map = label.get(name)
        if (not isinstance(selection, dict) or not isinstance(expected_map, dict)
                or not all(isinstance(v, dict) and isinstance(v.get(field), str) for v in selection.values())
                or not all(isinstance(v, str) for v in expected_map.values())):
            result[name] = _score(None, "차원별 정답 또는 관측 필드가 없다")
        else:
            result[name] = _score({k: v[field] for k, v in selection.items()} == expected_map)
    result["minimum_cost"] = _minimum(run, label)
    return result


def compute_evaluation(records: list[dict], truth: dict[str, dict]) -> dict:
    accepted = {}
    duplicates = []
    for index, run in enumerate(records):
        key = (run["case"], run["arm"])
        if key in accepted:
            duplicates.append(index)
        else:
            accepted[key] = run
    arms = sorted({arm for _, arm in accepted})
    runs = []
    for (case, arm), run in sorted(accepted.items()):
        runs.append({"case": case, "arm": arm, "selection_scores": score_selection(run, truth.get(case)),
                     "accounting": summarize_runs(run["role_executions"]) if isinstance(run.get("role_executions"), list) else None})
    comparison = {}
    for arm in arms:
        if arm == "single_high":
            continue
        subtotal, baseline = Decimal(0), Decimal(0)
        pairs = 0
        bases = set()
        for case in sorted(truth):
            run, base = accepted.get((case, arm)), accepted.get((case, "single_high"))
            if run is None or base is None:
                continue
            a, b = _number(run.get("cost_usd")), _number(base.get("cost_usd"))
            if a is None or b is None or run.get("cost_basis") not in ("observed", "estimated") or run.get("cost_basis") != base.get("cost_basis"):
                continue
            if not valid_sha(run.get("input_sha256")) or run.get("input_sha256") != base.get("input_sha256"):
                continue
            subtotal += Decimal(str(a))
            baseline += Decimal(str(b))
            bases.add(run["cost_basis"])
            pairs += 1
        savings = round(float(1 - subtotal / baseline), 6) if baseline > 0 else None
        complete = pairs == len(truth) and pairs > 0 and len(bases) == 1 and savings is not None
        comparison[arm] = {"paired_cases": pairs, "expected_cases": len(truth), "known_pair_savings": savings,
                           "complete_savings": savings if complete else None,
                           "known_arm_cost_usd": float(subtotal), "known_baseline_cost_usd": float(baseline),
                           "verdict": "unknown" if not complete else "pass" if savings >= 0.30 else "fail"}
    body = {"metric_set_version": "liveprobe-evaluation/v2", "definitions_sha256": definition_hash(definition()),
            "records_sha256": hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
            "truth_sha256": hashlib.sha256(json.dumps(truth, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
            "runs": runs, "duplicate_rows": duplicates, "cost_comparison": comparison,
            "publishable": False, "boundary_ko": "오프라인 계약 채점이며 전체 시스템 성능·운영 승인 증거가 아니다"}
    body["report_digest"] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return body
