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
# Same tiers and efforts on the Claude lineup (Haiku 5.5 / Sonnet 5.5 / Opus 5.5), through the Claude Code CLI.
CLAUDE_ARM = {"arm_id": "liveprobe-claude", "strategy": "adaptive", "provider": "claude_cli", "roles": {
    "router": {"model": "claude-haiku-5-5", "effort": "low"},
    "signal_analyst": {"model": "claude-haiku-5-5", "effort": "low"},
    "result_verifier": {"model": "claude-sonnet-5-5", "effort": "low"},
    "evidence_curator": {"model": "claude-sonnet-5-5", "effort": "medium"},
    "safety_verifier": {"model": "claude-opus-5-5", "effort": "xhigh"}}}
# Owner instruction 2026-11-27: tests run on Opus 5.5 only. Every role, group role, router candidate and escalation
# step uses it (app/evalrun.py `only_model`); each role keeps the effort of the tiered arm.
CLAUDE_OPUS_ARM = {**CLAUDE_ARM, "arm_id": "liveprobe-claude-opus", "only_model": "claude-opus-5-5", "roles": {
    role: {**rm, "model": "claude-opus-5-5"} for role, rm in CLAUDE_ARM["roles"].items()}}
ARMS = {"codex": ARM, "claude": CLAUDE_ARM, "claude-opus": CLAUDE_OPUS_ARM}
SIGNAL = {"C1": "pool_exhaustion", "C2": "upstream_5xx", "C3": "pool_exhaustion", "C3b": "pool_exhaustion", "C2b": "upstream_5xx",
          "C1c": "pool_exhaustion", "C2c": "upstream_5xx", "C3c": "pool_exhaustion"}
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
    if scenario in ("C1", "C1c"):
        time.sleep(90)
        k("-n", NS, "rollout", "restart", "deployment/shop-order")
        marks["decoy_order_rollout"] = stamp()
        time.sleep(60)
        frozen = "SKU-0005,SKU-0009" if scenario == "C1c" else "SKU-0003,SKU-0007"
        k("-n", NS, "patch", "configmap", "partner-gateway-state", "-p", json.dumps({"data": {"frozen": frozen}}))
        marks["t0_partner_frozen"] = stamp()
    elif scenario in ("C2", "C2b", "C2c"):
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
    elif scenario in ("C3", "C3b", "C3c"):
        time.sleep(90)
        k("-n", NS, "apply", "-f", "-", input=render(scenario, "--fault-objects"))
        marks["limitrange_applied"] = stamp()
        time.sleep(20)
        if scenario == "C3":
            k("-n", NS, "patch", "configmap", "shop-stock-config", "-p", '{"data":{"LOGGING_LEVEL_COM_ACME":"INFO,DEBUG(sampled)"}}')
            k("-n", NS, "rollout", "restart", "deployment/shop-stock")
        else:  # certificate rotation (C3b) or node maintenance (C3c) rolls the pods; no configuration change
            key = "certs.acme.io/rotated-at" if scenario == "C3b" else "maintenance.acme.io/drained-at"
            k("-n", NS, "patch", "deployment", "shop-stock", "-p",
              json.dumps({"spec": {"template": {"metadata": {"annotations": {key: stamp()}}}}}))
        wait_ready("deployment/shop-stock", 300)
        marks["t0_restart_with_default_limit"] = stamp()
    time.sleep(observe)
    marks["observe_end"] = stamp()


