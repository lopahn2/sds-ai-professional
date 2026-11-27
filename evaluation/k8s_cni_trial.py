"""Measure standalone model response to a real offline K3s CNI version mismatch."""
from argparse import ArgumentParser
from hashlib import sha256
from ipaddress import ip_address, ip_network
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.ki06_cni_fixture import (
    CASE, INCOMPATIBLE, K8S_VERSION, NODE, READY, TARGETS, Trial, fingerprint, is_dns_healthy, stable_error,
)
from evaluation.live_repair import JsonLineSession, TASK, host_broker_investigation, host_codex_provider
from evaluation.single_baseline import MODELS, estimate


PROTOCOL = "live-repair/v42"
VM = "ki06-iso-verify"
HOST_SOURCE = Path(__file__).with_name("ki06_cni_fixture.py")
SHUTDOWN = {"stopped": True, "resources_removed": True}
PREAMBLE = (
    "Request one operation per turn using the JSON schema. workload reads the node and Pods, logs reads "
    "native kubelet warnings, source reads the current network configuration and installed plugin capabilities, "
    "probe performs current checks, configure takes key and value for the supported setting, and finish stops. "
    "Keep the existing Pods and network configuration intact except for the incompatible setting.\n\n"
)


def guest_command(home):
    home = home.resolve()
    if home.parent != Path("/private/tmp") or not home.name.startswith("lp-ki06-lima."):
        raise ValueError("expected a disposable KI-06 VM")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    stage, target = "/tmp/ki06-eval-fixture.py", "/tmp/lp-ki06-k3s/fixture.py"
    subprocess.run(["limactl", "copy", str(HOST_SOURCE), f"{VM}:{stage}"], check=True, timeout=25)
    subprocess.run([*shell, "sudo", "-n", "install", "-m", "0400", stage, target], check=True, timeout=12)
    actual = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", target], text=True, timeout=12).split()[0]
    if actual != sha256(HOST_SOURCE.read_bytes()).hexdigest():
        raise RuntimeError("loaded the wrong CNI fixture code")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'p=$(cat /tmp/lp-ki06-k3s/server.pid); exec nsenter -t "$p" -m -n -- '
            'env K3S_DATA_DIR=/tmp/lp-ki06-k3s/state python3 -B "$1"', "sh", target]


def run(broker, arm, provider):
    baseline = broker.request({"tool": "prepare"})
    first = baseline["initial_error_log"]
    model, effort, _ = MODELS[arm]
    calls, history, failure = host_broker_investigation(broker, provider, model, effort, first)
    proof = broker.request({"tool": "verify"})
    recovered = all(Trial.recovered(item) for item in proof["independent_probes"])
    return {**baseline, **proof, "arm": arm, "protocol": PROTOCOL, "model": model, "effort": effort,
            "model_failed": failure,
            "status": "incomplete" if failure or not calls else "verified_recovered" if recovered else "unrecovered",
            "reason_code": "cni_network_restored" if recovered else "cni_sandboxes_still_pending",
            "input_sha256": sha256((TASK + first).encode()).hexdigest(), "calls": calls, "investigation": history,
            "usage": {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "real containerd CNI errors; native K3s kubelet, Pod identities and direct UDP/TCP DNS"}


def native_proof(row):
    healthy, failed = row["healthy_before"], row["fault_before"]
    first, second = row["independent_probes"]
    initial = row["initial_error_log"]
    assert row["input_sha256"] == sha256((TASK + initial).encode()).hexdigest()
    assert row["fixture_sha256"] == fingerprint([CASE, K8S_VERSION, row["fixture_contract"]])
    assert INCOMPATIBLE not in row["fixture_contract"]["plugin_supported_versions"]
    assert READY in row["fixture_contract"]["plugin_supported_versions"]
    assert healthy["cni_version"] == READY and healthy["control"]["ready"] and is_dns_healthy(healthy["control_dns"])
    assert Trial.pending(failed)
    targets = {pod["name"]: pod["uid"] for pod in failed["targets"]}
    assert set(targets) == set(TARGETS) and len(set(targets.values())) == 2
    assert {event["pod"]: event["pod_uid"] for event in failed["events"]} == targets
    assert len({event["uid"] for event in failed["events"]}) == 2
    assert "incompatible CNI versions" in initial and all(stable_error(event) == initial for event in failed["events"])
    assert all(event["reason"] == "FailedCreatePodSandBox" and event["node"] == NODE for event in failed["events"])
    assert [healthy["at"], failed["at"], first["at"], second["at"]] == sorted({
        healthy["at"], failed["at"], first["at"], second["at"]})
    assert all(sample["control"] == healthy["control"] for sample in (failed, first, second))
    assert all({p["name"]: p["uid"] for p in sample["targets"]} == targets
               and all(p["node"] == NODE for p in sample["targets"]) for sample in (first, second))
    if row["status"] == "unrecovered":
        return all(Trial.pending(item) for item in (first, second))
    assert row["status"] == "verified_recovered" and all(Trial.recovered(item) for item in (first, second))
    assert {p["name"]: p["pod_ip"] for p in first["targets"]} == {p["name"]: p["pod_ip"] for p in second["targets"]}
    addresses = {p["pod_ip"] for p in first["targets"]} | {first["control"]["pod_ip"]}
    assert len(addresses) == 3 and all(ip_address(address) in ip_network("10.42.0.0/24") for address in addresses)
    return all(first["target_dns"][name][-1]["at"] < second["target_dns"][name][0]["at"] for name in TARGETS)


def receipt_valid(row):
    try:
        assert row["case"] == CASE and row["protocol"] == PROTOCOL and row["kubernetes"] == K8S_VERSION
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
        changes = [step for step in row["investigation"] if step["request"]["tool"] == "configure"
                   and step["result"].get("applied") is True]
        if row["status"] == "unrecovered":
            return not changes
        return len(changes) == 1 and changes[0]["result"] == {
            "applied": True, "before": INCOMPATIBLE, "after": READY, "network_unchanged": True}
    except (AssertionError, KeyError, IndexError, TypeError, ValueError):
        return False


def product_proof(row):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA

    before, after = row["native_watch_witnesses"]
    events = {event["uid"] for event in row["fault_before"]["events"]}
    rules = row["deployed_prometheus_rules"]
    epochs = row["native_watch_epoch"]
    return (row["status"] == "unrecovered" and row["reason_code"] == "cni_version_failure_not_detected"
            and row["product_source_image"] == row["deployed_ready_image"] == IMAGE_SHA
            and row["matched_incident_count_after"] == row["agent_runs_after"] == 0 and row["calls"] == []
            and row["wall_duration_ms"] is None and row["observation_seconds"] >= 55
            and set(epochs) == {"event_watch", "pod_watch", "endpoint_poller"} and all(epochs.values())
            and (before["phase"], after["phase"]) == ("before", "after") and UUID(before["uid"]) != UUID(after["uid"])
            and events <= set(row["observed_failure_event_uids"])
            and rules["rule_count"] > 0 and rules["cni_sandbox_rules"] == []
            and re.fullmatch(r"[0-9a-f]{64}", rules["sha256"]) is not None)


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
            broker = JsonLineSession(command, timeout=190, stderr=err)
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
