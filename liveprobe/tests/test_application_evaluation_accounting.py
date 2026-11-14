"""결측 사용량·역할 행·공급자 시도 수의 순수 계약 시험."""
from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace
import unittest


def accounting():
    if importlib.util.find_spec("evaluation.accounting") is None:
        raise AssertionError("결측과 실제 영을 구별하는 평가 계측이 없다")
    from evaluation import accounting as module
    return module


def row(**kw):
    return {"id": 1, "role": "challenger", "model": "gpt-6-astra", "effort": "high",
            "attempts": 2, "input_tokens": None, "output_tokens": 10, "cost": None,
            "duration_ms": None, "usage_summary": {"known_input_tokens": 7,
            "known_output_tokens": 10, "missing_usage_attempts": 1}, **kw}


class AccountingTests(unittest.TestCase):
    def test_runner_does_not_turn_unknown_cost_into_zero(self):
        from evaluation.harness.real import CodexInClusterRunner, RESULT_PREFIX
        from evaluation.harness.arms import ARMS

        class StoredRunner(CodexInClusterRunner):
            def lock(self, ttl_s=1800):
                return None

            def _exec(self, *argv, timeout=None):
                return SimpleNamespace(returncode=0, stderr="", stdout=RESULT_PREFIX + json.dumps({
                    "schema": "kan59-run-output/v2", "cost_usd": None, "model_calls": [{"model": "gpt-6-astra"}],
                    "accounting": {"cost_usd": {"total": None, "known_subtotal": 0.12, "missing_rows": 1}}}))

        result = StoredRunner().run(ARMS[0], {"id": "합성-1", "target": "합성", "expected_signal": "image_pull"})
        self.assertIsNone(result.cost_usd)
        self.assertEqual(result.output["accounting"]["cost_usd"]["known_subtotal"], 0.12)

    def test_missing_totals_preserve_known_subtotals_and_actual_attempts(self):
        api = accounting()
        result = api.summarize_runs([row(), row(id=2, role="group_verifier", attempts=1,
            input_tokens=0, output_tokens=0, cost=0, duration_ms=0, usage_summary={})])
        self.assertEqual(result["input_tokens"], {"total": None, "known_subtotal": 7, "missing_rows": 1})
        self.assertEqual(result["output_tokens"]["total"], 10)
        self.assertEqual(result["cost_usd"]["total"], None)
        self.assertEqual(result["cost_usd"]["known_subtotal"], 0)
        self.assertEqual(result["role_execution_rows"], 2)
        self.assertEqual(result["provider_attempts"]["total"], 3)
        self.assertEqual(result["high_class_role_rows"], 2)
        self.assertEqual(result["high_class_provider_attempts"]["total"], 3)
        self.assertIsNone(result["escalations"])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_unknown_attempts_are_not_assumed_to_be_one(self):
        api = accounting()
        result = api.summarize_runs([row(attempts=None)])
        self.assertIsNone(result["provider_attempts"]["total"])
        self.assertEqual(result["provider_attempts"]["known_subtotal"], 0)
        empty = api.summarize_runs([])
        self.assertEqual(empty["provider_attempts"]["total"], 0)
        self.assertEqual(empty["cost_usd"]["total"], 0)

    def test_actual_zero_and_partial_cost_are_separate(self):
        api = accounting()
        partial = row(usage_summary={"known_cost_usd": 0.125, "known_input_tokens": 0})
        result = api.summarize_runs([partial], escalation_events=[])
        self.assertEqual(result["cost_usd"]["known_subtotal"], 0.125)
        self.assertIsNone(result["cost_usd"]["total"])
        self.assertEqual(result["escalations"], 0)
        zero = api.summarize_runs([row(input_tokens=0, output_tokens=0, cost=0, attempts=0)])
        self.assertEqual(zero["input_tokens"]["total"], 0)
        self.assertEqual(zero["cost_usd"]["total"], 0)

    def test_export_path_preserves_nullable_values_and_audit_subtotals(self):
        accounting()
        from app import evalrun
        run = SimpleNamespace(id=1, role="challenger", model="gpt-6-astra", reasoning_effort="high",
                              attempts=2, status="succeeded", input_tokens=None, output_tokens=10,
                              estimated_cost_usd=None, duration_ms=None, output={}, error=None)
        exported = evalrun.export_runs([run], {1: {"known_input_tokens": 7, "missing_usage_attempts": 1}})
        self.assertIsNone(exported[0]["input_tokens"])
        self.assertIsNone(exported[0]["cost"])
        self.assertEqual(exported[0]["usage_summary"]["known_input_tokens"], 7)
        self.assertIsNone(accounting().summarize_runs(exported)["provider_attempts"]["total"])
        self.assertEqual(exported[0]["validation_attempts"], 2)
        actual = evalrun.export_runs([run], {1: {"provider_attempts": 3}})
        self.assertEqual(accounting().summarize_runs(actual)["provider_attempts"]["total"], 3)

    def test_runtime_top_level_provider_audit_survives_export(self):
        from app import evalrun
        self.assertTrue(hasattr(evalrun, "audit_usage_summaries"),
                        "runtime provider attempts are top-level audit fields, not inside usage_summary")
        run = SimpleNamespace(id=1, role="challenger", model="gpt-6-astra", reasoning_effort="high",
                              attempts=3, status="succeeded", input_tokens=None, output_tokens=10,
                              estimated_cost_usd=None, duration_ms=None, output={}, error=None)
        audits = [SimpleNamespace(event="agent_run", detail={"agent_run_id": 1,
                    "provider_attempts": 3, "validation_rounds": 2,
                    "usage_summary": {"known_input_tokens": 7, "missing_usage_attempts": 1}}),
                  SimpleNamespace(event="unrelated", detail={"agent_run_id": 1, "provider_attempts": 999})]
        summaries = evalrun.audit_usage_summaries(audits)
        exported = evalrun.export_runs([run], summaries)
        self.assertEqual(exported[0]["attempts"], 3)
        self.assertEqual(exported[0]["validation_attempts"], 2)
        self.assertEqual(accounting().summarize_runs(exported)["provider_attempts"]["total"], 3)
        self.assertEqual(exported[0]["usage_summary"]["known_input_tokens"], 7)
        zero = [SimpleNamespace(event="agent_run", detail={"agent_run_id": 1, "provider_attempts": 0})]
        self.assertEqual(evalrun.export_runs([run], evalrun.audit_usage_summaries(zero))[0]["attempts"], 0)
        self.assertIsNone(evalrun.export_runs([run], {})[0]["attempts"])

    def test_invalid_counters_do_not_become_free_usage(self):
        api = accounting()
        for value in (-1, True, float("nan"), float("inf"), "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.summarize_runs([row(input_tokens=value)])


if __name__ == "__main__":
    unittest.main()
