"""Single-model baseline: one prompt with the incident logs to Terra 6.1 at xhigh, no tools, no memory.

    python -m evaluation.compound.baseline --bundle bundle.txt --out result.json [--model gpt-6.1-terra]
    python -m evaluation.compound.baseline --bundle bundle.txt --out result.json --model claude-opus-5-5

Claude models run through the Claude Code CLI inside liveprobe-api (`claude -p`, the provider LiveProbe itself
uses), so the subscription token stays in the cluster Secret: a throw-away HOME/CLAUDE_CONFIG_DIR, `--tools ""`,
no MCP, no settings sources, no session persistence, the same prompt and the same answer schema.

The run uses a disposable CODEX_HOME holding only a copy of the subscription credential, an empty working
directory outside any repository, a read-only sandbox and every tool feature disabled, so the answer comes
from the prompt alone. Token usage is read from the CLI event stream and priced with the same table as the
matched dev comparison (evaluation/single_baseline.py, 2026-11-23).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PRICES = {  # USD per 1M tokens: input, cached input, output
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
    "gpt-6.2-sol": (2.00, 0.10, 10.00),
    "gpt-6.2-astra": (8.00, 0.40, 40.00),
    # Claude API list prices 2026-11-27; cache writes add 1.25x (5 min) or 2x (1 h) of the input rate
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 0.10, 10.00),
}
PRICE_SOURCE = "evaluation/single_baseline.py MODELS (price date 2026-11-23); Claude: Claude API pricing 2026-11-27"
KUBE = ["kubectl", "--context", "rancher-desktop", "--request-timeout=0", "-n", "liveprobe-system"]
# Runs inside liveprobe-api. Reads {prompt, schema, model, effort, timeout} on stdin, prints one RESULT line.
POD_SCRIPT = r"""
import json, sys, time
from app.config import get_settings
from app.llm.base import LLMError
from app.llm.claude_cli import ClaudeCLIProvider
req = json.loads(sys.stdin.read())
s = get_settings()
p = ClaudeCLIProvider(s.claude_bin, s.claude_oauth_token, s.claude_oauth_token_file)
raw = {}
parse = p._parse
def spy(stdout, *a, **k):
    try:
        ev = json.loads(stdout.strip().splitlines()[-1])
        raw.update({key: ev.get(key) for key in ("usage", "modelUsage", "num_turns", "duration_ms", "duration_api_ms")})
    except Exception:
        pass
    return parse(stdout, *a, **k)
p._parse = spy
t0 = time.monotonic()
out = {"raw": raw}
try:
    r = p.complete("single_baseline", req["prompt"], req["schema"], req["model"], req["effort"], req["timeout"])
    out.update(answer=r.data, error=None)
except LLMError as e:
    out.update(answer=None, error=f"{e.kind}: {str(e)[:500]}")
