"""평가 정의와 과거 원본 보존의 순수 회귀 시험."""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest

from evaluation.metrics.compute import compute, verify_report
from evaluation.metrics.definitions import definitions_payload
from evaluation.metrics.store_import import load_store
from evaluation.metrics.truth import ApprovedTruthSnapshot
from evaluation.tests.test_metrics import Base, baseline_runs, write_store


def contracts():
    spec = importlib.util.find_spec("evaluation.metrics.contracts")
    if spec is None:
        raise AssertionError("버전별 정의 봉인과 과거 원본 재현 계약이 없다")
    from evaluation.metrics import contracts as module
    return module


class DefinitionContractTests(Base):
    def test_same_ledger_and_definition_reproduce_original_numbers(self):
        api = contracts()
        store = write_store(self.tmp / "store", baseline_runs())
        source = (store / "progress.jsonl").read_bytes()
        report = compute(load_store(store), ApprovedTruthSnapshot(self.cases))
        original = json.dumps(report, sort_keys=True).encode()
        sealed = api.seal_legacy_report(original, definitions_payload())
        replay = api.reproduce_legacy_report(sealed, load_store(store), ApprovedTruthSnapshot(self.cases))
        self.assertEqual(replay, report)
        self.assertEqual((store / "progress.jsonl").read_bytes(), source)
        self.assertEqual(sealed["original_report_utf8"].encode(), original)
        self.assertEqual(api.seal_legacy_report(original, definitions_payload()), sealed)

    def test_different_definition_is_explicitly_refused(self):
        api = contracts()
        report = self.report(baseline_runs())
        changed = copy.deepcopy(definitions_payload())
        changed["model_class"]["gpt-6-sol"] = "balanced"
        with self.assertRaisesRegex(ValueError, "정의"):
            api.seal_legacy_report(json.dumps(report).encode(), changed)
        other = dict(report, definitions_sha256="0" * 64)
        problems = verify_report(other, report)
        self.assertTrue(any("정의" in p for p in problems))

    def test_definition_archive_replays_older_model_map_without_relabeling(self):
        api = contracts()
        store = write_store(self.tmp / "store", baseline_runs())
        truth = ApprovedTruthSnapshot(self.cases)
        older = copy.deepcopy(definitions_payload())
        older["model_class"]["gpt-6-sol"] = "balanced"
        report = compute(load_store(store), truth, definition=older)
        high = next(m for m in report["metrics"] if m["metric_id"] == "high_class_call_reduction"
                    and m["arm_id"] == "adaptive")
        self.assertEqual(high["numerator"], 0)
        sealed = api.seal_legacy_report(json.dumps(report).encode(), older)
        self.assertEqual(api.reproduce_legacy_report(sealed, load_store(store), truth), report)
        self.assertNotEqual(report["report_digest"], compute(load_store(store), truth)["report_digest"])

    def test_formula_change_requires_a_new_version(self):
        api = contracts()
        changed = copy.deepcopy(definitions_payload())
        changed["metrics"][0]["numerator"] = "임의로 바뀐 수식"
        with self.assertRaisesRegex(ValueError, "버전"):
            api.validate_legacy_definition(changed)


if __name__ == "__main__":
    unittest.main()
