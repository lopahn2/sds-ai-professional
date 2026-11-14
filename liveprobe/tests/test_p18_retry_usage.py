"""F8: real Engine._call, fake provider/session; no DB, account or network calls.

Run directly with unittest to avoid tests/conftest.py's import-time DB probe.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "liveprobe"))
from app import engine as E
from app.config import AppConfig, Settings
from app.llm import estimate_cost
from app.llm.base import LLMError, LLMResult, Usage

spec = importlib.util.spec_from_file_location("p18_results", ROOT / "scripts/p18-campaign-results.py")
assert spec is not None and spec.loader is not None
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


class Output(BaseModel):
    summary: str


def success(usage, data=None):
    return LLMResult("", {"summary": "ok"} if data is None else data, usage, duration_ms=7)


class RetryUsage(unittest.TestCase):
    def run_call(self, attempts):
        stored = []

        class FakeSession:
            def add(self, row):
                row.id = 900 + len(stored)
                stored.append(row)

            def flush(self):
                pass

        @contextmanager
        def fake_session():
            yield FakeSession()

        provider = NS(name="fake_provider", complete=Mock(side_effect=attempts))
        engine = E.Engine(AppConfig(), Settings(background=False), None, provider, None)
        with patch.object(E.db, "session", fake_session), patch.object(E, "audit") as audit, \
                patch.object(E.broker, "publish"), patch.object(E.runlog, "gate"), \
                patch.object(E.runlog, "emit") as emit:
            call = engine._call(1, "router", "fake prompt", {}, Output, [])
        self.assertEqual(len(stored), 1)
        row = {k: getattr(stored[0], k) for k in
               ("id", "attempts", "input_tokens", "output_tokens", "estimated_cost_usd", "status", "duration_ms")}
        detail = audit.call_args.kwargs
        finished = next(c.args[3] for c in emit.call_args_list if c.args[1] == "agent_finished")
        return row, call, detail, finished, provider.complete.call_count

    def test_unknown_invalid_output_then_known_retry(self):
        row, call, _, finished, n = self.run_call([
            LLMError("invalid_output", "no usage"), success(Usage(100, 20))])
        self.assertEqual((n, row["attempts"]), (2, 2))
        self.assertTrue(call.ok)
        for item in (row, finished):
            self.assertIsNone(item["input_tokens"])
            self.assertIsNone(item["output_tokens"])
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertIsNone(call.input_tokens)
        self.assertIsNone(call.cost_usd)
        self.assertIsNone(summary.usage([row])["total_tokens"])

    def test_explicit_zero_is_observed_and_free(self):
        row, call, _, finished, _ = self.run_call([success(Usage(0, 0))])
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (0, 0))
        self.assertEqual((finished["input_tokens"], call.input_tokens), (0, 0))
        self.assertEqual(row["estimated_cost_usd"], 0.0)
        self.assertEqual(summary.usage([row])["total_tokens"], 0)
        self.assertEqual(summary.usage([row])["missing_usage_calls"], 0)

    def test_timeout_without_usage_is_unknown_not_free(self):
        row, call, _, _, n = self.run_call([LLMError("timeout", "no usage")])
        self.assertEqual(n, 1)
        self.assertFalse(call.ok)
        self.assertEqual(call.error_kind, "timeout")
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertIsNone(row["input_tokens"])

    def test_partial_input_only_is_not_priced(self):
        row, call, _, _, _ = self.run_call([success(Usage(100, None))])
        self.assertEqual(row["input_tokens"], 100)
        self.assertIsNone(row["output_tokens"])
        self.assertIsNone(row["estimated_cost_usd"])
        self.assertIsNone(call.cost_usd)

    def test_all_attempts_known_control_132(self):
        row, _, _, _, _ = self.run_call([
            LLMError("invalid_output", "bad JSON", usage=Usage(10, 2), duration_ms=3),
            success(Usage(100, 20))])
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["duration_ms"]), (110, 22, 10))
        self.assertEqual(summary.usage([row])["total_tokens"], 132)
        self.assertIsNotNone(row["estimated_cost_usd"])

    def test_schema_validation_retry_counts_each_usage_once(self):
        row, call, _, _, n = self.run_call([success(Usage(10, 2), {}), success(Usage(100, 20))])
        self.assertTrue(call.ok)
        self.assertEqual((n, row["attempts"], row["duration_ms"]), (2, 2, 14))
        self.assertEqual(summary.usage([row])["total_tokens"], 132)

    def test_schema_validation_unknown_attempt_cannot_be_erased(self):
        row, _, _, _, _ = self.run_call([success(Usage(), {}), success(Usage(100, 20))])
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["estimated_cost_usd"])

    def test_exhausted_invalid_output_preserves_failure_and_unknown(self):
        row, call, _, _, n = self.run_call([
            LLMError("invalid_output", "unknown"), LLMError("invalid_output", "known", usage=Usage(10, 2))])
        self.assertEqual((n, row["status"], call.error_kind), (2, "failed", "invalid_output"))
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["estimated_cost_usd"])

    def test_audit_preserves_known_subtotal_and_missing_attempt_count(self):
        row, _, detail, _, _ = self.run_call([
            LLMError("invalid_output", "no usage"), success(Usage(100, 20))])
        self.assertIn("usage_summary", detail, "known retry subtotal must survive in audit")
        self.assertEqual(detail["usage_summary"], {
            "known_input_tokens": 100, "known_output_tokens": 20, "missing_usage_attempts": 1})
        record = {"id": "a", "system": "payment", "planned_group": "SIMPLE", "incidents": [{
            "incident": {"id": 1}, "agent_runs": [row],
            "audit": [{"event": "agent_run", "detail": detail}]}]}
        result = summary.summarize([record])
        for aggregate in (result["overall"], result["case_rows"][0], result["observed_groups"]["UNOBSERVED"]):
            self.assertEqual(aggregate["known_tokens"], 120)
            self.assertEqual(aggregate["missing_usage_attempts"], 1)
            self.assertIsNone(aggregate["total_tokens"])
            self.assertIsNone(aggregate["estimated_cost_usd"])
        self.assertNotIn("usage_summary", row, "report must not mutate API snapshots")
        import csv
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary.write_report([record], output)
            saved = json.loads((output / "summary.json").read_text())
            self.assertEqual(saved["overall"]["known_tokens"], 120)
            self.assertIsNone(saved["overall"]["total_tokens"])
            with (output / "cases.csv").open() as stream:
                csv_row = next(csv.DictReader(stream))
            self.assertEqual(csv_row["total_tokens"], "")
            self.assertEqual(csv_row["estimated_cost_usd"], "")
            self.assertEqual(csv_row["missing_usage_attempts"], "1")

    def test_legacy_unknown_retry_does_not_invent_missing_attempt_count(self):
        result = summary.usage([{"id": 1, "attempts": 2, "input_tokens": None, "output_tokens": None}])
        self.assertIn("missing_usage_attempts", result)
        self.assertIsNone(result["missing_usage_attempts"])
        self.assertIsNone(result["total_tokens"])

    def test_missing_input_does_not_hide_complete_output(self):
        row, _, _, _, _ = self.run_call([
            LLMError("invalid_output", "partial", usage=Usage(None, 2)), success(Usage(100, 20))])
        self.assertIsNone(row["input_tokens"])
        self.assertEqual(row["output_tokens"], 22)
        self.assertIsNone(row["estimated_cost_usd"])


class CostCompleteness(unittest.TestCase):
    def test_results_refuses_unknown_usage_with_legacy_zero_price(self):
        result = summary.usage([{"id": 1, "input_tokens": None, "output_tokens": None,
                                 "estimated_cost_usd": 0.0}])
        self.assertIsNone(result["estimated_cost_usd"])
        self.assertEqual(result["missing_price_calls"], 1)

    def test_either_missing_counter_is_unpriced(self):
        cfg = AppConfig()
        for usage in (Usage(), Usage(100, None), Usage(None, 20)):
            with self.subTest(usage=usage):
                self.assertIsNone(estimate_cost(cfg, "gpt-6-luna", usage))

    def test_zero_and_existing_cached_prices(self):
        cfg = AppConfig()
        self.assertEqual(estimate_cost(cfg, "gpt-6-luna", Usage(0, 0)), 0.0)
        self.assertEqual(estimate_cost(cfg, "claude-sonnet-5", Usage(1_000_000, 1_000_000)), 12.0)
        self.assertEqual(estimate_cost(cfg, "claude-haiku-4-5", Usage(1_000_000, 0, 500_000)), 0.55)
        self.assertIsNone(estimate_cost(cfg, "unknown-model", Usage(1, 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
