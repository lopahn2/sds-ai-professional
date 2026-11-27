"""Capture anonymous benchmark observations from existing loopback service test fixtures."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CASES = {"catalog-05": "catalog", "catalog-18": "catalog", "notification-10": "notification"}


def sha(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def collect_runtime(case: str, service: str, http_case) -> tuple[dict, dict]:
    source_dir = ROOT / "services" / service
    runtime_source = (source_dir / "runtime.py").read_text(encoding="utf-8")
    controls = http_case()
    controls.setUp()
    runtime = controls.server.runtime
    business_path = runtime.business.api_prefix
    if service == "notification":
        runtime.worker_wait = 0.2  # keep the same exhaustion behavior inside HttpCase's request timeout

    def sample(path: str) -> dict:
        started = monotonic()
        status, body, _ = controls.request(path)
        result = {"status": status, "elapsed_ms": round((monotonic() - started) * 1_000)}
        if isinstance(body, dict) and body.get("exception") == "WorkerPoolExhausted":
            result["exception"] = "WorkerPoolExhausted"
        return result

    sections = {}
    try:
        for phase in ("before", "during", "after"):
            if phase == "during" and service == "catalog":
                assert controls.chaos("liveness_fail")[0] == 200
            elif phase == "during":
                assert controls.chaos("worker_exhaustion")[0] == 200
                deadline = monotonic() + 2
                while runtime.slots._value and monotonic() < deadline:
                    sleep(0.01)
                if runtime.slots._value:
                    raise RuntimeError("the fixture did not exhaust its worker pool")
            elif phase == "after":
                holders = tuple(runtime.holders)
                assert controls.request("/api/chaos", "DELETE")[0] == 200
                for holder in holders:
                    holder.join(2)
            sections[phase] = {"business": sample(business_path), "liveness": sample("/healthz/live"),
                               "readiness": sample("/healthz/ready")}
    finally:
        controls.tearDown()

    tree = ast.parse(runtime_source)
    failed_signal = "liveness" if service == "catalog" else "business"
    for phase, observations in sections.items():
        for signal, result in observations.items():
            expected_status = 503 if phase == "during" and signal == failed_signal else 200
            if result["status"] != expected_status:
                raise RuntimeError(f"fixture did not reach the expected {phase} {signal} state")

    if service == "catalog":
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                     and "'/healthz/live'" in ast.unparse(node.test))
        excerpt = ast.get_source_segment(runtime_source, block)
    else:
        excerpt = "\n\n".join(ast.get_source_segment(runtime_source, node) for node in ast.walk(tree)
                               if isinstance(node, ast.FunctionDef) and node.name in {"_hold_slot", "acquire_slot"})
    if not excerpt:
        raise RuntimeError("fixture source excerpt is empty")

    evidence = {"target": "test application A", "observations": [
        {"ref": "E1", "source": "Fixture HTTP observations", "observation": sections["during"]},
        {"ref": "E2", "source": "Public fixture source fragment", "observation": {"path": "runtime.py", "source": excerpt}},
    ]}
    receipt = {"case": case, "scope": "isolated loopback fixture; no cluster changes",
               "source_sha256": sha(runtime_source), "sections": sections}
    if service == "notification":
        receipt["fixture_worker_wait_s"] = runtime.worker_wait
    return evidence, receipt


def collect_catalog_startup(http_case) -> tuple[dict, dict]:
    service = ROOT / "services" / "catalog"
    app_source = (service / "app.py").read_text(encoding="utf-8")
    settings = next(n for n in ast.walk(ast.parse(app_source)) if isinstance(n, ast.FunctionDef)
                    and n.name == "load_settings")

    def ready() -> int:
        fixture = http_case()
        fixture.setUp()
        try:
            return fixture.request("/healthz/ready")[0]
        finally:
            fixture.tearDown()

    before = ready()
    # Specify the complete environment for the child so it cannot load the caller's service credentials.
    environment = {"SELF_TRAFFIC": "0", "APP_ENV": "dev", "PYTHONDONTWRITEBYTECODE": "1",
                   "RELEASE_FAULTS": json.dumps({"faults": {"startup_config_error": {}}})}
    program = "import app; from runtime import serve; serve(lambda: app.create_server('127.0.0.1', 0, background=False))"
    result = subprocess.run([sys.executable, "-c", program], cwd=service, env=environment,
                            text=True, capture_output=True, timeout=3)
    after = ready()
    if before != 200 or after != 200 or result.returncode != 1 or "in load_settings" not in result.stdout:
        raise RuntimeError("startup failure and baseline restart were not observed")
    exception = "KeyError" if "KeyError:" in result.stdout else None
    if exception is None:
        raise RuntimeError("expected startup exception type was missing")
    observed = {"process": {"exit_code": result.returncode, "exception": exception, "frame": "load_settings"}}
    evidence = {"target": "test application A", "observations": [
        {"ref": "E1", "source": "Fixture process observations", "observation": observed},
        {"ref": "E2", "source": "Public fixture source, normalized without comments", "observation": {
            "path": "app.py", "source": ast.unparse(settings),
        }},
    ]}
    receipt = {"case": "catalog-18", "scope": "local release environment fixture; no image deployed",
               "source_sha256": sha(app_source), "sections": {"before": {"ready_status": before},
               "during": observed, "after": {"ready_status": after}}}
    return evidence, receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    service = CASES[args.case]
    sys.path.insert(0, str(ROOT / "services" / service / "tests"))
    from test_contract import HttpCase

    with patch.dict(os.environ, {"SELF_TRAFFIC": "0"}, clear=True):
        if args.case == "catalog-18":
            evidence, receipt = collect_catalog_startup(HttpCase)
        else:
            evidence, receipt = collect_runtime(args.case, service, HttpCase)
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, data in (("anonymized-observation.json", evidence), ("application-observed.json", receipt)):
        with (args.output / filename).open("x", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
