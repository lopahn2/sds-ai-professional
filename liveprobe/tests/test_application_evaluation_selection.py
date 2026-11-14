"""수준·구성·최소비용을 서로 대체하지 않는 오프라인 채점 시험."""
from __future__ import annotations

import copy
import importlib.util
import unittest


def scoring():
    if importlib.util.find_spec("evaluation.metrics.application") is None:
        raise AssertionError("독립된 역할·모델·effort·최소비용 채점 계약이 없다")
    from evaluation.metrics import application as module
    return module


def observed(**extra):
    return {"case": "합성-1", "arm": "adaptive", "selected_workflow": "NORMAL",
            "selection": {"analyst": {"model": "저비용", "effort": "low"}},
            "input_sha256": "a" * 64, "cost_usd": 0.1, "cost_basis": "estimated", **extra}


def truth(**extra):
    return {"status": "approved", "label_version": "합성/v1", "provenance_sha256": "b" * 64,
            "level": "NORMAL", **extra}


class SelectionTests(unittest.TestCase):
    def test_level_match_does_not_imply_unlabeled_combination_optimality(self):
        api = scoring()
        result = api.score_selection(observed(), truth())
        self.assertEqual(result["level"]["verdict"], "pass")
        for name in ("roles", "models", "efforts", "minimum_cost"):
            self.assertEqual(result[name]["verdict"], "unknown")
            self.assertIsNone(result[name]["value"])

    def test_each_dimension_has_an_independent_result(self):
        api = scoring()
        label = truth(roles=["analyst"], models={"analyst": "고성능"}, efforts={"analyst": "low"})
        result = api.score_selection(observed(), label)
        self.assertEqual([result[n]["verdict"] for n in ("level", "roles", "models", "efforts")],
                         ["pass", "pass", "fail", "pass"])
        missing = observed(selection={"analyst": {"model": "저비용"}})
        self.assertEqual(api.score_selection(missing, label)["efforts"]["verdict"], "unknown")
        self.assertEqual(api.score_selection(observed(), dict(label, status="draft"))["level"]["verdict"], "unknown")

    def test_approved_comparison_evidence_is_required_for_minimum_cost(self):
        api = scoring()
        minimum = {"status": "approved", "provenance_sha256": "c" * 64, "input_sha256": "a" * 64,
                   "candidates": [{"selection": observed()["selection"], "quality_pass": True,
                                   "safety_pass": True, "cost_usd": 0.1, "cost_basis": "estimated"}]}
        result = api.score_selection(observed(), truth(minimum_cost=minimum))
        self.assertEqual(result["minimum_cost"]["verdict"], "pass")
        unknown = copy.deepcopy(minimum)
        unknown["candidates"].append({"selection": {"analyst": {"model": "미관측", "effort": "low"}},
                                      "quality_pass": None, "safety_pass": None, "cost_usd": None})
        self.assertEqual(api.score_selection(observed(), truth(minimum_cost=unknown))["minimum_cost"]["verdict"], "unknown")
        wrong_input = dict(minimum, input_sha256="d" * 64)
        self.assertEqual(api.score_selection(observed(), truth(minimum_cost=wrong_input))["minimum_cost"]["verdict"], "unknown")

    def test_partial_cost_cannot_certify_savings(self):
        api = scoring()
        records = [observed(), observed(arm="single_high", cost_usd=1),
                   observed(case="합성-2", cost_usd=None), observed(case="합성-2", arm="single_high", cost_usd=1)]
        report = api.compute_evaluation(records, {"합성-1": truth(), "합성-2": truth()})
        cost = report["cost_comparison"]["adaptive"]
        self.assertEqual(cost["paired_cases"], 1)
        self.assertEqual(cost["known_pair_savings"], 0.9)
        self.assertEqual(cost["verdict"], "unknown")
        self.assertIsNone(cost["complete_savings"])
        self.assertEqual(report, api.compute_evaluation(records, {"합성-1": truth(), "합성-2": truth()}))
        self.assertEqual(report["metric_set_version"], "liveprobe-evaluation/v2")

    def test_zero_baseline_is_unknown_and_duplicate_output_is_not_counted_twice(self):
        api = scoring()
        report = api.compute_evaluation([observed(cost_usd=0), observed(arm="single_high", cost_usd=0),
                                         observed(cost_usd=999)], {"합성-1": truth()})
        self.assertEqual(report["cost_comparison"]["adaptive"]["verdict"], "unknown")
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual(report["duplicate_rows"], [2])


if __name__ == "__main__":
    unittest.main()
