"""LiveProbe arm: deploy one scenario into dev, inject its fault, run the production engine on it, clean up.

    python -m evaluation.compound.liveprobe_arm --scenario C1 --out DIR

The engine runs inside liveprobe-api through `app.evalrun run` with the production role models, so evidence
collection, routing, groups, RAG (runbook/public_doc only, by evalrun's parity rule) and accounting are the real
paths. Only shop-* objects (and the C3 LimitRange) are created, and every one is deleted at the end.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

NS = "dev"
KUBE = ["kubectl", "--context", "rancher-desktop", "--request-timeout=60s"]
ARM = {"arm_id": "liveprobe", "strategy": "adaptive", "roles": {
    "router": {"model": "gpt-6-luna", "effort": "low"}, "signal_analyst": {"model": "gpt-6-luna", "effort": "low"},
    "result_verifier": {"model": "gpt-6.2-sol", "effort": "low"},
    "evidence_curator": {"model": "gpt-6.2-sol", "effort": "medium"},
    "safety_verifier": {"model": "gpt-6.2-astra", "effort": "xhigh"}}}
SIGNAL = {"C1": "pool_exhaustion", "C2": "upstream_5xx", "C3": "pool_exhaustion"}
ROOT = Path(__file__).resolve().parents[2]


def k(*args: str, input: str | None = None, check: bool = True, timeout: float = 120) -> str:
    r = subprocess.run([*KUBE, *args], input=input, text=True, capture_output=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"kubectl {' '.join(args[:4])}: {r.stderr.strip()[:300]}")
    return r.stdout


def render(scenario: str, *flags: str) -> str:
    r = subprocess.run(["python3", "-B", "-m", "evaluation.compound.k8s", "--scenario", scenario, *flags],
                       cwd=ROOT, text=True, capture_output=True, check=True)
    return r.stdout


def psql(pod: str, sql: str) -> str:
    return k("-n", NS, "exec", pod, "-c", "postgres", "--", "psql", "-XqAt", "-U", "postgres", "-c", sql)


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def wait_ready(kind_name: str, timeout: int = 300) -> None:
    k("-n", NS, "rollout", "status", kind_name, f"--timeout={timeout}s", timeout=timeout + 30)


def inject(scenario: str, marks: dict, observe: int) -> None:
    if scenario == "C1":
        time.sleep(90)
        k("-n", NS, "rollout", "restart", "deployment/shop-order")
        marks["decoy_order_rollout"] = stamp()
        time.sleep(60)
        k("-n", NS, "patch", "configmap", "partner-gateway-state", "-p", '{"data":{"frozen":"SKU-0003,SKU-0007"}}')
        marks["t0_partner_frozen"] = stamp()
    elif scenario == "C2":
        time.sleep(90)
        psql("shop-db-node-0", "alter system set default_transaction_read_only = on")
        psql("shop-db-node-0", "select pg_reload_conf()")
        lsn = psql("shop-db-node-0", "select pg_current_wal_lsn()").strip()
        for _ in range(60):
            if psql("shop-db-node-1", f"select pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn").strip() == "t":
                break
            time.sleep(1)
        psql("shop-db-node-1", "select pg_promote(true, 60)")
        k("-n", NS, "label", "pod", "shop-db-node-1", "role=primary", "--overwrite")
        marks["t0_switchover"] = stamp()   # shop-db-node-0 keeps role=primary (the relabel that failed)
    elif scenario == "C3":
        time.sleep(90)
        k("-n", NS, "apply", "-f", "-", input=render("C3", "--fault-objects"))
        marks["limitrange_applied"] = stamp()
        time.sleep(20)
        k("-n", NS, "patch", "configmap", "shop-stock-config", "-p", '{"data":{"LOGGING_LEVEL_COM_ACME":"INFO,DEBUG(sampled)"}}')
        k("-n", NS, "rollout", "restart", "deployment/shop-stock")
        wait_ready("deployment/shop-stock", 300)
        marks["t0_restart_with_default_limit"] = stamp()
    time.sleep(observe)
    marks["observe_end"] = stamp()


def setup(scenario: str, marks: dict) -> None:
    k("-n", NS, "apply", "-f", "-", input=render(scenario))
    if scenario == "C2":
        for _ in range(120):
            if "Running" in k("-n", NS, "get", "pod", "shop-db-node-0", "-o", "jsonpath={.status.phase}", check=False):
                break
            time.sleep(2)
        k("-n", NS, "label", "pod", "shop-db-node-0", "role=primary", "--overwrite")
        k("-n", NS, "rollout", "status", "statefulset/shop-db-node", "--timeout=420s", timeout=460)
    else:
        k("-n", NS, "rollout", "status", "statefulset/shop-db", "--timeout=300s", timeout=330)
    for name in ("shop-stock", "shop-order", "shop-traffic"):
        wait_ready(f"deployment/{name}")
    db = "shop-db-node-0" if scenario == "C2" else "shop-db-0"
    for _ in range(60):  # traffic must really reach the database before any fault
        count = k("-n", NS, "exec", db, "-c", "postgres", "--", "psql", "-XqAt", "-U", "postgres", "-d", "shop",
                  "-c", "select count(*) from reservation_log", check=False).strip()
        if count.isdigit() and int(count) > 50:
            break
        time.sleep(5)
    else:
        raise RuntimeError("baseline traffic never reached the database")
    marks["ready"] = stamp()


def fault_state(scenario: str) -> dict:
    """Read-only confirmation that the fault is really in place before the engine looks at it."""
    def q(pod, sql, db="shop"):
        return k("-n", NS, "exec", pod, "-c", "postgres", "--", "psql", "-XqAt", "-U", "postgres", "-d", db, "-c", sql,
                 check=False).strip()
    restarts = k("-n", NS, "get", "pods", "-l", "app=shop-stock", "-o",
                 "jsonpath={range .items[*]}{.status.containerStatuses[0].restartCount} {end}", check=False).split()
    state: dict = {"stock_restarts": restarts}
    if scenario == "C1":
        state["held_allocations"] = q("shop-db-0", "select count(*) from partner_allocation where status = 'HOLD'")
        state["idle_in_transaction"] = q("shop-db-0", "select count(*) from pg_stat_activity where state = 'idle in transaction'")
        state["fault_in_place"] = state["held_allocations"] == "2"
    elif scenario == "C2":
        state["old_primary_read_only"] = q("shop-db-node-0", "show default_transaction_read_only", "postgres")
        state["new_primary_in_recovery"] = q("shop-db-node-1", "select pg_is_in_recovery()", "postgres")
        state["service_endpoints"] = k("-n", NS, "get", "endpoints", "shop-db", "-o",
                                       "jsonpath={.subsets[*].addresses[*].targetRef.name}", check=False).split()
        state["fault_in_place"] = (state["old_primary_read_only"] == "on" and state["new_primary_in_recovery"] == "f"
                                   and len(state["service_endpoints"]) == 2)
    elif scenario == "C3":
        state["stock_cpu_limits"] = k("-n", NS, "get", "pods", "-l", "app=shop-stock", "-o",
                                      "jsonpath={range .items[*]}{.spec.containers[0].resources.limits.cpu} {end}", check=False).split()
        state["fault_in_place"] = "250m" in state["stock_cpu_limits"]
    return state


def run_engine(scenario: str) -> dict:
    args = ["-n", "liveprobe-system", "exec", "deploy/liveprobe-api", "-c", "api", "--", "python", "-m", "app.evalrun",
            "run", "--case", f"compound-{scenario}", "--target", "shop-stock", "--signal", SIGNAL[scenario],
            "--arm", json.dumps(ARM)]
    started = time.monotonic()
    r = subprocess.run([*KUBE[:-1], "--request-timeout=0", *args], text=True, capture_output=True, timeout=3600)
    line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("EVALRUN_RESULT ")), None)
    result = json.loads(line[len("EVALRUN_RESULT "):]) if line else {"error": (r.stderr or r.stdout)[-2000:]}
    result["client_wall_s"] = round(time.monotonic() - started, 1)
    return result


def collect(out: Path) -> None:
    pods = k("-n", NS, "get", "pods", "-o", "name", check=False).split()
    for pod in [p.split("/", 1)[1] for p in pods if "/shop-" in p or "/partner-feed-sync" in p]:
        text = k("-n", NS, "logs", pod, "--all-containers", "--timestamps", check=False, timeout=60)
        prev = k("-n", NS, "logs", pod, "--all-containers", "--timestamps", "--previous", check=False, timeout=60)
        (out / f"pod-{pod}.log").write_text(prev + text)
    (out / "events.txt").write_text(k("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False))


def cleanup(scenario: str) -> None:
    k("-n", NS, "delete", "-f", "-", "--ignore-not-found", "--wait=false", input=render(scenario), check=False)
    if scenario == "C3":
        k("-n", NS, "delete", "-f", "-", "--ignore-not-found", input=render("C3", "--fault-objects"), check=False)
    k("-n", NS, "delete", "job", "-l", "app=partner-feed-sync", "--ignore-not-found", check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("C1", "C2", "C3"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--observe", type=int, default=300)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    marks: dict = {"start": stamp()}
    record: dict = {"scenario": args.scenario, "arm": ARM}
    try:
        setup(args.scenario, marks)
        inject(args.scenario, marks, args.observe)
        record["fault_state"] = fault_state(args.scenario)
        if not record["fault_state"].get("fault_in_place"):
            raise RuntimeError(f"fault not in place: {record['fault_state']}")
        record["engine"] = run_engine(args.scenario)
        marks["engine_end"] = stamp()
        collect(args.out)
    finally:
        record["marks"] = marks
        cleanup(args.scenario)
        marks["cleaned"] = stamp()
        (args.out / "liveprobe.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    e = record.get("engine", {})
    print(json.dumps({"scenario": args.scenario, "fault": record.get("fault_state"), "incident": e.get("incident_id"), "cost_usd": e.get("cost_usd"),
                      "wall_s": (e.get("telemetry") or {}).get("wall_s"), "outcome": e.get("outcome"),
                      "candidates": [(c.get("cause_class"), c.get("raw_cause", "")[:100]) for c in e.get("root_cause_candidates", [])],
                      "error": e.get("error")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
