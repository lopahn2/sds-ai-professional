"""Best-effort public output only. Completed JSON string fields avoid split-secret leaks.

The provider contract and analysis result stay unchanged. Context-local observers isolate
parallel incidents and pool attempts without attaching mutable state to shared providers.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import selectors
import subprocess
import time
import uuid

from ..redact import redact

_sink = ContextVar("analysis_progress_sink", default=None)
_attempt = ContextVar("analysis_progress_attempt", default=None)
FIELDS = {"summary": "요약 원문", "summary_ko": "요약", "rationale": "판단 사유 원문",
          "rationale_ko": "판단 사유"}
MAX_OUTPUT = 12000


@contextmanager
def observe(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def active():
    return _attempt.get()


class PublicOutput:
    """Publish only complete top-level display strings, while the rest is generated."""
    def __init__(self, *, secrets=()):
        self.raw = ""
        self.secrets = [s for s in secrets if s]

    def add(self, text, *, replace=False):
        self.raw = (text if replace else self.raw + text)[:131072]
        decoder, pos, depth, fields = json.JSONDecoder(), 0, 0, {}
        # Lex strings before looking for keys: escaped quotes inside a value cannot
        # masquerade as another top-level field. Incomplete values stay private.
        while pos < len(self.raw):
            ch = self.raw[pos]
            if ch == '"':
                try:
                    value, end = decoder.raw_decode(self.raw, pos)
                except ValueError:
                    break
                rest = self.raw[end:].lstrip()
                if depth == 1 and value in FIELDS and rest.startswith(":"):
                    start = end + len(self.raw[end:]) - len(rest) + 1
                    while start < len(self.raw) and self.raw[start].isspace():
                        start += 1
                    try:
                        body, _ = decoder.raw_decode(self.raw, start)
                    except ValueError:
                        body = None
                    if isinstance(body, str):
                        for secret in self.secrets:
                            body = body.replace(secret, "[REDACTED]")
                        fields[value] = (redact(body) or "")[:MAX_OUTPUT]
                pos = end
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            pos += 1
        text = "\n\n".join(f"{FIELDS[k]}\n{v}" for k, v in fields.items() if v)
        current = active()
        if current and text:
            current.output(text[:MAX_OUTPUT])


class Attempt:
    def __init__(self, sink, role, model, provider):
        self.sink, self.role, self.model = sink, role, model
        self.id = uuid.uuid4().hex
        self.mode = "complete_only"
        self.text = ""
        self.provider = provider

    def send(self, phase, summary):
        try:
            self.sink(summary, {"role": self.role, "model": self.model, "attempt_id": self.id,
                               "provider": self.provider, "phase": phase, "output_mode": self.mode,
                               "text": self.text})
        except Exception:
            pass  # Rendering and event-storage failures must not fail a model call.

    def output(self, text):
        text = (redact(text) or "")[:MAX_OUTPUT]
        if text != self.text:
            self.text = text
            self.send("responding", "공개 답변이 도착했습니다. 아직 최종 결과가 아닙니다.")

    def mode_changed(self, mode):
        self.mode = mode
        message = ("공개 답변 항목이 완성되는 대로 표시합니다." if mode == "fields" else
                   "Codex 실행 이벤트를 수신합니다. 공개 답변은 메시지가 도착하면 표시합니다." if mode == "messages" else
                   "이 제공자는 완료 응답을 한 번에 전달합니다. 응답을 기다리고 있습니다.")
        self.send("waiting", message)


@contextmanager
def provider_attempt(role, model, provider):
    sink = _sink.get()
    if sink is None:
        yield
        return
    attempt = Attempt(sink, role, model, provider)
    token = _attempt.set(attempt)
    attempt.send("started", "모델에 분석을 요청했습니다.")
    try:
        yield
    except BaseException:
        attempt.send("failed", "이 호출이 실패했습니다. 최종 상태와 재시도 기록을 확인하세요.")
        raise
    else:
        attempt.send("succeeded", "응답 수신과 형식 검사가 끝났습니다. 단계의 최종 결과를 확인하세요.")
    finally:
        _attempt.reset(token)


def communicate_jsonl(proc, prompt, timeout, on_line):
    """Drain all pipes concurrently; observe JSONL before process exit (POSIX).

    No shell, new credentials, or persisted raw diagnostics. Retain bounded stdout/
    stderr for the existing classified parser, while draining even excess bytes.
    """
    outputs = {"out": bytearray(), "err": bytearray()}
    pending = bytearray()
    payload, offset = prompt.encode(), 0
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        for pipe, name in ((proc.stdout, "out"), (proc.stderr, "err")):
            os.set_blocking(pipe.fileno(), False)
            sel.register(pipe, selectors.EVENT_READ, name)
        os.set_blocking(proc.stdin.fileno(), False)
        sel.register(proc.stdin, selectors.EVENT_WRITE, "in")
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            for key, _ in sel.select(min(remaining, 0.25)):
                pipe, name = key.fileobj, key.data
                if name == "in":
                    try:
                        offset += os.write(pipe.fileno(), payload[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(payload)
                    if offset >= len(payload):
                        sel.unregister(pipe)
                        pipe.close()
                        proc.stdin = None
                    continue
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    sel.unregister(pipe)
                    continue
                limit = 2097152 if name == "out" else 65536
                outputs[name].extend(chunk[:max(0, limit - len(outputs[name]))])
                if name == "out":
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, pending = pending.partition(b"\n")
                        on_line(line.decode("utf-8", errors="replace"))
                    if len(pending) > 2097152:
                        pending.clear()
        if pending:
            on_line(pending.decode("utf-8", errors="replace"))
        proc.wait(timeout=max(0.001, deadline - time.monotonic()))
    return tuple(outputs[k].decode("utf-8", errors="replace") for k in ("out", "err"))
