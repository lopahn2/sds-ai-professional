"""Compare recovery from an actual version-specific Istio listener NACK."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.ki09_istio_fixture import (
    BINARIES, CASE, CURRENT, FAULT_PORT, KUBERNETES, PREVIOUS, SHUTDOWN, WORKLOAD,
    blocked, bootstrap, clean_error, digest, gateway, healthy_control, listener_sync, patch, recovered, routes,
)
from evaluation.live_repair import JsonLineSession, TASK, host_broker_investigation, host_codex_provider
from evaluation.single_baseline import MODELS, estimate


PROTOCOL = "live-repair/v48"
VM = "ki09-iso-verify"
SOURCE = Path(__file__).with_name("ki09_istio_fixture.py")
ERROR = (f"Error adding/updating listener(s) 0.0.0.0_{FAULT_PORT}: Protobuf message "
         "(type envoy.extensions.filters.network.http_connection_manager.v3."
         "HttpConnectionManager(root) with unknown field set {61}) has unknown fields")
BUILDS = {
    PREVIOUS: "6d807371331491b1ba0742bb02cefc7c4db36cd6/1.37.10-dev/Clean/RELEASE/BoringSSL",
    CURRENT: "3bf722f59561ef9747835e543c0b648eb4ab1237/1.38.5-dev/Clean/RELEASE/BoringSSL",
}
PREAMBLE = (
    "Request one operation per turn using the JSON schema. workload reads current proxy and route status, "
    "logs reads the native xDS error, source reads current mesh and available proxy versions, "
    "probe tests the routes, configure takes a setting key and value, and finish stops. The "
    "gateway_proxy_version setting selects an installed version. Preserve the mesh configuration and both routes.\n\n"
)


def guest_command(home):
    home = home.resolve()
    if home.parent != Path("/private/tmp") or not home.name.startswith("lp-ki09-lima."):
        raise ValueError("expected the disposable KI-09 VM")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    stage, target = "/tmp/ki09-stage/native.py", "/tmp/lp-ki09-k3s/native.py"
    subprocess.run(["limactl", "copy", str(SOURCE), f"{VM}:{stage}"], check=True, timeout=25)
    subprocess.run([*shell, "sudo", "-n", "install", "-m", "0400", stage, target], check=True, timeout=12)
    digest = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", target], text=True, timeout=12).split()[0]
    if digest != sha256(SOURCE.read_bytes()).hexdigest():
        raise RuntimeError("the active VM does not have the selected fixture")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'p=$(cat /tmp/lp-ki09-k3s/server.pid); exec nsenter -t "$p" -m -n -- '
            'env K3S_DATA_DIR=/tmp/lp-ki09-k3s/state python3 -B "$1"', "sh", target]


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
            "reason_code": "istio_listener_acked_and_route_restored" if restored else "istio_listener_still_rejected",
            "input_sha256": sha256((TASK + error).encode()).hexdigest(), "calls": calls, "investigation": history,
            "usage": {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "Istiod LDS delivery on a real Kubernetes API and version-specific gateway requests"}


def native_proof(row):
    baseline, fault, (first, second) = row["healthy_before"], row["fault_before"], row["independent_probes"]
    contract = row["fixture_contract"]
    assert row["initial_error_log"] == ERROR == clean_error(listener_sync(fault)["lastError"])
    assert row["input_sha256"] == sha256((TASK + ERROR).encode()).hexdigest()
    assert row["fixture_sha256"] == digest([CASE, contract]) and contract["kubernetes"] == KUBERNETES
    assert contract["binaries"] == BINARIES and contract["strict_dynamic_config"] is True
    assert contract["bootstraps"] == [bootstrap(PREVIOUS), bootstrap(CURRENT)]
    assert contract["gateway_initial"] == gateway([18080]) and contract["gateway_expanded"] == gateway([18080, FAULT_PORT])
    assert contract["patch"] == patch() and contract["routes"] == routes()
    assert healthy_control(baseline) and baseline["new"]["status"] is None and not baseline["nack"]
    baseline_lds = listener_sync(baseline)
    assert baseline_lds["sent"] == baseline_lds["acked"] and listener_sync(fault)["acked"] == baseline_lds["acked"]
    assert blocked(fault)
    assert row["mesh_identity"] == row["mesh_identity_after"]
    mesh = row["mesh_identity"]
    assert set(mesh) == {"gateway.networking.istio.io", "virtualservice", "envoyfilter"}
    for key, spec, generation in (("gateway.networking.istio.io", contract["gateway_expanded"]["spec"], 2),
                                  ("virtualservice", contract["routes"]["spec"], 1),
                                  ("envoyfilter", contract["patch"]["spec"], 1)):
        identity = mesh[key]
        assert UUID(identity["uid"]) and identity["generation"] == generation and identity["spec_sha256"] == digest(spec)
    states = (baseline, fault, first, second)
    assert [item["at"] for item in states] == sorted({item["at"] for item in states})
    assert all(item["envoy_build"] == BUILDS[item["version"]]
               and item["istiod_sync"]["istio_version"] == item["version"] for item in states)
    assert len({item[request]["nonce"] for item in states for request in ("control", "new")}) == 8
    if row["status"] == "unrecovered":
        return all(blocked(item) and item["nack"] == ERROR and clean_error(listener_sync(item)["lastError"]) == ERROR
                   for item in (first, second))
    assert row["status"] == "verified_recovered"
    return all(recovered(item) and item["nack"] is None and not item["xds_errors"] for item in (first, second))


def product_proof(row):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA

    before, after = row["native_watch_witnesses"]
    rules, watchers = row["deployed_prometheus_rules"], row["native_watch_epoch"]
    rejects = row["native_pilot_lds_reject_count"]
    return (row["status"] == "unrecovered" and row["reason_code"] == "istio_listener_nack_not_detected"
            and row["product_source_image"] == row["deployed_ready_image"] == IMAGE_SHA
            and row["matched_incident_count_after"] == row["agent_runs_after"] == 0 and not row["calls"]
            and row["wall_duration_ms"] is None and row["observation_seconds"] >= 145
            and set(watchers) == {"event_watch", "pod_watch", "endpoint_poller"} and all(watchers.values())
            and (before["phase"], after["phase"]) == ("before", "after") and UUID(before["uid"]) != UUID(after["uid"])
            and rules["rule_count"] > 0 and rules["xds_rules"] == []
            and type(rejects["before"]) is type(rejects["after"]) is int and 0 <= rejects["before"] < rejects["after"]
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
        changes = [step["result"] for step in row["investigation"] if step["request"]["tool"] == "configure"
                   and step["result"].get("applied") is True]
        if row["status"] == "unrecovered":
            return not changes
        return changes == [{"applied": True, "before": PREVIOUS, "after": CURRENT, "target": WORKLOAD}]
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