def setup(scenario: str, marks: dict) -> None:
    for _ in range(60):  # the previous run deletes without waiting; a terminating namesake breaks the StatefulSet
        left = [p for p in k("-n", NS, "get", "pods", "-o", "name", check=False).split()
                if "/shop-" in p or "/partner-feed-sync" in p]
        if not left:
            break
        time.sleep(5)
    else:
        raise RuntimeError(f"previous run still terminating: {left}")
    k("-n", NS, "apply", "-f", "-", input=render(scenario))
    if scenario in ("C2", "C2b", "C2c"):
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
    db = "shop-db-node-0" if scenario in ("C2", "C2b", "C2c") else "shop-db-0"
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
    if scenario in ("C1", "C1c"):
        state["held_allocations"] = q("shop-db-0", "select count(*) from partner_allocation where status = 'HOLD'")
        state["idle_in_transaction"] = q("shop-db-0", "select count(*) from pg_stat_activity where state = 'idle in transaction'")
        state["fault_in_place"] = state["held_allocations"] == "2"
    elif scenario in ("C2", "C2b", "C2c"):
        state["old_primary_read_only"] = q("shop-db-node-0", "show default_transaction_read_only", "postgres")
        state["new_primary_in_recovery"] = q("shop-db-node-1", "select pg_is_in_recovery()", "postgres")
        state["service_endpoints"] = k("-n", NS, "get", "endpoints", "shop-db", "-o",
                                       "jsonpath={.subsets[*].addresses[*].targetRef.name}", check=False).split()
        state["fault_in_place"] = (state["old_primary_read_only"] == "on" and state["new_primary_in_recovery"] == "f"
                                   and len(state["service_endpoints"]) == 2)
    elif scenario in ("C3", "C3b", "C3c"):
        state["stock_cpu_limits"] = k("-n", NS, "get", "pods", "-l", "app=shop-stock", "-o",
                                      "jsonpath={range .items[*]}{.spec.containers[0].resources.limits.cpu} {end}", check=False).split()
        state["fault_in_place"] = {"C3b": "300m", "C3c": "280m"}.get(scenario, "250m") in state["stock_cpu_limits"]
    return state


def run_engine(scenario: str, memory: bool = False, arm: dict | None = None) -> dict:
    arm = arm or ARM
    if memory:
        arm = {**arm, "arm_id": f"{arm['arm_id']}-memory", "rag_memory": True}
    args = ["-n", "liveprobe-system", "exec", "deploy/liveprobe-api", "-c", "api", "--", "python", "-m", "app.evalrun",
            "run", "--case", f"compound-{scenario}", "--target", "shop-stock", "--signal", SIGNAL[scenario],
            "--arm", json.dumps(arm)]
    started = time.monotonic()
    r = subprocess.run([*KUBE[:-1], "--request-timeout=0", *args], text=True, capture_output=True, timeout=3600)
    line = next((l for l in reversed(r.stdout.splitlines()) if l.startswith("EVALRUN_RESULT ")), None)
    result = json.loads(line[len("EVALRUN_RESULT "):]) if line else {"error": (r.stderr or r.stdout)[-2000:]}
    result["client_wall_s"] = round(time.monotonic() - started, 1)
    return result


