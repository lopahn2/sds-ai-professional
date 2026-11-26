"""Reproduce a compound incident, write its public log bundle, optionally apply remedies and soak.

    python -m evaluation.compound.bench --scenario C1 --jar shopfront-1.4.2.jar --out DIR \
        [--observe 540] [--remedy a,b] [--soak 420]

Without --remedy only the incident bundle is produced. Each remedy name is one operator action defined by the
scenario; the soak then decides durable recovery. Containers are always removed at the end.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from pathlib import Path

from . import infra as I
from .scenarios import SCENARIOS, Run

MAX_LINES = 700
HEAD_LINES = 120
FORBIDDEN = re.compile(r"\b(?:lpcb\S*|liveprobe|chaos|bench|answer[ _]key|injected|scenario)\b", re.I)


def render(bundle: dict, window: tuple[str, str]) -> str:
    """kubectl-shaped text an operator would collect for the incident window."""
    out = [f"# Incident log bundle — namespace {bundle['namespace']}",
           f"# window {window[0]} .. {window[1]} (UTC)", "",
           f"$ kubectl get events -n {bundle['namespace']} --sort-by=.lastTimestamp",
           "FIRST SEEN            LAST SEEN             COUNT TYPE     REASON             OBJECT                                   MESSAGE"]
    for e in sorted(bundle["events"], key=lambda e: e["last"]):
        out.append(f"{e['first']:<21} {e['last']:<21} {e['count']:>5} {e['type']:<8} {e['reason']:<18} "
                   f"{e['object']:<40} {e['message']}")
    for pod, text in bundle["pods"].items():
        lines = [line for line in text.splitlines() if window[0][:19] <= line[:19] <= window[1][:19]]
        if len(lines) > MAX_LINES:
            omitted = len(lines) - MAX_LINES
            lines = lines[:HEAD_LINES] + [f"... ({omitted} lines omitted) ..."] + lines[-(MAX_LINES - HEAD_LINES):]
        out += ["", f"$ kubectl logs -n {bundle['namespace']} {pod} --timestamps --all-containers "
                    "(current and previous container instances)", *lines]
    for pod, lines in bundle["jobs"].items():
        out += ["", f"$ kubectl logs -n {bundle['namespace']} {pod}", *lines]
    text = "\n".join(out) + "\n"
    leaked = sorted(set(m.group(0).lower() for m in FORBIDDEN.finditer(text)))
    if leaked:
        raise ValueError(f"bundle would reveal the test harness: {leaked}")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--observe", type=float, default=540)
    parser.add_argument("--remedy", default="")
    parser.add_argument("--soak", type=float, default=420)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    scenario = SCENARIOS[args.scenario]()
    run = Run(args.scenario, args.out, I.Events(scenario.namespace))
    record = {"scenario": args.scenario, "remedy": [r for r in args.remedy.split(",") if r], "started": I.stamp()}
    I.use_network(f"lpcb-{args.scenario.lower()}")
    try:
        I.cleanup(f"lpcb-{args.scenario.lower()}-")
        I.network()
        with tempfile.TemporaryDirectory(prefix="shopfront-image-") as context:
            I.build_image(args.jar, Path(context))
        scenario.setup(run)
        scenario.inject(run, args.observe)
        bundle = scenario.bundle(run)
        window = (run.marks["baseline_start"]["utc"], run.marks["observe_end"]["utc"])
        I.write_json(args.out / "bundle.json", bundle)
        (args.out / "bundle.txt").write_text(render(bundle, window))
        t0 = next(v for k, v in run.marks.items() if k.startswith("t0_"))
        record["incident"] = run.load.window(t0["epoch"])
        record["before_t0"] = run.load.window(run.marks["baseline_start"]["epoch"], t0["epoch"])
        record["restarts_during_incident"] = run.prober.restarts if run.prober else None
        if record["remedy"]:
            actions = scenario.remedies(run)
            run.mark("remedy_start")
            for name in record["remedy"]:
                actions[name]()
                run.mark(f"remedy_{name}")
            run.mark("soak_start")
            record["verify"] = scenario.verify(run, args.soak)
            run.mark("soak_end")
    finally:
        run.stop.set()
        if run.prober:
            run.prober.stop()
        if run.load:
            run.load.stop()
        record["marks"] = run.marks
        record["events"] = run.events.dump()
        for key, db in run.dbs.items():
            (args.out / f"db-{key}.log").write_text(I.all_logs(db.name))
        for pod, text in run.pod_logs.items():
            (args.out / f"pod-{pod.replace('~', '.')}.retired.log").write_text(text)
        for app in {id(a): a for a in [*run.apps.values(), *run.replicas]}.values():
            (args.out / f"pod-{app.hostname}.log").write_text(I.all_logs(app.name))
        record["finished"] = I.stamp()
        I.write_json(args.out / "run.json", record)
        I.cleanup(f"lpcb-{args.scenario.lower()}-")
    print(json.dumps({k: record.get(k) for k in ("scenario", "remedy", "incident", "restarts_during_incident",
                                                   "verify")}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
