"""순수 시험의 실제 종료코드·로그·코드 지문을 전용 경로에 보존한다."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "docs/evidence/application/lanes/evaluation"


def main():
    label, *targets = sys.argv[1:]
    if not re.fullmatch(r"[a-z0-9_-]+", label) or not targets:
        raise SystemExit("증거 이름과 시험 대상을 지정해야 한다")
    command = [sys.executable, "-B", "-m", "unittest", "-v", *targets]
    scratch = Path.home() / ".hermes/cache/scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "liveprobe:.",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "TMPDIR": str(scratch)}
    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True)
    text = result.stdout + result.stderr
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    log = EVIDENCE / f"{label}.log"
    log.write_text(text, encoding="utf-8")
    files = sorted(set(REPO.glob("evaluation/**/*.py")) | set(REPO.glob("evaluation/metrics/snapshots/*.json"))
                   | set(REPO.glob("liveprobe/tests/test_application_evaluation_*.py"))
                   | {REPO / "liveprobe/app/evalrun.py"})
    sha = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in files if p.is_file() and not p.is_symlink()}
    matched = re.search(r"Ran (\d+) tests?", text)
    receipt = {"command": "TMPDIR=" + shlex.quote(str(scratch)) + " PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=liveprobe:. "
                          + shlex.join(command), "cwd": str(REPO), "started_at": started,
               "finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": result.returncode,
               "tests_run": int(matched.group(1)) if matched else None, "tmpdir": str(scratch),
               "result": text.strip().splitlines()[-1] if text.strip() else "출력 없음",
               "scope": "DB·네트워크·모델 호출 없는 명시적 순수 시험", "log": str(log.relative_to(REPO)),
               "source_sha256": sha,
               "source_set_sha256": hashlib.sha256(json.dumps(sha, sort_keys=True).encode()).hexdigest()}
    (EVIDENCE / f"{label}.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text)
    print(json.dumps({k: v for k, v in receipt.items() if k != "source_sha256"}, ensure_ascii=False))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
