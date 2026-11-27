"""Run the four standalone models against the real KI-10 Deployment."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.k8s_dns_version_trial import PREAMBLE
from evaluation.ki10_dns_assets import ARCHIVES
from evaluation.ki10_dns_fixture import API_VERSION, CASE, COMMAND, ERROR, LEGACY, REPOSITORY, VERSIONS, failed, ready
from evaluation.live_repair import JsonLineSession, TASK, host_codex_provider, host_model_turn
from evaluation.single_baseline import K8S_MAX_TOOL_TURNS, MODELS, estimate


PROTOCOL = "live-repair/v38"
FIXTURE = Path(__file__).with_name("ki10_dns_fixture.py")
TOOLS = {"workload", "logs", "source", "configure", "probe", "finish"}
VM = "ki10-iso-verify"
COMPLETED_PRODUCT_STATES = {"resolved", "handed_off", "failed", "recovery_review", "action_pending"}


def fixture_key(baseline, assets):
    pinned = [asset["binary_sha256"] for asset in assets]
    digest = (PROTOCOL, CASE, baseline["kubernetes"], baseline["scope_before"]["sha256"], *pinned)
    return sha256(json.dumps(digest, separators=(",", ":")).encode()).hexdigest()


def product_measures(record):
    runs = record["runs"]
    calls = [{**run, "usage": {"input_tokens": run["input_tokens"], "output_tokens": run["output_tokens"]}}
             for run in runs]
    return {
        "calls": calls,
        "usage": {name: complete_sum(run["usage"].get(name) for run in calls) for name in TOKEN_FIELDS},
        "model_duration_ms": complete_sum(run["duration_ms"] for run in calls),
        "estimated_usd": complete_sum(run["estimated_cost_usd"] for run in calls),
        "scope": "deployed LiveProbe, real Kubernetes, original log present in Loki, RAG and independent DNS requests",
    }


def product_rollout_proof(data, before, fault, after):
    from evaluation.k8s_admission_request_trial import IMAGE_SHA
    from evaluation.ki10_dns_fixture import WORKLOAD

    incident = data["incident"]
    if (data.get("candidate_log_filter_sha256") or data["deployment_image"] != IMAGE_SHA or data["authority"] != {
            "same_version_args_allowed": True, "older_version_denied": True}
            or not data["actual_startup_log_in_loki"] or data["rag_chunks"] <= 0
            or incident["status"] not in COMPLETED_PRODUCT_STATES or WORKLOAD not in incident["title"]
            or "CrashLoopBackOff" not in incident["signal_reasons"] or not data["runs"]
            or not any(run["status"] == "succeeded" for run in data["runs"])
            or type(data["wall_duration_ms"]) is not int or data["wall_duration_ms"] <= 0
            or after["image"] != fault["image"] or after["image_id"] != fault["image_id"]):
        return False
    measured = product_measures(data)
    if (type(measured["usage"]["input_tokens"]) is not int or type(measured["usage"]["output_tokens"]) is not int
            or measured["estimated_usd"] is None or measured["model_duration_ms"] is None):
        return False
    if data["status"] == "unrecovered":
        return (after["args"] == fault["args"] and after["generation"] == fault["generation"]
                and failed(data["recovery"]["probes"]))
    return (data["status"] == "verified_recovered" and after["args"] == COMMAND
            and after["ready"] is True and after["available_replicas"] == 1
            and after["generation"] == after["observed_generation"] and fault["generation"] < after["generation"]
            and len({row["replica_set_uid"] for row in (before, fault, after)}) == 3
            and len({row["pod_uid"] for row in (before, fault, after)}) == 3
            and any(action["executed"] for action in data["actions"]) and ready(data["recovery"]["probes"]))


def rollout_proof(data):
    """Review model identity, the actual workload transition and independent DNS results."""
    try:
        if (data["protocol"], data["case"], data["kubernetes"], data["initial_error_log"], data.get("model_failed")) != (
                PROTOCOL, CASE, API_VERSION, ERROR, None):
            return False
        if (data["fault"]["first_error"] != ERROR or data["input_sha256"] != sha256((TASK+ERROR).encode()).hexdigest()
                or data["scope_before"] != data["scope_after"] or data["cleanup"] != {"removed": True, "dns_absent": True}):
            return False
        if ([(asset["version"], asset["upstream_sha256"]) for asset in data["assets"]] != list(ARCHIVES.items())
                or fixture_key(data, data["assets"]) != data["fixture_sha256"]):
            return False
        baseline, fault, after = [data[stage]["state"] for stage in ("baseline", "fault", "recovery")]
        healthy = lambda row: row["ready"] is True and row["available_replicas"] == 1 and (  # noqa: E731
            row["observed_generation"] == row["generation"])
        if (not healthy(baseline) or not ready(data["baseline"]["probes"]) or baseline["image"] != f"{REPOSITORY}:{VERSIONS[0]}"
                or fault["image"] != f"{REPOSITORY}:{VERSIONS[1]}" or fault["args"] != baseline["args"] or
                fault["args"] != [*LEGACY, *COMMAND] or not failed(data["fault"]["probes"])
                or baseline["image_id"] == fault["image_id"] or not all(row["image_id"] for row in (baseline, fault, after))
                or UUID(baseline["deployment_uid"]) != UUID(fault["deployment_uid"])
                or fault["deployment_uid"] != after["deployment_uid"]
                or UUID(baseline["replica_set_uid"]) == UUID(fault["replica_set_uid"])
                or UUID(baseline["pod_uid"]) == UUID(fault["pod_uid"])
                or baseline["generation"] >= fault["generation"]
                or not any(s.get("terminated", {}).get("exitCode", 0) != 0
                           for s in (fault["container_state"], fault["last_state"]))):
            return False
        if data["arm"] == "liveprobe":
            return product_rollout_proof(data, baseline, fault, after)
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
        changes = [entry for entry in data["investigation"] if entry["request"]["tool"] == "configure"
                   and entry["result"].get("applied") is True]
        if after["image"] != fault["image"] or after["image_id"] != fault["image_id"]:
            return False
        if data["status"] == "unrecovered":
            return (not changes and after["args"] == fault["args"] and after["generation"] == fault["generation"]
                    and failed(data["recovery"]["probes"]))
        return (data["status"] == "verified_recovered" and len(changes) == 1
                and changes[0]["request"]["key"] == "launchArguments" and json.loads(changes[0]["request"]["value"]) == []
                and after["args"] == COMMAND and healthy(after) and fault["generation"] < after["generation"]
                and len({UUID(row["replica_set_uid"]) for row in (baseline, fault, after)}) == 3
                and len({UUID(row["pod_uid"]) for row in (baseline, fault, after)}) == 3
                and ready(data["recovery"]["probes"]))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False


def command(home):
    assert home.parent == Path("/private/tmp") and home.name.startswith("lp-ki10-lima.")
    os.environ["LIMA_HOME"] = str(home)
    shell = ["limactl", "shell", "--workdir=/tmp", VM]
    staged = subprocess.check_output([*shell, "sudo", "-n", "sha256sum", "/opt/isolated/ki10_dns_fixture.py"],
                                     text=True, timeout=15).split()[0]
    if staged != sha256(FIXTURE.read_bytes()).hexdigest():
        raise RuntimeError("the installed test broker differs from the reviewed source")
    return [*shell, "sudo", "-n", "sh", "-ec",
            'pid=$(cat /tmp/lp-ki10-k3s/server.pid); exec nsenter -t "$pid" -m -n -- env '
            'K3S_DATA_DIR=/tmp/lp-ki10-k3s/state python3 -B /opt/isolated/ki10_dns_fixture.py']


def trial(broker, arm, provider, assets):
    baseline = broker.request({"tool": "prepare"})
    assert ready(baseline["baseline"]["probes"]) and failed(baseline["fault"]["probes"])
    error = baseline["fault"]["first_error"]
    assert error == ERROR
    model, effort, _ = MODELS[arm]
    calls, history, failure = [], [], None
    deadline = monotonic() + 240
    for _ in range(K8S_MAX_TOOL_TURNS):
        remaining = int(deadline - monotonic())
        if remaining <= 0:
            break
        reply = host_model_turn(provider, model, effort, error, history, timeout=min(45, remaining))
        calls.append({key: value for key, value in reply.items() if key != "data"})
        if (reply["model"], reply["effort"], reply["status"]) != (model, effort, "ok"):
            failure = reply["status"]
            break
        operation = reply["data"]
        if not isinstance(operation, dict) or operation.get("tool") not in TOOLS:
            failure = "invalid_tool_request"
            break
        if operation["tool"] == "finish":
            history.append({"request": operation, "result": {"finished": True}})
            break
        result = broker.request({key: operation.get(key) for key in ("tool", "key", "value")})
        history.append({"request": operation, "result": result})
        if operation["tool"] == "configure" and result.get("applied") is True:
            break
    verified = broker.request({"tool": "verify"})
    recovered = ready(verified["recovery"]["probes"])
    return {**baseline, **verified, "arm": arm, "protocol": PROTOCOL, "model": model, "effort": effort,
            "status": "incomplete" if failure or not calls else ("verified_recovered" if recovered else "unrecovered"),
            "model_failed": failure, "initial_error_log": error, "input_sha256": sha256((TASK+error).encode()).hexdigest(),
            "fixture_sha256": fixture_key(baseline, assets), "calls": calls, "investigation": history,
            "usage": {name: complete_sum(call["usage"].get(name) for call in calls) for name in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
            "scope": "separate offline Kubernetes Deployment, ConfigMap and real UDP/TCP DNS requests", "assets": assets}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--lima-home", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--arm", choices=MODELS, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if len(set(args.arm)) != len(args.arm) or any((args.output_dir / f"{a}.json").exists() for a in args.arm):
        parser.error("trial receipts already exist or an arm was repeated")
    assets = json.loads((args.assets / "fixture-images.json").read_text())
    assert [(row["version"], row["upstream_sha256"]) for row in assets] == list(ARCHIVES.items())
    guest = command(args.lima_home.resolve())
    provider = host_codex_provider(args.codex_bin, PREAMBLE)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for arm in args.arm:
        print(json.dumps({"phase": "starting", "case": CASE, "arm": arm}), flush=True)
        with TemporaryDirectory(prefix="lp-ki10-broker-", dir="/private/tmp") as tmp:
            log = Path(tmp) / "guest.log"
            with log.open("w", encoding="utf-8") as output:
                broker = JsonLineSession(guest, timeout=160, stderr=output)
                try:
                    data = trial(broker, arm, provider, assets)
                    data["cleanup"] = broker.request({"tool": "shutdown"})
                    assert data["cleanup"] == {"removed": True, "dns_absent": True}
                except Exception:
                    print(*log.read_text().splitlines()[-8:], sep="\n", file=sys.stderr)
                    raise
                finally:
                    broker.close()
        with (args.output_dir / f"{arm}.json").open("x") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
        print(json.dumps({k: data[k] for k in ("case", "arm", "status", "wall_duration_ms", "usage",
                                              "estimated_usd", "model_failed")}), flush=True)


if __name__ == "__main__":
    main()
