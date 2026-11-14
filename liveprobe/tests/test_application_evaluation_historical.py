"""A/B/C public receipts, not private stores: numeric and definition regression.

Reconstruct definition-bound metadata only; matching original byte hashes proves
identity with the already-reconciled reports, not an independent ledger replay.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from evaluation.metrics import contracts
from evaluation.metrics.compute import canon, compute, verify_report
from evaluation.metrics.store_import import load_store
from evaluation.metrics.truth import ApprovedTruthSnapshot
from evaluation.tests.test_metrics import baseline_runs, write_catalog, write_store
import tempfile

ROOT = Path(__file__).resolve().parents[2]
RECONCILIATION = ROOT / "docs/evidence/plan-assessment-20261101/reconciliation.json"


def historical_definition(sha):
    assert hasattr(contracts, "definition_by_hash"), "the actual A/B/C definition is not archived by fingerprint"
    return contracts.definition_by_hash(sha)


def reports():
    reconciliation = json.loads(RECONCILIATION.read_text("utf-8"))
    assert len(reconciliation["periods"]) == 3
    for period, receipt in zip("ABC", reconciliation["periods"]):
        assert receipt["period"] == period
        public = ROOT / f"docs/evidence/dashboard-detail-fix/metrics-{period}-recomputed.json"
        raw = public.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == receipt["recomputed_sha256"]
        yield receipt, json.loads(raw)


def original_bytes(report, receipt):
    """Restore only the three hash fields reconciliation says changed."""
    original = copy.deepcopy(report)
    old_sha = receipt["old_definitions_sha256"]
    historical_definition(old_sha)
    original["definitions_sha256"] = old_sha
    for metric in original["metrics"]:
        body = {k: v for k, v in metric.items() if k != "export_digest"}
        metric["export_digest"] = hashlib.sha256(canon({
            "definitions_sha256": old_sha,
            "label_version": original["inputs"]["truth_label_version"],
            "labels_manifest_sha256": original["inputs"]["truth_labels_manifest_sha256"], **body})).hexdigest()
    original.pop("report_digest")
    original["report_digest"] = hashlib.sha256(canon(original)).hexdigest()
    raw = (json.dumps(original, sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode()
    assert hashlib.sha256(raw).hexdigest() == receipt["original_sha256"]
    return raw


def test_abc_actual_historical_definition_is_archived_by_recorded_fingerprint():
    for receipt, _ in reports():
        old = historical_definition(receipt["old_definitions_sha256"])
        assert contracts.definition_hash(old) == receipt["old_definitions_sha256"]
        assert "gpt-6-astra" not in old["model_class"]
        assert old["model_class"]["gpt-6-sol"] == "high"
    with pytest.raises(ValueError, match="정의"):
        historical_definition("0" * 64)


def test_abc_reconciled_numbers_denominators_and_verdicts_remain_unchanged():
    fields = ("value", "numerator", "denominator", "scored", "expected_pairs", "verdict")
    counts = {"A": 16, "B": 44, "C": 22}
    for receipt, report in reports():
        raw = original_bytes(report, receipt)
        original = json.loads(raw)
        definition = historical_definition(receipt["old_definitions_sha256"])
        sealed = contracts.seal_legacy_report(raw, definition)
        assert sealed["original_report_utf8"].encode() == raw
        assert len(original["metrics"]) == receipt["metric_count"] == 40
        assert original["publishable"] is False
        assert original["publication_blockers"] == receipt["blockers"]
        assert original["completeness"]["adaptive"]["accepted"] == counts[receipt["period"]]
        assert original["completeness"]["adaptive"]["expected"] == 60
        for old, fresh in zip(original["metrics"], report["metrics"]):
            assert {k: v for k, v in old.items() if k != "export_digest"} == {
                k: v for k, v in fresh.items() if k != "export_digest"}
            if old["arm_id"] == "adaptive":
                assert {k: old[k] for k in fields} == receipt["adaptive"][old["metric_id"]]
        # Same exact frozen report passes; unlike mixed-definition verification.
        assert verify_report(original, json.loads(sealed["original_report_utf8"])) == []


def test_frozen_abc_definition_recomputes_a_synthetic_ledger_without_relabeling():
    old_sha = json.loads(RECONCILIATION.read_text())["periods"][0]["old_definitions_sha256"]
    definition = historical_definition(old_sha)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        labels = ApprovedTruthSnapshot(write_catalog(root / "cases"))
        path = write_store(root / "store", baseline_runs())
        before = (path / "progress.jsonl").read_bytes()
        store = load_store(path)
        old = compute(store, labels, definition=definition)
        current = compute(store, labels)
        archive = contracts.seal_legacy_report(json.dumps(old).encode(), definition)
        assert contracts.reproduce_legacy_report(archive, store, labels) == old
        assert old["definitions_sha256"] == old_sha
        for a, b in zip(old["metrics"], current["metrics"]):
            assert {k: v for k, v in a.items() if k != "export_digest"} == {
                k: v for k, v in b.items() if k != "export_digest"}
        assert old["report_digest"] != current["report_digest"]
        assert (path / "progress.jsonl").read_bytes() == before


def test_original_arm_hash_regression_has_no_private_store_dependency():
    import inspect
    from evaluation.tests.test_harness import test_p13_arms_differ_only_in_rag_and_keep_original_hashes
    source = inspect.getsource(test_p13_arms_differ_only_in_rag_and_keep_original_hashes)
    assert ".e2e-tmp" not in source, "the required offline suite still opens a forbidden private store"
    assert "if store.exists()" not in source, "pinned arm equality must be unconditional"


def test_abc_different_definition_is_rejected_despite_identical_numbers():
    for receipt, report in reports():
        raw = original_bytes(report, receipt)
        with pytest.raises(ValueError, match="정의"):
            contracts.seal_legacy_report(raw, contracts.legacy_definition())
        assert any("평가 정의" in problem for problem in verify_report(json.loads(raw), report))
