"""Check the Application comparison's selected receipts and observed recovery paths."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

from evaluation.comparison_summary import (
    _paired_measurement, recovery_action_matches, runtime_handoff_proof, verify_recovery_probes,
)
from evaluation.live_repair import TASK


MATCH_FIELDS = ("case", "arm", "protocol", "status", "application_state", "reason_code",
                "input_sha256", "fixture_sha256", "wall_duration_ms")
MEASUREMENTS = ("wall_duration_ms", "model_duration_ms", "total_tokens", "estimated_usd")
CONFIGURATION = "fixture_configuration"
SOURCE = "source_repair"
MIXED = "mixed_recovery"
UNOBSERVED = "unobserved_recovery"


def receipt_key(record, *, from_ledger=False):
    calls = record["model_calls"] if from_ledger else len(record.get("calls") or [])
    fields = tuple(str(record[field]) if record.get(field) is not None else "" for field in MATCH_FIELDS)
    return (*fields, str(calls))


def selected_receipts(ledger, evidence_dir):
    by_folder = {}
    for row in ledger:
        folder = evidence_dir / row["protocol"].replace("/", "-") / row["case"]
        if folder not in by_folder:
            by_folder[folder] = {}
            for path in sorted(folder.rglob("*.json")):
                raw = path.read_bytes()
                record = json.loads(raw)
                if isinstance(record, dict) and record.get("arm") and record.get("status"):
                    key = receipt_key(record)
                    by_folder[folder].setdefault(key, []).append((path, raw, record))
        matches = by_folder[folder].get(receipt_key(row, from_ledger=True), [])
        if len(matches) != 1:
            raise ValueError(f"expected one selected receipt for {row['case']}/{row['arm']}; found {len(matches)}")
        path, raw, record = matches[0]
        yield row, record, {"path": path.relative_to(evidence_dir).as_posix(),
                            "sha256": hashlib.sha256(raw).hexdigest()}


def receipt_outcome(record):
    context = f"{record['case']}/{record['arm']}"
    log = record.get("initial_error_log")
    if not isinstance(log, str) or not log or hashlib.sha256((TASK + log).encode()).hexdigest() != record["input_sha256"]:
        raise ValueError(f"opening input does not match the recorded hash for {context}")
    config = record.get("successful_config_writes", 0)
    source = record.get("successful_source_writes", 0)
    if record["arm"] != "liveprobe":
        accepted = Counter(step["request"]["tool"] for step in record.get("investigation") or []
                           if step["result"].get("accepted") is True)
        if (accepted["configure"], accepted["patch"]) != (config, source):
            raise ValueError(f"accepted mutations do not match the recorded totals for {context}")
    verified = record["status"] == "verified_recovered"
    if verified:
        verify_recovery_probes(record, record["case"], record["arm"])
        if not recovery_action_matches(record):
            raise ValueError(f"missing independent recovery action proof for {context}")
    intake = record["status"] == "unrecovered" and bool(record.get("runtime_intake"))
    if intake and not runtime_handoff_proof(record):
        raise ValueError(f"missing isolated intake, terminal observation or fixture restoration for {context}")
    return {"status": record["status"], "reason_code": record.get("reason_code"),
            "configuration_writes": config, "source_writes": source,
            "independent_recovery_verified": verified, "runtime_handoff_and_cleanup_verified": intake,
            "independent_probe_count": len(record.get("verified") or record.get("independent_probes") or []),
            "wall_duration_ms": record.get("wall_duration_ms"), "model_duration_ms": record.get("model_duration_ms"),
            "analysis_wall_ms": record.get("analysis_wall_ms")}


def observed_path(recovered):
    if not recovered:
        return UNOBSERVED
    if all(arm["configuration_writes"] > 0 and arm["source_writes"] == 0 for arm in recovered):
        return CONFIGURATION
    if all(arm["source_writes"] > 0 and arm["configuration_writes"] == 0 for arm in recovered):
        return SOURCE
    return MIXED


def summarize(catalog_path, ledger_path, evidence_dir):
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in catalog["application"]["cases"]}
    arms = {arm["id"]: arm for arm in catalog["modes"]}
    with ledger_path.open(encoding="utf-8", newline="") as handle:
        ledger = [row for row in csv.DictReader(handle) if row["cohort"] == "application"]
    keys = {(row["case"], row["arm"]) for row in ledger}
    if len(keys) != len(ledger) or keys != {(case, arm) for case in cases for arm in arms}:
        raise ValueError("Application ledger and selected catalog must contain the same unique case/arm pairs")

    outcomes = {case: {} for case in cases}
    bindings = {case: set() for case in cases}
    for row, record, identity in selected_receipts(ledger, evidence_dir):
        report = receipt_outcome(record)
        usage = record.get("usage") or {}
        parts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
        tokens = sum(parts) if all(type(part) is int for part in parts) else None
        cost = record.get("estimated_usd")
        if (tokens, cost) != (int(row["total_tokens"]) if row["total_tokens"] else None,
                              float(row["estimated_usd"]) if row["estimated_usd"] else None):
            raise ValueError(f"tokens or cost differ from the selected receipt: {row['case']}/{row['arm']}")
        if row["arm"] != "liveprobe" and any(record.get(key) != arms[row["arm"]][key] for key in ("model", "effort")):
            raise ValueError(f"model or effort differs from the selected catalog: {row['case']}/{row['arm']}")
        report.update(receipt=identity, total_tokens=tokens, estimated_usd=cost)
        outcomes[row["case"]][row["arm"]] = report
        bindings[row["case"]].add(tuple(row[key] for key in ("protocol", "input_sha256", "fixture_sha256")))

    summaries, pairs = {}, {arm: [] for arm in arms if arm != "liveprobe"}
    for case_id, results in outcomes.items():
        if len(bindings[case_id]) != 1:
            raise ValueError(f"arms do not share source, opening input and protocol for {case_id}")
        live = results["liveprobe"]
        standalone = [results[arm] for arm in pairs]
        recovered = [arm for arm in standalone if arm["independent_recovery_verified"]]
        path = observed_path(recovered)
        fixture_access_gap = (path == CONFIGURATION and live["runtime_handoff_and_cleanup_verified"]
                              and live["configuration_writes"] == 0)
        protocol, opening, fixture = next(iter(bindings[case_id]))
        summaries[case_id] = {"catalog_mode": cases[case_id]["mode"], "observed_standalone_recovery_path": path,
                              "asymmetric_fixture_configuration_writes": fixture_access_gap,
                              "protocol": protocol, "opening_input_sha256": opening, "fixture_sha256": fixture,
                              "arms": results}
        for arm in pairs:
            pairs[arm].append((path, live, results[arm]))

    totals = {}
    for arm, rows in pairs.items():
        by_path = {}
        for path in dict.fromkeys(branch for branch, _, _ in rows):
            selected = [(live, single) for branch, live, single in rows if branch == path]
            both_recovered = [(live, single) for live, single in selected
                              if live["independent_recovery_verified"] and single["independent_recovery_verified"]]
            by_path[path] = {"cases": len(selected),
                "liveprobe_recovered": sum(live["independent_recovery_verified"] for live, _ in selected),
                "standalone_recovered": sum(single["independent_recovery_verified"] for _, single in selected),
                "both_recovered": len(both_recovered),
                "both_recovered_measurements": {key: _paired_measurement(both_recovered, key) for key in MEASUREMENTS}}
        totals[arm] = by_path
    return {"schema": "application-comparison-audit/v1", "cases": len(summaries), "attempts": len(ledger),
            "observed_paths": dict(Counter(row["observed_standalone_recovery_path"] for row in summaries.values())),
            "asymmetric_fixture_configuration_cases": [case for case, row in summaries.items()
                                                       if row["asymmetric_fixture_configuration_writes"]],
            "liveprobe_unrecovered_reasons": dict(Counter(row["arms"]["liveprobe"]["reason_code"]
                for row in summaries.values() if row["arms"]["liveprobe"]["status"] == "unrecovered")),
            "paired_results_by_path": totals, "case_results": summaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.catalog, args.ledger, args.evidence_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
