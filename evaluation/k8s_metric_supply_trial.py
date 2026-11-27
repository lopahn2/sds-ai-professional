"""Compare four models on a Kubernetes metrics outage and independently verify HPA recovery."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.ki08_metric_supply_fixture import (
    CASE, LOAD_HIGH, NS, PROVIDER, VERSION, WorkloadTrial, comparable_contract,
)
from evaluation.live_repair import JsonLineSession, TASK, host_broker_investigation, host_codex_provider
from evaluation.single_baseline import MODELS, estimate


PROTOCOL = "live-repair/v41"
VM = "ki08-iso-verify"
FIXTURE = Path(__file__).with_name("ki08_metric_supply_fixture.py")
SHUTDOWN = {"stopped": True, "resources_removed": True}
PREAMBLE = (
    "Request one operation per turn using the JSON schema. workload reads the deployment and dependent services, "
    "logs reads the current failure record, source reads the current editable configuration and field name, "
    "probe checks the live metrics API and workload, configure with key and value applies the supported "
    "configuration field, and finish stops. Preserve the HPA policy and the original Deployment.\n\n"
)


def guest_command(home):
    home = home.resolve()
    if home.parent != Path("/private/tmp") or not home.name.startswith("lp-ki08-lima."):
        raise ValueError("expected the disposable KI-08 VM")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    staged, target = "/tmp/ki08-standalone-source.py", "/tmp/lp-ki08-k3s/fixture.py"
    subprocess.run(["limactl", "copy", str(FIXTURE), f"{VM}:{staged}"], check=True, timeout=25)
    subprocess.run([*shell, "sudo", "-n", "install", "-m", "0400", staged, target], check=True, timeout=10)
    actual = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", target], text=True, timeout=10).split()[0]
    if sha256(FIXTURE.read_bytes()).hexdigest() != actual:
        raise RuntimeError("wrong metric provider code in VM")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'p=$(cat /tmp/lp-ki08-k3s/server.pid); exec nsenter -t "$p" -m -n -- python3 -B "$1"', "sh", target]


def run(broker, arm, provider):
    baseline = broker.request({"tool": "prepare"})
    first = baseline["initial_error_log"]
    model, effort, _ = MODELS[arm]
    calls, history, failure = host_broker_investigation(broker, provider, model, effort, first)
    proof = broker.request({"tool": "verify"})
    recovered = all(WorkloadTrial.restored(item) for item in proof["independent_probes"])
    return {**baseline, **proof, "arm": arm, "protocol": PROTOCOL, "model": model, "effort": effort,
            "model_failed": failure,
            "status": "incomplete" if failure or not calls else "verified_recovered" if recovered else "unrecovered",
            "reason_code": "hpa_metrics_recovered" if recovered else "hpa_metrics_unavailable",
            "input_sha256": sha256((TASK + first).encode()).hexdigest(), "calls": calls, "investigation": history,
            "usage": {field: complete_sum(call["usage"].get(field) for call in calls) for field in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "offline K3s with synthetic CPU source, real aggregation and HPA, native Deployment and running Pods"}


def samples_valid(row):
    healthy, fault = row["healthy_before"], row["fault_before"]
    first, second = row["independent_probes"]
    all_samples = (healthy, fault, first, second)
    assert WorkloadTrial.normal(healthy) and WorkloadTrial.broken(fault)
    assert fault["provider_condition"]["reason"] == "MissingEndpoints"
    snapshot = fault["hpa_condition"]
    assert "currently unable to handle the request" in snapshot["message"]
    assert row["initial_error_log"] == f"{snapshot['reason']}: {snapshot['message']}"
    assert row["input_sha256"] == sha256((TASK + row["initial_error_log"]).encode()).hexdigest()
    assert len({sample["endpoint_uid"] for sample in all_samples}) == 1
    assert {sample["deployment_uid"] for sample in all_samples} == {row["service_contract"][2]["uid"]}
    times = [sample["observed_at"] for sample in all_samples]
    assert sorted(set(times)) == times
    original = healthy["pods"][0]["uid"]
    assert all(original in {pod["uid"] for pod in sample["pods"]} for sample in all_samples)
    if row["status"] == "unrecovered":
        return all(WorkloadTrial.broken(x) for x in (first, second))
    assert row["status"] == "verified_recovered"
    assert all(WorkloadTrial.restored(x) for x in (first, second))
    assert {pod["uid"] for pod in first["pods"]} == {pod["uid"] for pod in second["pods"]}
    assert all({pod["name"] for pod in item["pods"]} == {m["pod"] for m in item["metrics"]}
               and all(m["cpu"] == LOAD_HIGH for m in item["metrics"]) for item in (first, second))
    assert max(m["at"] for m in first["metrics"]) < min(m["at"] for m in second["metrics"])
    return True


def standalone_proof(row):
    try:
        assert row["case"] == CASE and row["protocol"] == PROTOCOL and row["kubernetes"] == VERSION
        assert row["cleanup"] == SHUTDOWN and row["model_failed"] is None
        assert row["fixture_sha256"] == comparable_contract(row["service_contract"])
        assert samples_valid(row)
        model, effort, _ = MODELS[row["arm"]]
        calls = row["calls"]
        assert calls and (row["model"], row["effort"]) == (model, effort)
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
        expected = {"name": PROVIDER, "namespace": NS, "uid": row["fault_before"]["endpoint_uid"],
                    "before": False, "after": True}
        return len(writes) == 1 and writes[0]["result"]["write"] == expected
    except (AssertionError, KeyError, IndexError, TypeError, ValueError):
        return False


def product_proof(row):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA

    try:
        assert row["case"] == CASE and row["protocol"] == PROTOCOL and row["kubernetes"] == VERSION
        assert row["arm"] == "liveprobe" and row["status"] == "unrecovered"
        assert row["reason_code"] == "metrics_supply_failure_not_detected" and row["cleanup"] == SHUTDOWN
        assert row["fixture_sha256"] == comparable_contract(row["service_contract"]) and samples_valid(row)
        assert row["product_source_image"] == row["deployed_ready_image"] == IMAGE_SHA
        assert row["matched_incident_count_after"] == row["agent_runs_after"] == 0 and row["calls"] == []
        assert row["wall_duration_ms"] is None and row["observation_seconds"] >= 55
        epochs = row["native_watch_epoch"]
        assert set(epochs) == {"event_watch", "pod_watch", "endpoint_poller"} and all(epochs.values())
        before, after = row["native_watch_witnesses"]
        assert (before["phase"], after["phase"]) == ("before", "after") and UUID(before["uid"]) != UUID(after["uid"])
        event = row["initial_event"]
        if event:
            assert event["uid"] in row["observed_failure_event_uids"] and event["reason"] == "FailedGetResourceMetric"
        rules = row["deployed_prometheus_rules"]
        return rules["rule_count"] > 0 and rules["metrics_hpa_rules"] == [] and re.fullmatch(r"[0-9a-f]{64}", rules["sha256"]) is not None
    except (AssertionError, KeyError, IndexError, TypeError, ValueError):
        return False


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--lima-home", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--arm", choices=MODELS, action="append", required=True)
    args = parser.parse_args()
    if len(args.arm) != len(set(args.arm)) or any((args.output_dir / f"{arm}.json").exists() for arm in args.arm):
        raise ValueError("select new standalone arms for this output")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cmd = guest_command(args.lima_home)
    provider = host_codex_provider(args.codex_bin, PREAMBLE)
    for arm in args.arm:
        print(json.dumps({"phase": "starting", "case": CASE, "arm": arm}), flush=True)
        attempt = args.output_dir / f"{arm}.stderr"
        with attempt.open("x") as err:
            broker = JsonLineSession(cmd, timeout=140, stderr=err)
            try:
                receipt = run(broker, arm, provider)
                receipt["cleanup"] = broker.request({"tool": "shutdown"})
            except Exception:
                print(*attempt.read_text().splitlines()[-8:], sep="\n", flush=True)
                raise
            finally:
                broker.close()
        valid = standalone_proof(receipt)
        filename = f"{arm}.json" if valid else f"{arm}.incomplete"
        with (args.output_dir / filename).open("x") as out:
            json.dump(receipt, out, indent=2)
            out.write("\n")
        print(json.dumps({**{key: receipt[key] for key in ("case", "arm", "status", "wall_duration_ms",
                                                           "estimated_usd", "model_failed")},
                          "receipt_valid": valid}), flush=True)


if __name__ == "__main__":
    main()
