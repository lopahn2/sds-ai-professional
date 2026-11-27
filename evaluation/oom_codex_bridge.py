#!/usr/bin/env python3
"""Relay isolated evaluation prompts to the pinned, authenticated host Codex CLI."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import select
import socket
import subprocess
import sys


SOCKET_PATH = "/tmp/liveprobe-evaluation-codex.sock"
MAX_PACKET = 1024 * 1024
MODELS = {"gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-astra"}
EFFORTS = {None, "minimal", "low", "medium", "high", "xhigh"}
MAX_CALLS = 320


def read_packet(stream):
    raw = stream.readline(MAX_PACKET + 1)
    if not raw:
        raise EOFError
    if len(raw) > MAX_PACKET or not raw.endswith(b"\n"):
        raise ValueError("invalid relay frame")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid relay payload")
    return value


def write_packet(stream, message):
    raw = json.dumps(message, ensure_ascii=False).encode() + b"\n"
    if len(raw) > MAX_PACKET:
        raise ValueError("oversize relay payload")
    stream.write(raw)
    stream.flush()


def reject_external_references(schema):
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"$id", "$ref", "$dynamicRef", "$recursiveRef"} and (
                not isinstance(value, str) or not value.startswith("#")
            ):
                raise ValueError("nonlocal schema reference")
            reject_external_references(value)
    elif isinstance(schema, list):
        for value in schema:
            reject_external_references(value)


def complete_on_host(provider, request):
    from app.llm.base import LLMError

    if set(request) != {"model", "effort", "schema", "prompt"}:
        raise ValueError("unrecognized relay fields")
    model, effort, schema, prompt = (request[key] for key in ("model", "effort", "schema", "prompt"))
    if (not isinstance(model, str) or model not in MODELS
            or (effort is not None and not isinstance(effort, str)) or effort not in EFFORTS
            or not isinstance(schema, dict) or not isinstance(prompt, str) or not prompt):
        raise ValueError("invalid model request")
    reject_external_references(schema)
    try:
        result = provider.complete("isolated_evaluation", prompt, schema, model, effort, 120)
    except LLMError as error:
        messages = {"auth": "authentication failed", "quota": "usage limit reached"}
        return {"error": messages.get(error.kind, "provider execution failed"), "usage": asdict(error.usage)}
    return {"output": result.data, "usage": asdict(result.usage)}


def host_loop(provider, incoming, outgoing):
    for _ in range(MAX_CALLS):
        try:
            request = read_packet(incoming)
        except EOFError:
            return
        try:
            reply = complete_on_host(provider, request)
        except (ValueError, RecursionError):
            reply = {"error": "relay request rejected"}
        write_packet(outgoing, reply)


def pod_loop(path=SOCKET_PATH):
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        try:
            Path(path).chmod(0o600)
            server.listen()
            while True:
                available, _, _ = select.select([incoming, server], [], [])
                if incoming in available:
                    return
                connection, _ = server.accept()
                with connection, connection.makefile("rwb") as stream:
                    connection.settimeout(8)
                    try:
                        request = read_packet(stream)
                    except (EOFError, OSError, ValueError):
                        continue
                    write_packet(outgoing, request)
                    try:
                        reply = read_packet(incoming)
                    except EOFError:
                        return
                    try:
                        write_packet(stream, reply)
                    except OSError:
                        continue
        finally:
            Path(path).unlink(missing_ok=True)


def wrapper(args, path=SOCKET_PATH):
    # The container supplies data only. The host builds its own tool-disabled command line.
    model = args[args.index("-m") + 1]
    effort = next((arg.split("=", 1)[1] for arg in args if arg.startswith("model_reasoning_effort=")), None)
    if args[-1] != "-":
        raise ValueError("evaluation wrapper requires stdin")
    schema_path = Path(args[args.index("--output-schema") + 1])
    with schema_path.open(encoding="utf-8") as schema_file:
        schema = json.loads(schema_file.read(MAX_PACKET + 1))
    request = {"model": model, "effort": effort, "schema": schema, "prompt": sys.stdin.read(MAX_PACKET + 1)}
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(130)
        client.connect(path)
        with client.makefile("rwb") as stream:
            write_packet(stream, request)
            reply = read_packet(stream)
    if "error" in reply:
        if "usage" in reply:
            print(json.dumps({"type": "turn.completed", "usage": reply["usage"]}))
        print(json.dumps({"type": "turn.failed", "error": {"message": reply["error"]}}))
        return 1
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(reply["output"])}}))
    print(json.dumps({"type": "turn.completed", "usage": reply["usage"]}))
    return 0


def main():
    if sys.argv[1:2] == ["exec"]:
        try:
            return wrapper(sys.argv[2:])
        except (OSError, ValueError, IndexError, EOFError):
            print("evaluation Codex transport unavailable", file=sys.stderr)
            return 1
    if sys.argv[1:] == ["--pod-server"]:
        pod_loop()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="verified kubectl exec command after --")
    args = parser.parse_args()
    if args.command[:1] != ["--"] or not args.command[1:]:
        parser.error("supply the evaluation pod command after --")
    from evaluation.live_repair import host_codex_provider

    provider = host_codex_provider(args.codex, "")
    with subprocess.Popen(args.command[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as worker:
        try:
            host_loop(provider, worker.stdout, worker.stdin)
        finally:
            worker.stdin.close()
            worker.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
