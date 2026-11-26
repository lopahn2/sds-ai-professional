"""First-pass rule grading of a single-model answer against the answer key, plus primitive mapping.

    python -m evaluation.compound.grade --scenario C1 --result result.json

Rules are deliberately literal (Korean and English cues). The verdict is a screening aid: the decisive check is
executing the mapped primitives in the reproduction and running the soak (bench.py --remedy ...).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

KEYS = json.loads((Path(__file__).resolve().parent / "answer_keys.json").read_text())

CAUSE = {
    "C1": [r"partner|파트너", r"hold|held|보류|동결|frozen|on hold", r"leak|누수|반환되지|반납|close|닫히지|닫지|release"],
    "C2": [r"stock-db-0", r"label|라벨|레이블|selector|셀렉터|endpoint|엔드포인트", r"service|서비스|routing|라우팅"],
    "C3": [r"cpu", r"throttl|스로틀|limit|제한|한도|quota", r"limitrange|default|기본값|기본 한도|new pod|새 파드|resources"],
}
PRIMITIVES = {
    "C1": {
        "probe_liveness_group": r"liveness|livenessprobe|/actuator/health/liveness",
        "pool_increase": r"maximum-?pool-?size|maximumpoolsize|pool.{0,20}(늘|증가|increase|raise)|풀.{0,20}(늘|증가|확대)",
        "restart_stock": r"재시작|restart|rollout restart",
        "leak_detection": r"leak-?detection|leakdetectionthreshold",
        "db_idle_tx_timeout": r"idle_in_transaction|idle in transaction",
        "patched_ledger": r"(partner|파트너).{0,80}(finally|try-with-resources|close|rollback|반환|닫)",
        "disable_partner_ledger": r"(partner|파트너).{0,60}(비활성|disable|중단|차단|끄|feature flag|off)",
        "release_partner_holds": r"hold.{0,40}(해제|open|release)|status.{0,20}open",
    },
    "C2": {
        "relabel_old_primary": r"stock-db-0.{0,80}(role=replica|label|라벨|레이블|제외|remove|endpoint)",
        "restart_stock": r"재시작|restart|rollout restart|재배포",
        "pool_max_lifetime_short": r"max-?lifetime|maxlifetime",
        "unfence_old_primary": r"default_transaction_read_only.{0,40}(off|reset|해제)|stock-db-0.{0,40}(쓰기 가능|writable|read-?write)",
        "pool_increase": r"maximum-?pool-?size|풀.{0,20}(늘|증가)",
        "allow_app_on_new_primary": r"pg_hba|hba\.conf|no pg_hba",
    },
    "C3": {
        "explicit_cpu_limit": r"(resources|limits?|requests?).{0,60}cpu|cpu.{0,40}(limit|request|한도|요청)",
        "rollback_stock": r"rollback|롤백|rollout undo|되돌",
        "restart_stock": r"재시작|restart",
        "scale_stock": r"replicas?\s*[:=]?\s*[2-9]|scale|스케일|레플리카.{0,20}(늘|증가)|hpa",
        "pool_increase": r"maximum-?pool-?size|풀.{0,20}(늘|증가)",
        "order_retries": r"retr(y|ies)|재시도|backoff|백오프",
        "order_timeout": r"read-?timeout|timeout.{0,20}(늘|증가|increase)|타임아웃.{0,20}(늘|증가)",
    },
}


def text_of(answer: dict | None, raw: str | None) -> tuple[str, str]:
    if not answer:
        return raw or "", raw or ""
    causes = " ".join(f"{c.get('cause', '')} {c.get('evidence', '')}" for c in answer.get("root_causes", []))
    actions = " ".join(f"{a.get('target', '')} {a.get('change', '')} {a.get('purpose', '')}" for a in answer.get("actions", []))
    return causes, actions


def grade(scenario: str, result: dict) -> dict:
    causes, actions = text_of(result.get("answer"), result.get("answer_raw"))
    lower_causes, lower_actions = causes.lower(), actions.lower()
    cause_hits = [bool(re.search(p, lower_causes)) for p in CAUSE[scenario]]
    proposed = sorted(k for k, p in PRIMITIVES[scenario].items() if re.search(p, lower_actions))
    durable = {"C1": [{"patched_ledger"}, {"disable_partner_ledger"}],
               "C2": [{"relabel_old_primary", "allow_app_on_new_primary"}],
               "C3": [{"explicit_cpu_limit", "order_retries"}]}[scenario]
    cause_ok = all(cause_hits)
    fix_ok = any(combo <= set(proposed) for combo in durable)
    return {"scenario": scenario, "cause_cues": cause_hits, "cause_identified": cause_ok,
            "proposed_primitives": proposed, "durable_primitive_proposed": fix_ok,
            "screening_verdict": "pass" if cause_ok and fix_ok else "fail",
            "pass_rule": KEYS[scenario]["pass_rule"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("C1", "C2", "C3"))
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(grade(args.scenario, json.loads(args.result.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
