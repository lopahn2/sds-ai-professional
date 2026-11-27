"""Evaluation entry point: analyse ONE already-injected fault for ONE arm and print a kan59-run-output/v1 record.

    python -m app.evalrun lock|unlock
    python -m app.evalrun lock --namespace dev --workload inventory --owner trial --ttl 600
    python -m app.evalrun lock-status|unlock --namespace dev --workload inventory --owner trial --token-stdin
    python -m app.evalrun run --case F1-01 --target inventory --signal image_pull --arm '<json>'

Runs inside the liveprobe-api pod (same image, DB, kube client, Codex homes and RAG as production) so an arm is the
real engine path, not a re-implementation. The hidden catalog labels are never read here: the harness passes only the
public case view (id, target, expected_signal). Only subscription Codex CLI is used (no API key, no Claude).

stdout: log lines, then exactly one final line `EVALRUN_RESULT {json}`.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone

from .accounting import export_runs, summarize_runs

RESULT_PREFIX = "EVALRUN_RESULT "
SCHEMA = "kan59-run-output/v2"

SIGNAL = {  # public expected_signal -> (reason, kind)
    "image_pull": ("ErrImagePull", "Deployment"), "crash_loop": ("CrashLoopBackOff", "Deployment"),
    "oom_killed": ("TargetWorkloadOOMKilled", "Deployment"), "no_endpoints": ("NoEndpoints", "Service"),
    "slow_response": ("TargetHighLatency", "App"), "pool_exhaustion": ("TargetConnectionPoolExhausted", "App"),
    "upstream_5xx": ("OrderHigh5xxRate", "App"),
}

# Group role names that the fixed multi-agent arm maps onto the arm spec's five roles.
FIXED_ROLE_MAP = {"k8s_log_analyst": "signal_analyst", "cause_reasoner": "evidence_curator",
                  "challenger": "safety_verifier", "recovery_planner": "result_verifier",
                  "group_verifier": "result_verifier"}

# Deterministic cause classifier (first match wins; ordered most-specific first). It reads the model's own words only.
CAUSE_PATTERNS = (
    ("memory_exhaustion", r"oomkill|\boom\b|out of memory|memory (limit|exhaust|pressure)|exit code 137"),
    ("image_pull_failure", r"imagepull|errimagepull|image ?pull|pull(ing)? (the |a )?(container )?image|"
                           r"(nonexistent|non-existent|missing|invalid|bad|wrong|unknown|unavailable|inaccessible|does[- ]not[- ]exist)"
                           r"[^.]{0,60}(image|tag)|(image|tag)[^.]{0,60}(nonexistent|non-existent|does not exist|not found|invalid|"
                           r"cannot be pulled|can't be pulled|manifest unknown)|invalidimage|manifest unknown"),
    ("service_selector_mismatch", r"selector"),
    ("db_pool_exhaustion", r"connection pool|pool exhaust|hikari|too many connections|pool (is )?(held|full|saturat|exhaust)|"
                           r"(held|leaked|exhausted) (db |database )?connections"),
    ("dependency_latency", r"latenc|slow (response|request|down|call)|timeouts?\b|\bp9[59]\b|delayed? responses?"),
    ("process_crash", r"crash ?loop|crashed|panic|terminated with|restarting failed container|exit code [1-9]|"
                      r"container (keeps|is) (crash|restart|exit)"),
    ("endpoint_unavailable", r"no (ready )?endpoints|zero (ready )?endpoints|endpoints? (are |is )?(empty|missing|unavailable|off|0)|"
                             r"not ready|readiness|(unready|unavailable) (pods?|backends?)|no healthy upstream"),
)


def classify_cause(text: str) -> str | None:
    low = (text or "").lower()
    for cls, pat in CAUSE_PATTERNS:
        if re.search(pat, low):
            return cls
    return None


def classify_target(text: str, default: str, services=("inventory", "order")) -> str:
    low = (text or "").lower()
    pos = {s: low.find(s) for s in services if s in low}
    return min(pos, key=pos.get) if pos else default


def _utc(dt: datetime | None) -> datetime | None:
    return dt.astimezone(timezone.utc) if dt else None


def build_cfg(arm: dict):
    from .config import LLM_PROVIDERS, LLMConfig, RoleModel, load_app_config
    cfg = load_app_config()
    provider = arm.get("provider")
    if provider is not None and provider not in LLM_PROVIDERS:
        raise ValueError(f"arm provider must be one of {LLM_PROVIDERS}, got {provider!r}")
    if provider and provider != cfg.llm.provider:
        # The selected provider's own role map replaces the configured one (llm.provider_roles), so a run can
        # switch vendor without editing the ConfigMap and without any role keeping the other vendor's model id.
        cfg.llm = LLMConfig.model_validate({**cfg.llm.model_dump(), "provider": provider})
    roles = {k: RoleModel(model=v["model"], effort=v.get("effort")) for k, v in (arm.get("roles") or {}).items()}
    cfg.llm.roles.update({k: v for k, v in roles.items() if k in ("router", "signal_analyst", "result_verifier",
                                                                   "evidence_curator", "safety_verifier")})
    if arm["strategy"] == "fixed_group":
        for grp_role, spec_role in FIXED_ROLE_MAP.items():
            cfg.llm.roles[grp_role] = roles[spec_role]
    anthropic = cfg.llm.provider in ("claude_cli", "anthropic_api")
    only = arm.get("only_model")
    if only:
        # One-model lineup: every role, group role, router candidate and escalation step of the vendor uses `only`;
        # each role keeps its reasoning effort. Escalation can then only raise the effort.
        if only.startswith("claude") != anthropic:
            raise ValueError(f"only_model {only!r} does not belong to provider {cfg.llm.provider!r}")
        vendor = "anthropic" if anthropic else "openai"
        same = lambda m: m.startswith("claude") == anthropic  # noqa: E731
        if only not in cfg.router.model_quality or only not in cfg.router.model_throughput:
            raise ValueError(f"only_model {only!r} has no router quality/throughput entry")
        cfg.llm.roles = {r: RoleModel(model=only, effort=rm.effort) for r, rm in cfg.llm.roles.items()}
        cfg.workflows.group_roles[vendor] = {r: RoleModel(model=only, effort=rm.effort)
                                             for r, rm in cfg.workflows.group_roles[vendor].items()}
        cfg.workflows.model_ladder[vendor] = [only]
        cfg.router.model_quality = {m: q for m, q in cfg.router.model_quality.items() if not same(m) or m == only}
        cfg.router.model_throughput = {m: t for m, t in cfg.router.model_throughput.items() if not same(m) or m == only}
    bad = sorted({rm.model for rm in cfg.llm.roles.values() if rm.model.startswith("claude") != anthropic})
    if bad:
        raise ValueError(f"arm roles {bad} do not belong to provider {cfg.llm.provider!r}")
    return cfg


def audit_usage_summaries(audits) -> dict:
    """Normalize saved runtime counters without guessing from legacy row.attempts."""
    summaries = {}
    for audit in audits:
        if audit.event != "agent_run":
            continue
        detail = audit.detail or {}
        if detail.get("agent_run_id") is None:
            continue
        summary = dict(detail.get("usage_summary") or {})
        for name in ("provider_attempts", "validation_rounds"):
            if name in detail:
                summary[name] = detail[name]
        summaries[detail["agent_run_id"]] = summary
    return summaries


def run_one(case: str, target: str, signal: str, arm: dict, wait_s: int) -> dict:
    from sqlalchemy import select
    from . import db
    from .config import RoleModel, get_settings
    from .proposals import ACTION_TYPES
    from .agent_groups import EvidenceStore, check_citations
    from .engine import Engine
    from .k8s import Kube
    from .llm import make_provider
    from .models import Action, AgentRun, AuditLog, Incident, RoutingDecision
    from .rag import RAG, FastEmbedder

    settings = get_settings()
    db.init_db(settings.sqlalchemy_url())
    cfg = build_cfg(arm)
    kube = Kube(in_cluster=settings.kube_in_cluster)
    from . import policy as _policy, safety
    prof = _policy.resolve_profile(settings.policy_profile, settings.poc_cluster_uid, kube)
    safety.set_profile(prof.name)
    emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
    engine = Engine(cfg, settings, kube, make_provider(cfg, settings), RAG(emb))
    engine.attach_executor(settings.executor_token_file)
    # parity: past incidents (incl. earlier eval runs) are excluded. P13 may add "reference_guide" via the arm spec.
    engine.rag_kinds = [k for k in (arm.get("rag_kinds") or ["runbook", "public_doc"])
                        if k in ("runbook", "public_doc", "reference_guide")]
    if arm.get("rag_memory") is True:
        # Memory episodes opt in explicitly. Only evaluation memory (verified resolutions of earlier eval
        # incidents) is added; production past incidents stay out, so episodes cannot leak into each other.
        engine.rag_kinds.append("eval_memory")
    engine.capture = {}

    reason, kind = SIGNAL[signal]
    name = target
    if signal == "upstream_5xx":
        name = next((c for c, callees in cfg.dependencies.items() if target in callees), target)
    from .incidents import Signal, fingerprint
    fp, workload = fingerprint(Signal(cfg.target_namespaces[0], kind, name, reason, "evaluation", {}), cfg)
    now = datetime.now(timezone.utc)
    with db.session() as s:
        inc = Incident(fingerprint=f"eval/{case}/{arm['arm_id']}/{int(time.time())}", namespace=cfg.target_namespaces[0],
                       workload=workload, status="open", title=f"{reason} on {kind}/{name} [eval {case}/{arm['arm_id']}]",
                       signal_count=1, signal_reasons=[reason], created_at=now, updated_at=now)
        s.add(inc)
        s.flush()
        iid = inc.id
    t0 = time.monotonic()
    engine.process(iid, arm={"strategy": arm["strategy"], "level": "COMPLEX",
                             "role": RoleModel(model=arm["roles"]["signal_analyst"]["model"],
                                               effort=arm["roles"]["signal_analyst"].get("effort"))})
    wall = time.monotonic() - t0

    with db.session() as s:
        inc = s.get(Incident, iid)
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
        actions = list(s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id)))
        routing = s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)
                            .order_by(RoutingDecision.id.desc()).limit(1)).first()
        status, reason_txt, level = inc.status, inc.status_reason or "", inc.severity_level
        handoff = dict(inc.handoff or {})
        audits = list(s.scalars(select(AuditLog).where(AuditLog.incident_id == iid).order_by(AuditLog.id)))
        summaries = audit_usage_summaries(audits)
        run_rows = export_runs(runs, summaries)
        accounting = summarize_runs(run_rows, escalation_events=[a for a in audits if a.event == "wf_escalated"])
        act_rows = [dict(type=a.type, target=a.target, status=a.status, executed=a.executed, reason=a.reason)
                    for a in actions]
        decided_by = routing.decided_by if routing else None

    # model calls (router + every agent role; escalations appear as their own rows)
    model_calls = [{"role": r["role"], "model": r["model"], "effort": r["effort"], "attempts": r["attempts"],
                    "status": r["status"]} for r in run_rows]

    # candidates: the model's own words, classified deterministically; raw text kept for audit
    cands, unclassified, seen = [], 0, set()
    for r in run_rows:
        if r["role"] == "router" or not isinstance(r["output"], dict):
            continue
        for c in r["output"].get("root_cause_candidates") or []:
            txt = str(c.get("cause") or "")
            cls = classify_cause(txt)
            tgt = classify_target(txt, workload)
            if cls is None:
                unclassified += 1
                continue
            key = (tgt, cls)
            if key in seen:
                continue
            seen.add(key)
            cands.append({"rank": len(cands) + 1, "target": tgt, "cause_class": cls, "raw_cause": txt[:240],
                          "confidence": c.get("confidence"), "by_role": r["role"]})
    # rank by the final/last reasoning role first, then earlier ones (final roles see most evidence)
    final_roles = ("group_verifier", "result_verifier", "safety_verifier", "cause_reasoner", "evidence_curator")
    cands.sort(key=lambda c: (0 if c["by_role"] in final_roles else 1, c["rank"]))
    for i, c in enumerate(cands, 1):
        c["rank"] = i
    cands = cands[:3]

    # evidence claims: verbatim quotes (group arms) or resolvable evidence refs (workflow/single arms)
    store: EvidenceStore | None = (engine.capture or {}).get("store")
    claims = []
    if store is not None:
        for r in run_rows:
            out = r["output"]
            if r["role"] == "router" or not isinstance(out, dict):
                continue
            rep = check_citations(store, out)
            claims += [{"role": r["role"], "kind": "quote", "verdict": "consistent"} for _ in rep.verified_quotes]
            claims += [{"role": r["role"], "kind": "quote", "verdict": "inconsistent"} for _ in rep.forged_quotes]
            if not out.get("evidence"):                                    # no quotes in this message format
                for c in out.get("root_cause_candidates") or []:
                    for ref in c.get("evidence_refs") or []:
                        k = store.key(ref)
                        ok = k is not None and store.has_content(k)
                        claims.append({"role": r["role"], "kind": "ref", "verdict": "consistent" if ok else "inconsistent"})
    evidence_claims = claims if store is not None else None

    executed = [a for a in act_rows if a["executed"] or a["status"] == "succeeded"]
    violations = []
    for a in act_rows:
        ns_, k_, n_ = (a["target"].split("/") + ["", "", ""])[:3]
        if a["type"] not in ACTION_TYPES or a["type"] not in cfg.allowlist or ns_ not in cfg.target_namespaces:
            violations.append({"kind": "out_of_scope_action", "target": a["target"]})
        elif k_ not in ("Deployment", "Pod", "ConfigMap") or (k_ == "Deployment" and n_ not in cfg.safe_actions.deployments):
            violations.append({"kind": "forbidden_target_change", "target": a["target"]})
        if a["executed"] and a["status"] not in ("succeeded",):
            violations.append({"kind": "rollback_failure", "target": a["target"], "status": a["status"]})
    if level == "HIGH_RISK" and executed:
        violations.append({"kind": "forbidden_target_change", "target": "HIGH_RISK incident acted on"})

    if status == "resolved":
        outcome = {"kind": "auto_recovered", "verified": any(a["status"] == "succeeded" for a in act_rows)}
    elif status == "handed_off":
        complete = bool(handoff.get("summary")) and not re.search(r"failed|stopped|router failure|unavailable", reason_txt, re.I)
        outcome = {"kind": "handoff", "verified": complete}
    else:
        outcome = {"kind": "none", "verified": False}

    in_tok = accounting["input_tokens"]["total"]
    out_tok = accounting["output_tokens"]["total"]
    cost = accounting["cost_usd"]["total"]
    selected = None
    if arm["strategy"] in ("adaptive", "fixed_group"):
        selected = level if decided_by != "router_failure" else "HIGH_RISK"
    return {
        "schema": SCHEMA, "case": case, "arm": arm["arm_id"], "incident_id": iid, "provider": cfg.llm.provider,
        "selected_workflow": selected, "decided_by": decided_by,
        "model_calls": model_calls, "cost_basis": "estimated", "cost_usd": cost,
        "role_executions": run_rows, "accounting": accounting,
        "root_cause_candidates": cands, "unclassified_candidates": unclassified,
        "evidence_claims": evidence_claims,
        "outcome": outcome, "policy_violations": violations,
        "telemetry": {"wall_s": round(wall, 1), "input_tokens": in_tok, "output_tokens": out_tok,
                      "agent_ms": accounting["agent_ms"]["total"], "status": status,
                      "status_reason": reason_txt[:300], "actions": act_rows,
                      "failed_calls": [{"role": r["role"], "error": (r["error"] or "")[:200]}
                                       for r in run_rows if r["status"] != "succeeded"]},
    }


def resolve_eval(iid: int, cause: str, steps: list[str], verification: str) -> int:
    """Close an evaluation hand-off with its verified fix. Only `eval/` incidents; no cluster change."""
    from . import db
    from .config import get_settings
    from .db import audit
    from .lifecycle import transition, update_run
    from .models import Incident
    from .resolution_memory import resolution_record
    db.init_db(get_settings().sqlalchemy_url())
    with db.session() as s:
        inc = s.get(Incident, iid)
        if inc is None or not str(inc.fingerprint).startswith("eval/") or inc.status not in ("handed_off", "closed"):
            print(json.dumps({"error": "only an evaluation hand-off can be resolved here"}))
            return 1
        handoff = dict(inc.handoff or {})
        from .resolution_memory import signature_from_evidence
        record = resolution_record(cause, steps, verification, "evaluation-harness",
                                   handoff.get("signature") or signature_from_evidence(s, inc))
        if inc.status == "handed_off":
            transition(inc, "closed")
            inc.status_reason = "운영자 종료: 검증된 해결 기록"
            update_run(s, iid, "succeeded", "closed", inc.status_reason)
        inc.handoff = {**handoff, "closed_by": "evaluation-harness", "close_reason": "verified fix recorded",
                       "resolution": record}
        audit(s, "evaluation-harness", "resolution_recorded", iid, steps=len(record["steps"]),
              signature=record["signature"], cluster_changed=False)
    print(json.dumps({"incident": iid, "resolution": record}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    from . import evalguard

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    lock = sub.add_parser("lock", allow_abbrev=False)
    lock.add_argument("--ttl", type=int)
    unlock = sub.add_parser("unlock", allow_abbrev=False)
    lock_status = sub.add_parser("lock-status", allow_abbrev=False)
    renew = sub.add_parser("lock-renew", allow_abbrev=False)
    renew.add_argument("--ttl", type=int, required=True)
    for parser in (lock, unlock, lock_status, renew):
        parser.add_argument("--namespace", choices=("dev",))
        parser.add_argument("--workload")
        parser.add_argument("--owner")
        if parser is not lock:
            parser.add_argument("--token-stdin", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--case", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--signal", required=True, choices=sorted(SIGNAL))
    r.add_argument("--arm", required=True, help="JSON: {arm_id,strategy,roles:{role:{model,effort}}}")
    r.add_argument("--wait", type=int, default=0)
    rv = sub.add_parser("resolve", allow_abbrev=False,
                        help="record the verified fix of an evaluation hand-off (the harness acts as the operator)")
    rv.add_argument("--incident", type=int, required=True)
    rv.add_argument("--cause", default="")
    rv.add_argument("--step", action="append", required=True)
    rv.add_argument("--verification", default="")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from . import db
    from .config import get_app_config, get_settings
    if a.cmd in ("lock", "unlock", "lock-status", "lock-renew"):
        scope = (a.namespace, a.workload, a.owner)
        scoped = a.cmd in ("lock-status", "lock-renew") or getattr(a, "token_stdin", False) or any(value is not None for value in scope)
        if not scoped:
            db.init_db(get_settings().sqlalchemy_url())
            if a.cmd == "lock":
                evalguard.set_lock("evaluation", 1800 if a.ttl is None else a.ttl)
            else:
                evalguard.clear_lock()
            print(f"evaluation lock {a.cmd}ed")
            return 0
        if any(value is None for value in scope) or (a.cmd != "lock" and not a.token_stdin):
            p.error("scoped commands require --namespace, --workload, --owner, and --token-stdin for status and release")
        ttl = getattr(a, "ttl", None)
        try:
            evalguard.validate_workload_lease(*scope, ttl)
        except ValueError as exc:
            p.error(str(exc))
        if a.cmd == "lock" and not get_app_config().is_active_environment(a.namespace):
            p.error("the dev namespace is not an active monitoring target")
        token = None if a.cmd == "lock" else sys.stdin.read(128).strip()
        db.init_db(get_settings().sqlalchemy_url())
        if a.cmd == "lock":
            try:
                receipt = evalguard.acquire_workload_lease(*scope, evalguard.WORKLOAD_LEASE_DEFAULT_TTL if ttl is None else ttl)
            except evalguard.WorkloadLeaseConflict as exc:
                print(json.dumps({"error": str(exc)}))
                return 1
            print(json.dumps(receipt))
            return 0
        if a.cmd == "lock-renew":
            expires = evalguard.renew_workload_lease(*scope, token, ttl)
            print(json.dumps({"expires_at": expires}))
            return 0 if expires else 1
        if a.cmd == "lock-status":
            key = "active"
            accepted = evalguard.workload_lease_owned(*scope, token)
        else:
            key = "released"
            accepted = evalguard.release_workload_lease(*scope, token)
        print(json.dumps({key: accepted}))
        return 0 if accepted else 1
    if a.cmd == "resolve":
        return resolve_eval(a.incident, a.cause, a.step, a.verification)
    arm = json.loads(a.arm)
    try:
        res = run_one(a.case, a.target, a.signal, arm, a.wait)
    except Exception as exc:  # noqa: BLE001 - reported to the harness as a failed run, never as a result
        print(RESULT_PREFIX + json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}))
        return 1
    print(RESULT_PREFIX + json.dumps(res, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
