"""Measure standalone model recovery from genuine Kubernetes image filesystem pressure."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.kw07_node_pressure_fixture import (
    CASE, FILLER, KUBERNETES, MAX_FILL, TAINT, Trial, ready, sha,
)
from evaluation.live_repair import JsonLineSession, TASK, host_broker_investigation, host_codex_provider
from evaluation.single_baseline import MODELS, estimate


PROTOCOL = "live-repair/v43"
VM = "kw07-iso-verify"
HOST_SOURCE = Path(__file__).with_name("kw07_node_pressure_fixture.py")
SHUTDOWN = {"stopped": True, "namespace_removed": True, "filler_removed": True}
PREAMBLE = (
    "Request one operation per turn using the JSON schema. workload reads the target and node, "
    "logs reads native Kubernetes warnings, source reads the disk and currently editable test resource, "
    "probe performs current checks, configure takes key and value for the supported change, and finish stops. "
    "Preserve the workload, node scheduling policy, and unrelated data.\n\n"
)


def guest_command(home):
    home = home.resolve()
    if home.parent != Path("/private/tmp") or not home.name.startswith("lp-kw07-lima."):
        raise ValueError("expected a disposable KW-07 VM")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    stage, target = f"/tmp/kw07-{HOST_SOURCE.name}", f"/tmp/lp-kw07-k3s/{HOST_SOURCE.name}"
    subprocess.run(["limactl", "copy", str(HOST_SOURCE), f"{VM}:{stage}"], check=True, timeout=25)
    subprocess.run([*shell, "sudo", "-n", "install", "-m", "0400", stage, target], check=True, timeout=12)
    actual = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", target], text=True, timeout=12).split()[0]
    if actual != sha256(HOST_SOURCE.read_bytes()).hexdigest():
        raise RuntimeError("loaded the wrong node pressure fixture")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'p=$(cat /tmp/lp-kw07-k3s/server.pid); exec nsenter -t "$p" -m -n -- '
            'env K3S_DATA_DIR=/tmp/lp-kw07-k3s/state python3 -B "$1"', "sh", target]


def run(broker, arm, provider):
    initial = broker.request({"tool": "prepare"})
    first = initial["initial_error_log"]
    model, effort, _ = MODELS[arm]
    calls, history, failure = host_broker_investigation(broker, provider, model, effort, first)
    proof = broker.request({"tool": "verify"})
    recovered = all(Trial.healthy(sample) for sample in proof["independent_probes"])
    return {**initial, **proof, "arm": arm, "protocol": PROTOCOL, "model": model, "effort": effort,
            "model_failed": failure,
            "status": "incomplete" if failure or not calls else "verified_recovered" if recovered else "unrecovered",
            "reason_code": "disk_pressure_recovered" if recovered else "imagefs_pressure_persists",
            "input_sha256": sha256((TASK + first).encode()).hexdigest(), "calls": calls, "investigation": history,
            "usage": {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "isolated image filesystem pressure, real eviction, taint and HTTP replacement readback"}


def native_proof(row):
    healthy, failure = row["healthy_before"], row["fault_before"]
    first, second = row["independent_probes"]
    event = row["initial_event"]
    assert row["fixture_sha256"] == sha([CASE, KUBERNETES, row["fixture_contract"]])
    assert event in failure["events"] and event["reason"] == "FailedScheduling" and "untolerated taint" in event["message"]
    assert failure["no_schedule_taints"] == [TAINT]
    assert row["initial_error_log"] == f"{event['reason']}: {event['message']}"
    assert row["input_sha256"] == sha256((TASK + row["initial_error_log"]).encode()).hexdigest()
    assert 16 * 1024**2 <= row["filler_bytes"] <= MAX_FILL
    assert Trial.healthy(healthy) and Trial.fault(failure)
    states = (healthy, failure, first, second)
    for key in ("node_uid", "deployment_uid"):
        assert len({state[key] for state in states}) == 1
    assert len({state["control"]["uid"] for state in states}) == 1
    assert all(ready(sample["control"]) for sample in states)
    assert all(sample["disk"]["capacity_bytes"] != sample["disk"]["nodefs_capacity"] for sample in states)
    assert len({sample["disk"]["capacity_bytes"] for sample in states}) == 1
    assert [state["observed_at"] for state in states] == sorted({state["observed_at"] for state in states})
    assert first["disk"]["fs_observed_at"] < second["disk"]["fs_observed_at"]
    original = row["original_pod_uid"]
    assert any(pod["uid"] == original and ready(pod) for pod in healthy["pods"])
    assert any(e["pod_uid"] == original and e["reason"] == "Evicted" for e in failure["events"])
    assert any(pod["uid"] == event["pod_uid"] and pod["phase"] == "Pending" for pod in failure["pods"])
    nonces = [sample["control"]["http"]["nonce"] for sample in states]
    assert len(set(nonces)) == len(nonces)
    if row["status"] == "unrecovered":
        return Trial.fault(first) and Trial.fault(second)
    assert row["status"] == "verified_recovered" and Trial.healthy(first) and Trial.healthy(second)
    a = next(p for p in first["pods"] if ready(p))
    b = next(p for p in second["pods"] if ready(p))
    return (a["uid"] == b["uid"] == event["pod_uid"] != original and a["pod_ip"] == b["pod_ip"]
            and a["http"]["nonce"] != b["http"]["nonce"] and not {a["http"]["nonce"], b["http"]["nonce"]}.intersection(nonces))


def product_proof(row):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA

    before, after = row["native_watch_witnesses"]
    fault_uids = {e["uid"] for e in row["fault_before"]["events"]}
    rules = row["deployed_prometheus_rules"]
    watchers = row["native_watch_epoch"]
    return (row["status"] == "unrecovered" and row["reason_code"] == "node_disk_pressure_not_detected"
            and row["product_source_image"] == row["deployed_ready_image"] == IMAGE_SHA
            and row["matched_incident_count_after"] == row["agent_runs_after"] == 0 and row["calls"] == []
            and row["wall_duration_ms"] is None and row["observation_seconds"] >= 55
            and set(watchers) == {"event_watch", "pod_watch", "endpoint_poller"} and all(watchers.values())
            and (before["phase"], after["phase"]) == ("before", "after") and UUID(before["uid"]) != UUID(after["uid"])
            and fault_uids <= set(row["observed_failure_event_uids"])
            and rules["rule_count"] > 0 and rules["disk_pressure_rules"] == []
            and re.fullmatch(r"[0-9a-f]{64}", rules["sha256"]) is not None)


def receipt_valid(row):
    try:
        assert row["case"] == CASE and row["protocol"] == PROTOCOL and row["kubernetes"] == KUBERNETES
        assert row["cleanup"] == SHUTDOWN and native_proof(row)
        if row["arm"] == "liveprobe":
            return product_proof(row)
        model, effort, _ = MODELS[row["arm"]]
        calls = row["calls"]
        assert row["model_failed"] is None and calls and (row["model"], row["effort"]) == (model, effort)
        assert all((c["model"], c["effort"], c["status"]) == (model, effort, "ok") for c in calls)
        usage = {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS}
        assert row["usage"] == usage and None not in usage.values()
        assert row["model_duration_ms"] == complete_sum(c["duration_ms"] for c in calls)
        assert 0 < row["model_duration_ms"] <= row["wall_duration_ms"]
        assert row["estimated_usd"] == complete_sum(estimate(row["arm"], c["usage"]) for c in calls)
        writes = [step for step in row["investigation"] if step["request"]["tool"] == "configure"
                  and step["result"].get("applied") is True]
        if row["status"] == "unrecovered":
            return not writes
        return len(writes) == 1 and writes[0]["result"] == {
            "applied": True, "freed_bytes": row["filler_bytes"], "file": FILLER.name}
    except (AssertionError, KeyError, IndexError, TypeError, ValueError, StopIteration):
        return False


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--lima-home", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--arm", choices=MODELS, action="append", required=True)
    args = parser.parse_args()
    if len(args.arm) != len(set(args.arm)) or any((args.output_dir / f"{arm}.json").exists() for arm in args.arm):
        raise ValueError("select unused arms for this output")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    command = guest_command(args.lima_home)
    provider = host_codex_provider(args.codex_bin, PREAMBLE)
    for arm in args.arm:
        print(json.dumps({"phase": "starting", "case": CASE, "arm": arm}), flush=True)
        log = args.output_dir / f"{arm}.stderr"
        with log.open("x") as err:
            broker = JsonLineSession(command, timeout=220, stderr=err)
            try:
                row = run(broker, arm, provider)
                row["cleanup"] = broker.request({"tool": "shutdown"})
            except Exception:
                print(*log.read_text().splitlines()[-7:], sep="\n", flush=True)
                raise
            finally:
                broker.close()
        valid = receipt_valid(row)
        name = f"{arm}.json" if valid else f"{arm}.incomplete"
        with (args.output_dir / name).open("x") as out:
            json.dump(row, out, indent=2)
            out.write("\n")
        print(json.dumps({**{k: row[k] for k in ("case", "arm", "status", "wall_duration_ms", "estimated_usd")},
                          "receipt_valid": valid}), flush=True)


if __name__ == "__main__":
    main()