out["wall_s"] = round(time.monotonic() - t0, 1)
print("RESULT " + json.dumps(out, ensure_ascii=False))
"""
PROMPT = (
    "다음은 운영 중인 Kubernetes 서비스에서 수집한 장애 로그다. 이 장애를 해결하라.\n"
    "최종 답변에는 근본 원인과 그 근거 로그, 지금 실행할 조치(대상 리소스와 바꿀 값까지, 실행 순서대로), "
    "복구를 확인하는 방법을 적어라.\n\n<logs>\n"
)
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["root_causes", "actions", "verification"],
    "properties": {
        "root_causes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["cause", "evidence"],
            "properties": {"cause": {"type": "string"}, "evidence": {"type": "string"}}}},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["step", "target", "change", "purpose"],
            "properties": {"step": {"type": "integer"}, "target": {"type": "string"},
                           "change": {"type": "string"}, "purpose": {"type": "string"}}}},
        "verification": {"type": "string"},
    },
}
TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}
CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"  # pinned CLI used by the matched runs
DISABLED = (  # every enabled tool-bearing feature of the bundled codex-cli 0.169.1 (`codex features list`)
    "shell_tool", "unified_exec", "shell_snapshot", "view_image", "apps", "plugins", "plugin_sharing",
    "remote_plugin", "hooks", "multi_agent", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "in_app_browser", "computer_use", "image_generation", "goals", "skill_search", "skill_mcp_dependency_install",
    "workspace_dependencies", "tool_suggest", "tool_call_mcp_elicitation", "sleep_tool", "worktrees",
    "in_app_local_automation", "code_mode_host",
)


def run_claude(bundle: str, model: str, effort: str, timeout: float) -> dict:
    prompt = PROMPT + bundle + "</logs>\n"
    started = time.monotonic()
    req = json.dumps({"prompt": prompt, "schema": SCHEMA, "model": model, "effort": effort, "timeout": int(timeout)})
    proc = subprocess.run([*KUBE, "exec", "-i", "deploy/liveprobe-api", "-c", "api", "--", "python", "-c", POD_SCRIPT],
                          input=req, text=True, capture_output=True, timeout=timeout + 120)
    line = next((l for l in reversed(proc.stdout.splitlines()) if l.startswith("RESULT ")), None)
    out = json.loads(line[len("RESULT "):]) if line else {"raw": {}, "answer": None, "error": proc.stderr[-1500:]}
    u = (out.get("raw") or {}).get("usage") or {}
    fresh, read = u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0
    write = u.get("cache_creation_input_tokens") or 0
    split = u.get("cache_creation") or {}
    write_1h = split.get("ephemeral_1h_input_tokens") or 0
    write_5m = write - write_1h
    n_out = u.get("output_tokens") or 0
    rate_in, rate_cached, rate_out = PRICES[model]
    cost = (fresh * rate_in + write_5m * rate_in * 1.25 + write_1h * rate_in * 2 + read * rate_cached
            + n_out * rate_out) / 1_000_000
    usage = {"input_tokens": fresh + write + read, "cached_input_tokens": read, "cache_write_input_tokens": write,
             "cache_write_1h_input_tokens": write_1h, "output_tokens": n_out}
    return {
        "model": model, "effort": effort, "exit_code": proc.returncode if line else (proc.returncode or 1),
        "wall_seconds": round(time.monotonic() - started, 1), "call_seconds": out.get("wall_s"),
        "usage": usage, "estimated_usd": round(cost, 6), "price_source": PRICE_SOURCE,
        "tool_items": [], "notices": [out["error"]] if out.get("error") else [],
        "answer": out.get("answer"), "answer_raw": None,
        "cli": {k: v for k, v in (out.get("raw") or {}).items() if k != "usage"},
        "stderr_tail": proc.stderr[-1500:] if proc.returncode else "",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_chars": len(prompt),
    }


def run(bundle: str, model: str, effort: str, timeout: float) -> dict:
    if model.startswith("claude-"):
        return run_claude(bundle, model, effort, timeout)
    prompt = PROMPT + bundle + "</logs>\n"
    auth = Path.home() / ".codex" / "auth.json"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="single-") as tmp:
        home, work = Path(tmp) / "home", Path(tmp) / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        shutil.copyfile(auth, home / "auth.json")
        os.chmod(home / "auth.json", 0o600)
        schema = Path(tmp) / "schema.json"
        schema.write_text(json.dumps(SCHEMA))
        last = Path(tmp) / "last.json"
        cmd = [CODEX, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
               "--ignore-rules", "--sandbox", "read-only", "-C", str(work), "-m", model,
               "-c", f'model_reasoning_effort="{effort}"', "-c", 'cli_auth_credentials_store="file"', "-c", "tools.web_search=false",
               "--output-schema", str(schema), "-o", str(last)]
        for feature in DISABLED:
            cmd += ["--disable", feature]
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "LANG": "en_US.UTF-8"}
        proc = subprocess.run([*cmd, "-"], input=prompt, text=True, capture_output=True, timeout=timeout, env=env)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
        answer = last.read_text() if last.exists() else None
    usage = {}
    tool_items, notices = [], []
    for event in events:
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
        item = event.get("item") or {}
        if item.get("type") in TOOL_ITEMS:
            tool_items.append(item.get("type"))
        elif item.get("type") == "error":
            notices.append(str(item.get("message"))[:300])
    n_in, n_cached, n_out = (usage.get(k, 0) or 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
    rate_in, rate_cached, rate_out = PRICES[model]
    cost = ((n_in - n_cached) * rate_in + n_cached * rate_cached + n_out * rate_out) / 1_000_000
    try:
        parsed = json.loads(answer) if answer else None
    except json.JSONDecodeError:
        parsed = None
    return {
        "model": model, "effort": effort, "exit_code": proc.returncode, "wall_seconds": round(time.monotonic() - started, 1),
        "usage": usage, "estimated_usd": round(cost, 6), "price_source": PRICE_SOURCE,
        "tool_items": tool_items, "notices": sorted(set(notices)), "answer": parsed, "answer_raw": None if parsed else answer,
        "stderr_tail": proc.stderr[-1500:] if proc.returncode else "",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_chars": len(prompt),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-terra", choices=sorted(PRICES))
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    result = run(args.bundle.read_text(), args.model, args.effort, args.timeout)
    result["bundle"] = str(args.bundle)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: result[k] for k in ("model", "effort", "exit_code", "wall_seconds", "usage", "estimated_usd",
                                              "tool_items")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
