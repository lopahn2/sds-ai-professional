"""Resume the approved native dev cases one physical trial at a time."""
from argparse import ArgumentParser
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic, sleep

from evaluation import k8s_dev_crash_trial as crash
from evaluation import k8s_dev_image_trial as live
from evaluation import k8s_dev_oom_trial as oom
from evaluation import isolated_oom_target
from evaluation.comparison_summary import REPAIR_FIELDS, dev_crash_record, dev_oom_record, paired_repair_coverage, repair_rows
from evaluation.single_baseline import MODELS
from scripts.family2_live import DevFaultAuthority


ARMS = ("liveprobe", *MODELS)
CASES = {**{case: crash for case in crash.CASES}, **{case: oom for case in oom.CASES}}
FINISHED = {"verified_recovered", "verified_natural_recovery", "unrecovered"}
CATALOG = live.ROOT / "evaluation/catalogs/comparative-2026-11-23.json"
NUMBER_FIELDS = ("wall_duration_ms", "model_duration_ms", "total_tokens", "estimated_usd", "high_model_calls", "first_error_to_operation_end_ms",
                 "first_error_to_verified_recovery_ms",
                 "citation_reviewed_calls", "citation_missing_calls", "citations_grounded", "citations_ungrounded")


@dataclass(frozen=True)
class Attempt:
    case_id: str
    arm: str

    @property
    def module(self):
        return CASES[self.case_id]

    def directory(self, evidence):
        return evidence / self.module.PROTOCOL.replace("/", "-") / self.case_id

    def receipt(self, evidence):
        return self.directory(evidence) / (self.arm + ".json")


def read_ledger(path):
    with path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    keys = {(row["case"], row["arm"]) for row in rows}
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    expected = {(case["id"], arm) for cohort in ("basic", "kubernetes_new", "application")
                for case in catalog[cohort]["cases"] for arm in ARMS}
    if len(rows) != len(expected) or keys != expected:
        raise ValueError("the comparison ledger does not contain the fixed case and arm grid")
    return rows


def choices(rows, evidence, case_ids, attempted):
    counts = Counter(row["case"] for row in rows if row["status"] in FINISHED)
    pending = [Attempt(row["case"], row["arm"]) for row in rows
               if row["case"] in case_ids and row["status"] == "pending"]
    ordered = sorted(pending, key=lambda x: (-counts[x.case_id], x.case_id, ARMS.index(x.arm)))
    for item in ordered:
        if item in attempted or item.receipt(evidence).exists():
            continue
        reference = Attempt(item.case_id, "liveprobe").receipt(evidence)
        if item.arm == "liveprobe" or reference.is_file():
            yield item


def scope_idle(evidence):
    journal = evidence / "dev-k8s-fault-active.json"
    if journal.exists():
        raise live.Refused("an earlier trial journal requires recovery or observation")
    target = isolated_oom_target.active()
    (oom.authority() if target else DevFaultAuthority(live.CONTEXT)).check()
    work = live.query("""SELECT json_build_object(
        'incidents',(SELECT count(*) FROM incidents WHERE status IN
                       ('open','analyzing','action_pending','acting')),
        'leases',(SELECT count(*) FROM settings WHERE value ? 'token_sha256'
                        AND (value->>'until')::timestamptz > now()))""")
    return not any(work.values())


def eligible_targets(items):
    ready_at = {}
    for item in items:
        case = item.module.load_case(item.case_id)
        if case.workload not in ready_at:
            baseline, pod, _, _ = crash.pulse.preflight(case)
            status, _ = crash.pod_snapshot(case, baseline, pod)
            _, ready_at[case.workload] = crash.restart_baseline(status)
    return ready_at


def command(item, evidence, raw):
    args = [sys.executable, "-B", "-m", item.module.__name__, "--case", item.case_id,
            "--arm", item.arm, "--output", str(raw)]
    if item.module is oom:
        args.extend(("--execute", "--node-settle-seconds", "50"))
    target = isolated_oom_target.active()
    if target:
        target.require_case(item.case_id)
        args.extend(target.cli_flags())
    if item.arm != "liveprobe":
        args.extend(("--reference", str(Attempt(item.case_id, "liveprobe").receipt(evidence))))
    return args


