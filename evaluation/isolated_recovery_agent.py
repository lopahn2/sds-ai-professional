"""A fresh, broker-only Codex subagent for one comparison run on macOS."""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
from typing import Callable


MODELS = {"astra": "gpt-6.2-astra", "sol": "gpt-6.2-sol", "terra": "gpt-6.1-terra"}
EFFORT = "xhigh"
TASK = "장애를 복구하라."
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "view_image", "apps", "plugins", "remote_plugin",
    "hooks", "multi_agent", "multi_agent_v2", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "computer_use", "image_generation", "sleep_tool",
    "skill_search", "skill_mcp_dependency_install", "workspace_dependencies",
)
LIMIT = 1_048_576
BUILTIN_ITEMS = frozenset(("commandExecution", "fileChange", "mcpToolCall", "collabAgentToolCall", "webSearch"))
EMPTY_INVENTORY_TOOLS = {"list_mcp_resources": "resources", "list_mcp_resource_templates": "resourceTemplates"}


class Unobserved(RuntimeError):
    """The isolation or model receipt is missing, or the run could not continue."""


def starting_message(error_line: str) -> str:
    if not isinstance(error_line, str) or not 0 < len(error_line) <= 4096 or len(error_line.splitlines()) != 1:
        raise Unobserved("the coordinator must provide one reviewed, redacted error line")
    if not error_line.strip() or any(ord(char) < 32 and char != "\t" for char in error_line):
        raise Unobserved("invalid error line")
    return f"{error_line}\n{TASK}"


def _profile(binary: Path, home: Path, tool_host: Path | None = None) -> str:
    # The parent broker alone can run kubectl, Git or Maven. The model process cannot launch another binary,
    # read the workspace, connect to localhost or write outside its disposable home.
    roots = ("/System", "/usr/lib", "/usr/share", "/etc", "/private/etc", "/dev", str(home))
    reads = " ".join(f"(subpath {json.dumps(root)})" for root in roots)
    # macOS resolves through mDNSResponder. Only the socket and its directory chain are exposed.
    resolver = {Path("/var"), Path("/var/run"), Path("/var/run/mDNSResponder"),
                Path("/private/var"), Path("/private/var/run"), Path("/private/var/run/mDNSResponder")}
    trusted_programs = (binary, tool_host) if tool_host else (binary,)
    program_rules = " ".join(f"(literal {json.dumps(str(path))})" for path in trusted_programs)
    filesystem_entries = sorted({parent for path in trusted_programs for parent in path.parents} | set(home.parents) | resolver)
    literals = " ".join(f"(literal {json.dumps(str(path))})" for path in filesystem_entries)
    return f'''(version 1)
(allow default)
(deny process-exec)
(allow process-exec {program_rules})
(deny file-read*)
(allow file-read* {reads} {literals} {program_rules})
(deny file-write*)
(allow file-write* (subpath {json.dumps(str(home))}) (literal "/dev/null"))
(deny network-outbound (remote ip "localhost:*"))
'''


