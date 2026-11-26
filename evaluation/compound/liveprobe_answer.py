"""Read a LiveProbe hand-off (GET only) into the answer shape the single-model baseline produces, then screen it.

    python -m evaluation.compound.liveprobe_answer --scenario C3 --incident 1722 --out answer.json

The screening is grade.py's rule pass. As with the baseline, the decisive check is executing the mapped
primitives in the reproduction and running the soak (bench.py --remedy ...).
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from .grade import grade

API = "http://127.0.0.1:4175"


def fetch(iid: int) -> dict:
    with urllib.request.urlopen(f"{API}/api/incidents/{iid}", timeout=20) as r:  # noqa: S310 - local read-only API
        return json.load(r)


def answer_of(detail: dict) -> dict:
    inc = detail["incident"]
    h = inc.get("handoff") or {}
    causes = [{"cause": c.get("cause") or "", "evidence": " ".join(c.get("evidence_refs") or [])}
              for c in h.get("root_cause_candidates") or []]
    actions = [{"target": inc.get("workload") or "", "change": step, "purpose": ""}
               for step in h.get("recommended_steps") or []]
    return {"root_causes": causes, "actions": actions,
            "verification": [h.get("verification")] if h.get("verification") else [],
            "_status": inc.get("status"), "_summary": h.get("summary") or inc.get("summary"),
            "_signature": h.get("signature"), "_precedent": h.get("precedent")}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", required=True, choices=("C1", "C2", "C3"))
    p.add_argument("--incident", type=int, required=True)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    ans = answer_of(fetch(a.incident))
    result = {"answer": ans}
    report = {"answer": ans, "screening": grade(a.scenario, result)}
    if a.out:
        a.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