def receipt_status(item, data):
    target = isolated_oom_target.active()
    if target:
        target.verify_receipt(data)
    if (data.get("case"), data.get("arm")) != (item.case_id, item.arm):
        raise ValueError("the receipt belongs to a different case or arm")
    normalize = dev_oom_record if item.module is oom else dev_crash_record
    verified = normalize(data, item.arm)
    if verified["status"] not in FINISHED:
        return None
    # Use the same catalog/model/effort validation as the aggregate, before touching the receipt directory.
    if item.arm != "liveprobe":
        model, effort, _ = MODELS[item.arm]
        expected = (model, effort)
        if (data.get("model"), data.get("effort")) != expected or any(
            (call.get("model"), call.get("effort")) != expected for call in data.get("calls", ())
        ):
            raise ValueError("the standalone receipt includes a different model or effort")
    return verified["status"]


def publish_receipt(item, data, evidence):
    result = item.receipt(evidence)
    result.parent.mkdir(parents=True, exist_ok=True)
    oom.private_write(result, data)


def verify_receipt(item, data, evidence):
    status = receipt_status(item, data)
    if status:
        publish_receipt(item, data, evidence)
    return status


def recover_saved_oom_receipts(items, evidence):
    missing = [item for item in items if item.module is oom and not item.receipt(evidence).exists()]
    if not missing:
        return
    # A child may outlive the campaign coordinator. Recover its report only after
    # its lock has been released and no outstanding fault journal remains.
    with (evidence / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (evidence / "dev-k8s-fault-active.json").exists():
            return
        runs = sorted(path for path in evidence.iterdir() if not path.is_symlink() and path.is_dir()
                      and re.fullmatch(r"native-dev-campaign-\d{8}T\d{12}Z", path.name))
        for item in missing:
            candidates = []
            for run in runs:
                raw = run / f"{item.case_id}-{item.arm}.json"
                if not raw.is_file() or raw.is_symlink():
                    continue
                data = json.loads(raw.read_text(encoding="utf-8"))
                if (data.get("status") == "oom_observed" and data.get("kubernetes_lease_released") is True
                        and not any(data.get(key) for key in ("trial_error", "cleanup_error", "model_failed"))
                        and receipt_status(item, data)):
                    candidates.append(data)
            if len(candidates) > 1:
                raise ValueError(f"multiple finished OOM receipts for {item.case_id}/{item.arm}")
            if candidates:
                publish_receipt(item, candidates[0], evidence)


def write_update(previous, root, directory, appended):
    affected = {item.case_id for item in appended}
    if not affected:
        return None
    rows = read_ledger(previous)
    # Index only affected cases so that existing reviewed rows retain their recorded values.
    subset = directory / "verified-index"
    subset.mkdir()
    for case_id in sorted(affected):
        item = Attempt(case_id, "liveprobe")
        folder = subset / item.module.PROTOCOL.replace("/", "-")
        folder.mkdir(exist_ok=True)
        (folder / case_id).symlink_to(item.directory(root), target_is_directory=True)
    verified = {(r["case"], r["arm"]): r for r in repair_rows(CATALOG, subset)
                if r["case"] in affected and r["status"] in FINISHED}
    merged = []
    for row in rows:
        key = row["case"], row["arm"]
        if row["status"] == "pending" and key in verified:
            row = verified[key]
        merged.append(row)
    destination = directory / "comparison.csv"
    with destination.open("x", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=tuple(dict.fromkeys((*rows[0], *REPAIR_FIELDS))))
        writer.writeheader()
        writer.writerows(merged)
    live.write_once(directory / "paired.json", ledger_coverage(merged))
    return destination


def ledger_coverage(rows):
    return paired_repair_coverage([
        {**row, **{name: float(row[name]) if row.get(name) not in ("", None) else None for name in NUMBER_FIELDS}}
        for row in rows
    ])


def run(rows, case_ids, limit, wait_s, directory, checkpoint):
    target = isolated_oom_target.active()
    evidence = target.evidence(live.EVIDENCE) if target else live.EVIDENCE
    attempted, appended = set(), []
    deadline = monotonic() + wait_s
    while len(attempted) < limit:
        items = list(choices(rows, evidence, case_ids, attempted))
        if not items:
            break
        try:
            live.CHAOS.node_gate()
        except live.CHAOS.Stop as error:
            if monotonic() >= deadline:
                record = {"phase": "waiting_for_node", "pending": len(items), "reason": str(error)}
                if isinstance(error, live.CHAOS.NodeGateStop):
                    record.update(node=error.snapshot, breaches=error.breaches)
                live.write_once(directory / "node-stop.json", record)
                print(json.dumps(record), flush=True)
                break
            sleep(12)
            continue
        if not scope_idle(evidence):
            if monotonic() >= deadline:
                print(json.dumps({"phase": "waiting_for_existing_work"}), flush=True)
                break
            sleep(12)
            continue
        readiness = eligible_targets(items)
        now = datetime.now(timezone.utc)
        item = next((i for i in items if readiness[i.module.load_case(i.case_id).workload] <= now), None)
        if item is None:
            until = min(readiness.values())
            if monotonic() >= deadline:
                print(json.dumps({"phase": "cooldown", "ready_at": until.isoformat()}), flush=True)
                break
            sleep(min(12, max(0.1, (until - now).total_seconds())))
            continue
        raw = directory / f"{item.case_id}-{item.arm}.json"
        print(json.dumps({"phase": "starting", "case": item.case_id, "arm": item.arm}), flush=True)
        child = subprocess.run(command(item, evidence, raw), capture_output=True, text=True)
        lines = child.stdout.splitlines()
        print(lines[-1] if lines else json.dumps({"phase": "child_exit", "exit_code": child.returncode}), flush=True)
        if (evidence / "dev-k8s-fault-active.json").exists():
            raise live.Refused("the finished child retained a trial journal")
        if not raw.is_file():
            if "node stop gate breached" in child.stdout and monotonic() < deadline:
                sleep(12)
                continue
            break
        attempted.add(item)
        data = json.loads(raw.read_text(encoding="utf-8"))
        if not child.returncode and not data.get("model_failed") and verify_receipt(item, data, evidence):
            appended.append(item)
            checkpoint([item])
        if data.get("cleanup_error") or not scope_idle(evidence):
            break
        deadline = monotonic() + wait_s
    return appended


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--wait-seconds", type=int, default=180)
    parser.add_argument("--execute", action="store_true")
    isolated_oom_target.arguments(parser)
    args = parser.parse_args()
    if not 1 <= args.limit <= 8 or not 0 <= args.wait_seconds <= 600:
        parser.error("use 1 to 8 trials and a 0 to 600 second idle wait")
    rows = read_ledger(args.ledger)
    target = isolated_oom_target.bind(args)
    allowed = target.case_ids if target else set(CASES)
    selected = set(args.case) if args.case else allowed
    if not selected <= allowed:
        parser.error("a selected case is outside the admitted isolated fixture scope")
    evidence = target.evidence(live.EVIDENCE) if target else live.EVIDENCE
    if not args.execute:
        print(json.dumps([{"case": x.case_id, "arm": x.arm} for x in choices(rows, evidence, selected, set())]))
        return
    if target:
        evidence.mkdir(mode=0o700, exist_ok=True)
    with (evidence / "native-dev-comparison-campaign.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory = evidence / ("native-dev-campaign-" + name)
        directory.mkdir(mode=0o700)
        ledger = args.ledger
        revisions = 0

        def checkpoint(items):
            nonlocal ledger, revisions
            revisions += 1
            snapshot = directory / f"checkpoint-{revisions:02d}"
            snapshot.mkdir()
            ledger = write_update(ledger, evidence, snapshot, items)

        reconciliation = [Attempt(row["case"], row["arm"]) for row in rows
                          if row["case"] in selected and row["status"] == "pending"]
        recover_saved_oom_receipts(reconciliation, evidence)
        reconciliation = [item for item in reconciliation if item.receipt(evidence).is_file()]
        if reconciliation:
            if target:
                for item in reconciliation:
                    target.verify_receipt(json.loads(item.receipt(evidence).read_text()))
            checkpoint(reconciliation)
        appended = run(read_ledger(ledger), selected, args.limit, args.wait_seconds, directory, checkpoint)
        summary = {"verified": [f"{x.case_id}/{x.arm}" for x in appended],
                   "reconciled": [f"{x.case_id}/{x.arm}" for x in reconciliation],
                   "ledger": str(ledger), "evidence": str(directory)}
        live.write_once(directory / "campaign.json", summary)
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (live.Refused, live.CHAOS.Stop, ValueError, BlockingIOError) as error:
        print(json.dumps({"phase": "stopped", "reason": str(error)}), flush=True)
        raise SystemExit(2) from None
