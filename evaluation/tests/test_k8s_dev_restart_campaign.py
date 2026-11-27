from collections import Counter
import csv
import json

import pytest

from evaluation import k8s_dev_restart_campaign as campaign


def test_queue_requires_a_product_receipt_and_preserves_the_other_arms(tmp_path):
    rows = [{"case": case, "arm": arm, "status": "pending"}
            for case in ("F2-01", "F2-03") for arm in campaign.ARMS]
    product = campaign.Attempt("F2-03", "liveprobe")
    product.receipt(tmp_path).parent.mkdir(parents=True)
    product.receipt(tmp_path).touch()
    next_attempt = campaign.Attempt("F2-01", "liveprobe")
    attempted = {campaign.Attempt("F2-03", "single_astra")}

    queue = list(campaign.choices(rows, tmp_path, {"F2-01", "F2-03"}, attempted))

    assert queue[0] == next_attempt
    assert Counter(item.case_id for item in queue) == {"F2-01": 1, "F2-03": 3}
    assert all(item.arm != "single_astra" for item in queue)


def test_ledger_rejects_a_case_or_arm_outside_the_approved_grid(tmp_path):
    catalog = json.loads(campaign.CATALOG.read_text())
    rows = [{"case": case["id"], "arm": arm, "status": "pending"}
            for cohort in ("basic", "kubernetes_new", "application")
            for case in catalog[cohort]["cases"] for arm in campaign.ARMS]
    ledger = tmp_path / "comparison.csv"

    def write_rows():
        with ledger.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    write_rows()
    assert len(campaign.read_ledger(ledger)) == 375
    rows[-1]["arm"] = "single_other"
    write_rows()
    with pytest.raises(ValueError, match="fixed case and arm grid"):
        campaign.read_ledger(ledger)


def test_receipt_cannot_be_published_in_a_different_case(tmp_path):
    item = campaign.Attempt("F2-01", "single_astra")
    with pytest.raises(ValueError, match="different case"):
        campaign.verify_receipt(item, {"case": "F2-03", "arm": "single_astra"}, tmp_path)
    assert not item.receipt(tmp_path).exists()


def test_saved_raw_oom_is_reconciled_before_the_next_injection(tmp_path, monkeypatch):
    item = campaign.Attempt("F2-07", "liveprobe")
    rows = [{"case": item.case_id, "arm": item.arm, "status": "pending"}]
    run = tmp_path / "native-dev-campaign-20261125T001122333333Z"
    run.mkdir()
    raw = run / f"{item.case_id}-{item.arm}.json"
    data = {"case": item.case_id, "arm": item.arm, "status": "oom_observed", "kubernetes_lease_released": True}
    raw.write_text(json.dumps(data))
    verified = []

    def verify(attempt, report):
        verified.append((attempt, report))
        return "verified_natural_recovery"

    monkeypatch.setattr(campaign, "receipt_status", verify)
    journal = tmp_path / "dev-k8s-fault-active.json"
    journal.touch()
    campaign.recover_saved_oom_receipts([item], tmp_path)
    assert not verified and not item.receipt(tmp_path).exists()
    journal.unlink()

    campaign.recover_saved_oom_receipts([item], tmp_path)
    campaign.recover_saved_oom_receipts([item], tmp_path)

    assert verified == [(item, data)]
    assert json.loads(item.receipt(tmp_path).read_text()) == json.loads(raw.read_text()) == data
    assert not list(campaign.choices(rows, tmp_path, {item.case_id}, set()))


def test_receipt_publication_preserves_original_and_hides_incomplete_json(tmp_path):
    receipt = tmp_path / "result.json"
    with pytest.raises(TypeError):
        campaign.oom.private_write(receipt, {"invalid": object()})
    assert not list(tmp_path.iterdir())

    campaign.oom.private_write(receipt, {"status": "complete"})
    with pytest.raises(FileExistsError):
        campaign.oom.private_write(receipt, {"status": "override"})
    assert json.loads(receipt.read_text()) == {"status": "complete"}
    assert sorted(tmp_path.iterdir()) == [receipt]


def test_checkpoint_metrics_preserve_unobserved_usage():
    rows = [{"cohort": "basic", "case": "F2-01", "arm": arm, "status": "verified_natural_recovery",
             "estimated_usd": "", "total_tokens": "", "wall_duration_ms": wall}
            for arm, wall in (("liveprobe", "60000"), ("single_astra", "75000"))]
    result = campaign.ledger_coverage(rows)["paired_standalone_comparisons"]["single_astra"]
    assert result["estimated_usd"]["measured_pairs"] == 0
    assert result["estimated_usd"]["liveprobe_sum"] is None
    assert result["wall_duration_ms"]["measured_pairs"] == 1
    assert result["wall_duration_ms"]["liveprobe_sum"] == 60000