class _Session:
    def __init__(self, process: subprocess.Popen, deadline: float):
        self.process = process
        self.deadline = deadline
        self.sequence = 0
        self.incoming = Queue(maxsize=128)
        Thread(target=self._read, daemon=True).start()

    def _read(self):
        stdout = self.process.stdout
        assert stdout is not None
        try:
            while line := stdout.readline(LIMIT + 1):
                self.incoming.put(line if len(line) <= LIMIT else None)
                if len(line) > LIMIT:
                    return
        except ValueError:
            if not stdout.closed:
                raise
        self.incoming.put(None)

    def send(self, packet: dict):
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(packet, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict:
        try:
            line = self.incoming.get(timeout=max(0, self.deadline - monotonic()))
        except Empty:
            raise Unobserved("agent time budget expired") from None
        if line is None:
            raise Unobserved("agent closed the protocol or exceeded the packet bound")
        try:
            data = json.loads(line)
        except ValueError:
            raise Unobserved("invalid agent protocol packet") from None
        if not isinstance(data, dict):
            raise Unobserved("invalid agent protocol packet")
        return data

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        while True:
            data = self.receive()
            if data.get("id") != self.sequence and "id" not in data:
                continue
            if data.get("id") != self.sequence or not isinstance(data.get("result"), dict):
                raise Unobserved(f"app-server rejected {method}")
            return data["result"]


def _registered_model(session: _Session, model: str):
    cursor = None
    for _ in range(8):
        params = {"limit": 100, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        result = session.request("model/list", params)
        for row in result.get("data", []):
            if row.get("model") == model and any(
                effort.get("reasoningEffort") == EFFORT for effort in row.get("supportedReasoningEfforts", [])
            ):
                return
        cursor = result.get("nextCursor")
        if not cursor:
            break
    raise Unobserved(f"app-server has no verified {EFFORT} setting for {model}")


def _assert_no_mcp_servers(session, thread):
    status = session.request("mcpServerStatus/list", {"threadId": thread, "limit": 100})
    if status.get("data") != [] or status.get("nextCursor") is not None:
        raise Unobserved("the standalone thread has a configured MCP server")


def _empty_inventory_result(item, key):
    result = item.get("result") or {}
    expected = {key: []}
    if (item.get("status") != "completed" or item.get("error") is not None or not isinstance(result, dict)
            or set(result) - {"content", "structuredContent", "_meta"}
            or result.get("structuredContent") not in (None, expected) or result.get("_meta") not in (None, {})):
        return False
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return False
    entry = content[0]
    if not isinstance(entry, dict) or set(entry) != {"type", "text"} or entry["type"] != "text":
        return False
    try:
        return json.loads(entry["text"]) == expected
    except (TypeError, ValueError):
        return False


def _turn(session: _Session, thread: str, turn: str, tools: dict, broker: Callable, *, empty_mcp_inventory=False) -> dict:
    calls = set()
    pending_inventory = {}
    empty_checks = 0
    usage = None
    while True:
        data = session.receive()
        method, params = data.get("method"), data.get("params") or {}
        if "id" in data:
            if (method != "item/tool/call" or params.get("threadId") != thread or params.get("turnId") != turn
                    or params.get("tool") not in tools or params.get("namespace") is not None
                    or not isinstance(params.get("arguments"), dict) or not isinstance(params.get("callId"), str)
                    or params["callId"] in calls or len(calls) >= 32):
                raise Unobserved("unexpected or out-of-scope agent tool call")
            calls.add(params["callId"])
            result = json.dumps(broker(params["tool"], params["arguments"]), ensure_ascii=False)
            if len(result.encode()) > 65_536:
                raise Unobserved("observation exceeded broker limit")
            session.send({"id": data["id"], "result": {"success": True,
                          "contentItems": [{"type": "inputText", "text": result}]}})
        elif method == "thread/tokenUsage/updated" and (params.get("threadId"), params.get("turnId")) == (thread, turn):
            usage = params.get("tokenUsage", {}).get("total")
        elif method == "turn/completed" and params.get("threadId") == thread and params.get("turn", {}).get("id") == turn:
            if pending_inventory:
                raise Unobserved("an internal MCP inventory check did not return an empty receipt")
            finished = params["turn"]
            result = {"status": finished["status"], "tool_calls": len(calls), "usage": usage}
            if empty_mcp_inventory:
                result["empty_mcp_list_calls"] = empty_checks
            if finished.get("error"):
                result["provider_error_kind"] = provider_error_kind(finished["error"])
            return result
        elif method in ("model/rerouted", "item/mcpToolCall/progress"):
            raise Unobserved("model was rerouted or attempted to use MCP")
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            kind = item.get("type")
            name, identity = item.get("tool"), item.get("id")
            if (kind == "mcpToolCall" and empty_mcp_inventory and item.get("server") == "codex"
                    and isinstance(name, str) and name in EMPTY_INVENTORY_TOOLS and item.get("arguments") == {}
                    and isinstance(identity, str) and (params.get("threadId"), params.get("turnId")) == (thread, turn)):
                if method == "item/started" and identity not in pending_inventory and len(pending_inventory)+empty_checks < 8:
                    pending_inventory[identity] = name
                    continue
                if method == "item/completed" and pending_inventory.pop(identity, None) == name:
                    if _empty_inventory_result(item, EMPTY_INVENTORY_TOOLS[name]):
                        empty_checks += 1
                        continue
                    raise Unobserved("the internal MCP inventory check did not return an empty result")
            if isinstance(kind, str) and kind in BUILTIN_ITEMS:
                raise Unobserved(f"agent attempted to use a built-in capability outside the scoped broker ({kind})")


def provider_error_kind(error: dict) -> str:
    """Retain recognized diagnostics without persisting provider text, URLs or credentials."""
    message = " ".join(str(error.get(key) or "") for key in ("message", "additionalDetails", "codexErrorInfo")).lower()
    patterns = (
        ("access_denied", r"operation not permitted|permission denied"),
        ("authentication", r"\b401\b|\b403\b|unauthorized|authentication|token refresh|oauth"),
        ("usage_limit", r"\b429\b|rate.limit|usage limit|quota"),
        ("certificate", r"certificate|tls handshake"),
        ("connection", r"network|stream disconnected|connection|error sending request|dns|transport|workspace routing discovery failed"),
        ("model", r"unsupported model|model not (?:found|supported|available)|reasoning effort"),
    )
    return next((kind for kind, pattern in patterns if re.search(pattern, message)), "unclassified")


def run_agent(*, binary: Path, auth_file: Path, arm: str, error_line: str, tools: list[dict],
              broker: Callable[[str, dict], dict], deadline: float) -> dict:
    """Start a new agent with no chat, memory, skills, repo, kubeconfig or LiveProbe credentials."""
    message = starting_message(error_line)
    binary = binary.resolve(strict=True)
    tool_host = binary.with_name("codex-code-mode-host")
    if not tool_host.is_file() or tool_host.is_symlink() or not os.access(tool_host, os.X_OK):
        raise Unobserved("the pinned Codex distribution's tool host must be installed next to the CLI")
    source = auth_file.lstat()
    if (not stat.S_ISREG(source.st_mode) or source.st_uid != os.getuid() or source.st_mode & 0o077):
        raise Unobserved("an owner-only standalone CLI authentication file is required")
    allowed = {tool["name"]: {**tool, "type": "function", "deferLoading": False} for tool in tools}
    if len(allowed) != len(tools) or not allowed:
        raise Unobserved("broker tools must be named and unique")
    model = MODELS[arm]
    started = monotonic()
    if started >= deadline:
        raise Unobserved("agent time budget expired")
    with TemporaryDirectory(prefix="lp-standalone-") as directory:
        home = Path(directory).resolve()
        codex_home, workspace = home / "codex", home / "workspace"
        codex_home.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        (home / "tmp").mkdir(mode=0o700)
        with auth_file.open("rb") as src, open(codex_home / "auth.json", "xb", opener=_private_file) as dst:
            shutil.copyfileobj(src, dst)
        config = ["-c", f'model_reasoning_effort="{EFFORT}"', "-c", 'cli_auth_credentials_store="file"',
                  "-c", 'web_search="disabled"', "-c", "mcp_servers={}",
                  "-c", "features.memory_tool=false", "-c", "features.memories=false"]
        for feature in DISABLED_FEATURES:
            config.extend(("--disable", feature))
        env = {"PATH": f"{binary.parent}:/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(codex_home),
               "TMPDIR": str(home / "tmp"), "LANG": "en_US.UTF-8"}
        args = ["/usr/bin/sandbox-exec", "-p", _profile(binary, home, tool_host), str(binary), "app-server", "--stdio", *config]
        with subprocess.Popen(args, cwd=workspace, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True) as process:
            try:
                session = _Session(process, deadline)
                session.request("initialize", {"clientInfo": {"name": "standalone-evaluation", "version": "1"},
                                                "capabilities": {"experimentalApi": True}})
                session.send({"method": "initialized"})
                _registered_model(session, model)
                info = session.request("thread/start", {"model": model, "cwd": str(workspace),
                    "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                    "dynamicTools": list(allowed.values())})
                thread = info.get("thread") or {}
                if (info.get("model"), info.get("reasoningEffort"), info.get("instructionSources"),
                    info.get("modelProvider"), thread.get("ephemeral"), thread.get("turns"), info.get("cwd")) != (
                        model, EFFORT, [], "openai", True, [], str(workspace)):
                    raise Unobserved("app-server did not attest a fresh thread with the requested model and effort")
                _assert_no_mcp_servers(session, thread["id"])
                turn = session.request("turn/start", {"threadId": thread["id"], "model": model, "effort": EFFORT,
                    "summary": "none", "input": [{"type": "text", "text": message, "text_elements": []}]})["turn"]
                result = _turn(session, thread["id"], turn["id"], allowed, broker, empty_mcp_inventory=True)
                _assert_no_mcp_servers(session, thread["id"])
                return {**result, "model": model, "effort": EFFORT, "ephemeral": True,
                        "connected_mcp_servers": 0,
                        "duration_ms": round((monotonic() - started) * 1000)}
            finally:
                if process.poll() is None:
                    process.kill()


def _private_file(path, flags):
    return os.open(path, flags, 0o600)
