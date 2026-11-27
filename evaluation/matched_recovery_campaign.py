"""Contracts and read-only preflight for the matched dev recovery comparison."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess


ARMS = ("liveprobe", "astra", "sol", "terra")
RECOVERY_BUDGET_S = 900
CONTEXT = "rancher-desktop"
NS = "dev"


@dataclass(frozen=True)
class Case:
    id: str
    initial_signal: str
    observation_sources: tuple[str, ...]
    verification: str


K8S = (
    Case("K8S-IMAGE-CONTRACT", "TargetHigh5xxRate", ("rollout_revision", "business_http"),
         "Ready rollout to pinned prior image and repeated order-to-inventory business request"),
    Case("K8S-POOL-CONFIG", "TargetConnectionPoolExhausted", ("hikari", "configmap_version"),
         "Pinned config version and repeated business requests with idle pool and no waiting callers"),
    Case("K8S-UPSTREAM-RESTART", "TargetHigh5xxRate", ("caller_log", "upstream_business_http"),
         "New caller instance and repeated requests after upstream is independently healthy"),
    Case("K8S-REPLICA-BACKLOG", "TargetHighLatency", ("work_queue", "database_latency"),
         "Two Ready replicas drain backlog under unchanged arrival rate and preserve business results"),
    Case("KW-03", "FailedMount", ("current_pod_event", "pvc_pv_binding"),
         "Original read-only Deployment claim reference, unchanged binding and data digest, repeated HTTP 200"),
)
APP = Case("APP-JDBC-LEAK", "ApplicationException", ("application_exception", "hikari"),
           "Same regression before and after source change, successful dev rollout and pool back to baseline")
OPTIONAL = Case("K8S-OOM", "TargetWorkloadOOMKilled", ("kubelet_termination", "container_resources"),
                "Proved original termination and sustained recovery under fault workload at approved memory limit")


class PreflightBlocked(RuntimeError):
    pass


def comparison_matrix(include_oom: bool = False) -> tuple[dict, ...]:
    cases = (*K8S, APP, OPTIONAL) if include_oom else (*K8S, APP)
    rows = []
    for index, case in enumerate(cases):
        shift = index % len(ARMS)
        for arm in (*ARMS[shift:], *ARMS[:shift]):
            rows.append({"case": case.id, "arm": arm, "namespace": NS})
    return tuple(rows)


def canonical_digest(value: dict) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(wire.encode()).hexdigest()


def matched_start(receipt: dict) -> str:
    """Identity of comparable K8s initial conditions; temporal counters and Pod UIDs are run-specific."""
    keys = ("context", "namespace", "node_uid", "deployment_uid", "fixture_digest", "fault_digest",
            "verifier_digest", "initial_error_digest")
    if (receipt.get("context"), receipt.get("namespace")) != (CONTEXT, NS) or any(
        not isinstance(receipt.get(key), str) or not receipt[key] for key in keys
    ):
        raise PreflightBlocked("incomplete evidence of a common dev starting state")
    return canonical_digest({key: receipt[key] for key in keys})


def require_ready_api() -> None:
    try:
        result = subprocess.run(
            ["kubectl", "--context", CONTEXT, "--request-timeout=4s", "get", "--raw=/readyz"],
            capture_output=True, text=True, timeout=7,
        )
    except FileNotFoundError:
        raise PreflightBlocked("kubectl is not installed or is missing from PATH") from None
    except subprocess.TimeoutExpired:
        raise PreflightBlocked("rancher-desktop kube-apiserver readiness request timed out") from None
    if result.returncode == 0 and result.stdout.strip() == "ok":
        return

    error = result.stderr.lower()
    if "dial tcp" in error and ("operation not permitted" in error or "permission denied" in error):
        raise PreflightBlocked("local Kubernetes socket access is blocked for this process; run from an authorized host session")
    if "connection refused" in error or "was refused" in error:
        raise PreflightBlocked("rancher-desktop kube-apiserver connection refused")
    if "error from server (forbidden)" in error or "error from server (unauthorized)" in error:
        raise PreflightBlocked("rancher-desktop kube-apiserver denied this identity's readiness request")
    if "deadline exceeded" in error or "i/o timeout" in error:
        raise PreflightBlocked("rancher-desktop kube-apiserver readiness request timed out")
    raise PreflightBlocked("rancher-desktop kube-apiserver readiness could not be verified")


def dev_preflight() -> dict:
    require_ready_api()
    # Reuse the same native resource gate as existing dev trials. No fixture is prepared here.
    from evaluation.harness.real import _load_chaos

    chaos = _load_chaos()
    try:
        return {"context": CONTEXT, "namespace": NS, "api_ready": True, "headroom": chaos.node_gate()}
    except chaos.NodeGateStop as exc:
        sample = exc.snapshot
        raise PreflightBlocked(
            f"Kubernetes API is reachable. The dev resource gate blocked the trial ({', '.join(exc.breaches)}) "
            f"with CPU {sample['cpu_m']}/{sample['cpu_cap_m']}m and memory {sample['mem_mi']}/{sample['alloc_mi']}Mi"
        ) from None
    except chaos.Stop as exc:
        raise PreflightBlocked(str(exc)) from exc


def write_new(path: Path, payload: dict):
    wire = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8") as output:
        output.write(wire + "\n")
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = {"checked_at": datetime.now(timezone.utc).isoformat(), "rows": comparison_matrix(),
               "recovery_budget_s": RECOVERY_BUDGET_S}
    try:
        receipt["preflight"] = dev_preflight()
        receipt["status"] = "preflight_passed"
    except (PreflightBlocked, OSError, subprocess.TimeoutExpired) as exc:
        receipt.update(status="blocked", reason=str(exc) if isinstance(exc, PreflightBlocked) else type(exc).__name__)
    write_new(args.receipt, receipt)
    return 0 if receipt["status"] == "preflight_passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
