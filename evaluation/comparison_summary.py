"""Build the case-by-arm runtime and token table without imputing missing executions."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

from evaluation.dev_image_case import IMAGE_WORKLOADS, load_image_case, successful_probes
from evaluation.dev_selector_case import ENDPOINT_GRACE_SECONDS, SELECTOR_WORKLOADS, load_selector_case
from evaluation.application_fixture_replay import RUNTIME_FIXTURE_SETTINGS


COHORTS = ("basic", "kubernetes_new", "application")
ORDER_SINGLE_PROTOCOLS = ("live-repair/v20", "live-repair/v22")
ORDER_DUAL_PROTOCOLS = ("live-repair/v21", "live-repair/v23")
ORDER_PROTOCOLS = (*ORDER_SINGLE_PROTOCOLS, *ORDER_DUAL_PROTOCOLS)
DEV_WORKLOAD_PROTOCOL = "live-repair/v36"
DEV_WORKLOAD_EVIDENCE = DEV_WORKLOAD_PROTOCOL.replace("/", "-")
MEMBER_RELEASE_PROTOCOLS = {"member-13": "live-repair/v32", "member-23": "live-repair/v33"}
ORDER_RUNTIME = {
    "order-01": ("live-repair/v24", "latency"),
    "order-04": ("live-repair/v25", "not_ready"),
    "order-16": ("live-repair/v26", "dependency_failure"),
}
FIELDS = ("cohort", "case", "arm", "model", "effort", "status", "input_tokens", "cached_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens", "model_duration_ms",
          "analysis_wall_ms", "client_wall_ms", "time_to_incident_s", "estimated_usd",
          "observation_seconds", "application_state", "reason_code", "scope", "evidence_sha256")
REPAIR_FIELDS = ("cohort", "case", "arm", "model", "effort", "protocol", "status", "scope",
                 "verified_local_recovery", "model_calls", "wall_duration_ms", "model_duration_ms", "input_tokens",
                 "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "estimated_usd",
                 "application_state", "reason_code", "input_sha256", "fixture_sha256", "isolated_fixture_profile",
                 "observation_seconds", "matched_incident_count_after", "high_model_calls",
                 "citation_reviewed_calls", "citation_missing_calls", "citations_grounded", "citations_ungrounded",
                 "first_error_to_operation_end_ms", "first_error_to_verified_recovery_ms")


def selected_rows(catalog_path: Path, evidence_dir: Path):
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema") != "liveprobe-comparison-catalog/v1":
        raise ValueError("unknown comparison catalog")
    modes = {mode["id"]: mode for mode in catalog["modes"]}
    if len(modes) != len(catalog["modes"]) or "liveprobe" not in modes:
        raise ValueError("invalid comparison modes")
    case_ids = set()
    for cohort in COHORTS:
        for case in catalog[cohort]["cases"]:
            case_id = case["id"]
            if case_id in case_ids:
                raise ValueError(f"duplicate comparison case: {case_id}")
            case_ids.add(case_id)
            case_dir = evidence_dir / case_id
            live_file, single_file = case_dir / "liveprobe-result.json", case_dir / "single-results.jsonl"
            live = json.loads(live_file.read_text(encoding="utf-8")) if live_file.is_file() else {}
            singles = {}
            if single_file.is_file():
                for line in single_file.read_text(encoding="utf-8").splitlines():
                    saved = json.loads(line)
                    if saved["case"] != case_id:
                        raise ValueError(f"wrong case in {single_file}")
                    arm_id = saved["arm"]
                    mode = modes.get(arm_id)
                    if mode is None or arm_id == "liveprobe":
                        raise ValueError(f"unexpected standalone mode in {single_file}")
                    if saved["status"] == "ok" and any(saved.get(key) != mode[key] for key in ("model", "effort")):
                        raise ValueError(f"wrong standalone model or effort in {single_file}")
                    earlier = singles.get(arm_id)
                    if earlier is None or earlier.get("status") != "ok":
                        singles[arm_id] = saved
                    elif saved["status"] == "ok" and any(saved[key] != earlier[key]
                                                          for key in ("evidence_sha256", "protocol_sha256")):
                        raise ValueError(f"mixed successful attempts; separate the benchmark versions in {single_file}")
            if live and (live["case"] != case_id or live.get("arm", "liveprobe") != "liveprobe"):
                raise ValueError(f"wrong case or arm in {live_file}")
            inputs = {run["evidence_sha256"] for run in singles.values() if run["status"] == "ok"}
            protocols = {run["protocol_sha256"] for run in singles.values() if run["status"] == "ok"}
            if len(inputs) > 1 or len(protocols) > 1:
                raise ValueError(f"baseline arms do not share the same frozen input and prompt: {case['id']}")
            for arm in modes.values():
                record = live if arm["id"] == "liveprobe" else singles.get(arm["id"], {})
                usage = record.get("usage") or {}
                row = {key: record.get(key) for key in FIELDS}
                row.update(cohort=cohort, case=case_id, arm=arm["id"], model=arm.get("model"),
                           effort=arm.get("effort"), status=record.get("status", "pending"))
                for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                    row[key] = usage.get(key)
                if type(row["input_tokens"]) is int and type(row["output_tokens"]) is int:
                    row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
                yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--live-repair", action="store_true", help="use log-only repair receipts instead of legacy analysis snapshots")
    parser.add_argument("--prefer-live-protocol", action="append",
                        help="select a log-only protocol for cases with multiple versions; may be repeated")
    parser.add_argument("--verified-retry", action="append", type=Path, default=[],
                        help="use a verified retry for the matching preserved first attempt")
    parser.add_argument("--reviewed-retry", action="append", type=Path, default=[],
                        help="use a retry with verified recovery or independent evidence of continued failure")
    parser.add_argument("--coverage-json", type=Path, help="write paired recovery metrics and evidence coverage")
    args = parser.parse_args()
    if (args.verified_retry or args.reviewed_retry or args.coverage_json) and not args.live_repair:
        parser.error("retry and coverage options require --live-repair")
    rows = list(repair_rows(args.catalog, args.evidence, preferred=args.prefer_live_protocol,
                           retries=args.verified_retry, reviewed_retries=args.reviewed_retry) if args.live_repair
                else selected_rows(args.catalog, args.evidence))
    with args.csv.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=REPAIR_FIELDS if args.live_repair else FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    if args.coverage_json:
        args.coverage_json.write_text(json.dumps(paired_repair_coverage(rows), ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    totals = {status: sum(row["status"] == status for row in rows) for status in sorted({r["status"] for r in rows})}
    print(json.dumps({"planned_runs": len(rows), "by_status": totals}, ensure_ascii=False))


def repair_rows(catalog_path: Path, evidence_dir: Path, *, preferred=None, retries=(), reviewed_retries=()):
    from evaluation.ki02_admission_fixture import PROTOCOL as ADMISSION_REQUEST_PROTOCOL
    from evaluation.k8s_cgroup_trial import PROTOCOL as CGROUP_PROTOCOL, receipt_valid as cgroup_proof
    from evaluation.k8s_cni_trial import PROTOCOL as CNI_PROTOCOL, receipt_valid as cni_proof
    from evaluation.k8s_cri_trial import PROTOCOL as CRI_PROTOCOL, receipt_valid as cri_proof
    from evaluation.k8s_csi_migration_trial import PROTOCOL as CSI_MIGRATION_PROTOCOL, receipt_valid as csi_proof
    from evaluation.k8s_istio_rollout_trial import PROTOCOL as ISTIO_ROLLOUT_PROTOCOL, receipt_valid as istio_proof
    from evaluation.k8s_endpoint_observation_trial import PROTOCOL as ENDPOINT_OBSERVATION_PROTOCOL, receipt_valid
    from evaluation.k8s_metric_supply_trial import (
        PROTOCOL as METRIC_SUPPLY_PROTOCOL, product_proof as metric_product_proof,
        standalone_proof as metric_standalone_proof,
    )
    from evaluation.k8s_node_pressure_trial import PROTOCOL as NODE_PRESSURE_PROTOCOL, receipt_valid as node_pressure_proof
    from evaluation.k8s_version_skew_trial import PROTOCOL as VERSION_SKEW_PROTOCOL, receipt_valid as skew_proof
    from evaluation.k8s_dns_kube_trial import PROTOCOL as DNS_ROLLOUT_PROTOCOL, product_measures, rollout_proof
    from evaluation.k8s_ingress_api_trial import PROTOCOL as RETIRED_INGRESS_PROTOCOL, ingress_proof
    from evaluation.dev_endpoint_pulse_case import PROTOCOL as PULSE_PROTOCOL
    from evaluation.dev_latency_case import PROTOCOL as LATENCY_PROTOCOL
    from evaluation.dev_oom_case import PROTOCOL as OOM_PROTOCOL
    from evaluation.dev_pool_case import POOL_PROTOCOL
    from evaluation.dev_restart_case import PROTOCOL as CRASH_PROTOCOL

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("schema") != "liveprobe-comparison-catalog/v1":
        raise ValueError("unknown comparison catalog")
    modes = {mode["id"]: mode for mode in catalog["modes"]}
    high_model = modes["single_astra"]["model"]
    repair_dirs = sorted(evidence_dir.glob("live-repair-v*"))
    pulse_dir = evidence_dir / PULSE_PROTOCOL.replace("/", "-")
    pool_dir = evidence_dir / POOL_PROTOCOL.replace("/", "-")
    latency_dir = evidence_dir / LATENCY_PROTOCOL.replace("/", "-")
    crash_dir = evidence_dir / CRASH_PROTOCOL.replace("/", "-")
    oom_dir = evidence_dir / OOM_PROTOCOL.replace("/", "-")
    repair_dirs.extend(folder for folder in (pulse_dir, pool_dir, latency_dir, crash_dir, oom_dir) if folder.is_dir())
    preferred_folders = {protocol.replace("/", "-") for protocol in
                         ((preferred,) if isinstance(preferred, str) else (preferred or ()))}
    retry_records = {}
    for filename in [*retries, *reviewed_retries]:
        record = json.loads(filename.read_text(encoding="utf-8"))
        if record.get("case") in IMAGE_WORKLOADS and record.get("protocol") in (DEV_WORKLOAD_PROTOCOL, "dev-image-trial/v1"):
            record = dev_image_record(record, record["arm"])
        key = (record["case"], record["arm"])
        probes = record.get("independent_probes") or []
        verified_failure = (filename in reviewed_retries and record.get("status") == "unrecovered"
                            and len(probes) >= 2 and any(probe.get("contract_ok") is False for probe in probes))
        if key in retry_records or (record.get("status") != "verified_recovered" and not verified_failure):
            raise ValueError(f"expected one retry with independent recovery or failure evidence for {key}")
        retry_records[key] = record
    seen = set()
    for cohort in COHORTS:
        for case in catalog[cohort]["cases"]:
            case_id = case["id"]
            if case_id in seen:
                raise ValueError(f"duplicate comparison case: {case_id}")
            seen.add(case_id)
            case_dirs = [folder / case_id for folder in repair_dirs if (folder / case_id).is_dir()]
            if len(case_dirs) > 1 and preferred_folders:
                case_dirs = [folder for folder in case_dirs if folder.parent.name in preferred_folders]
                if not case_dirs:
                    raise ValueError(f"preferred protocol not found for {case_id}")
            if len(case_dirs) > 1:
                raise ValueError(f"multiple protocol runs found for {case_id}; select one set of receipts")
            records = {}
            if case_dirs:
                for filename in sorted(case_dirs[0].glob("*.json")):
                    record = json.loads(filename.read_text(encoding="utf-8"))
                    arm = filename.stem
                    dev_workload = case_dirs[0].parent.name == DEV_WORKLOAD_EVIDENCE
                    dev_image = case_id in IMAGE_WORKLOADS and dev_workload
                    dev_selector = case_id in SELECTOR_WORKLOADS and dev_workload
                    dev_replica = case_id == "KW-04" and dev_workload
                    dev_rollout = case_id in ("KW-01", "KW-02", "KW-03", "KW-05", "KW-06") and dev_workload
                    if dev_image:
                        record = dev_image_record(record, arm)
                    if dev_selector:
                        record = dev_selector_record(record, arm)
                    if dev_replica:
                        record = dev_replica_record(record, arm, case)
                    if dev_rollout:
                        record = dev_rollout_record(record, arm, case)
                    if case_dirs[0].parent == pulse_dir:
                        record = dev_pulse_record(record, arm)
                    if case_dirs[0].parent == pool_dir:
                        record = dev_pool_record(record, arm)
                    if case_dirs[0].parent == latency_dir:
                        record = dev_latency_record(record, arm)
                    if case_dirs[0].parent == crash_dir:
                        record = dev_crash_record(record, arm)
                    if case_dirs[0].parent == oom_dir:
                        record = dev_oom_record(record, arm)
                    alternate = retry_records.pop((case_id, arm), None)
                    if alternate:
                        if any(alternate.get(key) != record.get(key) for key in
                               ("case", "arm", "protocol", "input_sha256", "fixture_sha256")):
                            raise ValueError(f"retry differs from the original protocol or evidence for {case_id}/{arm}")
                        record = alternate
                    if (arm not in modes or record.get("arm") != arm or record.get("case") != case_id
                            or (record.get("protocol") or "").replace("/", "-") != case_dirs[0].parent.name):
                        raise ValueError(f"unexpected case, arm or protocol in {filename}")
                    profile = record.get("isolated_fixture_profile")
                    if profile is not None and (profile != "legacy-member" or arm != "liveprobe" or not case_id.startswith("member-")):
                        raise ValueError(f"unexpected isolated service profile in {filename}")
                    if arm != "liveprobe" and any(record.get(name) != modes[arm][name] for name in ("model", "effort")):
                        raise ValueError(f"wrong standalone model or effort in {filename}")
                    if arm != "liveprobe" and any(
                        call.get(name) != modes[arm][name] for call in record.get("calls", []) for name in ("model", "effort")
                    ):
                        raise ValueError(f"mixed model calls in {filename}")
                    if (record["protocol"] not in (ADMISSION_REQUEST_PROTOCOL, DNS_ROLLOUT_PROTOCOL,
                                                   RETIRED_INGRESS_PROTOCOL, ENDPOINT_OBSERVATION_PROTOCOL,
                                                   METRIC_SUPPLY_PROTOCOL, CNI_PROTOCOL, NODE_PRESSURE_PROTOCOL,
                                                   CRI_PROTOCOL, CGROUP_PROTOCOL, VERSION_SKEW_PROTOCOL,
                                                   CSI_MIGRATION_PROTOCOL, ISTIO_ROLLOUT_PROTOCOL)
                            and (not record["healthy_before"]["contract_ok"] or record["fault_before"]["contract_ok"])):
                        raise ValueError(f"missing initial fault evidence in {filename}")
                    if case_id in ("order-11", "order-21"):
                        case_protocols = ORDER_SINGLE_PROTOCOLS if case_id == "order-11" else ORDER_DUAL_PROTOCOLS
                        if record["protocol"] not in case_protocols or not observed_order_release_fault(record):
                            raise ValueError(f"missing independently observed Java order exception in {filename}")
                    if case_id in ORDER_RUNTIME and not observed_order_runtime_fault(record):
                        raise ValueError(f"missing independently observed Java runtime fault in {filename}")
                    if case_id == "payment-08" and not observed_payment_cpu_fault(record):
                        raise ValueError(f"missing independently observed payment CPU saturation in {filename}")
                    if case_id == "member-03" and not observed_member_crash(record):
                        raise ValueError(f"missing actual member process exits in {filename}")
                    if case_id in ("member-17", "member-28") and not observed_member_tail_latency(record):
                        raise ValueError(f"missing actual intermittent member request timeouts in {filename}")
                    if case_id in MEMBER_RELEASE_PROTOCOLS and not observed_member_release_fault(record):
                        raise ValueError(f"missing actual legacy member release exceptions in {filename}")
                    if case_id == "catalog-05" and (record.get("watchdog_restarts_before_analysis") != 1
                                                    or record["fault_before"].get("liveness") != 503):
                        raise ValueError(f"missing liveness or restart evidence in {filename}")
                    if case_id == "catalog-06" and not observed_timeout(record["fault_before"]):
                        raise ValueError(f"missing original HTTP timeout evidence in {filename}")
                    if case_id == "notification-19" and not observed_mixed_notification_fault(record["fault_before"]):
                        raise ValueError(f"missing independent error and delay observations in {filename}")
                    if case_id == "notification-20" and not observed_notification_memory_delay(record):
                        raise ValueError(f"missing independent memory and latency observations in {filename}")
                    if case_id == "notification-26" and not observed_dependency_and_delay(record["fault_before"]):
                        raise ValueError(f"missing dependency refusal and delay observations in {filename}")
                    if case_id == "notification-30" and not observed_startup_and_readiness_faults(record):
                        raise ValueError(f"missing startup exit or separate readiness failure in {filename}")
                    if case_id == "delivery-07" and not observed_intermittent_delivery(record["fault_before"]):
                        raise ValueError(f"missing intermittent shipment failure and counter evidence in {filename}")
                    if case_id == "delivery-09" and not observed_delivery_runtime_leak(record):
                        raise ValueError(f"missing independently measured delivery memory growth in {filename}")
                    if case_id == "delivery-15" and not observed_delivery_invariant_failure(record):
                        raise ValueError(f"missing actual shipment status exception or error count in {filename}")
                    if case_id == "delivery-25" and not observed_delivery_release_faults(record):
                        raise ValueError(f"missing independent shipment input and history failures in {filename}")
                    if case_id == "delivery-29" and not observed_delivery_memory_faults(record):
                        raise ValueError(f"missing actual shipment exception and measured buffer retention in {filename}")
                    if case_id == "payment-27" and not observed_payment_pool_and_exception(record):
                        raise ValueError(f"missing independent slot and exception observations in {filename}")
                    if (case_id in RUNTIME_FIXTURE_SETTINGS and arm == "liveprobe" and record.get("status") == "unrecovered"
                            and not runtime_handoff_proof(record)):
                        raise ValueError(f"missing native application intake or independent terminal failure in {filename}")
                    if case_id == "payment-22" and not observed_dual_payment_code_fault(record):
                        raise ValueError(f"missing independent fee and receipt exception observations in {filename}")
                    if case_id == "catalog-14" and not observed_background_failure(record["fault_before"]):
                        raise ValueError(f"missing the scheduler's actual failed job in {filename}")
                    if case_id == "catalog-24" and not observed_catalog_release_faults(record):
                        raise ValueError(f"missing independent catalog job and price response failures in {filename}")
                    if record["protocol"] == ADMISSION_REQUEST_PROTOCOL:
                        if not kubernetes_admission_request_proof(record):
                            raise ValueError(f"unverified Kubernetes request recovery or failure in {filename}")
                    elif record["protocol"] == DNS_ROLLOUT_PROTOCOL:
                        if not rollout_proof(record):
                            raise ValueError(f"unverified Kubernetes DNS rollout or response in {filename}")
                        if arm == "liveprobe":
                            record.update(product_measures(record))
                    elif record["protocol"] == RETIRED_INGRESS_PROTOCOL:
                        if not ingress_proof(record):
                            raise ValueError(f"unverified Kubernetes Ingress API result in {filename}")
                    elif record["protocol"] == ENDPOINT_OBSERVATION_PROTOCOL:
                        if not receipt_valid(record):
                            raise ValueError(f"unverified Kubernetes endpoint observation result in {filename}")
                    elif record["protocol"] == METRIC_SUPPLY_PROTOCOL:
                        check = metric_product_proof if arm == "liveprobe" else metric_standalone_proof
                        if not check(record):
                            raise ValueError(f"unverified Kubernetes HPA metrics result in {filename}")
                    elif record["protocol"] == CNI_PROTOCOL:
                        if not cni_proof(record):
                            raise ValueError(f"unverified Kubernetes CNI result in {filename}")
                    elif record["protocol"] == NODE_PRESSURE_PROTOCOL:
                        if not node_pressure_proof(record):
                            raise ValueError(f"unverified Kubernetes node pressure result in {filename}")
                    elif record["protocol"] == CRI_PROTOCOL:
                        if not cri_proof(record):
                            raise ValueError(f"unverified Kubernetes CRI negotiation result in {filename}")
                    elif record["protocol"] == CGROUP_PROTOCOL:
                        if not cgroup_proof(record):
                            raise ValueError(f"unverified kubelet cgroup result in {filename}")
                    elif record["protocol"] == VERSION_SKEW_PROTOCOL:
                        if not skew_proof(record):
                            raise ValueError(f"unverified kubelet version skew result in {filename}")
                    elif record["protocol"] == CSI_MIGRATION_PROTOCOL:
                        if not csi_proof(record):
                            raise ValueError(f"unverified legacy CSI volume result in {filename}")
                    elif record["protocol"] == ISTIO_ROLLOUT_PROTOCOL:
                        if not istio_proof(record):
                            raise ValueError(f"unverified Istio LDS version result in {filename}")
                    elif case_id in ("KI-01", "KI-02", "KW-04") and not dev_replica and not kubernetes_preflight_proof(record):
                        raise ValueError(f"unverified Kubernetes preflight scope or manifest in {filename}")
                    records[arm] = record
            for key in ("input_sha256", "fixture_sha256"):
                values = {record.get(key) for record in records.values()}
                if len(values) > 1 or any(not isinstance(value, str) or len(value) != 64 for value in values):
                    raise ValueError(f"non-matching initial evidence ({key}) for {case_id}")
            if case_id in ("delivery-07", "delivery-09", "delivery-15", "delivery-25", "delivery-29",
                           "member-03", "member-17", "member-28", *MEMBER_RELEASE_PROTOCOLS):
                runtimes = {record.get("fixture_runtime") for record in records.values()}
                if len(runtimes) > 1 or any(not isinstance(version, str) or not version.startswith("v") for version in runtimes):
                    raise ValueError("Node.js comparisons require the same observed runtime")
            if case_id in ("order-11", "order-21", *ORDER_RUNTIME):
                runtimes = {record.get("fixture_runtime") for record in records.values()}
                if len(runtimes) > 1 or any(not isinstance(version, str) or 'version "21.' not in version for version in runtimes):
                    raise ValueError("order comparisons require the same observed Java 21 fixture runtime")
            for arm, mode in modes.items():
                data = records.get(arm, {})
                row = {key: data.get(key) for key in REPAIR_FIELDS}
                if row['first_error_to_verified_recovery_ms'] is None:
                    del row['first_error_to_verified_recovery_ms']
                row.update(cohort=cohort, case=case_id, arm=arm, model=mode.get("model"), effort=mode.get("effort"),
                           status=data.get("status", "pending"))
                if data:
                    calls = data.get("calls", [])
                    row["model_calls"] = len(calls)
                    row["high_model_calls"] = sum(call.get("model") == high_model for call in calls)
                    row.update(citation_metrics(calls))
                    row["scope"] = data.get("scope") or "isolated loopback service and independent probe"
                    if row["status"] == "verified_recovered" and data["protocol"] in (
                        ADMISSION_REQUEST_PROTOCOL, DNS_ROLLOUT_PROTOCOL, RETIRED_INGRESS_PROTOCOL,
                        ENDPOINT_OBSERVATION_PROTOCOL, METRIC_SUPPLY_PROTOCOL, CNI_PROTOCOL, NODE_PRESSURE_PROTOCOL,
                        CRI_PROTOCOL, CGROUP_PROTOCOL, VERSION_SKEW_PROTOCOL, CSI_MIGRATION_PROTOCOL,
                        ISTIO_ROLLOUT_PROTOCOL
                    ):
                        row["verified_local_recovery"] = True
                    elif row["status"] == "verified_recovered":
                        observed_write = (data.get("verified_dev_action") is True
                                          if data["protocol"] in (DEV_WORKLOAD_PROTOCOL, PULSE_PROTOCOL, POOL_PROTOCOL, LATENCY_PROTOCOL,
                                                                  CRASH_PROTOCOL, OOM_PROTOCOL)
                                          else recovery_action_matches(data))
                        if not observed_write:
                            raise ValueError(f"recovery has no independent probe or action evidence: {case_id}/{arm}")
                        verify_recovery_probes(data, case_id, arm)
                        row["verified_local_recovery"] = True
                    if row["status"] == "unrecovered":
                        row["verified_local_recovery"] = False
                    usage = data.get("usage") or {}
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                        row[key] = usage.get(key)
                    if type(row["input_tokens"]) is int and type(row["output_tokens"]) is int:
                        row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
                yield row
    if retry_records:
        raise ValueError(f"no matching preserved first attempt for {sorted(retry_records)}")


def verify_recovery_probes(data, case_id, arm):
    verified = data.get("verified") or []
    if len(verified) < 2 or not all(p.get("contract_ok") and p.get("business") == p.get("ready") == 200 for p in verified):
        raise ValueError(f"recovery has no independent probe or action evidence: {case_id}/{arm}")
    if case_id == "catalog-05" and any(probe.get("liveness") != 200 for probe in verified):
        raise ValueError(f"liveness did not recover: {case_id}/{arm}")
    if case_id == "catalog-06" and any(not within_original_deadline(probe, data["fault_before"]) for probe in verified):
        raise ValueError(f"the original HTTP deadline was not met: {case_id}/{arm}")
    if case_id == "notification-19" and any(not notification_probe_recovered(probe) for probe in verified):
        raise ValueError(f"both notification symptoms did not clear: {case_id}/{arm}")
    if case_id == "notification-26" and any(not dependency_probe_recovered(probe) for probe in verified):
        raise ValueError(f"successful upstream request or queue recovery is missing: {case_id}/{arm}")
    if case_id == "payment-27" and any(probe.get("ledger_intact") is not True or probe.get("exception") is not None
                                       for probe in verified):
        raise ValueError(f"payment business proof missing: {case_id}/{arm}")
    if case_id == "catalog-14" and not background_jobs_recovered(verified, data["fault_before"]):
        raise ValueError(f"two successful scheduled rebuilds not observed: {case_id}/{arm}")


def dev_image_record(record, arm):
    """Independently review the actual dev fault before admitting it to the paired ledger."""
    from evaluation.live_repair import TASK

    case = load_image_case(record.get("case"))
    baseline, bad = record.get("baseline_image"), record.get("injected_image")
    fault = record.get("fault_before") or {}
    if ((record.get("arm"), record.get("namespace"), record.get("target")) != (arm, "dev", case.workload)
            or record.get("protocol") != ("dev-image-trial/v1" if arm == "liveprobe" else DEV_WORKLOAD_PROTOCOL)
            or not isinstance(baseline, str)
            or not re.fullmatch(rf"liveprobe/{case.workload}@sha256:[a-f0-9]{{64}}", baseline)
            or bad != case.bad_image
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or fault.get("reason") not in {"ErrImagePull", "ImagePullBackOff"}
            or bad not in (fault.get("initial_error_log") or "")):
        raise ValueError(f"invalid dev workload, fixture, or recorded first error for {arm}")

    initial = f'{fault["reason"]}: {bad}' if arm == "liveprobe" else record.get("initial_error_log")
    if initial != f'{fault["reason"]}: {bad}':
        raise ValueError(f"non-matching canonical dev image error for {arm}")
    fingerprint = hashlib.sha256((TASK + initial).encode()).hexdigest()
    if arm != "liveprobe" and record.get("input_sha256") != fingerprint:
        raise ValueError(f"standalone first error digest mismatch for {arm}")

    def positive(probes):
        return (isinstance(probes, list) and len(probes) == 2
                and all(probe.get("contract_ok") is True and probe.get("ready") == probe.get("business") == 200
                        and (case.case_id == "F1-01" or probe.get("order_dependency") == 200)
                        for probe in probes))

    before = record.get("baseline" if arm == "liveprobe" else "healthy_before")
    fault_service = record.get("fault_service_probe") or {}
    if (not positive(before) or not positive(record.get("restored"))
            or not isinstance(before[0].get("endpoint_uid"), str) or not before[0]["endpoint_uid"]
            or before[0]["endpoint_uid"] != before[1].get("endpoint_uid")
            or fault_service.get("endpoint_uid") != before[0]["endpoint_uid"]
            or fault_service.get("contract_ok") is not True
            or (case.case_id != "F1-01" and fault_service.get("order_dependency") != 200)):
        raise ValueError(f"baseline or independent recovery evidence is missing for {arm}")

    if arm == "liveprobe":
        incident = record.get("incident") or {}
        executed = [action for action in (incident.get("actions") or []) if action.get("executed") is True]
        action = len(executed) == 1 and (
            executed[0].get("type"), executed[0].get("target"), executed[0].get("status"),
            executed[0].get("from_images"), executed[0].get("to_images"),
        ) == ("rollout_undo", case.target, "succeeded", [bad], [baseline])
        calls = incident.get("calls") or []
        usage = {key: sum(call[key] for call in calls) if calls and all(type(call.get(key)) is int for call in calls) else None
                 for key in ("input_tokens", "output_tokens")}
        cost = (sum(call["estimated_usd"] for call in calls)
                if calls and all(type(call.get("estimated_usd")) in (int, float) for call in calls) else None)
        start, end = record.get("first_error_ms"), record.get("wall_duration_ms")
        duration = end - start if type(start) is type(end) is int and 0 <= start <= end else None
        model_time = (sum(call["duration_ms"] for call in calls)
                      if calls and all(type(call.get("duration_ms")) is int for call in calls) else None)
        handed_off_without_action = (incident.get("status") == "handed_off" and not executed
                                     and not record.get("pre_cleanup_verified"))
    else:
        action = record.get("standalone_action") == {
            "accepted": True, "action": "image_cas", "from_images": [bad], "to_images": [baseline],
        } and record.get("lease_released") is True and record.get("liveprobe_incidents") == []
        calls, usage = record.get("calls") or [], record.get("usage") or {}
        cost, duration = record.get("estimated_usd"), record.get("first_error_to_verified_ms")
        model_time = record.get("model_duration_ms")
        handed_off_without_action = False
    proof = record.get("pre_cleanup_verified")
    verified = (record.get("status") == "verified_dev_recovered" and action and positive(proof)
                and not record.get("trial_error"))
    if record.get("status") == "verified_dev_recovered" and not verified:
        raise ValueError(f"dev recovery is not attributable to the measured arm {arm}")
    return {**record, "protocol": DEV_WORKLOAD_PROTOCOL, "input_sha256": fingerprint,
            "scope": "actual dev rollout; original Service maintained; verified before trial cleanup",
            "healthy_before": {"contract_ok": True}, "fault_before": {**fault, "contract_ok": False},
            "status": ("verified_recovered" if verified else "unrecovered" if handed_off_without_action
                       else "product_observation_only"),
            "verified_dev_action": verified, "verified": proof if verified else None,
            "wall_duration_ms": duration, "calls": calls, "usage": usage, "estimated_usd": cost,
            "model_duration_ms": model_time}


def dev_replica_record(record, arm, case):
    from evaluation.live_repair import TASK

    workload = "lp-eval-kw04"
    missing_account = f"{workload}-missing"
    fault = record.get("fault_before") or {}
    message, replica_set = fault.get("error"), fault.get("replica_set")
    error = (f'Error creating: pods "{replica_set}-" is forbidden: error looking up service account '
             f'dev/{missing_account}: serviceaccount "{missing_account}" not found')
    case_hash = hashlib.sha256(json.dumps(case, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    fingerprint = hashlib.sha256((TASK + error).encode()).hexdigest()
    if ((record.get("case"), record.get("arm"), record.get("target")) != ("KW-04", arm, f"dev/Deployment/{workload}")
            or case.get("environment") != "isolated-workload"
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or not re.fullmatch(r"lp-eval-kw04-[a-z0-9]+", replica_set or "")
            or message != error or fault.get("error_sha256") != hashlib.sha256(error.encode()).hexdigest()
            or not fault.get("uid") or not fault.get("event_uid") or (fault.get("replicas"), fault.get("desired")) != (0, 1)
            or any(record.get(key) for key in ("trial_error", "cleanup_error", "lease_error"))):
        raise ValueError(f"invalid actual replica creation failure for {arm}")
    if fault.get("error_source") == "current_replicaset_condition":
        previous, current = record.get("replicaset_generation_before"), fault.get("replicaset_generation")
        if not (type(previous) is type(current) is int and current > previous and fault.get("condition_transition")):
            raise ValueError(f"replica failure condition is not from the new generation for {arm}")

    def requests(probe):
        return (isinstance(probe, dict) and probe.get("contract_ok") is True and probe.get("replicas") == 1
                and probe.get("requests") == [200, 200] and type(probe.get("generation")) is int and bool(probe.get("pod_uid")))

    before, restored = record.get("healthy_before"), record.get("restored")
    if not requests(before) or not requests(restored) or restored["generation"] <= before["generation"]:
        raise ValueError(f"replica fixture did not return to the original baseline for {arm}")
    failure = record.get("still_failed_before_cleanup") or []
    same_fault = (len(failure) == 2 and all(
        (row.get("uid"), row.get("event_uid"), row.get("error_sha256"), row.get("replicas"), row.get("desired"))
        == (fault["uid"], fault["event_uid"], fault["error_sha256"], 0, 1) for row in failure))
    unrecovered = (record.get("status") == "observed_terminal_unrecovered"
                   and same_fault and record.get("operator_restored") is True)
    start, end = record.get("first_error_ms"), record.get("wall_duration_ms")
    duration = end - start if type(start) is type(end) is int and 0 <= start <= end else None
    verified = False
    if arm == "liveprobe":
        incident = record.get("incident") or {}
        if (not unrecovered or incident.get("status") != "handed_off" or not incident.get("id")
                or "ReplicaCreationFailed" not in (incident.get("signal_reasons") or [])
                or any(action.get("executed") is True for action in (incident.get("actions") or []))):
            raise ValueError("missing product handoff with independent pre-cleanup failure evidence")
        calls = incident.get("calls") or []
        usage = {key: sum(call[key] for call in calls) if calls and all(type(call.get(key)) is int for call in calls) else None
                 for key in ("input_tokens", "output_tokens")}
        cost = (sum(call["estimated_usd"] for call in calls)
                if calls and all(type(call.get("estimated_usd")) in (int, float) for call in calls) else None)
        model_time = (sum(call["duration_ms"] for call in calls)
                      if calls and all(type(call.get("duration_ms")) is int for call in calls) else None)
    else:
        if ((record.get("case_sha256"), record.get("initial_error_log"), record.get("input_sha256"))
                != (case_hash, error, fingerprint) or record.get("protocol") != DEV_WORKLOAD_PROTOCOL
                or record.get("lease_released") is not True or record.get("liveprobe_incidents") != []):
            raise ValueError(f"standalone replica inputs or ownership did not match for {arm}")
        proof = record.get("verified_before_cleanup")
        verified = (record.get("status") == "verified_dev_recovered" and requests(proof)
                    and proof["generation"] == restored["generation"] and record.get("operator_restored") is False
                    and record.get("standalone_action") == {"accepted": True, "action": "service_account_alias_cas",
                                                            "from": missing_account, "to": "default"})
        if not (verified or unrecovered):
            raise ValueError(f"replica outcome is missing independent evidence for {arm}")
        calls, usage = record.get("calls") or [], record.get("usage") or {}
        cost, model_time = record.get("estimated_usd"), record.get("model_duration_ms")
        if verified:
            duration = record.get("first_error_to_verified_ms")
    http_proof = [{"contract_ok": True, "ready": status, "business": status}
                  for status in proof["requests"]] if verified else None
    return {**record, "protocol": DEV_WORKLOAD_PROTOCOL, "input_sha256": fingerprint,
            "scope": "actual isolated dev replica creation failure; fixture HTTP contract independently verified",
            "fault_before": {**fault, "contract_ok": False}, "verified": http_proof, "verified_dev_action": verified,
            "status": "verified_recovered" if verified else "unrecovered", "wall_duration_ms": duration,
            "calls": calls, "usage": usage, "estimated_usd": cost, "model_duration_ms": model_time}


def dev_rollout_record(record, arm, case):
    from evaluation.dev_rollout_case import (CASES, LIVENESS_ERROR, LIVENESS_OBSERVATION_SECONDS,
                                             VOLUME_ERROR, VOLUME_NAME, VOLUME_OBSERVATION_SECONDS)
    from evaluation.live_repair import TASK

    case_id = case["id"]
    fixture = CASES.get(case_id)
    if fixture is None:
        raise ValueError(f"unknown isolated rollout case: {case_id}")
    workload = fixture.workload
    broken, healthy_value, repair = fixture.broken, fixture.healthy, fixture.repair_key + "_cas"
    reason = fixture.signal
    fault = record.get("fault_before") or {}
    replica_set = fault.get("replicaset") or ""
    expected_ready = (0,)
    stable_fields = ("pod_uid", "generation", "reason", "sha256")
    if case_id == "KW-01":
        error = fault.get("error") or ""
        symptoms = {"waiting_reason": "Unschedulable", "pod_scheduled": False,
                    "node_selector": {"kubernetes.io/os": broken}}
        os_labels = record.get("node_os_labels")
        if (not re.match(r"^0/[1-9][0-9]* nodes are available:", error)
                or "node affinity/selector" not in error.lower() or not isinstance(os_labels, list)
                or healthy_value not in os_labels or broken in os_labels):
            raise ValueError(f"scheduler rejection or node inventory not proved for {arm}")
    elif case_id == "KW-02":
        error = f'configmap "{broken}" not found'
        symptoms = {"waiting_reason": "CreateContainerConfigError", "container_started": False}
    elif case_id == "KW-03":
        error = VOLUME_ERROR
        bindings = record.get("volume_bindings") or {}
        faulty_binding = bindings.get(broken) or {}
        symptoms = {"claim": broken, "claim_binding": faulty_binding, "pod_scheduled": True, "container_started": False}
        stable_fields += ("event_uid", "first_warning_at")
        repeated = record.get("fault_repeated") or {}
        if (set(bindings) != {healthy_value, broken} or record.get("restored_volume_bindings") != bindings
                or faulty_binding.get("pv") != VOLUME_NAME or bindings[healthy_value].get("pv") == VOLUME_NAME
                or not all(row.get("claim_uid") and row.get("pv_uid") and row.get("pv") for row in bindings.values())
                or not fault.get("event_uid") or not fault.get("first_warning_at")
                or not all(fault.get(key) == repeated.get(key) for key in (*stable_fields, *symptoms))
                or type(fault.get("warnings")) is not int or fault["warnings"] < 1
                or type(repeated.get("warnings")) is not int or repeated["warnings"] < max(2, fault["warnings"])):
            raise ValueError(f"same-Pod mount event and original volume bindings were not proved for {arm}")
    elif case_id == "KW-06":
        error = LIVENESS_ERROR
        symptoms = {"liveness_path": broken, "min_ready_seconds": 24}
        expected_ready = (0, 1)
        stable_fields += ("event_uid", "first_warning_at")
        repeated = record.get("fault_repeated") or {}
        if (not fault.get("event_uid") or not fault.get("first_warning_at")
                or type(fault.get("warnings")) is not int or fault["warnings"] < 1
                or not all(fault.get(key) == repeated.get(key) for key in (*stable_fields, *symptoms))
                or type(repeated.get("warnings")) is not int or repeated["warnings"] < 3
                or type(repeated.get("restarts")) is not int or repeated["restarts"] < 1):
            raise ValueError(f"same-Pod warning and repeated liveness restarts not proved for {arm}")
    else:
        error = f'ReplicaSet "{replica_set}" has timed out progressing.'
        symptoms = {"requests": [404, 404]}
    case_hash = hashlib.sha256(json.dumps(case, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    fingerprint = hashlib.sha256((TASK + error).encode()).hexdigest()
    if ((record.get("case"), record.get("arm"), record.get("target")) != (case_id, arm, f"dev/Deployment/{workload}")
            or case.get("environment") != "isolated-workload" or record.get("protocol") != DEV_WORKLOAD_PROTOCOL
            or record.get("case_sha256") != case_hash or record.get("input_sha256") != fingerprint
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or not re.fullmatch(re.escape(workload) + r"-[a-z0-9]+", replica_set)
            or (fault.get("reason"), fault.get("error"), fault.get("sha256")) != (
                reason, error, hashlib.sha256(error.encode()).hexdigest())
            or fault.get("available_old") != 1 or fault.get("ready_new") not in expected_ready
            or any(fault.get(key) != value for key, value in symptoms.items())
            or not fault.get("pod_uid") or any(record.get(key) for key in ("trial_error", "cleanup_error", "model_failed"))):
        raise ValueError(f"invalid actual rollout or fixture evidence for {arm}")

    def healthy(probe):
        return (isinstance(probe, dict) and probe.get("contract_ok") is True and probe.get("replicas") == 1
                and probe.get("requests") == [200, 200] and type(probe.get("generation")) is int and bool(probe.get("pod_uid"))
                and (case_id != "KW-06" or (probe.get("restarts"), probe.get("min_ready_seconds")) == (0, 24)))

    before, restored = record.get("healthy_before"), record.get("restored")
    generation = fault.get("generation")
    if not (healthy(before) and healthy(restored) and type(generation) is int
            and before["generation"] < generation < restored["generation"]):
        raise ValueError(f"original rollout did not regain health for {arm}")
    still = record.get("still_failed_before_cleanup") or {}
    still_failed = (record.get("operator_restored") is True
                    and all(fault[key] == still.get(key) for key in (*stable_fields, *symptoms)))
    if case_id == "KW-06" and still_failed:
        still_failed = (type(still.get("warnings")) is int and still["warnings"] >= repeated["warnings"]
                        and type(still.get("restarts")) is int and still["restarts"] >= repeated["restarts"])
    if case_id == "KW-03" and still_failed:
        still_failed = type(still.get("warnings")) is int and still["warnings"] >= repeated["warnings"]
    unrecovered = record.get("status") == "observed_terminal_unrecovered" and still_failed
    proof = record.get("verified_before_cleanup")
    verified = (record.get("status") == "verified_dev_recovered" and healthy(proof)
                and proof["generation"] == restored["generation"] and record.get("operator_restored") is False
                and record.get("standalone_action") == {"accepted": True, "action": repair,
                                                        "from": broken, "to": healthy_value})
    if arm == "liveprobe":
        incident = record.get("incident") or {}
        if case_id in ("KW-03", "KW-06") and record.get("status") == "product_observation_only":
            watch = record.get("watchers_at_start") or {}
            duration = VOLUME_OBSERVATION_SECONDS if case_id == "KW-03" else LIVENESS_OBSERVATION_SECONDS
            if (incident or not still_failed or record.get("matched_incident_count_after") != 0
                    or set(watch) != {"pod_watch", "event_watch"} or not all(watch.values())
                    or watch != record.get("watchers_at_end") or type(record.get("observation_seconds")) is not int
                    or record["observation_seconds"] < duration):
                raise ValueError("product observation did not establish a bounded undetected failure")
            return {**record, "scope": f"isolated dev {reason} fault; no incident during bounded observation",
                    "fault_before": {**fault, "contract_ok": False}, "calls": [], "usage": {}}
        matched_signals = {"CrashLoopBackOff", "LivenessProbeFailed"} if case_id == "KW-06" else {reason}
        if (not unrecovered or incident.get("status") != "handed_off" or not incident.get("id")
                or not matched_signals.intersection(incident.get("signal_reasons") or [])):
            raise ValueError("product rollout handoff or independent failure proof missing")
        calls = incident.get("calls") or []
        usage = {key: sum(c[key] for c in calls) if calls and all(type(c.get(key)) is int for c in calls) else None
                 for key in ("input_tokens", "output_tokens")}
        cost = (sum(c["estimated_usd"] for c in calls)
                if calls and all(type(c.get("estimated_usd")) in (int, float) for c in calls) else None)
        model_time = (sum(c["duration_ms"] for c in calls)
                      if calls and all(type(c.get("duration_ms")) is int for c in calls) else None)
    else:
        if not ((verified or unrecovered) and record.get("lease_released") is True and record.get("liveprobe_incident") is None):
            raise ValueError(f"standalone rollout repair, ownership or independent result not proved for {arm}")
        calls, usage = record.get("calls") or [], record.get("usage") or {}
        cost, model_time = record.get("estimated_usd"), record.get("model_duration_ms")
    http_proof = [{"contract_ok": True, "ready": status, "business": status}
                  for status in proof["requests"]] if verified else None
    return {**record, "scope": "isolated dev rollout fault; current rollout and fixture HTTP verified independently",
            "status": "verified_recovered" if verified else "unrecovered", "wall_duration_ms": record.get("duration_ms"),
            "calls": calls, "usage": usage, "estimated_usd": cost, "model_duration_ms": model_time,
            "fault_before": {**fault, "contract_ok": False}, "verified": http_proof, "verified_dev_action": verified}


def dev_selector_record(record, arm):
    from evaluation.live_repair import TASK

    case = load_selector_case(record["case"])
    fault = record.get("fault_before") or {}
    original = record.get("selector_before") or {}
    uid = record.get("service_uid")
    expected = {**original, "app": case.bad_app}
    message = fault.get("error")

    def is_failed(observed):
        return (isinstance(observed, dict) and observed.get("contract_ok") is False
                and observed.get("selector") == expected and observed.get("service_uid") == uid
                and observed.get("ready_endpoints") == 0 and observed.get("http_status") == 503
                and observed.get("error") == message)

    def is_healthy(rows):
        return (isinstance(rows, list) and len(rows) == 2
                and all(row.get("contract_ok") is True and row.get("ready") == row.get("business") == 200
                        and row.get("order_dependency") == 200 and row.get("endpoint_uid") for row in rows))

    before = (record.get("healthy_before") or {}).get("probes")
    restored = record.get("restored")
    if (record.get("protocol") != DEV_WORKLOAD_PROTOCOL or record.get("target") != case.target
            or record.get("namespace") != "dev" or record.get("arm") != arm or not uid
            or record.get("case_sha256") != case.case_sha256 or original.get("app") != case.workload
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("fixture_sha256") or "")
            or message not in case.errors or record.get("input_sha256") != hashlib.sha256((TASK + message).encode()).hexdigest()
            or not is_failed(fault) or not is_healthy(before) or not is_healthy(restored)
            or len({row["endpoint_uid"] for row in before + restored}) != 1
            or any(record.get(key) for key in ("trial_error", "cleanup_error", "lease_error", "model_failed"))):
        raise ValueError("the original Service 503 and checked restoration were not proved")
    status = record.get("status")
    recovered = False
    if arm == "liveprobe":
        incident = record.get("incident") or {}
        if record.get("watcher_continuous") is not True:
            raise ValueError("live Service monitoring was not continuous")
        if status == "product_observation_only":
            probes = record.get("independent_probes") or []
            elapsed = record.get("observation_seconds")
            if incident or type(elapsed) is not int or elapsed < ENDPOINT_GRACE_SECONDS or len(probes) != 1 or not is_failed(probes[0]):
                raise ValueError("the bounded no-detection observation was not proved")
        else:
            signal = incident.get("first_endpoint_signal_at")
            started = record.get("injection_started_at")
            if not isinstance(signal, str) or not isinstance(started, str):
                raise ValueError("endpoint signal is missing its original timestamp")
            delta = datetime.fromisoformat(signal) - datetime.fromisoformat(started)
            if (status != "unrecovered" or incident.get("status") != "handed_off"
                    or incident.get("executed_actions") != 0 or not incident.get("id")
                    or not {"NoEndpoints", "TargetServiceNoEndpoints"}.intersection(incident.get("signal_reasons") or [])
                    or round(delta.total_seconds() * 1000) != record.get("detection_ms")
                    or delta.total_seconds() < ENDPOINT_GRACE_SECONDS):
                raise ValueError("product terminal handoff or 10-minute grace period was not proved")
    else:
        if record.get("lease_released") is not True or record.get("interfering_incidents") != 0 or not record.get("calls"):
            raise ValueError("standalone Service trial did not prove scoped isolation")
        if status == "verified_recovered":
            probes = record.get("verified_before_cleanup")
            action = {"accepted": True, "action": "selector_app_cas", "from": case.bad_app, "to": case.workload}
            if (record.get("operator_restored") is not False or record.get("standalone_action") != action
                    or not is_healthy(probes) or any(row["endpoint_uid"] != before[0]["endpoint_uid"] for row in probes)):
                raise ValueError("recovery did not precede the trial cleanup")
            recovered = True
        elif status != "unrecovered":
            raise ValueError("standalone Service trial did not reach a terminal result")
    if status == "unrecovered":
        probes = record.get("independent_probes") or []
        if record.get("operator_restored") is not True or len(probes) != 2 or not all(is_failed(row) for row in probes):
            raise ValueError("the Service 503 was not independently confirmed before cleanup")
    return {**record, "scope": "dev Service selector change, original endpoint identity and HTTP checks",
            "verified_dev_action": recovered, "verified": record.get("verified_before_cleanup") if recovered else None}


def dev_pulse_record(record, arm):
    from evaluation import dev_endpoint_pulse_case as pulse
    from evaluation.live_repair import TASK

    target = record.get("target")
    case = pulse.load_case(record["case"], isolated_order=target == f"dev/Service/{pulse.ORDER_FIXTURE}",
                           isolated_inventory=target == f"dev/Service/{pulse.INVENTORY_FIXTURE}")
    fault = record.get("fault_before") or {}
    before = (record.get("healthy_before") or {}).get("probes")
    restored = record.get("restored")
    if ((arm, pulse.PROTOCOL, case.target, case.case_sha256) != tuple(record.get(k) for k in (
            "arm", "protocol", "target", "case_sha256")) or record.get("duration_seconds") != case.duration
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("fixture_sha256") or "")
            or any(record.get(k) for k in ("trial_error", "cleanup_error", "model_failed"))
            or fault.get("error") not in case.errors or fault.get("ready_endpoints") != 0
            or fault.get("http_status") != 503 or fault.get("order_dependency") not in (500, 502, 503, 504)
            or fault.get("contract_ok") is not False or record.get("input_sha256") != hashlib.sha256(
                (TASK + fault["error"]).encode()).hexdigest()
            or not successful_probes(before) or not successful_probes(restored)):
        raise ValueError(f"invalid scoped pulse, request failure or recovery probes for {arm}")
    if (not before[0].get("endpoint_uid") or any(
            row.get("endpoint_uid") != before[0]["endpoint_uid"] or row.get("order_dependency") != 200
            for row in before + restored)):
        raise ValueError(f"pulse restored a different endpoint or dependency for {arm}")
    operation = record["operation"]
    start, expires = pulse.receipt_times(case, operation)
    first = datetime.fromisoformat(fault["first_503_at"])
    terminal = pulse.bounded_termination(case, operation, record.get("termination_events") or [])
    recovered_at = datetime.fromisoformat(record["recovered_observed_at"])
    observed = datetime.fromisoformat(fault["observed_at"])
    timing = ("control_observed_at", "dependency_started_at", "dependency_finished_at")
    if any(key in fault for key in timing):
        control_at, dependency_start, dependency_end = (datetime.fromisoformat(fault[key]) for key in timing)
        if (not start <= min(control_at, dependency_start) or max(first, control_at, dependency_start) != observed
                or not dependency_start <= dependency_end <= recovered_at
                or (dependency_end - dependency_start).total_seconds() > pulse.DEPENDENCY_PROBE_LIMIT_SECONDS):
            raise ValueError(f"pulse dependency did not start during the observed hold for {arm}")
    if not terminal or not start <= first <= observed < expires or not first <= terminal[1] <= recovered_at:
        raise ValueError(f"pulse termination could not be attributed on the observed timeline for {arm}")
    result = {**record, "scope": "bounded dev Service pulse with scoped termination and same-Pod HTTP verification",
              "first_error_to_operation_end_ms": round((terminal[1] - first).total_seconds() * 1000)}
    actor = terminal[0]
    if arm == "liveprobe":
        if (record.get("status") != "product_pulse_observed" or record.get("incidents") != [] or actor != "expired"
                or record.get("observation_seconds_after_recovery", 0) < pulse.OBSERVE_AFTER_RECOVERY
                or datetime.fromisoformat(record["endpoint_watcher_epoch"]) > start):
            raise ValueError("product pulse must show natural expiry and continuous observation without a new incident")
        return {**result, "status": "verified_natural_recovery", "reason_code": "pulse_expired_without_incident",
                "calls": [], "wall_duration_ms": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}, "estimated_usd": 0}
    if (case.workload == case.application or record.get("lease_released") is not True
            or record.get("incidents") != [] or not record.get("calls")):
        raise ValueError(f"pulse standalone execution identity or isolation was not verified for {arm}")
    if actor == "expired" and not record.get("standalone_action"):
        completion = pulse.standalone_completion(record)
        status = record.get("status")
        if completion and (status == "verified_natural_pulse_recovery"
                           or (status == "trial_unverified" and completion == "standalone_tool_limit_without_conclusion")):
            return {**result, "status": "verified_natural_recovery", "reason_code": completion,
                    "wall_duration_ms": record["model_evaluation_duration_ms"]}
    checks = record.get("verified_before_cleanup")
    if (record.get("status") != "verified_model_pulse_recovery" or actor != "reset"
            or record.get("standalone_action") != {"accepted": True, "action": "scoped_operation_reset",
                                                   "operation_id": operation["operation_id"]}
            or not successful_probes(checks) or checks != restored):
        raise ValueError(f"pulse model reset and independent recovery were not proved for {arm}")
    return {**result, "status": "verified_recovered", "verified_dev_action": True, "verified": checks,
            "wall_duration_ms": record["model_evaluation_duration_ms"]}


def dev_pool_record(record, arm):
    from evaluation.dev_endpoint_pulse_case import standalone_completion
    from evaluation.dev_pool_case import (
        POOL_DURATION_SECONDS, POOL_ERRORS, POOL_FIXTURES, POOL_PROTOCOL, load_pool_case, operation_window,
    )
    from evaluation.live_repair import TASK

    case_id = record.get("case")
    if case_id not in POOL_FIXTURES:
        raise ValueError(f"unsupported pool case for {arm}")
    case = load_pool_case(case_id)
    fault = record.get("fault_before") or {}
    healthy = (record.get("healthy_before") or {}).get("probes")
    restored = record.get("restored")
    if ((case.case_id, arm, POOL_PROTOCOL, f"dev/Service/{POOL_FIXTURES[case_id]}", case.case_sha256)
            != tuple(record.get(key) for key in ("case", "arm", "protocol", "target", "case_sha256"))
            or record.get("status") != "pool_observed" or record.get("duration_seconds") != POOL_DURATION_SECONDS
            or any(record.get(key) for key in ("trial_error", "cleanup_error", "model_failed"))
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or not POOL_ERRORS[case.application].fullmatch(fault.get("error") or "") or record["input_sha256"] != hashlib.sha256(
                (TASK + fault["error"]).encode()).hexdigest()
            or (fault.get("contract_ok"), fault.get("http_status"), fault.get("held_connections"))
            != (False, 503, case.connections)
            or not successful_probes(healthy) or not successful_probes(restored)):
        raise ValueError(f"pool hold, actual initial error or recovery probes are invalid for {arm}")
    if ((case.application == "inventory" and fault.get("order_dependency") not in (500, 502, 503, 504))
            or (case.application == "order" and (fault.get("order_lookup"), fault.get("inventory_lookup")) != (200, 200))):
        raise ValueError(f"independent observations do not match the workload pool symptoms for {arm}")
    if (not fault.get("endpoint_uid") or any(row.get("endpoint_uid") != fault["endpoint_uid"]
                                            or row.get("order_dependency") != 200 for row in healthy + restored)):
        raise ValueError(f"pool recovery did not keep the same Pod and dependency for {arm}")
    start, expiry = operation_window(case, record["operation"])
    first = datetime.fromisoformat(fault["first_error_at"])
    observed = datetime.fromisoformat(fault["observed_at"])
    recovered = datetime.fromisoformat(record["recovered_at"])
    terminal, = record.get("termination_events") or []
    ended = datetime.fromisoformat(terminal["timestamp"])
    actor = terminal["event"]
    if (not start <= first <= observed < expiry or not observed <= ended <= recovered
            or not ((actor == "reset" and ended < expiry) or (actor == "expired" and ended >= expiry))):
        raise ValueError(f"pool termination is inconsistent with its observed operation for {arm}")
    result = {**record, "scope": f"same ready dev Pod, {case.connections} exhausted pool slots and real stock and order requests",
              "first_error_to_operation_end_ms": round((ended - first).total_seconds() * 1000)}
    if arm == "liveprobe":
        if record.get("observation_seconds_after_recovery", 0) < 75 or actor != "expired" or record.get("standalone_action"):
            raise ValueError("pool product observation did not prove a bounded natural recovery")
        incidents = record.get("incidents")
        if incidents == []:
            sampled = any(sample["value"] == case.connections and start.timestamp() <= sample["timestamp"] < expiry.timestamp()
                          for sample in record.get("pool_active_samples") or [])
            return {**result, "status": "verified_natural_recovery" if sampled else "product_observation_only",
                    "reason_code": "pool_expired_without_incident" if sampled else "pool_saturation_metric_unobserved",
                    "wall_duration_ms": None, "calls": [], "estimated_usd": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 0}}
        incident, = incidents
        if ("TargetConnectionPoolExhausted" not in (incident.get("signal_reasons") or [])
                or incident.get("executed_actions") != 0):
            raise ValueError("the product had an unrelated incident or an unattributed pool action")
        signal_at = incident.get("first_pool_signal_at")
        if case.case_id != "F3-02" and (not signal_at or datetime.fromisoformat(signal_at) < start):
            raise ValueError("pool incident signal preceded the current fixture operation")
        calls = incident.get("calls") or []
        observed_terminal = record.get("product_terminal_observed_at")
        if not observed_terminal:
            return {**result, "status": "product_observation_only", "calls": calls,
                    "reason_code": "product_incident_still_active_at_observation_limit"}
        terminal_at = datetime.fromisoformat(observed_terminal)
        if terminal_at < first or incident.get("status") in ("open", "analyzing", "action_pending", "acting"):
            raise ValueError("the pool incident's reported adjudication is inconsistent")
        return {**result, "status": "verified_natural_recovery", "reason_code": "pool_expired_with_product_incident",
                "calls": calls, "wall_duration_ms": round((terminal_at - first).total_seconds() * 1000),
                "estimated_usd": complete_metric(calls, "estimated_usd"),
                "model_duration_ms": complete_metric(calls, "duration_ms"),
                "usage": {key: complete_metric(calls, key) for key in ("input_tokens", "output_tokens")}}
    completion = standalone_completion(record)
    if record.get("lease_released") is not True or record.get("incidents") != [] or not record.get("calls"):
        raise ValueError(f"pool standalone execution was not isolated for {arm}")
    if actor == "expired" and not record.get("standalone_action") and completion:
        return {**result, "status": "verified_natural_recovery", "reason_code": completion,
                "wall_duration_ms": record["model_evaluation_duration_ms"]}
    if (actor == "reset" and record.get("standalone_action") == {
            "accepted": True, "action": "scoped_operation_reset", "operation_id": record["operation"]["operation_id"]}
            and record.get("verified_before_cleanup") == restored):
        return {**result, "status": "verified_recovered", "verified_dev_action": True, "verified": restored,
                "wall_duration_ms": record["model_evaluation_duration_ms"]}
    raise ValueError(f"pool trial has no scoped completion or verified action for {arm}")


def dev_latency_record(record, arm):
    from evaluation.dev_endpoint_pulse_case import standalone_completion
    from evaluation.dev_latency_case import ALERT, DEADLINE_MS, PROTOCOL, initial_error, load_case, operation_window
    from evaluation.live_repair import TASK

    if record.get("status") != "latency_observed" or not record.get("fault_before"):
        raise ValueError("the latency attempt did not prove the active hold")
    case = load_case(record["case"])
    fault = record["fault_before"]
    uid = fault.get("endpoint_uid")

    def healthy(check):
        probes, timed = (check or {}).get("probes"), (check or {}).get("timed") or []
        return (successful_probes(probes) and all(row.get("endpoint_uid") == uid for row in probes)
                and len(timed) == 2 and all((row.get("http_status"), row.get("curl_exit"), row.get("deadline_ms"))
                == (200, 0, DEADLINE_MS) and 0 < row["elapsed_ms"] < DEADLINE_MS for row in timed))

    if ((case.case_id, arm, PROTOCOL, case.target, case.case_sha256, "latency_observed") !=
            tuple(record.get(key) for key in ("case", "arm", "protocol", "target", "case_sha256", "status"))
            or any(record.get(key) for key in ("trial_error", "cleanup_error", "model_failed"))
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or not uid or fault.get("contract_ok") is not False
            or not healthy(record.get("healthy_before")) or not healthy(record.get("restored"))):
        raise ValueError("latency recovery did not preserve the pinned healthy Pod and deadline")
    error = initial_error(case, fault["initial_sample"])
    slow = fault.get("delay_samples") or []
    if (error != fault.get("error") or record["input_sha256"] != hashlib.sha256((TASK + error).encode()).hexdigest()
            or len(slow) != 5 or any((row.get("http_status"), row.get("curl_exit"), row.get("deadline_ms")) != (200, 0, 3000)
                                    or not case.delay_ms <= row["elapsed_ms"] < 3000 for row in slow)):
        raise ValueError("latency receipt is missing real delayed requests or the matching initial failure")
    isolated = record.get("observation") or {}
    control = isolated.get("control") or {}
    if ((control.get("mode"), control.get("operation_id"), control.get("latency_delay_ms"), control.get("readiness"))
            != ("latency", record["operation"]["operation_id"], case.delay_ms, "ACCEPTING_TRAFFIC")
            or isolated.get("ready_endpoints") != [uid] or isolated.get("observed_at") != fault["observed_at"]):
        raise ValueError("the active latency observation did not confirm the same ready Pod")
    start, expiry = operation_window(case, record["operation"])
    first = datetime.fromisoformat(fault["first_error_at"])
    observed = datetime.fromisoformat(fault["observed_at"])
    recovered = datetime.fromisoformat(record["recovered_at"])
    terminal, = record.get("termination_events") or []
    ended = datetime.fromisoformat(terminal["timestamp"])
    actor = terminal["event"]
    if (not start <= first <= observed < expiry or not observed <= ended <= recovered
            or not ((actor == "reset" and ended < expiry) or (actor == "expired" and ended >= expiry))):
        raise ValueError("latency termination is inconsistent with the observed hold")
    result = {**record, "scope": "same ready dev Pod, real client timeout, measured delay and deadline probes",
              "first_error_to_operation_end_ms": round((ended - first).total_seconds() * 1000)}
    if arm == "liveprobe":
        if (record.get("observation_seconds_after_recovery", 0) < 75 or actor != "expired"
                or record.get("latency_rules_checked_after") is not True or not record.get("recorded_latency")):
            raise ValueError("latency product observation did not prove a bounded, monitored natural recovery")
        incidents = record["incidents"]
        if not incidents:
            above = any(float(row["seconds"]) > .5 for row in record["recorded_latency"])
            return {**result, "status": "verified_natural_recovery",
                    "reason_code": "latency_above_threshold_without_incident" if above else "latency_below_alert_threshold",
                    "calls": [], "usage": {"input_tokens": 0, "output_tokens": 0}, "estimated_usd": 0}
        incident, = incidents
        signal = incident.get("first_latency_signal_at")
        if (ALERT not in (incident.get("signal_reasons") or []) or incident.get("executed_actions") != 0
                or not signal or datetime.fromisoformat(signal) < start):
            raise ValueError("the latency incident is unrelated or has an unattributed action")
        calls = incident.get("calls") or []
        terminal_at = record.get("product_terminal_observed_at")
        if not terminal_at:
            return {**result, "status": "product_observation_only", "calls": calls,
                    "reason_code": "product_incident_still_active_at_observation_limit"}
        terminal_at = datetime.fromisoformat(terminal_at)
        if terminal_at < first or incident.get("status") in ("open", "analyzing", "action_pending", "acting"):
            raise ValueError("the latency product verdict is inconsistent")
        return {**result, "status": "verified_natural_recovery", "reason_code": "latency_expired_with_product_incident",
                "wall_duration_ms": round((terminal_at - first).total_seconds() * 1000), "calls": calls,
                "estimated_usd": complete_metric(calls, "estimated_usd"), "model_duration_ms": complete_metric(calls, "duration_ms"),
                "usage": {key: complete_metric(calls, key) for key in ("input_tokens", "output_tokens")}}
    if record.get("lease_released") is not True or record.get("incidents") != [] or not record.get("calls"):
        raise ValueError("the standalone latency attempt was not isolated")
    completed = standalone_completion(record)
    if actor == "expired" and not record.get("standalone_action") and completed:
        return {**result, "status": "verified_natural_recovery", "reason_code": completed,
                "wall_duration_ms": record["model_evaluation_duration_ms"]}
    checked = record.get("verified_before_cleanup")
    if (actor == "reset" and healthy(checked) and record.get("standalone_action") == {
            "accepted": True, "action": "scoped_operation_reset", "operation_id": record["operation"]["operation_id"]}):
        return {**result, "status": "verified_recovered", "verified_dev_action": True, "verified": checked["probes"],
                "wall_duration_ms": record["model_evaluation_duration_ms"]}
    raise ValueError("latency trial has no verified action or scoped termination")


def dev_crash_record(record, arm):
    from evaluation.dev_endpoint_pulse_case import standalone_completion
    from evaluation.dev_restart_case import PROTOCOL, RESTART_BACKOFF_RESET, SIGNALS, first_error, load_case, operation_window
    from evaluation.live_repair import TASK

    case = load_case(record["case"])
    if ((arm, PROTOCOL, case.target, case.case_sha256, "crash_observed")
            != tuple(record.get(k) for k in ("arm", "protocol", "target", "case_sha256", "status"))
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or any(record.get(k) for k in ("trial_error", "cleanup_error", "model_failed"))):
        raise ValueError("invalid crash receipt envelope")
    fault = record["fault_before"]
    uid = fault["endpoint_uid"]
    if (not uid or fault.get("contract_ok") is not False
            or (fault.get("ready_endpoints"), fault.get("http_status")) != (0, 503)
            or fault.get("service_error") not in case.errors):
        raise ValueError("invalid original Pod")

    def healthy(check):
        probes = (check or {}).get("probes")
        return (successful_probes(probes) and all(p["endpoint_uid"] == uid for p in probes)
                and bool(check["container_id"]) and type(check["restarts"]) is int)

    original, restored = record["healthy_before"], record["restored"]
    if (not healthy(original) or not healthy(restored)
            or original["container_id"] == restored["container_id"]
            or restored["restarts"] < original["restarts"] + 2):
        raise ValueError("actual in-place container restart and healthy probes are required")
    start, expiry = operation_window(case, record["operation"])
    if original["restarts"]:
        running_since = datetime.fromisoformat(original["running_since"])
        eligible_at = datetime.fromisoformat(original["restart_backoff_eligible_at"])
        if not running_since.tzinfo or eligible_at != running_since + RESTART_BACKOFF_RESET or start < eligible_at:
            raise ValueError("the original container still had restart backoff")
    primary = fault["first_termination"]
    initial = datetime.fromisoformat(primary["finished_at"])
    error = first_error(primary)
    if (primary["container_id"] != original["container_id"] or not start.replace(microsecond=0) <= initial <= expiry
            or (fault.get("error"), fault.get("first_error_at")) != (error, primary["finished_at"])
            or record["input_sha256"] != hashlib.sha256((TASK + error).encode()).hexdigest()):
        raise ValueError("the first crash does not bind to the running container")
    samples = record["observations"]
    terminations = [e for sample in samples for e in sample["terminations"]]
    if (primary not in terminations or len({e["container_id"] for e in terminations}) < 2
            or any(first_error(e) != error or not initial <= datetime.fromisoformat(e["finished_at"]) <= expiry for e in terminations)
            or not any(s["waiting"] == "CrashLoopBackOff" or s["restarts"] >= original["restarts"] + 2 for s in samples)):
        raise ValueError("the repeated crash is not proven by native observations")
    event, = record.get("termination_events") or []
    ended = datetime.fromisoformat(event["timestamp"])
    recovered = datetime.fromisoformat(record["recovered_at"])
    actor = event["event"]
    unavailable_at = datetime.fromisoformat(fault["unavailable_at"])
    if not (initial <= unavailable_at <= recovered and initial <= ended <= recovered
            and ((actor == "expired" and ended >= expiry) or (actor == "reset" and ended < expiry))):
        raise ValueError("the pinned operation has no attributable reset or expiry")
    incidents = record["incidents"]
    result = {**record, "healthy_before": {**original, "contract_ok": True},
              "first_error_to_operation_end_ms": round((ended - initial).total_seconds() * 1000),
              "scope": "native repeated exits on the same dev Pod and two healthy business probes"}
    if arm == "liveprobe":
        if actor != "expired" or record.get("observation_seconds", 0) < 75:
            raise ValueError("the product trial did not prove an observed natural recovery")
        if not incidents:
            return {**result, "status": "verified_natural_recovery", "reason_code": "crash_loop_without_product_incident",
                    "calls": [], "usage": {"input_tokens": 0, "output_tokens": 0}, "estimated_usd": 0}
        incident, = incidents
        signal = incident.get("first_restart_signal_at")
        if (not SIGNALS.intersection(incident["signal_reasons"] or []) or incident["executed_actions"] != 0
                or not signal or datetime.fromisoformat(signal) < initial):
            raise ValueError("the product incident cannot be attributed to the restart")
        calls = incident.get("calls") or []
        terminal = record.get("product_terminal_observed_at")
        if not terminal:
            return {**result, "status": "product_observation_only", "calls": calls,
                    "reason_code": "product_incident_still_active_at_observation_limit"}
        terminal_at = datetime.fromisoformat(terminal)
        if terminal_at < initial or incident["status"] in ("open", "analyzing", "action_pending", "acting"):
            raise ValueError("invalid product crash verdict time")
        return {**result, "status": "verified_natural_recovery", "reason_code": "crash_expired_with_product_incident",
                "wall_duration_ms": round((terminal_at - initial).total_seconds() * 1000), "calls": calls,
                "estimated_usd": complete_metric(calls, "estimated_usd"), "model_duration_ms": complete_metric(calls, "duration_ms"),
                "usage": {key: complete_metric(calls, key) for key in ("input_tokens", "output_tokens")}}
    if record.get("lease_released") is not True or incidents or not record.get("calls"):
        raise ValueError("the standalone crash attempt was not isolated")
    result["wall_duration_ms"] = record["model_evaluation_duration_ms"]
    completed = standalone_completion(record)
    if actor == "expired" and not record.get("standalone_action") and completed:
        return {**result, "status": "verified_natural_recovery", "reason_code": completed}
    checked = record.get("verified_before_cleanup")
    if actor == "reset" and healthy(checked) and record.get("standalone_action") == {
            "accepted": True, "action": "scoped_operation_reset", "operation_id": record["operation"]["operation_id"]}:
        return {**result, "status": "verified_recovered", "verified_dev_action": True, "verified": checked["probes"]}
    raise ValueError("the revised workload did not demonstrate an attributable recovery")


def dev_oom_record(record, arm):
    from evaluation.dev_endpoint_pulse_case import standalone_completion
    from evaluation.dev_oom_case import (
        ALERT, ALERT_FRESHNESS_SECONDS, LIMIT_MIB, MIB, PROTOCOL, RECOVERY_OBSERVER, expiry_abort, first_error, load_case,
        operation_window,
    )
    from evaluation.dev_restart_case import RESTART_BACKOFF_RESET
    from evaluation.live_repair import TASK

    case = load_case(record["case"])
    if ((arm, PROTOCOL, case.target, case.case_sha256, "oom_observed")
            != tuple(record.get(k) for k in ("arm", "protocol", "target", "case_sha256", "status"))
            or not re.fullmatch(r"[a-f0-9]{64}", record.get("fixture_sha256") or "")
            or record.get("kubernetes_lease_released") is not True
            or any(record.get(k) for k in ("trial_error", "cleanup_error", "model_failed"))):
        raise ValueError("invalid or unclosed OOM receipt")
    fault = record["fault_before"]
    uid = fault["endpoint_uid"]
    if (not uid or fault.get("contract_ok") is not False
            or (fault.get("ready_endpoints"), fault.get("http_status")) != (0, 503)
            or fault.get("service_error") not in case.errors):
        raise ValueError("native OOM business impact was not proven")

    def healthy(check):
        probes = (check or {}).get("probes")
        return (successful_probes(probes) and all(p["endpoint_uid"] == uid for p in probes)
                and bool(check["container_id"]) and type(check["restarts"]) is int)

    original, restored = record["healthy_before"], record["restored"]
    gates = record["gate_evidence"]
    cgroups = gates["cgroups"]
    if (not healthy(original) or not healthy(restored) or original["container_id"] == restored["container_id"]
            or restored["restarts"] <= original["restarts"] or cgroups[case.application]["limit_bytes"] != LIMIT_MIB * MIB
            or len(cgroups) != 2 or any(g["limit_bytes"] != 64 * MIB for name, g in cgroups.items() if name != case.application)):
        raise ValueError("actual isolated cgroup and in-place restart are required")
    start, expiry = operation_window(case, record["operation"])
    running = datetime.fromisoformat(gates["running_since"])
    eligible = datetime.fromisoformat(gates["restart_backoff_eligible_at"])
    if not running.tzinfo or not running <= eligible <= start or (original["restarts"] and eligible != running + RESTART_BACKOFF_RESET):
        raise ValueError("the original container still had restart backoff")
    primary = fault["first_termination"]
    initial = datetime.fromisoformat(primary["finished_at"])
    error = first_error(primary)
    if (primary["container_id"] != original["container_id"] or not start.replace(microsecond=0) <= initial <= expiry
            or (fault.get("error"), fault.get("first_error_at")) != (error, primary["finished_at"])
            or primary not in [event for row in record["observations"] for event in row["terminations"]]
            or record["input_sha256"] != hashlib.sha256((TASK + error).encode()).hexdigest()):
        raise ValueError("the first native OOM does not bind to the running container")
    event, = record.get("termination_events") or []
    ended = datetime.fromisoformat(event["timestamp"])
    recovered = datetime.fromisoformat(record["recovered_at"])
    actor = event["event"]
    elapsed_naturally = ((actor == "expired" and ended >= expiry)
                         or expiry_abort(case, record["operation"], initial, event, record.get("abort_readback")))
    if not (initial <= datetime.fromisoformat(fault["unavailable_at"]) <= recovered and initial <= ended <= recovered
            and (elapsed_naturally or (actor == "reset" and ended < expiry))):
        raise ValueError("the pinned OOM operation did not prove reset or expiry")
    result = {**record, "first_error_to_operation_end_ms": round((ended - initial).total_seconds() * 1000),
              "scope": "native kernel OOM on the same dev Pod and two healthy business probes"}
    # The legacy `recovered_at` is a cleanup checkpoint and must not fill this measurement.
    result.pop('first_error_to_verified_recovery_ms', None)
    if 'independent_recovery' in record:
        observation = record['independent_recovery']
        try:
            armed, probe_started, verified = (datetime.fromisoformat(observation[key])
                                              for key in ('armed_at', 'probe_started_at', 'verified_at'))
            model_started = datetime.fromisoformat(record['model_evaluation_started_at']) if arm != 'liveprobe' else None
            checks = observation['checks']
            if (observation['observer'] != RECOVERY_OBSERVER
                    or not datetime.fromisoformat(fault['unavailable_at']) <= armed <= probe_started <= verified <= recovered
                    or (model_started is not None and not armed <= model_started <= recovered)
                    or verified < ended or not healthy(checks)
                    or any(probe.get('order_dependency') != 200 for probe in checks['probes'])
                    or any(checks[key] != restored[key] for key in ('container_id', 'restarts'))):
                raise ValueError('independent OOM recovery proof changed')
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError('invalid independent OOM recovery evidence') from error
        result['first_error_to_verified_recovery_ms'] = round((verified - initial).total_seconds() * 1000)
    incidents = record["incidents"]
    if arm == "liveprobe":
        if not elapsed_naturally or record.get("observation_seconds", 0) < 75:
            raise ValueError("the product OOM trial has no independently verified observation window")
        samples = record["signal_samples"]
        if (set(samples) != {"kube_state_oom", "firing_oom_alert"}
                or any(not start.timestamp() <= s["sample_timestamp"] <= s["evaluated_at"] for s in samples.values())
                or not incidents):
            return {**result, "status": "product_observation_only", "reason_code": "incomplete_oom_signal_chain"}
        incident, = incidents
        signal = incident.get("first_oom_signal_at")
        if (ALERT not in (incident["signal_reasons"] or []) or incident["executed_actions"] != 0
                or not signal or datetime.fromisoformat(signal) < initial):
            raise ValueError("the product incident cannot be attributed to this OOM")
        calls = incident.get("calls") or []
        terminal = record.get("product_terminal_observed_at")
        if not terminal:
            return {**result, "status": "product_observation_only", "calls": calls, "reason_code": "oom_incident_still_active"}
        terminal_at = datetime.fromisoformat(terminal)
        if terminal_at < initial or incident["status"] in ("open", "analyzing", "action_pending", "acting"):
            raise ValueError("invalid product OOM verdict time")
        return {**result, "status": "verified_natural_recovery", "reason_code": "oom_expired_with_product_incident",
                "wall_duration_ms": round((terminal_at - initial).total_seconds() * 1000), "calls": calls,
                "estimated_usd": complete_metric(calls, "estimated_usd"), "model_duration_ms": complete_metric(calls, "duration_ms"),
                "usage": {key: complete_metric(calls, key) for key in ("input_tokens", "output_tokens")}}
    settled = record.get("standalone_alert_settled_at")
    settled_at = datetime.fromisoformat(settled) if settled else None
    lease_closed = record.get("lease_released") is True
    lease_expiry = record.get("lease_expiry_verified")
    if not lease_closed and actor == "oom_not_killed" and settled_at and (lease_expiry or {}).get("inactive") is True:
        deadline = datetime.fromisoformat(lease_expiry["deadline"])
        observed = datetime.fromisoformat(lease_expiry["observed_at"])
        lease_closed = settled_at < deadline <= observed
    if (not lease_closed or incidents or not record.get("calls") or not settled_at
            or settled_at < expiry + timedelta(seconds=ALERT_FRESHNESS_SECONDS + 60)):
        raise ValueError("the standalone OOM attempt was not isolated")
    result["wall_duration_ms"] = record["model_evaluation_duration_ms"]
    completed = standalone_completion(record)
    if elapsed_naturally and not record.get("standalone_action") and completed:
        return {**result, "status": "verified_natural_recovery", "reason_code": completed}
    checked = record.get("verified_before_cleanup")
    if actor == "reset" and healthy(checked) and record.get("standalone_action") == {
            "accepted": True, "action": "scoped_operation_reset", "operation_id": record["operation"]["operation_id"]}:
        return {**result, "status": "verified_recovered", "verified_dev_action": True, "verified": checked["probes"]}
    raise ValueError("the revised OOM workload did not demonstrate an attributable recovery")


def complete_metric(calls, key):
    values = [call.get(key) for call in calls]
    return sum(values) if values and all(value is not None for value in values) else None


def citation_metrics(calls):
    analysis_calls = [call for call in calls if call.get("role") != "patch"]
    reviews = [call.get("citation_review") or {} for call in analysis_calls]
    observed = [review for review in reviews if review.get("scope") == "evidence_as_presented_to_model"
                and review.get("evidence_observed") is True
                and isinstance(review.get("citations"), list)
                and all(type(item) is bool for item in review["citations"])]
    if not observed:
        return {}
    marks = [grounded for review in observed for grounded in review["citations"]]
    return {"citation_reviewed_calls": len(observed),
            "citation_missing_calls": len(analysis_calls) - len(observed) + sum(not review["citations"] for review in observed),
            "citations_grounded": sum(marks), "citations_ungrounded": len(marks) - sum(marks)}


def product_detection_exclusion(row):
    """Explain why a case without a product incident lacks a comparable model run."""
    reason = row.get("reason_code")
    if row["status"] == "unrecovered" and reason in {
        "admission_request_not_detected", "retired_ingress_api_not_detected", "truncated_endpoint_inventory_not_detected",
        "metrics_supply_failure_not_detected", "cni_version_failure_not_detected",
        "node_disk_pressure_not_detected", "cri_startup_failure_not_detected",
        "cgroup_v1_startup_failure_not_detected", "kubelet_version_preflight_not_detected",
        "csi_migration_failure_not_detected",
        "istio_listener_nack_not_detected",
    }:
        return "persistent_fault_without_product_incident"
    if row["status"] != "verified_natural_recovery":
        return None
    if reason == "latency_below_alert_threshold":
        return "latency_below_comparison_threshold"
    if reason == "pulse_expired_without_incident" or row.get("protocol") == "dev-endpoint-pulse/v1":
        return "endpoint_pulse_ended_before_detection_window"
    if reason == "pool_saturation_metric_unobserved":
        return "source_signal_unobserved"
    if reason in {"crash_loop_without_product_incident", "latency_above_threshold_without_incident",
                  "pool_expired_without_incident"}:
        return "naturally_recovered_without_product_incident"
    return None


def product_nondetection(row):
    return product_detection_exclusion(row) is not None


def _paired_measurement(pairs, measure):
    values = [(a.get(measure), b.get(measure)) for a, b in pairs]
    valid = [(a, b) for a, b in values if all(type(v) in (int, float) and math.isfinite(v) and v >= 0
                                            for v in (a, b))]
    live, single = ((sum(a for a, _ in valid), sum(b for _, b in valid)) if valid else (None, None))
    return {"measured_pairs": len(valid), "liveprobe_sum": live, "standalone_sum": single,
            "sample_reduction_percent": (1 - live / single) * 100 if single else None}


def paired_repair_coverage(rows):
    terminal = {"verified_recovered", "unrecovered", "verified_natural_recovery"}
    model_measurements = ("wall_duration_ms", "model_duration_ms", "total_tokens", "estimated_usd", "high_model_calls")
    physical_measurements = ("first_error_to_operation_end_ms", "first_error_to_verified_recovery_ms")
    measurements = model_measurements + tuple(measure for measure in physical_measurements
                                               if any(row.get(measure) is not None for row in rows))
    liveprobe = {row["case"]: row for row in rows if row["arm"] == "liveprobe"}
    detection_exclusions = {}
    for case_id, row in sorted(liveprobe.items()):
        reason = product_detection_exclusion(row)
        if reason:
            detection_exclusions.setdefault(reason, []).append(case_id)
    excluded_cases = {case_id for cases in detection_exclusions.values() for case_id in cases}
    arms = sorted({row["arm"] for row in rows} - {"liveprobe"})
    reports = {}
    for arm in arms:
        both_run, skipped = [], Counter()
        for single in (row for row in rows if row["arm"] == arm):
            live = liveprobe[single["case"]]
            if live["status"] not in terminal or single["status"] not in terminal:
                skipped[f'{live["status"]} / {single["status"]}'] += 1
                continue
            both_run.append((live, single))
        report = {"planned_cases": len(liveprobe), "paired_cases": [a["case"] for a, _ in both_run],
                  "excluded_status_pairs": dict(sorted(skipped.items())),
                  "paired_outcomes": dict(Counter(f'{a["status"]} / {b["status"]}' for a, b in both_run)),
                  "product_nondetection_cases": sorted({a["case"] for a, _ in both_run} & excluded_cases)}
        for measure in measurements:
            pairs = [(a, b) for a, b in both_run
                     if measure in physical_measurements or a["case"] not in excluded_cases]
            comparison = _paired_measurement(pairs, measure)
            complete = bool(liveprobe) and comparison["measured_pairs"] == len(liveprobe)
            comparison["complete"] = complete
            if arm == "single_astra" and measure in ("estimated_usd", "high_model_calls"):
                reduction = comparison["sample_reduction_percent"]
                comparison.update(target_percent=30, verdict=("met" if reduction >= 30 else "not_met")
                                  if complete and reduction is not None else "unverified")
            report[measure] = comparison
        # Compare resource use conditional on both arms actually restoring the service.
        recovered = [(a, b) for a, b in both_run if a["status"] == b["status"] == "verified_recovered"]
        report["common_verified_recovery"] = {
            "cases": [a["case"] for a, _ in recovered],
            "cases_by_cohort": dict(Counter(a["cohort"] for a, _ in recovered)),
            "metrics": {measure: _paired_measurement(recovered, measure) for measure in model_measurements},
        }
        reports[arm] = report
    cohorts = {cohort: dict(Counter(row["status"] for row in rows if row["cohort"] == cohort))
               for cohort in COHORTS}
    reviewed = [row for row in liveprobe.values() if row.get("citation_reviewed_calls")]
    grounded = sum(row["citations_grounded"] for row in reviewed)
    ungrounded = sum(row["citations_ungrounded"] for row in reviewed)
    missing = sum(row["citation_missing_calls"] for row in reviewed)
    check = {"scope": "evidence_as_presented_to_model", "reviewed_cases": len(reviewed),
             "planned_cases": len(liveprobe), "reviewed_calls": sum(row["citation_reviewed_calls"] for row in reviewed),
             "missing_calls_in_reviewed_cases": missing, "grounded": grounded, "ungrounded": ungrounded,
             "sample_grounding_percent": 100 * grounded / (grounded + ungrounded) if grounded + ungrounded else None}
    return {"case_count": len(liveprobe), "by_cohort": cohorts, "paired_standalone_comparisons": reports,
            "liveprobe_nondetection_cases": sorted(excluded_cases),
            "liveprobe_detection_exclusions": detection_exclusions,
            "liveprobe_call_citation_review": check}


def recovery_action_matches(data):
    release_protocols = ("live-repair/v5", "live-repair/v12", "live-repair/v16", "live-repair/v17", "live-repair/v18",
                         *ORDER_PROTOCOLS, *MEMBER_RELEASE_PROTOCOLS.values())
    if data.get("arm") == "liveprobe" and data["protocol"] in release_protocols:
        return release_runner_recovery_matches(data)
    source_repair = data["protocol"] in ("live-repair/v4", *release_protocols)
    tool = "patch" if source_repair else "configure"
    count = "successful_source_writes" if source_repair else "successful_config_writes"
    accepted = [step["result"] for step in data.get("investigation", [])
                if step.get("request", {}).get("tool") == tool and step.get("result", {}).get("accepted") is True]
    if not accepted or data.get(count) != len(accepted):
        return False
    if not source_repair:
        if data.get("case") in ORDER_RUNTIME:
            _, setting = ORDER_RUNTIME[data["case"]]
            verified = data.get("verified") or []
            return (observed_order_runtime_fault(data) and accepted == [
                {"accepted": True, "setting": setting, "still_active": []}
            ] and order_release_recovered(verified) and all(
                probe.get("same_instance") is True and probe.get("liveness") == 200
                and (setting != "latency" or order_deadline_met(probe)) for probe in verified))
        if data.get("case") == "payment-08":
            verified = data.get("verified") or []
            return (observed_payment_cpu_fault(data)
                    and accepted == [{"accepted": True, "setting": "cpu_spin", "still_active": []}]
                    and len(verified) == 2 and all(payment_cpu_probe(probe, saturated=False) for probe in verified))
        if data.get("case") == "member-03":
            verified = data.get("verified") or []
            return (observed_member_crash(data) and accepted == [
                {"accepted": True, "setting": "crash", "still_active": [], "restarted": True}
            ] and len(verified) == 2 and all(member_probe_healthy(probe) for probe in verified))
        if data.get("case") == "member-17":
            verified = data.get("verified") or []
            return (observed_member_tail_latency(data) and accepted == [
                {"accepted": True, "setting": "latency_spike", "still_active": []}
            ] and len(verified) == 2 and all(member_profile_probe_healthy(probe) for probe in verified))
        if data.get("case") == "member-28":
            verified = data.get("verified") or []
            return (observed_member_tail_latency(data) and len(accepted) == 2
                    and {result.get("setting") for result in accepted} == {"cpu_spin", "latency_spike"}
                    and accepted[0].get("still_active") == [accepted[1]["setting"]]
                    and accepted[1].get("still_active") == []
                    and len(verified) == 2 and all(member_profile_probe_healthy(probe)
                    and member_cpu_sample(probe, saturated=False) for probe in verified))
        if data.get("case") == "delivery-09":
            verified = data.get("verified") or []
            return (observed_delivery_runtime_leak(data) and accepted == [
                {"accepted": True, "setting": "memory_leak", "still_active": [], "restarted": True}
            ] and len(verified) == 2 and all(delivery_runtime_memory_healthy(
                probe, data["healthy_before"]["memory"]) for probe in verified))
        if data.get("case") == "notification-20":
            verified = data.get("verified") or []
            return (observed_notification_memory_delay(data) and len(accepted) == 2
                    and {result.get("setting") for result in accepted} == {"memory_leak", "latency"}
                    and accepted[0].get("still_active") == [accepted[1]["setting"]]
                    and accepted[1].get("still_active") == []
                    and len(verified) == 2 and all(notification_memory_probe_healthy(probe) for probe in verified))
        if data["protocol"] in ("live-repair/v6", "live-repair/v11"):
            return accepted[-1].get("still_active") == []
        if data["protocol"] == "live-repair/v14":
            verified = data.get("verified") or []
            return (observed_startup_and_readiness_faults(data)
                    and {item.get("setting") for item in accepted} == {"NOTIFICATION_SENDER_NAME", "release.not_ready"}
                    and accepted[-1].get("sender_configured") is True
                    and len(verified) == 2 and all(notification_release_healthy(observation) for observation in verified))
        if data["protocol"] == "live-repair/v15":
            verified = data.get("verified") or []
            return (accepted[-1].get("effective_ratio") == 0 and len(verified) == 2
                    and all(delivery_probe_healthy(observation) for observation in verified)
                    and verified[0]["exception_count"] == verified[1]["exception_count"])
        if data["protocol"] == "live-repair/v19":
            return (accepted[-1].get("still_active") == [] and delivery_invariant_recovered(data))
        compounds = {"live-repair/v7": {"error_rate", "latency_spike"},
                     "live-repair/v10": {"intermittent_exception", "worker_exhaustion"},
                     "live-repair/v13": {"dependency_failure", "latency"}}
        if data["protocol"] in compounds:
            return (accepted[-1].get("active_impact") == []
                    and {item.get("setting") for item in accepted} == compounds[data["protocol"]])
        return True
    original, final = data.get("original_source_sha256"), data.get("final_source_sha256")
    if not all(isinstance(digest, str) and len(digest) == 64 for digest in (original, final)) or original == final:
        return False
    before, after = data.get("business_regression_before") or {}, data.get("business_regression_after")
    if before != after or before.get("exit_code") != 0 or before.get("tests", 0) <= 0:
        return False
    if data["protocol"] in release_protocols[1:]:
        inputs = [step.get("from_sha256") for step in accepted]
        outputs = [step.get("to_sha256") for step in accepted]
        verified = data.get("verified") or []
        if (inputs != [original, *outputs[:-1]] or outputs[-1] != final
                or any(not isinstance(digest, str) or len(digest) != 64 for digest in outputs)
                or any(previous == following for previous, following in zip(inputs, outputs, strict=True))):
            return False
        assignments = {step.get("assignment") for step in accepted}
        if data["protocol"] in ORDER_PROTOCOLS:
            dual = data["protocol"] in ORDER_DUAL_PROTOCOLS
            expected = {"calculateOrderAmount.divisor"}
            if dual:
                expected.add("normalizeOrderItem.found")
            return (observed_order_release_fault(data) and len(accepted) == len(expected)
                    and assignments == expected and order_release_recovered(verified, dual=dual))
        if data["protocol"] in MEMBER_RELEASE_PROTOCOLS.values():
            dual = data["case"] == "member-23"
            expected = {"serializeProfile.profileCreatedAt"}
            if dual:
                expected.add("normalizeMemberName.submittedName")
            return (observed_member_release_fault(data) and len(accepted) == len(expected)
                    and assignments == expected and member_release_recovered(verified, dual=dual))
        if data["protocol"] == "live-repair/v16":
            return (observed_catalog_release_faults(data) and assignments == {"background_job.release_key", "price_response.display"}
                    and catalog_release_recovered(verified))
        if data["protocol"] == "live-repair/v17":
            return (observed_delivery_release_faults(data)
                    and assignments == {"parseArrivalDate.effectiveDate", "createBusiness.releaseStatus"}
                    and delivery_release_recovered(verified))
        if data["protocol"] == "live-repair/v18":
            return (observed_delivery_memory_faults(data)
                    and assignments == {"rememberResponse.keepResponse", "transition.releaseEventIndex"}
                    and delivery_memory_recovered(verified))
        return (observed_dual_payment_code_fault(data) and assignments == {"calculate_fee.fee", "receipt.amount_text"}
                and len(verified) == 2 and all(dual_payment_recovered(probe) for probe in verified))
    action = accepted[-1]
    runtime_mode = data["protocol"] == "live-repair/v4"
    return (action.get("from_sha256") == original and action.get("to_sha256") == final
            and (runtime_mode or data.get("runtime_faults_before") == [])
            and data.get("refund_journey") == {"code_defect_mode_active": runtime_mode, "cancel_status": 200,
                                               "cancelled_refund_status": 200, "cancelled_amount": 0,
                                               "cancelled_fee": 0, "missing_payment_status": 404})


def observed_timeout(observation):
    elapsed, deadline = observation.get("duration_ms"), observation.get("deadline_ms")
    return (type(elapsed) is int and type(deadline) is int and deadline > 0 and elapsed >= deadline * 0.9
            and observation.get("transport") == "request_timeout" and observation.get("ready") == 200
            and observation.get("business") is None)


def within_original_deadline(probe, initial):
    elapsed = probe.get("duration_ms")
    return (type(elapsed) is int and 0 <= elapsed < initial["deadline_ms"]
            and probe.get("deadline_ms") == initial["deadline_ms"] and probe.get("transport") is None)


def observed_mixed_notification_fault(probe):
    rows = probe.get("samples") or []
    if probe.get("ready") != 200 or len(rows) != 2 or any(type(row.get("duration_ms")) is not int for row in rows):
        return False
    first, second = rows
    return (first.get("status") == 503 and 0 <= first["duration_ms"] < 1000
            and second.get("status") == 200 and second["duration_ms"] >= 2800
            and second.get("notification_intact") is True)


def notification_probe_recovered(probe):
    rows = probe.get("samples") or []
    return (len(rows) == 2 and all(row.get("status") == 200 and row.get("notification_intact") is True
                                   and type(row.get("duration_ms")) is int and 0 <= row["duration_ms"] < 1000
                                   for row in rows))


def notification_mixed_symptoms_persist(probes):
    if not 2 <= len(probes) <= 4 or any(p.get("ready") != 200 or len(p.get("samples") or []) != 2 for p in probes):
        return False
    rows = [row for probe in probes for row in probe["samples"]]
    if any(row.get("status") not in (200, 503) or type(row.get("duration_ms")) is not int
           or row["duration_ms"] < 0 or (row["status"] == 200 and row.get("notification_intact") is not True)
           for row in rows):
        return False
    return (any(row["status"] == 503 and row["duration_ms"] < 1000 for row in rows)
            and any(row["status"] == 200 and row["duration_ms"] >= 2800 for row in rows))


def observed_dependency_and_delay(probe):
    return (probe.get("ready") == probe.get("business") == 200 and probe.get("enqueue_status") == 502
            and probe.get("exception") == "DependencyError" and probe.get("failed_request_left_no_item") is True
            and probe.get("upstream_received") is False and probe.get("seed_preserved") is True
            and all(type(probe.get(key)) is int and probe[key] >= 1800 for key in ("enqueue_ms", "lookup_ms")))


def dependency_probe_recovered(probe):
    return (probe.get("ready") == probe.get("business") == 200 and probe.get("enqueue_status") == 202
            and probe.get("exception") is None and probe.get("queued_once") is True
            and probe.get("upstream_received") is True and probe.get("seed_preserved") is True
            and all(type(probe.get(key)) is int and 0 <= probe[key] < 1000 for key in ("enqueue_ms", "lookup_ms")))


def observed_startup_and_readiness_faults(data):
    failed, masked = data.get("fault_before") or {}, data.get("masked_readiness") or {}
    return (data.get("protocol") == "live-repair/v14" and failed.get("transport") == "service_not_listening"
            and failed.get("ready") is None and failed.get("business") is None
            and data.get("startup_diagnostic") == {"exit_code": 1, "error": "KeyError", "frame": "load_settings",
                                                  "missing_config_key": "NOTIFICATION_SENDER_NAME"}
            and (masked.get("liveness"), masked.get("ready"), masked.get("business")) == (200, 503, 200)
            and masked.get("seed_preserved") is True
            and masked.get("release_active") == ["not_ready", "startup_config_error"])


def notification_release_healthy(probe):
    return (probe.get("contract_ok") is True and probe.get("seed_preserved") is True
            and probe.get("ready") == probe.get("business") == probe.get("liveness") == 200
            and probe.get("release_active") == ["startup_config_error"])


def observed_intermittent_delivery(probe, *, previous_exceptions=None):
    counter = probe.get("exception_count")
    return (probe.get("ready") == 200 and probe.get("business") == 500
            and type(counter) is int and (counter == 1 if previous_exceptions is None else counter > previous_exceptions)
            and probe.get("samples") == [{"status": 500, "shipment_intact": False},
                                         {"status": 200, "shipment_intact": True}])


def delivery_probe_healthy(probe):
    counter = probe.get("exception_count")
    return (probe.get("ready") == probe.get("business") == 200 and probe.get("contract_ok") is True
            and type(counter) is int and counter >= 1
            and probe.get("samples") == [{"status": 200, "shipment_intact": True}] * 2)


def observed_payment_pool_and_exception(data):
    pool = data.get("fault_before") or {}
    diagnostic = data.get("masked_fault_observation") or {}
    return ((pool.get("ready"), pool.get("business"), pool.get("exception")) == (200, 503, "WorkerPoolExhausted")
            and (diagnostic.get("ready"), diagnostic.get("business"), diagnostic.get("exception"))
            == (200, 500, "TransientStateError"))


def runtime_handoff_proof(data):
    case = data.get("case")
    expected_settings = RUNTIME_FIXTURE_SETTINGS.get(case)
    if not expected_settings:
        return False
    intake = data.get("runtime_intake") or {}
    reports = intake.get("reports") or []
    log = data.get("initial_error_log")
    fault, healthy = data.get("fault_before") or {}, data.get("healthy_before") or {}
    settings = data.get("runtime_configuration_before_cleanup") or {}
    reason = data.get("reason_code")
    followup = data.get("followup_collection") or {}
    follows_process = case in ORDER_RUNTIME or case in ("catalog-18", "delivery-09", "notification-20", "notification-30")
    followup_field = "process" if follows_process else "worker_slots"
    if case == "delivery-07":
        followup_field = "worker_pool"
    if case.startswith("member-") or case == "delivery-15":
        followup_field = "observed_health"
    resets = data.get("fixture_resets") or []
    end_probes = data.get("independent_probes") or []
    cleanup = data.get("fixture_cleanup") or []
    calls = data.get("calls") or []
    last_review = (calls[-1].get("citation_review") or {}) if calls else {}
    if not runtime_handoff_observations(data, fault, healthy, end_probes, cleanup):
        return False
    changed_keys = tuple(expected_settings)
    expected_resets = [(True, key, sorted(changed_keys[index + 1:])) for index, key in enumerate(changed_keys)]
    did_reset = [(row.get("accepted"), row.get("setting"), row.get("active_impact")) for row in resets] == expected_resets
    if case in ("catalog-18", "notification-30"):
        did_reset = resets == [{"accepted": True, "settings": sorted(changed_keys), "active_impact": []}]
    return (data.get("application_state") == "handoff"
            and reason in {"high_risk_handoff", "application_fix_not_supported",
                           "analysis_unresolved_or_low_confidence", "additional_observation_exhausted",
                           "citation_not_grounded"}
            and (reason != "citation_not_grounded" or (
                last_review.get("scope") == "evidence_as_presented_to_model"
                and last_review.get("evidence_observed") is True
                and any(value is False for value in (last_review.get("citations") or []))))
            and (reason != "additional_observation_exhausted" or (
                followup.get("report_state") in {"analysis_queued", "handoff"}
                and followup_field in (followup.get("observed_fields") or [])
                and re.fullmatch(r"[a-f0-9]{64}", followup.get("result_sha256") or "")))
            and intake.get("intake_state") == "collecting" and isinstance(log, str) and bool(log)
            and intake.get("initial_error_sha256") == hashlib.sha256(log.encode()).hexdigest()
            and len(reports) == 2 and [(r.get("kind"), r.get("state")) for r in reports]
            == [("collect", "source_pinning"), ("source", "analysis_queued")]
            and all(re.fullmatch(r"[a-f0-9]{64}", r.get("result_sha256") or "") for r in reports)
            and data.get("successful_config_writes") == 0 and bool(calls)
            and fault.get("contract_ok") is False
            and (healthy.get("ready"), healthy.get("business"), healthy.get("contract_ok")) == (200, 200, True)
            and did_reset
            and settings == expected_settings)


def runtime_handoff_observations(data, fault, healthy, end_probes, cleanup):
    case = data["case"]
    if len(cleanup) != 2 or any(row.get("contract_ok") is not True for row in cleanup):
        return False
    if case == "notification-19":
        return (observed_mixed_notification_fault(fault) and notification_mixed_symptoms_persist(end_probes)
                and all(notification_probe_recovered(row) for row in cleanup))
    if len(end_probes) != 2:
        return False
    if case in ("delivery-09", "notification-20"):
        stopped = {"contract_ok": False, "connection_refused": True, "process": "stopped_by_fixture_memory_containment"}
        elapsed = data.get("fixture_cleanup_elapsed_ms")
        if end_probes != [stopped, stopped] or type(elapsed) is not int or elapsed < 1250:
            return False
        if case == "delivery-09":
            return (observed_delivery_runtime_leak(data)
                    and all(delivery_runtime_memory_healthy(row, healthy.get("memory")) for row in cleanup))
        return observed_notification_memory_delay(data) and all(notification_memory_probe_healthy(row) for row in cleanup)
    if case == "catalog-18":
        diagnostic = data.get("startup_diagnostic") or {}
        return (diagnostic.get("exit_code") == 1 and diagnostic.get("error") == "KeyError"
                and diagnostic.get("missing_config_key") == "CATALOG_INDEX_LANGUAGE"
                and diagnostic.get("frame") in ("load_settings", "__getitem__")
                and fault.get("transport") == "service_not_listening"
                and fault.get("ready") is fault.get("business") is None and end_probes == [fault, fault]
                and all(row == healthy for row in cleanup))
    if case == "notification-30":
        return (observed_startup_and_readiness_faults(data) and end_probes == [fault, fault]
                and all(row == healthy and row.get("release_active") == [] and row.get("seed_preserved") is True
                        for row in cleanup))
    if case == "member-03":
        elapsed = data.get("fixture_cleanup_elapsed_ms")
        return (observed_member_crash(data) and all(row == fault for row in end_probes)
                and all(member_probe_healthy(row) for row in cleanup)
                and type(elapsed) is int and elapsed >= 3250
                and bool(data.get("fixture_resets")) and data["fixture_resets"][0].get("restarted") is True)
    if case in ("member-17", "member-28"):
        coupled_cpu = case == "member-28"
        return (all(observed_member_tail_latency({**data, "fault_before": row}) for row in (fault, *end_probes))
                and all(member_profile_probe_healthy(row) for row in cleanup)
                and (not coupled_cpu or all(member_cpu_sample(row, saturated=False) for row in cleanup)))
    if case in ORDER_RUNTIME:
        _, setting = ORDER_RUNTIME[case]
        return (all(observed_order_runtime_fault({**data, "fault_before": row}) for row in (fault, *end_probes))
                and order_release_recovered(cleanup)
                and all(row.get("same_instance") is True and row.get("liveness") == 200
                        and (setting != "latency" or order_deadline_met(row)) for row in cleanup))
    if case == "catalog-14":
        if not all(observed_background_failure(row) for row in (fault, *end_probes)):
            return False
        first_job, second_job = (row["job"] for row in end_probes)
        return (fault["job"]["sequence"] < first_job["sequence"] < second_job["sequence"]
                and background_jobs_recovered(cleanup, end_probes[-1]))
    if case == "delivery-07":
        if not observed_intermittent_delivery(fault):
            return False
        previous = fault["exception_count"]
        for probe in end_probes:
            if not observed_intermittent_delivery(probe, previous_exceptions=previous):
                return False
            previous = probe["exception_count"]
        return (all(delivery_probe_healthy(probe) for probe in cleanup)
                and cleanup[0]["exception_count"] == cleanup[1]["exception_count"] >= previous)
    if case == "delivery-15":
        if not delivery_invariant_recovered({**data, "verified": cleanup}):
            return False
        first, second = end_probes
        if not all(delivery_invariant_shipments_intact(probe) and probe.get("contract_ok") is False
                   and type(probe.get("errors_before")) is int
                   and probe.get("errors_after") == probe["errors_before"] + 2 for probe in end_probes):
            return False
        return (fault["errors_after"] <= first["errors_before"] < first["errors_after"] <= second["errors_before"]
                < second["errors_after"] <= cleanup[0]["errors_before"])
    if case == "payment-08":
        return (observed_payment_cpu_fault(data) and all(payment_cpu_probe(row, saturated=True) for row in end_probes)
                and all(payment_cpu_probe(row, saturated=False) for row in cleanup))
    if case == "notification-26":
        return (observed_dependency_and_delay(fault) and all(observed_dependency_and_delay(row) for row in end_probes)
                and all(dependency_probe_recovered(row) for row in cleanup))
    if case == "catalog-06":
        return (observed_timeout(fault) and all(observed_timeout(row) and row["deadline_ms"] == fault["deadline_ms"]
                                                 for row in end_probes)
                and all(within_original_deadline(row, fault) for row in cleanup))
    if end_probes != [fault, fault] or cleanup != [healthy, healthy]:
        return False
    if case == "payment-27":
        return observed_payment_pool_and_exception(data) and healthy.get("ledger_intact") is True
    if case == "catalog-05":
        return (data.get("watchdog_restarts_before_analysis") == 1 and healthy.get("liveness") == 200
                and (fault.get("ready"), fault.get("business"), fault.get("liveness")) == (200, 200, 503))
    if case == "notification-10" and (data.get("runtime_initial_detail") or {}).get("exception") != "WorkerPoolExhausted":
        return False
    observed = data.get("initial_log_observation") or {}
    return ((fault.get("ready"), fault.get("business")) == (200, 503)
            and (observed.get("event"), observed.get("method"), observed.get("status")) == ("http_access", "GET", 503))


def dual_payment_failed(observation):
    return (observation.get("ready") == 200 and observation.get("fee_status") == observation.get("business") == 500
            and observation.get("fee_exception") == "ZeroDivisionError"
            and observation.get("receipt_exception") == "ValueError"
            and observation.get("failed_payment_left_ledger_intact") is True
            and observation.get("contract_ok") is False)


def observed_dual_payment_code_fault(data):
    frames = data.get("original_exception_frames") or []
    return (data.get("protocol") == "live-repair/v12" and data.get("runtime_faults_before") == []
            and dual_payment_failed(data.get("fault_before") or {})
            and len(frames) == 2 and all(frame.get("path") == "app.py" and type(frame.get("line")) is int
                                         and frame["line"] > 0 and frame.get("message") for frame in frames)
            and [(frame.get("function"), frame.get("exception")) for frame in frames] ==
                [("calculate_fee", "ZeroDivisionError"), ("receipt", "ValueError")])


def dual_payment_recovered(observation):
    return (observation.get("ready") == observation.get("business") == 200 and observation.get("fee_status") == 201
            and observation.get("fee_exception") is None and observation.get("receipt_exception") is None
            and observation.get("payment_fee") == 975 and observation.get("charged_amount") == 32500
            and observation.get("receipt_amount_text") == "32,000원"
            and observation.get("ledger_intact") is True and observation.get("contract_ok") is True)


def observed_background_failure(observation):
    job = observation.get("job") or {}
    return ((observation.get("ready"), observation.get("business"), job.get("status"), job.get("exception"))
            == (200, 200, "failed", "IndexRebuildError") and type(job.get("sequence")) is int)


def background_jobs_recovered(probes, initial):
    if not observed_background_failure(initial) or len(probes) != 2:
        return False
    failed_sequence = initial["job"]["sequence"]
    jobs = [probe.get("job") or {} for probe in probes]
    if not all(job.get("status") == "passed" and job.get("index_size") == 6
               and job.get("index_contract_ok") is True and type(job.get("sequence")) is int
               and type(job.get("observed_at_monotonic")) in (int, float) for job in jobs):
        return False
    return (failed_sequence < jobs[0]["sequence"] < jobs[1]["sequence"]
            and jobs[1]["observed_at_monotonic"] - jobs[0]["observed_at_monotonic"] >= 14)


def catalog_release_failed(probe):
    job = probe.get("job") or {}
    return (probe.get("ready") == 200 and probe.get("business") == 500 and probe.get("exception") == "ValueError"
            and probe.get("contract_ok") is False and probe.get("inventory_intact") is True
            and job.get("status") == "failed" and job.get("exception") == "IndexRebuildError"
            and type(job.get("sequence")) is int)


def observed_catalog_release_faults(data):
    probe = data.get("fault_before") or {}
    frames = data.get("original_exception_frames") or []
    return (data.get("protocol") == "live-repair/v16" and data.get("runtime_faults_before") == []
            and catalog_release_failed(probe) and len(frames) == 2
            and [(frame.get("function"), frame.get("exception")) for frame in frames]
                == [("price_response", "ValueError"), ("background_job", "IndexRebuildError")]
            and all(frame.get("path") == "app.py" and type(frame.get("line")) is int and frame["line"] > 0
                    and frame.get("message") for frame in frames)
            and probe["job"].get("frame") == frames[1])


def catalog_release_recovered(probes):
    if len(probes) != 2:
        return False
    for probe, price in zip(probes, (79000, 81050), strict=True):
        job = probe.get("job") or {}
        if (probe.get("ready") != 200 or probe.get("business") != 200 or probe.get("exception") is not None
                or probe.get("requested_price") != price or probe.get("price_text") != f"{price:,}원"
                or probe.get("inventory_intact") is not True or probe.get("contract_ok") is not True
                or job.get("status") != "passed" or job.get("index_size") != 6 or job.get("index_contract_ok") is not True
                or type(job.get("sequence")) is not int or type(job.get("observed_at_monotonic")) not in (int, float)):
            return False
    first, second = (probe["job"] for probe in probes)
    return (second["sequence"] > first["sequence"] and second["observed_at_monotonic"] - first["observed_at_monotonic"] >= 14)


def delivery_release_failed(probe):
    return (probe.get("ready") == 200 and probe.get("business") == probe.get("submit_status") == 500
            and probe.get("stored_status") == 404 and probe.get("stored_exactly") is False
            and probe.get("seed_status") == "접수" and probe.get("seed_event") == "출고"
            and probe.get("neighbor_intact") is True and probe.get("contract_ok") is False)


def delivery_invariant_shipments_intact(probe):
    expected = [("LP1001", "출고"), ("LP1002", "배송 중")]
    rows = probe.get("samples") or []
    return (probe.get("ready") == probe.get("business") == 200 and len(rows) == len(expected)
            and all((row.get("status"), row.get("tracking"), row.get("stage"), row.get("stages"))
                    == (200, tracking, stage, [stage]) and row.get("shipment_intact") is True
                    for row, (tracking, stage) in zip(rows, expected, strict=True)))


def observed_delivery_invariant_failure(data):
    probe, frame = data.get("fault_before") or {}, data.get("original_exception_frame") or {}
    return (data.get("protocol") == "live-repair/v19" and delivery_invariant_shipments_intact(probe)
            and probe.get("contract_ok") is False and (probe.get("errors_before"), probe.get("errors_after")) == (0, 2)
            and (frame.get("path"), frame.get("function"), frame.get("exception"))
                == ("business.js", "checkIntegrity", "StatusRegressionError")
            and type(frame.get("line")) is int and frame["line"] > 0 and bool(frame.get("message")))


def delivery_invariant_recovered(data):
    probes = data.get("verified") or []
    if not observed_delivery_invariant_failure(data) or len(probes) != 2:
        return False
    counter = probes[0].get("errors_before")
    return (type(counter) is int and counter >= data["fault_before"]["errors_after"]
            and all(delivery_invariant_shipments_intact(probe) and probe.get("contract_ok") is True
                    and probe.get("errors_before") == probe.get("errors_after") == counter for probe in probes))


def observed_delivery_release_faults(data):
    frames = data.get("original_exception_frames") or []
    return (data.get("protocol") == "live-repair/v17" and data.get("runtime_faults_before") == []
            and delivery_release_failed(data.get("fault_before") or {}) and len(frames) == 2
            and [(frame.get("function"), frame.get("exception")) for frame in frames]
                == [("parseArrivalDate", "RangeError"), ("Object.handle", "StatusRegressionError")]
            and all(frame.get("path") == "business.js" and type(frame.get("line")) is int and frame["line"] > 0
                    and frame.get("message") for frame in frames))


def delivery_release_recovered(probes):
    if len(probes) != 2:
        return False
    dates = ("2032-02-29T12:34:56.000Z", "2032-02-29T13:14:15.000Z")
    for probe, expected in zip(probes, dates, strict=True):
        if (probe.get("ready") != 200 or probe.get("business") != 200 or probe.get("submit_status") != 201
                or probe.get("stored_status") != 200 or probe.get("stored_exactly") is not True
                or probe.get("requested_due_at") != expected or probe.get("neighbor_intact") is not True
                or probe.get("seed_status") != "출고" or probe.get("seed_event") != "출고"
                or probe.get("contract_ok") is not True or not probe.get("tracking")):
            return False
    return probes[0]["tracking"] != probes[1]["tracking"]


def order_release_failed(probe, *, dual=False):
    failed = (probe.get("ready") == 200 and probe.get("business") == probe.get("unavailable_status")
            == probe.get("missing_status") == 500 and probe.get("error") == "ArithmeticException"
            and probe.get("stock_intact") is False and probe.get("out_of_stock_intact") is False
            and probe.get("contract_ok") is False)
    return failed and (not dual or (
        probe.get("create_status") == 500 and probe.get("create_error") == "NullPointerException"
        and probe.get("order_id") is None and probe.get("stored_status") is None
        and probe.get("seed_intact") is True and probe.get("invalid_item_status") == 400
    ))


def order_release_recovered(probes, *, dual=False):
    recovered = len(probes) == 2 and all(
        probe.get("ready") == probe.get("business") == probe.get("unavailable_status") == 200
        and probe.get("missing_status") == 404 and probe.get("error") is None
        and probe.get("stock_intact") is True and probe.get("out_of_stock_intact") is True
        and probe.get("contract_ok") is True for probe in probes)
    return recovered and (not dual or (
        all(probe.get("create_status") == 201 and probe.get("create_error") is None
            and probe.get("stored_status") == 200 and probe.get("stored_intact") is True
            and probe.get("seed_intact") is True and probe.get("invalid_item_status") == 400
            and type(probe.get("order_id")) is int and probe["order_id"] > 1 for probe in probes)
        and probes[0]["order_id"] != probes[1]["order_id"]
    ))


def order_deadline_met(probe):
    elapsed = probe.get("duration_ms")
    return (probe.get("deadline_ms") == 1000 and probe.get("deadline_timeout") is False
            and type(elapsed) is int and 0 <= elapsed < 1000)


def observed_order_runtime_fault(data):
    protocol, setting = ORDER_RUNTIME[data["case"]]
    healthy = data.get("healthy_before") or {}
    failed = data.get("fault_before") or {}
    first = data.get("first_observation")
    if (data.get("protocol") != protocol or healthy.get("contract_ok") is not True
            or failed.get("contract_ok") is not False or any(
                probe.get("same_instance") is not True or probe.get("liveness") != 200
                for probe in (healthy, failed))):
        return False
    if setting == "latency":
        elapsed = failed.get("duration_ms")
        return (order_deadline_met(healthy) and failed.get("ready") == 200 and failed.get("business") is None
                and failed.get("deadline_timeout") is True and failed.get("deadline_ms") == 1000
                and type(elapsed) is int and elapsed >= 900
                and first == {"event": "client_timeout", "exception": "TimeoutError", "deadline_ms": 1000})
    if setting == "not_ready":
        return (failed.get("ready") == 503 and failed.get("business") == failed.get("unavailable_status") == 200
                and failed.get("missing_status") == 404 and failed.get("stock_intact") is True
                and failed.get("out_of_stock_intact") is True and first == {
                    "event": "http_access", "method": "GET", "uri": "/actuator/health/readiness", "status": 503})
    return (failed.get("ready") == 200
            and failed.get("business") == failed.get("unavailable_status") == failed.get("missing_status") == 502
            and failed.get("error") == "DependencyError" and failed.get("stock_intact") is False
            and failed.get("out_of_stock_intact") is False and first == {
                "event": "app_exception", "exception": "DependencyError", "cause": "Connection refused", "status": 502})


def payment_cpu_probe(probe, *, saturated):
    elapsed, cpu, fraction = (probe.get(key) for key in ("sample_seconds", "cpu_seconds", "cpu_core_fraction"))
    return (probe.get("ready") == probe.get("business") == probe.get("liveness") == 200
            and probe.get("same_instance") is True and probe.get("business_intact") is True
            and probe.get("contract_ok") is (not saturated)
            and all(type(sample) in (int, float) and math.isfinite(sample) for sample in (elapsed, cpu, fraction))
            and elapsed >= 2 and cpu >= 0 and abs(cpu / elapsed - fraction) <= 0.0001
            and (fraction >= 0.65 if saturated else 0 <= fraction < 0.20))


def observed_payment_cpu_fault(data):
    return (data.get("protocol") == "live-repair/v27"
            and data.get("first_observation") == {
                "level": "ERROR", "service": "test-application", "event": "process_cpu_high",
                "sample_seconds": 2, "threshold_percent_of_core": 65}
            and payment_cpu_probe(data.get("healthy_before") or {}, saturated=False)
            and payment_cpu_probe(data.get("fault_before") or {}, saturated=True))


def member_probe_healthy(probe):
    return (all(probe.get(name) == 200 for name in ("ready", "business", "liveness", "login", "profile"))
            and probe.get("unknown_profile") == 404 and probe.get("data_intact") is True
            and probe.get("contract_ok") is True)


def observed_member_crash(data):
    failed = data.get("fault_before") or {}
    return (data.get("protocol") == "live-repair/v28"
            and data.get("process_exits") == [{"exit_code": 1, "runtime_exception_observed": True}] * 2
            and member_probe_healthy(data.get("healthy_before") or {})
            and failed.get("process_exit") == 1 and failed.get("connection_refused") is True
            and failed.get("business") is None and failed.get("ready") is None and failed.get("contract_ok") is False)


def member_profile_within_deadline(sample):
    duration = sample.get("duration_ms")
    return (type(duration) is int and 0 <= duration < 1000 and sample.get("http_status") == 200
            and sample.get("profile_intact") is True)


def member_profile_probe_healthy(probe):
    samples = probe.get("profile_samples") or []
    return (member_probe_healthy(probe) and probe.get("deadline_ms") == 1000 and probe.get("same_instance") is True
            and len(samples) == 2 and all(member_profile_within_deadline(sample) for sample in samples))


def member_cpu_sample(data, *, saturated):
    elapsed, cpu, fraction = (data.get(key) for key in ("sample_seconds", "cpu_seconds", "cpu_core_fraction"))
    return (data.get("cpu_workers") == (2 if saturated else 0)
            and all(type(sample) in (int, float) and math.isfinite(sample) for sample in (elapsed, cpu, fraction))
            and elapsed >= 2 and cpu >= 0 and abs(cpu / elapsed - fraction) <= 0.0001
            and (fraction >= 1.1 if saturated else 0 <= fraction < 0.20))


def observed_member_tail_latency(data):
    coupled_cpu = data.get("case") == "member-28"
    healthy = data.get("healthy_before") or {}
    failed = data.get("fault_before") or {}
    samples = failed.get("profile_samples") or []
    if len(samples) != 2:
        return False
    first, second = samples
    duration = first.get("duration_ms")
    return (data.get("protocol") == ("live-repair/v34" if coupled_cpu else "live-repair/v29")
            and member_profile_probe_healthy(healthy)
            and (not coupled_cpu or (member_cpu_sample(healthy, saturated=False) and member_cpu_sample(failed, saturated=True)))
            and failed.get("ready") == failed.get("liveness") == 200 and failed.get("same_instance") is True
            and failed.get("deadline_ms") == 1000 and failed.get("contract_ok") is False
            and first.get("http_status") is None and first.get("profile_intact") is False
            and type(duration) is int and duration >= 900 and member_profile_within_deadline(second))


def delivery_runtime_memory_healthy(probe, baseline=None):
    memory = probe.get("memory") or {}
    rss, buffers = memory.get("rss_bytes"), memory.get("array_buffer_bytes")
    return (probe.get("ready") == probe.get("business") == probe.get("liveness") == 200
            and probe.get("missing_status") == 404 and probe.get("shipment_intact") is True
            and probe.get("contract_ok") is True and memory.get("retained_bytes") == 0
            and type(rss) is int and rss > 0 and type(buffers) is int and buffers >= 0 and
            (baseline is None or (rss <= baseline["rss_bytes"] + 16 * 1024 * 1024
                                  and buffers <= baseline["array_buffer_bytes"] + 2 * 1024 * 1024)))


def observed_delivery_runtime_leak(data):
    failed = data.get("fault_before") or {}
    growth = failed.get("growth") or []
    mib = 1024 * 1024
    if (data.get("protocol") != "live-repair/v30"
            or not delivery_runtime_memory_healthy(data.get("healthy_before") or {})
            or failed.get("stop_reason") != "fixture_memory_containment"
            or failed.get("connection_refused_after_stop") is not True or failed.get("contract_ok") is not False
            or not 3 <= len(growth) <= 5 or any(type(row.get(key)) is not int or row[key] < 0
                                              for row in growth for key in ("retained_bytes", "rss_bytes", "array_buffer_bytes"))):
        return False
    return (growth[0]["retained_bytes"] == 0 and growth[0]["rss_bytes"] > 0
            and growth[-1]["array_buffer_bytes"] - growth[0]["array_buffer_bytes"] >= 12 * mib
            and all(next_row["retained_bytes"] - row["retained_bytes"] >= 8 * mib
                    for row, next_row in zip(growth, growth[1:])))


def notification_memory_probe_healthy(probe):
    elapsed = probe.get("duration_ms")
    return (probe.get("ready") == probe.get("business") == probe.get("liveness") == 200
            and probe.get("notifications_intact") is True and probe.get("allocated_mib") == 0
            and type(elapsed) is int and 0 <= elapsed < 500 and probe.get("contract_ok") is True)


def observed_notification_memory_delay(data):
    failed = data.get("fault_before") or {}
    growth = failed.get("allocated_mib_samples") or []
    elapsed = failed.get("duration_ms")
    return (data.get("protocol") == "live-repair/v31"
            and notification_memory_probe_healthy(data.get("healthy_before") or {})
            and failed.get("ready") == failed.get("business") == failed.get("liveness") == 200
            and failed.get("notifications_intact") is True and failed.get("contract_ok") is False
            and failed.get("stop_reason") == "fixture_memory_containment"
            and type(elapsed) is int and elapsed >= 850 and len(growth) == 3
            and all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in growth)
            and growth[0] == 0 and all(following - previous >= 4 for previous, following in zip(growth, growth[1:])))


def member_release_fixture_contract(probe):
    return (probe.get("ready") == probe.get("liveness") == 200 and probe.get("seed_intact") is True
            and probe.get("created") == 201 and probe.get("missing_status") == 404
            and isinstance(probe.get("profile_id"), str) and probe["profile_id"].isdigit())


def member_release_failed(probe, *, dual):
    return (member_release_fixture_contract(probe) and probe.get("business") == 500
            and probe.get("profile_intact") is False and probe.get("contract_ok") is False
            and (not dual or (probe.get("signup_status") == 500 and probe.get("rejected_signup_unstored") is True)))


def member_release_recovered(probes, *, dual):
    return (len(probes) == 2 and all(
        member_release_fixture_contract(probe) and probe.get("business") == 200
        and probe.get("profile_intact") is True and probe.get("contract_ok") is True
        and (not dual or (probe.get("signup_status") == 201 and probe.get("signup_intact") is True))
        for probe in probes) and probes[0]["profile_id"] != probes[1]["profile_id"])


def observed_member_release_fault(data):
    case = data.get("case")
    dual = case == "member-23"
    frames = data.get("original_exception_frames") or []
    expected = {("serializeProfile", "RangeError")}
    if dual:
        expected.add(("normalizeMemberName", "TypeError"))
    return (case in MEMBER_RELEASE_PROTOCOLS and data.get("protocol") == MEMBER_RELEASE_PROTOCOLS[case]
            and data.get("runtime_faults_before") == [] and len(frames) == len(expected)
            and {(frame.get("function"), frame.get("exception")) for frame in frames} == expected
            and all(frame.get("path") == "business.js" and type(frame.get("line")) is int
                    and frame["line"] > 0 for frame in frames)
            and member_release_failed(data.get("fault_before") or {}, dual=dual))


def observed_order_release_fault(data):
    frames = data.get("original_exception_frames") or []
    protocol = data.get("protocol")
    dual = protocol in ORDER_DUAL_PROTOCOLS
    expected = [("calculateOrderAmount", "ArithmeticException")]
    if dual:
        expected.append(("normalizeOrderItem", "NullPointerException"))
    if (protocol not in ORDER_PROTOCOLS or data.get("runtime_faults_before") != []
            or not order_release_failed(data.get("fault_before") or {}, dual=dual)
            or [(frame.get("function"), frame.get("exception")) for frame in frames] != expected
            or frames[0].get("message") != "/ by zero"):
        return False
    return all(frame.get("path") == "src/main/java/io/liveprobe/target/order/AppFaultBusiness.java"
               and type(frame.get("line")) is int and frame["line"] > 0 and bool(frame.get("message")) for frame in frames)


def delivery_memory_measurements(probe):
    before, after = probe.get("memory_before") or {}, probe.get("memory_after") or {}
    names = ("retained_bytes", "array_buffer_bytes", "rss_bytes")
    if all(type(snapshot.get(name)) is int and snapshot[name] >= 0
           for snapshot in (before, after) for name in names) and before["rss_bytes"] > 0 and after["rss_bytes"] > 0:
        return before, after
    return None


def delivery_memory_failed(probe):
    measurements = delivery_memory_measurements(probe)
    if not measurements:
        return False
    before, after = measurements
    return (probe.get("ready") == probe.get("dispatched") == probe.get("saved_status") == 200
            and probe.get("created") == 201 and probe.get("business") == 500
            and bool(probe.get("tracking")) and probe["tracking"] == probe.get("saved_tracking")
            and probe.get("saved_stage") == "출고" and probe.get("events") == ["접수", "출고"]
            and probe.get("seed_intact") is True and probe.get("contract_ok") is False
            and after["retained_bytes"] - before["retained_bytes"] == 12 * 1024 * 1024
            and after["array_buffer_bytes"] - before["array_buffer_bytes"] >= 8 * 1024 * 1024)


def observed_delivery_memory_faults(data):
    frames = data.get("original_exception_frames") or []
    if (data.get("protocol") != "live-repair/v18" or data.get("runtime_faults_before") != []
            or not delivery_memory_failed(data.get("fault_before") or {}) or len(frames) != 1):
        return False
    frame = frames[0]
    return ((frame.get("path"), frame.get("function"), frame.get("exception")) == ("business.js", "transition", "TypeError")
            and type(frame.get("line")) is int and frame["line"] > 0 and bool(frame.get("message")))


def delivery_memory_recovered(probes):
    if len(probes) != 2:
        return False
    for probe in probes:
        measurements = delivery_memory_measurements(probe)
        if not measurements:
            return False
        before, after = measurements
        if (probe.get("ready") != 200 or probe.get("business") != 200 or probe.get("dispatched") != 200
                or probe.get("saved_status") != 200 or probe.get("created") != 201
                or not probe.get("tracking") or probe["tracking"] != probe.get("saved_tracking")
                or probe.get("saved_stage") != "배송 중" or probe.get("events") != ["접수", "출고", "배송 중"]
                or probe.get("transition_note") not in (None, "단계 변경") or probe.get("seed_intact") is not True
                or probe.get("contract_ok") is not True
                or before["retained_bytes"] != 0 or after["retained_bytes"] != 0
                or after["array_buffer_bytes"] - before["array_buffer_bytes"] >= 8 * 1024 * 1024
                or after["rss_bytes"] - before["rss_bytes"] >= 8 * 1024 * 1024):
            return False
    return probes[0]["tracking"] != probes[1]["tracking"]


def kubernetes_admission_request_proof(data):
    """Require the same real rejection and independent recovery or persistence evidence."""
    from uuid import UUID

    from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
    from evaluation.ki02_admission_fixture import API_SERVER_VERSION, API_VERSION, CASE, OLD_VERSION, PROTOCOL
    from evaluation.k8s_admission_request_trial import EXACT_FIRST_ERROR, fixture_digest
    from evaluation.live_repair import TASK
    from evaluation.single_baseline import MODELS, estimate

    def stored_request(row, review_versions):
        return (row["stored"] is True and str(UUID(row["uid"])) == row["uid"]
                and row["webhook_requests"] == review_versions)

    try:
        initial = data["initial_error_log"]
        initial_config = data["initial_registration"]
        scope = initial_config["scope_sha256"]
        observed_fault = {"stored": False, "sent_review_version": OLD_VERSION, "message": initial}
        if (data["case"] != CASE or data["protocol"] != PROTOCOL or data["api_version"] != API_SERVER_VERSION
                or data["status"] not in ("verified_recovered", "unrecovered") or data["model_failed"] is not None
                or data["cleanup_verified"] is not True or data["injected_request"] != observed_fault
                or data["fault_before"] != observed_fault or initial_config["versions"] != ["v1beta1"]
                or not initial_config["resource_version"] or not re.fullmatch(r"[0-9a-f]{64}", scope)
                or hashlib.sha256(initial.encode()).hexdigest() != EXACT_FIRST_ERROR
                or hashlib.sha256((TASK + initial).encode()).hexdigest() != data["input_sha256"]
                or fixture_digest(scope) != data["fixture_sha256"]):
            return False
        UUID(initial_config["uid"])
        baseline, control, final_control = (data[key] for key in (
            "baseline_request", "unmatched_request_during_fault", "unmatched_request"))
        if not (stored_request(baseline, [API_VERSION]) and stored_request(control, [])
                and stored_request(final_control, [])):
            return False
        if data["arm"] == "liveprobe":
            return kubernetes_admission_product_nondetection(data, initial_config, observed_fault)
        model, effort, _ = MODELS[data["arm"]]
        calls = data["calls"]
        if ((data["model"], data["effort"]) != (model, effort) or not calls
                or any((call["model"], call["effort"], call["status"]) != (model, effort, "ok") for call in calls)):
            return False
        usage = {name: complete_sum(call["usage"].get(name) for call in calls) for name in TOKEN_FIELDS}
        model_ms = complete_sum(call["duration_ms"] for call in calls)
        if (data["usage"] != usage or None in usage.values() or model_ms != data["model_duration_ms"]
                or type(data["wall_duration_ms"]) is not int or not 0 < model_ms <= data["wall_duration_ms"]
                or data["estimated_usd"] != complete_sum(estimate(data["arm"], call["usage"]) for call in calls)
                or data["estimated_usd"] is None):
            return False
        last = initial_config
        for step in data["investigation"]:
            request, result = step["request"], step["result"]
            if request["tool"] != "configure" or result.get("applied") is not True:
                continue
            after = result["after"]
            versions = json.loads(request["value"])
            if (request["key"] != "admissionReviewVersions" or result["before"] != last
                    or after["uid"] != last["uid"] or after["scope_sha256"] != scope or not versions
                    or after["versions"] != versions or len(set(versions)) != len(versions)
                    or any(version not in ("v1", "v1beta1") for version in versions)
                    or (after["versions"] != last["versions"] and after["resource_version"] == last["resource_version"])):
                return False
            last = after
        checks = data["independent_probes"]
        if len(checks) != 2 or any(probe["registration"] != last for probe in checks):
            return False
        requests = [probe["request"] for probe in checks]
        if data["status"] == "unrecovered":
            return last["versions"][0] == "v1beta1" and requests == [observed_fault, observed_fault]
        return (last["versions"][0] == "v1" and last["resource_version"] != initial_config["resource_version"]
                and all(stored_request(row, [API_VERSION]) for row in requests)
                and len({row["uid"] for row in (baseline, control, final_control, *requests)}) == 5)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return False


def kubernetes_admission_product_nondetection(data, registration, fault):
    from uuid import UUID

    from evaluation.k8s_admission_request_trial import IMAGE_SHA
    from evaluation.ki02_admission_fixture import WEBHOOK

    if (data["status"] != "unrecovered" or data["reason_code"] != "admission_request_not_detected"
            or data["product_source_image"] != IMAGE_SHA or data["deployed_ready_image"] != IMAGE_SHA
            or data["calls"] != []
            or data["agent_runs_after"] != 0 or data["matched_incident_count_after"] != 0
            or data["wall_duration_ms"] is not None or not math.isfinite(data["observation_seconds"])
            or data["observation_seconds"] < 80):
        return False
    epochs = data["native_watch_epoch"]
    if set(epochs) != {"pod_watch", "event_watch", "endpoint_poller"} or not all(epochs.values()):
        return False
    before, after = data["native_watch_witnesses"]
    if ((before["phase"], after["phase"]) != ("before", "after")
            or UUID(before["uid"]) == UUID(after["uid"])):
        return False
    rules = data["deployed_prometheus_rules"]
    if (not re.fullmatch(r"[0-9a-f]{64}", rules["rules_sha256"]) or rules["rule_count"] <= 0
            or not rules["alert_names"] or rules["admission_rule_names"] != []):
        return False
    checks = data["independent_probes"]
    if checks != [{"registration": registration, "request": fault}] * 2:
        return False
    rejected = 0.0
    for sample in data["rejection_counter"]:
        labels, value = sample.rsplit(maxsplit=1)
        if labels.startswith("apiserver_admission_webhook_rejection_count{") and f'name="{WEBHOOK}"' in labels:
            rejected += float(value)
    return math.isfinite(rejected) and rejected >= 3


def kubernetes_preflight_proof(data):
    discovery = data.get("api_discovery") or {}
    baseline = data.get("healthy_before") or {}
    reason = data["fault_before"].get("failure")
    if (data.get("case"), data["protocol"]) == ("KI-01", "live-repair/v8"):
        versions = discovery.get("served_versions") or []
        expected_rejection = (reason == "gvk_not_served" and "networking.k8s.io/v1" in versions
                              and "networking.k8s.io/v1beta1" not in versions)
    elif (data.get("case"), data["protocol"]) == ("KW-04", "live-repair/v9"):
        expected_rejection = (reason == "service_account_missing"
                              and discovery.get("service_accounts") == {"lp-eval-kw04-missing": False, "default": True})
    elif (data.get("case"), data["protocol"]) == ("KI-02", "live-repair/v35"):
        from evaluation.k8s_compatibility_trial import CASES, manifest_for

        expected_rejection = (reason == "unsupported_admission_review_version"
                              and data["fault_before"].get("error_log") == CASES["KI-02"].log
                              and "admissionregistration.k8s.io/v1" in (discovery.get("served_versions") or [])
                              and discovery.get("fixture_webhook_review_versions") == ["v1"]
                              and baseline.get("observed") == manifest_for(CASES["KI-02"], corrected=True))
    else:
        return False
    if not expected_rejection or data.get("resource_absent_after") is not True or baseline.get("resource_absent") is not True:
        return False
    status = data["status"]
    if data["arm"] == "liveprobe":
        return (status == "product_observation_only" and not data.get("calls")
                and type(data.get("matched_incident_count_after")) is int
                and data["matched_incident_count_after"] >= 0 and data.get("observation_seconds", 0) > 0)
    if status == "preflight_not_verified":
        return True
    observations = data.get("manifest_probes") or []
    applied = any(step.get("request", {}).get("tool") == "configure" and step["result"].get("contract_ok") is True
                  for step in data.get("investigation", []))
    return (status == "verified_manifest_only" and applied and len(observations) == 2
            and all(result.get("accepted") is True and result.get("contract_ok") is True
                    and result.get("resource_absent") is True and result.get("observed") == baseline.get("observed")
                    for result in observations))


def release_runner_recovery_matches(data):
    before, after = data.get("baseline_probe") or {}, data.get("candidate_probe") or {}
    build, patch = data.get("test_build") or {}, data.get("patch") or {}
    tested, artifact = build.get("tests") or {}, build.get("artifact") or {}
    if (build.get("ok") is not True or not all(tested.get(key) is True for key in
            ("before_failed", "after_passed", "regression_executed")) or tested.get("failed") != 0
            or tested.get("total", 0) <= (data.get("business_regression_before") or {}).get("tests", 0)):
        return False
    if not patch.get("source_snapshot_id") or not patch.get("diff_sha256"):
        return False
    if (artifact.get("source_snapshot_id") != patch["source_snapshot_id"]
            or artifact.get("diff_sha256") != patch["diff_sha256"]
            or hashlib.sha256(patch.get("diff", "").encode()).hexdigest() != patch["diff_sha256"]):
        return False
    if "native_build_reports" in data:
        reports = data["native_build_reports"]
        if (not reports or reports[-1].get("reported_ok") is not True
                or reports[-1].get("diff_sha256") != patch["diff_sha256"]
                or reports[-1].get("state") != "dev_targets_collecting"
                or data.get("application_state") != "dev_targets_collecting"):
            return False
    results = {(test.get("id"), test.get("phase")): test.get("status")
               for test in tested.get("executed_tests", [])}
    if not patch.get("tests_added") or any(
            results.get((test, "before")) not in ("failed", "error") or results.get((test, "after")) != "passed"
            for test in patch["tests_added"]):
        return False
    manifests = (data.get("baseline_image"), artifact.get("image"))
    service = {**dict.fromkeys(ORDER_PROTOCOLS, "order"), **dict.fromkeys(MEMBER_RELEASE_PROTOCOLS.values(), "member"),
               "live-repair/v16": "catalog", "live-repair/v17": "delivery",
               "live-repair/v18": "delivery"}.get(data["protocol"], "payment")
    if (not all(isinstance(image, str) and image.startswith(f"liveprobe-eval/{service}@sha256:")
                for image in manifests) or manifests[0] == manifests[1]):
        return False
    if data["protocol"] in ORDER_PROTOCOLS:
        dual = data["protocol"] in ORDER_DUAL_PROTOCOLS
        initial_probes, after_probes = before.get("samples") or [], after.get("samples") or []
        runtime = before.get("java_runtime")
        return (observed_order_release_fault(data) and bool(data.get("native_build_reports"))
                and len(set(patch["tests_added"])) >= (2 if dual else 1)
                and len(initial_probes) == 2 and all(order_release_failed(probe, dual=dual) for probe in initial_probes)
                and isinstance(runtime, str) and runtime.startswith("21.") and after.get("java_runtime") == runtime
                and before.get("runtime_faults") == after.get("runtime_faults") == []
                and data.get("verified") == after_probes and order_release_recovered(after_probes, dual=dual))
    if data["protocol"] in MEMBER_RELEASE_PROTOCOLS.values():
        dual = data["case"] == "member-23"
        initial_probes, after_probes = before.get("samples") or [], after.get("samples") or []
        runtime = before.get("node_runtime")
        return (observed_member_release_fault(data) and bool(data.get("native_build_reports"))
                and len(set(patch["tests_added"])) >= (2 if dual else 1)
                and len(initial_probes) == 2 and all(member_release_failed(probe, dual=dual) for probe in initial_probes)
                and isinstance(runtime, str) and runtime.startswith("v22.") and after.get("node_runtime") == runtime
                and before.get("runtime_faults") == after.get("runtime_faults") == []
                and data.get("verified") == after_probes and member_release_recovered(after_probes, dual=dual)
                and after.get("invalid_name_status") == 400 and after.get("duplicate_name_status") == 409)
    if data["protocol"] == "live-repair/v16":
        initial_probes, after_probes = before.get("samples") or [], after.get("samples") or []
        return (observed_catalog_release_faults(data) and bool(data.get("native_build_reports"))
                and len(set(patch["tests_added"])) >= 2
                and len(initial_probes) == 2 and all(catalog_release_failed(check) for check in initial_probes)
                and before.get("runtime_faults") == after.get("runtime_faults") == []
                and data.get("verified") == after_probes and catalog_release_recovered(after_probes)
                and after.get("invalid_price_status") == 400 and after.get("missing_product_status") == 404)
    if data["protocol"] in ("live-repair/v17", "live-repair/v18"):
        initial_probes, after_probes = before.get("samples") or [], after.get("samples") or []
        runtime = before.get("node_runtime")
        if (not data.get("native_build_reports") or len(set(patch["tests_added"])) < 2 or len(initial_probes) != 2
                or before.get("runtime_faults") != [] or after.get("runtime_faults") != []
                or not isinstance(runtime, str) or not runtime.startswith("v22.") or runtime != after.get("node_runtime")
                or data.get("verified") != after_probes):
            return False
        if data["protocol"] == "live-repair/v18":
            return (observed_delivery_memory_faults(data) and all(delivery_memory_failed(probe) for probe in initial_probes)
                    and delivery_memory_recovered(after_probes) and after.get("backward_status") == 409
                    and after.get("missing_shipment_status") == 404)
        return (observed_delivery_release_faults(data) and all(delivery_release_failed(probe) for probe in initial_probes)
                and delivery_release_recovered(after_probes) and after.get("invalid_date_status") == 400
                and after.get("duplicate_status") == 409)
    initial_probes = before.get("paid") or []
    after_probes = after.get("paid") or []
    if data["protocol"] == "live-repair/v12":
        return (observed_dual_payment_code_fault(data) and bool(data.get("native_build_reports"))
                and len(set(patch["tests_added"])) >= 2
                and len(initial_probes) == 2 and all(dual_payment_failed(check) for check in initial_probes)
                and before.get("runtime_faults") == after.get("runtime_faults") == []
                and len(after_probes) == 2 and data.get("verified") == after_probes
                and all(dual_payment_recovered(row) for row in after_probes)
                and after.get("invalid_amount_status") == 400 and after.get("missing_receipt_status") == 404)
    return (len(initial_probes) == 2 and all(check.get("business") == 500 and check.get("ready") == 200
                                             for check in initial_probes)
            and before.get("runtime_faults") == after.get("runtime_faults") == []
            and len(after_probes) == 2 and data.get("verified") == after_probes
            and all(row.get("refund") == {"payment_id": "2", "refundable_amount": 32000, "fee_refund": 960}
                    for row in after_probes)
            and after.get("cancel_status") == 200
            and after.get("cancelled") == "cancelled" and after.get("cancelled_refund_status") == 200
            and after.get("cancelled_refund") == {"payment_id": "1", "refundable_amount": 0, "fee_refund": 0}
            and after.get("missing_status") == 404)


if __name__ == "__main__":
    main()
