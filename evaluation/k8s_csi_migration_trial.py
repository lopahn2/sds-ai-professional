"""Compare recovery of a mounted legacy EBS volume through the Kubernetes CSI path."""
from argparse import ArgumentParser
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.ki11_csi_fixture import (
    BINARIES, BODY, CASE, DRIVER, KUBERNETES, LEGACY_ID, MISSING_DRIVER, NODE, PV, SHUTDOWN, WORKLOAD,
    blocked, deployment, digest, recovered, storage,
)
from evaluation.live_repair import JsonLineSession, TASK, host_broker_investigation, host_codex_provider
from evaluation.single_baseline import MODELS, estimate


PROTOCOL = "live-repair/v47"
VM = "ki11-iso-verify"
SOURCE = Path(__file__).with_name("ki11_csi_fixture.py")
ERROR = (f'MountVolume.MountDevice failed for volume "{PV}" : kubernetes.io/csi: '
         f'attacher.MountDevice failed to create newCsiDriverClient: {MISSING_DRIVER}')
PREAMBLE = (
    "Request one operation per turn using the JSON schema. workload reads the target and binding, "
    "logs reads the real volume error, source outlines the available node storage driver, "
    "probe checks current status, configure takes a setting key and a value, and finish stops. "
    "Preserve the Pod, its initial binding and all existing files. The node_csi_driver setting "
    "registers one installed driver.\n\n"
)


def guest_command(home):
    home = home.resolve()
    if home.parent != Path("/private/tmp") or not home.name.startswith("lp-ki11-lima."):
        raise ValueError("expected the disposable KI-11 VM")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    stage, target = "/tmp/ki11-stage/native.py", "/tmp/lp-ki11-k3s/native.py"
    subprocess.run(["limactl", "copy", str(SOURCE), f"{VM}:{stage}"], check=True, timeout=25)
    subprocess.run([*shell, "sudo", "-n", "install", "-m", "0400", stage, target], check=True, timeout=12)
    digest = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", target], text=True, timeout=12).split()[0]
    if digest != sha256(SOURCE.read_bytes()).hexdigest():
        raise RuntimeError("the active VM does not have the selected fixture")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'p=$(cat /tmp/lp-ki11-k3s/server.pid); exec nsenter -t "$p" -m -n -- '
            'env K3S_DATA_DIR=/tmp/lp-ki11-k3s/state python3 -B "$1"', "sh", target]


def run(broker, arm, provider):
    initial = broker.request({"tool": "prepare"})
    error = initial["initial_error_log"]
    model, effort, _ = MODELS[arm]
    calls, history, failure = host_broker_investigation(broker, provider, model, effort, error)
    proof = broker.request({"tool": "verify"})
    restored = all(recovered(probe) for probe in proof["independent_probes"])
    return {**initial, **proof, "arm": arm, "protocol": PROTOCOL, "model": model, "effort": effort,
            "model_failed": failure,
            "status": "incomplete" if failure or not calls else "verified_recovered" if restored else "unrecovered",
            "reason_code": "csi_registered_and_existing_volume_read" if restored else "csi_driver_still_unregistered",
            "input_sha256": sha256((TASK + error).encode()).hexdigest(), "calls": calls, "investigation": history,
            "usage": {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "native CSI migration, existing PV, real node plugin registration and volume-backed HTTP"}


def native_proof(row):
    state, (first, second) = row["fault_before"], row["independent_probes"]
    contract = row["fixture_contract"]
    assert row["initial_error_log"] == ERROR == state["mount_error"]["message"] and blocked(state)
    assert row["input_sha256"] == sha256((TASK + ERROR).encode()).hexdigest()
    assert row["fixture_sha256"] == digest([CASE, contract]) and contract["kubernetes"] == KUBERNETES
    assert contract["storage"] == list(storage()) and contract["deployment"] == deployment()
    expected_hash = sha256(BODY.encode()).hexdigest()
    assert contract["binary_sha256"] == BINARIES and contract["body_sha256"] == expected_hash
    for key in ("pod_uid", "pv_uid", "pvc_uid"):
        assert len({UUID(item[key]) for item in (state, first, second)}) == 1
    observed = [item["at"] for item in (state, first, second)]
    assert observed == sorted(set(observed))
    assert first["seed"] == second["seed"] == state["seed"] and state["seed"]["sha256"] == expected_hash
    if row["status"] == "unrecovered":
        return (blocked(first) and blocked(second) and first["mount_error"]["message"] == second["mount_error"]["message"] == ERROR
                and second["mount_error"]["count"] >= first["mount_error"]["count"] >= state["mount_error"]["count"])
    assert row["status"] == "verified_recovered"
    return (recovered(first) and recovered(second) and first["pod_ip"] == second["pod_ip"]
            and first["http"]["nonce"] != second["http"]["nonce"])


def product_proof(row):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA

    before, after = row["native_watch_witnesses"]
    watchers = row["native_watch_epoch"]
    assert row["status"] == "unrecovered" and row["cleanup"] == SHUTDOWN
    assert row["product_source_image"] == row["deployed_ready_image"] == IMAGE_SHA
    assert set(watchers) == {"event_watch", "pod_watch", "endpoint_poller"} and all(watchers.values())
    assert (before["phase"], after["phase"]) == ("before", "after") and UUID(before["uid"]) != UUID(after["uid"])
    assert row["mount_event_seen"] == row["fault_before"]["mount_error"]["uid"]
    assert len(row["workload_poll_witnesses"]) == 2 and row["workload_poll_witnesses"][0] < row["workload_poll_witnesses"][1]
    selection = row["workload_selection"]
    assert WORKLOAD not in selection["deployed_allowlist"] and selection["isolated_addition"] == WORKLOAD
    error = row["independent_probes"][-1]["mount_error"]
    duration = datetime.fromisoformat(error["last"]) - datetime.fromisoformat(error["first"])
    return (row["reason_code"] == "csi_migration_failure_not_detected" and row["pvc_access_denied"] is True
            and row["live_storage_read"] == {"pvc": False, "pv": False, "csinode": False}
            and row["matched_incident_count_after"] == 0 and not row["calls"] and row["wall_duration_ms"] is None
            and row["observation_seconds"] >= 120 and duration.total_seconds() >= 120 and error["count"] >= 2)


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
        changes = [step["result"] for step in row["investigation"] if step["request"]["tool"] == "configure"
                   and step["result"].get("applied") is True]
        if row["status"] == "unrecovered":
            return not changes
        return changes == [{"applied": True, "driver": DRIVER, "node": NODE, "volume_id": LEGACY_ID}]
    except (AssertionError, TypeError, ValueError, KeyError, IndexError):
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
        with log.open("x") as error:
            broker = JsonLineSession(command, timeout=200, stderr=error)
            try:
                row = run(broker, arm, provider)
                row["cleanup"] = broker.request({"tool": "shutdown"})
            except Exception:
                print(*log.read_text().splitlines()[-7:], sep="\n", flush=True)
                raise
            finally:
                broker.close()
        valid = receipt_valid(row)
        with (args.output_dir / f"{arm}{'.json' if valid else '.incomplete'}").open("x") as file:
            json.dump(row, file, indent=2)
            file.write("\n")
        print(json.dumps({**{key: row[key] for key in ("case", "arm", "status", "wall_duration_ms", "estimated_usd")},
                          "receipt_valid": valid}), flush=True)


if __name__ == "__main__":
    main()
