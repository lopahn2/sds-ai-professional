"""저장 역할 실행·shadow 추천·신뢰도 보정의 순수 시험."""
from __future__ import annotations

import copy
import importlib.util
import unittest


def shadow():
    if importlib.util.find_spec("evaluation.role_replay") is None:
        raise AssertionError("저장 역할 실행을 비교하는 오프라인 재생 계약이 없다")
    from evaluation import role_replay as module
    return module


def saved(model="고성능", effort="high", cost=1.0, quality=1.0, **extra):
    return {"run_id": f"합성-{model}", "case": "합성-1", "role": "challenger", "variant": "execute",
            "model": model, "effort": effort, "input_sha256": "a" * 64, "context_sha256": "b" * 64,
            "output_sha256": "c" * 64, "cost_usd": cost, "cost_basis": "estimated", "safety_pass": True,
            "confidence": 0.8, "quality": {"status": "approved", "definition_sha256": "d" * 64,
            "label_sha256": "e" * 64, "value": quality, "correct": True}, **extra}


class ShadowTests(unittest.TestCase):
    def test_one_paired_context_does_not_hide_another_unpaired_execution(self):
        api = shadow()
        another = saved(run_id="합성-다른문맥", context_sha256="f" * 64)
        result = api.compare_shadow([saved(), another, saved("저비용", "low", 0.2)], role="challenger",
                                    baseline={"model": "고성능", "effort": "high"},
                                    recommendation={"model": "저비용", "effort": "low"})
        self.assertEqual(result["verdict"], "unknown")
        self.assertIsNone(result["complete_cost_savings"])

    def test_observed_omission_is_only_a_stored_outcome_comparison(self):
        api = shadow()
        omission = saved(None, None, 0, variant="omit", run_id="합성-생략")
        result = api.compare_shadow([saved(), omission], role="challenger",
                                    baseline={"model": "고성능", "effort": "high"}, recommendation={"variant": "omit"})
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["pairs"][0]["candidate_run_id"], "합성-생략")
        self.assertFalse(result["operational_policy_changed"])

    def test_stored_low_cost_role_can_be_compared_without_a_new_call(self):
        api = shadow()
        records = [saved(), saved("저비용", "low", 0.2)]
        original = copy.deepcopy(records)
        result = api.compare_shadow(records, role="challenger", baseline={"model": "고성능", "effort": "high"},
                                    recommendation={"model": "저비용", "effort": "low"})
        self.assertEqual(result["paired_cases"], 1)
        self.assertEqual(result["quality_delta"], 0)
        self.assertEqual(result["known_cost_savings"], 0.8)
        self.assertEqual(result["verdict"], "pass")
        self.assertFalse(result["operational_policy_changed"])
        self.assertEqual(records, original)
        self.assertEqual(result["pairs"][0]["candidate_run_id"], "합성-저비용")

    def test_unknown_counterfactual_and_unobserved_omission_remain_unknown(self):
        api = shadow()
        for recommendation in ({"model": "미관측", "effort": "low"}, {"variant": "omit"}):
            result = api.compare_shadow([saved()], role="challenger", baseline={"model": "고성능", "effort": "high"},
                                        recommendation=recommendation)
            self.assertEqual(result["paired_cases"], 0)
            self.assertIsNone(result["quality_delta"])
            self.assertIsNone(result["complete_cost_savings"])
            self.assertEqual(result["verdict"], "unknown")

    def test_different_input_context_or_quality_definition_is_not_a_pair(self):
        api = shadow()
        for changed in ({"input_sha256": "f" * 64}, {"context_sha256": "f" * 64},
                        {"quality": dict(saved()["quality"], definition_sha256="f" * 64)}):
            candidate = dict(saved("저비용", "low", 0.2), **changed)
            result = api.compare_shadow([saved(), candidate], role="challenger",
                                        baseline={"model": "고성능", "effort": "high"},
                                        recommendation={"model": "저비용", "effort": "low"})
            self.assertEqual(result["verdict"], "unknown")
            self.assertEqual(result["paired_cases"], 0)

    def test_known_quality_loss_or_safety_failure_rejects_shadow(self):
        api = shadow()
        for candidate in (saved("저비용", "low", 0.2, quality=0.5), saved("저비용", "low", 0.2, safety_pass=False)):
            result = api.compare_shadow([saved(), candidate], role="challenger", baseline={"model": "고성능", "effort": "high"},
                                        recommendation={"model": "저비용", "effort": "low"})
            self.assertEqual(result["verdict"], "fail")

    def test_partial_cost_and_unapproved_quality_do_not_certify(self):
        api = shadow()
        for candidate in (saved("저비용", "low", None), saved("저비용", "low", 0.2, quality=None)):
            result = api.compare_shadow([saved(), candidate], role="challenger", baseline={"model": "고성능", "effort": "high"},
                                        recommendation={"model": "저비용", "effort": "low"})
            self.assertEqual(result["verdict"], "unknown")

    def test_calibration_and_role_utility_use_only_approved_labels(self):
        api = shadow()
        first = saved()
        second = saved("저비용", "low", 0.2, quality=0.0, confidence=0.2)
        second["quality"]["correct"] = False
        draft = saved("미채점", quality=None)
        report = api.role_utility([first, second, draft])
        self.assertEqual(report["calibration"]["scored"], 2)
        self.assertAlmostEqual(report["calibration"]["brier_score"], 0.04)
        self.assertEqual(len(report["roles"]), 3)
        unknown = next(row for row in report["roles"] if row["model"] == "미채점")
        self.assertIsNone(unknown["mean_quality"])
        self.assertIsNone(report["causal_marginal_utility"])


if __name__ == "__main__":
    unittest.main()