def collect(out: Path) -> None:
    pods = k("-n", NS, "get", "pods", "-o", "name", check=False).split()
    for pod in [p.split("/", 1)[1] for p in pods if "/shop-" in p or "/partner-feed-sync" in p]:
        try:  # bounded: a failing stock Pod can log ~100 MB of stack traces
            text = k("-n", NS, "logs", pod, "--all-containers", "--timestamps", "--tail=20000", check=False, timeout=90)
            prev = k("-n", NS, "logs", pod, "--all-containers", "--timestamps", "--previous", "--tail=20000",
                     check=False, timeout=90)
        except subprocess.TimeoutExpired:
            text, prev = "(log copy timed out)\n", ""
        (out / f"pod-{pod}.log").write_text(prev + text)
    (out / "events.txt").write_text(k("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False))


def cleanup(scenario: str) -> None:
    k("-n", NS, "delete", "-f", "-", "--ignore-not-found", "--wait=false", input=render(scenario), check=False)
    if scenario in ("C3", "C3b", "C3c"):
        k("-n", NS, "delete", "-f", "-", "--ignore-not-found", input=render(scenario, "--fault-objects"), check=False)
    k("-n", NS, "delete", "job", "-l", "app=partner-feed-sync", "--ignore-not-found", check=False)


# ------------------------------------------------------------------ operator rounds (C2 family)
# The harness acts as the operator who reads a hand-off. It applies only the primitives a decision file names,
# written after reading that round's hand-off, by the same rule as the baseline grading: committed steps, and
# conditional steps whose condition the evidence already answers. Unsafe primitives are refused and recorded.
UNSAFE = {"unfence_old_primary": "writes on both nodes would make two primaries (split brain)"}


def _psql_root(pod: str, sql: str) -> str:
    return k("-n", NS, "exec", pod, "-c", "postgres", "--", "psql", "-XqAt", "-U", "postgres", "-c", sql)


def settle(kind_name: str, timeout: int = 300) -> str:
    """A rollout that cannot finish is an observation for the next round, not a harness failure."""
    try:
        wait_ready(kind_name, timeout)
        return ""
    except RuntimeError:
        return f" (rollout of {kind_name} not complete within {timeout}s)"


def apply_primitive(name: str) -> str:
    if name == "relabel_old_primary":
        k("-n", NS, "label", "pod", "shop-db-node-0", "role=replica", "--overwrite")
        return "kubectl label pod shop-db-node-0 role=replica --overwrite"
    if name == "restart_stock":
        k("-n", NS, "rollout", "restart", "deployment/shop-stock")
        return "kubectl rollout restart deployment/shop-stock" + settle("deployment/shop-stock")
    if name == "allow_app_on_new_primary":
        k("-n", NS, "exec", "shop-db-node-1", "-c", "postgres", "--", "sh", "-c",
          'echo "host all shop_app all scram-sha-256" >> "$PGDATA/pg_hba.conf"')
        _psql_root("shop-db-node-1", "select pg_reload_conf()")
        return "allow role shop_app over TCP in pg_hba.conf on shop-db-node-1 + pg_reload_conf()"
    if name == "pool_max_lifetime_60s":
        k("-n", NS, "set", "env", "deployment/shop-stock", "SPRING_DATASOURCE_HIKARI_MAX_LIFETIME=60000")
        return "SPRING_DATASOURCE_HIKARI_MAX_LIFETIME=60000" + settle("deployment/shop-stock")
    env = {  # C1 family: configuration an operator changes on the stock Deployment (each rolls the Pods)
        "leak_detection": "SPRING_DATASOURCE_HIKARI_LEAK_DETECTION_THRESHOLD=2000",
        "pool_max_8": "SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE=8",
        "patched_ledger": "SHOPFRONT_STOCK_LEDGER_RELEASE=1.4.3",   # the reproduction's patched build of the leak path
        "disable_partner_ledger": "SHOPFRONT_STOCK_PARTNER_LEDGER=false",
    }
    if name in env:
        k("-n", NS, "set", "env", "deployment/shop-stock", env[name])
        return env[name] + settle("deployment/shop-stock")
    if name in ("explicit_cpu_limit_2000m", "explicit_cpu_limit_1000m"):
        cpu = name.rsplit("_", 1)[1]
        k("-n", NS, "set", "resources", "deployment/shop-stock", f"--limits=cpu={cpu}")
        return f"shop-stock limits.cpu={cpu}" + settle("deployment/shop-stock")
    if name == "order_single_attempt":
        k("-n", NS, "set", "env", "deployment/shop-order", "SHOPFRONT_ORDER_STOCK_RETRIES=0")
        return "shop-order retries=0" + settle("deployment/shop-order")
    if name == "order_retries_1_backoff":
        k("-n", NS, "set", "env", "deployment/shop-order", "SHOPFRONT_ORDER_STOCK_RETRIES=1",
          "SHOPFRONT_ORDER_STOCK_BACKOFFMS=300")
        return "shop-order retries=1 backoff-ms=300" + settle("deployment/shop-order")
    if name == "probe_liveness_group":
        k("-n", NS, "patch", "deployment", "shop-stock", "--type", "json", "-p", json.dumps([
            {"op": "replace", "path": "/spec/template/spec/containers/0/livenessProbe/httpGet/path",
             "value": "/actuator/health/liveness"}]))
        return "livenessProbe.httpGet.path=/actuator/health/liveness" + settle("deployment/shop-stock")
    raise ValueError(f"unknown primitive {name}")


HEALTH = r"""
import httpx, json
from app.config import get_settings
from app.observation_sources import adapter_url
base = adapter_url(get_settings(), 'prometheus')
out = {}
for app in ("shop-order", "shop-stock"):
    q = ('sum(increase(http_server_requests_seconds_count{namespace="dev",app="%s",status=~"5.."}[%s]))' % (app, "WINDOW"),
         'sum(increase(http_server_requests_seconds_count{namespace="dev",app="%s"}[%s]))' % (app, "WINDOW"))
    vals = []
    for expr in q:
        rows = httpx.get(f"{base}/api/v1/query", params={"query": expr}, timeout=20).json()["data"]["result"]
        vals.append(float(rows[0]["value"][1]) if rows else 0.0)
    out[app] = {"errors_5xx": round(vals[0], 1), "requests": round(vals[1], 1),
                "error_ratio": round(vals[0] / vals[1], 4) if vals[1] else None}
print(json.dumps(out))
"""


def health(window: str = "2m") -> dict:
    r = subprocess.run([*KUBE, "-n", "liveprobe-system", "exec", "-i", "deploy/liveprobe-api", "-c", "api", "--",
                        "python", "-"], input=HEALTH.replace("WINDOW", window), text=True, capture_output=True, timeout=120)
    return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {"error": r.stderr[-300:]}


def wait_decision(out: Path, n: int, timeout: int = 1800) -> dict:
    path = out / f"round-{n}-decision.json"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(5)
    return {"stop": True, "note": "no operator decision within the timeout"}


def operator_rounds(scenario: str, out: Path, record: dict, marks: dict, rounds: int, memory: bool,
                    arm: dict | None = None) -> None:
    from .liveprobe_answer import answer_of, fetch
    record["rounds"] = []
    for n in range(1, rounds + 1):
        if n > 1:
            record["rounds"][-1]["engine"] = None  # keep the list lean; the full result is in round files
            engine = run_engine(scenario, memory=memory, arm=arm)
        else:
            engine = record["engine"]
        iid = engine.get("incident_id")
        handoff = answer_of(fetch(iid)) if iid else {}
        entry = {"round": n, "incident": iid, "cost_usd": engine.get("cost_usd"),
                 "wall_s": (engine.get("telemetry") or {}).get("wall_s"), "handoff": handoff}
        (out / f"round-{n}-handoff.json").write_text(json.dumps({**entry, "engine": engine}, ensure_ascii=False,
                                                                indent=2, default=str))
        marks[f"round_{n}_handoff"] = stamp()
        decision = wait_decision(out, n)
        entry["decision"] = decision
        record["rounds"].append(entry)
        if decision.get("stop"):
            break
        applied, refused = [], []
        for name in decision.get("apply") or []:
            if name in UNSAFE:
                refused.append({"primitive": name, "why": UNSAFE[name]})
                continue
            applied.append({"primitive": name, "command": apply_primitive(name), "at": stamp()})
        entry["applied"], entry["refused"] = applied, refused
        marks[f"round_{n}_applied"] = stamp()
        time.sleep(180)
        entry["health_after_3m"] = health("2m")
        healthy = all((v or {}).get("error_ratio") is not None and v["error_ratio"] < 0.01
                      for v in entry["health_after_3m"].values() if isinstance(v, dict))
        if healthy:
            time.sleep(300)
            entry["verify_5m"] = health("5m")
            record["resolved_in_round"] = n
            break
        marks[f"round_{n}_unhealthy"] = stamp()
        time.sleep(60)  # let the new failure mode accumulate evidence before the next analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("C1", "C1c", "C2", "C2b", "C2c", "C3", "C3b", "C3c"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--memory", action="store_true",
                        help="memory episode: the engine may retrieve verified resolutions of earlier eval incidents")
    parser.add_argument("--observe", type=int, default=300)
    parser.add_argument("--operator-rounds", type=int, default=0,
                        help="C2 family: apply operator decisions after each hand-off and re-analyse (max rounds)")
    parser.add_argument("--provider", choices=sorted(ARMS), default="codex",
                        help="LLM provider of every LiveProbe role: codex (GPT lineup) or claude (Claude lineup)")
    args = parser.parse_args()
    arm = ARMS[args.provider]
    args.out.mkdir(parents=True, exist_ok=False)
    marks: dict = {"start": stamp()}
    record: dict = {"scenario": args.scenario, "arm": arm}
    try:
        setup(args.scenario, marks)
        inject(args.scenario, marks, args.observe)
        record["fault_state"] = fault_state(args.scenario)
        if not record["fault_state"].get("fault_in_place"):
            raise RuntimeError(f"fault not in place: {record['fault_state']}")
        record["engine"] = run_engine(args.scenario, memory=args.memory, arm=arm)
        marks["engine_end"] = stamp()
        if args.operator_rounds:
            operator_rounds(args.scenario, args.out, record, marks, args.operator_rounds, args.memory, arm)
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
