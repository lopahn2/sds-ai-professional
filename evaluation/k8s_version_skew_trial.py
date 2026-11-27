"""Compare recovery from a real kubeadm worker preflight version failure."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.ki03_skew_fixture import (
    BODY, CASE, CONTROL_PLANE, FAILURE, KUBEADM_SHA, NODE, OLD, READY, RELEASES, RUNTIME, SHUTDOWN,
    blocked, fingerprint, pod, recovered,
)
from evaluation.live_repair import JsonLineSession, TASK, host_broker_investigation, host_codex_provider
from evaluation.single_baseline import MODELS, estimate


PROTOCOL = "live-repair/v46"
VM = "ki03-iso-verify"
SOURCE = Path(__file__).with_name("ki03_skew_fixture.py")
JOIN_CONFIG_SHA = "d65e9395ce190dc19157654af8f4ba87aba49ce6ce13ebdd5ec7a9bbfb2b2007"
KUBELET_CONFIG_SHA = "b4b3c94e54d9b4f65b4b67937ed2a9e7c8ce1fb660982c1322fffd63333ca69e"
PREAMBLE = (
    "Request one operation per turn using the JSON schema. workload reads the target and the cluster, "
    "logs reads the actual node admission error, source reads locally available software and configuration, "
    "probe checks current status, configure takes a setting key and a value, and finish stops. "
    "The kubelet_version setting may select an installed version. Preserve the same node and workload.\n\n"
)


def guest_command(home):
    home = home.resolve()
    if home.parent != Path("/private/tmp") or not home.name.startswith("lp-ki03-lima."):
        raise ValueError("expected the disposable KI-03 VM")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    stage, target = "/tmp/ki03-stage/native.py", "/tmp/lp-ki03-k3s/native.py"
    subprocess.run(["limactl", "copy", str(SOURCE), f"{VM}:{stage}"], check=True, timeout=25)
    subprocess.run([*shell, "sudo", "-n", "install", "-m", "0400", stage, target], check=True, timeout=12)
    digest = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", target], text=True, timeout=12).split()[0]
    if digest != sha256(SOURCE.read_bytes()).hexdigest():
        raise RuntimeError("the active VM does not have the selected fixture")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'p=$(cat /tmp/lp-ki03-k3s/server.pid); exec nsenter -t "$p" -m -n -- '
            'env K3S_DATA_DIR=/tmp/lp-ki03-k3s/state python3 -B "$1"', "sh", target]


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
            "reason_code": "compatible_kubelet_registered" if restored else "kubelet_preflight_still_rejected",
            "input_sha256": sha256((TASK + error).encode()).hexdigest(), "calls": calls, "investigation": history,
            "usage": {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "native kubeadm join preflight, K3s control plane and a real kubelet-served HTTP workload"}


def refused(attempt):
    return {"version": OLD, "exit": 1, "errors": [FAILURE], "fetched_cluster_config": False, "attempt": attempt}


def accepted():
    return {"version": READY, "exit": 0, "errors": [], "fetched_cluster_config": True, "attempt": 2}


def native_proof(row):
    initial, (first, second), contract = row["fault_before"], row["independent_probes"], row["fixture_contract"]
    assert row["initial_error_log"] == FAILURE and initial["preflight"] == refused(1) and blocked(initial)
    assert row["input_sha256"] == sha256((TASK + FAILURE).encode()).hexdigest()
    assert row["fixture_sha256"] == fingerprint([CASE, contract])
    assert contract["control_plane"] == CONTROL_PLANE and contract["kubeadm_sha"] == KUBEADM_SHA
    assert contract["kubelet_sha"] == {version: detail[1] for version, detail in RELEASES.items()}
    assert contract["runtime"] == RUNTIME and contract["pod"] == pod() and contract["http_body"] == BODY
    assert contract["join_config_sha"] == JOIN_CONFIG_SHA and contract["kubelet_config_sha"] == KUBELET_CONFIG_SHA
    states = (initial, first, second)
    assert len({UUID(state["pod_uid"]) for state in states}) == 1
    assert all(state["runtime"] == RUNTIME for state in states)
    assert [state["at"] for state in states] == sorted({state["at"] for state in states})
    if row["status"] == "unrecovered":
        return blocked(first) and blocked(second) and first["preflight"] == refused(2) and second["preflight"] == refused(3)
    assert row["status"] == "verified_recovered"
    return (recovered(first) and recovered(second) and first["preflight"] == second["preflight"] == accepted()
            and first["node"]["name"] == second["node"]["name"] == NODE
            and UUID(first["node"]["uid"]) == UUID(second["node"]["uid"])
            and first["pod_ip"] == second["pod_ip"] and first["http"]["nonce"] != second["http"]["nonce"])


def product_proof(row):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA

    before, after = row["native_watch_witnesses"]
    rules, watchers = row["deployed_prometheus_rules"], row["native_watch_epoch"]
    return (row["status"] == "unrecovered" and row["reason_code"] == "kubelet_version_preflight_not_detected"
            and row["product_source_image"] == row["deployed_ready_image"] == IMAGE_SHA
            and row["matched_incident_count_after"] == row["agent_runs_after"] == 0 and row["calls"] == []
            and row["wall_duration_ms"] is None and row["observation_seconds"] >= 145
            and isinstance(row["workload_poll_witnesses"], list) and len(row["workload_poll_witnesses"]) == 2
            and row["workload_poll_witnesses"][0] < row["workload_poll_witnesses"][1]
            and set(watchers) == {"event_watch", "pod_watch", "endpoint_poller"} and all(watchers.values())
            and (before["phase"], after["phase"]) == ("before", "after") and UUID(before["uid"]) != UUID(after["uid"])
            and rules["rule_count"] > 0 and rules["version_skew_rules"] == []
            and re.fullmatch(r"[0-9a-f]{64}", rules["sha256"]) is not None)


def receipt_valid(row):
    try:
        assert row["case"] == CASE and row["protocol"] == PROTOCOL and row["kubernetes"] == CONTROL_PLANE
        assert row["cleanup"] == SHUTDOWN and native_proof(row)
        if row["arm"] == "liveprobe":
            return product_proof(row)
        model, effort, _ = MODELS[row["arm"]]
        calls = row["calls"]
        assert row["model_failed"] is None and calls and (row["model"], row["effort"]) == (model, effort)
        assert all((call["model"], call["effort"], call["status"]) == (model, effort, "ok") for call in calls)
        usage = {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS}
        assert row["usage"] == usage and None not in usage.values()
        assert row["model_duration_ms"] == complete_sum(call["duration_ms"] for call in calls)
        assert 0 < row["model_duration_ms"] <= row["wall_duration_ms"]
        assert row["estimated_usd"] == complete_sum(estimate(row["arm"], call["usage"]) for call in calls)
        changes = [step["result"] for step in row["investigation"] if step["request"]["tool"] == "configure"
                   and step["result"].get("applied") is True]
        if row["status"] == "unrecovered":
            return not changes
        return changes == [{"applied": True, "before": OLD, "after": READY, "node": NODE, "preflight": accepted()}]
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
            broker = JsonLineSession(command, timeout=145, stderr=error)
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
