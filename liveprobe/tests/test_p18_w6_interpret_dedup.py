"""W6 재검토 P3: 실제 incident_items → Interpreter.run → 캐시 경계를 외부 연결 없이 시험한다."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace as N

import pytest

from app import db
from app.config import AppConfig
from app.interpret import Interpreter, incident_items, readings, sha
from app.llm.base import LLMError, LLMResult, Usage
from app.models import AgentRun, Evidence, Incident, Interpretation, RoutingDecision, WorkflowRun

T = datetime(2026, 10, 25, tzinfo=timezone.utc)


class Session:
    def __init__(self, lines):
        self.rows = {
            Incident: [N(id=1, handoff=None)], RoutingDecision: [], Interpretation: [],
            Evidence: [N(id=1, source="loki", target="시험", payload={"lines": lines}, error=None, collected_at=T)],
            AgentRun: [N(id=300, role="cause_reasoner", output={"summary": "agent conclusion X"}, created_at=T,
                         attempts=1, model="gpt-6-astra", input_tokens=10, output_tokens=10,
                         estimated_cost_usd=0.001, duration_ms=1)],
            # 사건 전체 예산(보완-10)이 같은 WorkflowRun에 사용량을 기록하므로 전송 계층에 실행 행만 추가한다.
            WorkflowRun: [WorkflowRun(id=7, incident_id=1, status="running", snapshot={})],
        }

    def get(self, cls, key):
        return next((r for r in self.rows.get(cls, []) if r.id == key), None)

    def scalars(self, query):
        return list(self.rows.get(query.column_descriptions[0]["entity"], []))

    def scalar(self, query):
        return next(iter(self.scalars(query)), None)

    def add(self, value):
        rows = self.rows.setdefault(type(value), [])
        value.id = len(rows) + 900
        rows.append(value)

    def flush(self):
        pass


class Provider:
    name = "가짜"

    def __init__(self, mode="all"):
        self.calls = []
        self.mode = mode

    def complete(self, role, prompt, schema, model, effort, timeout):
        payload = json.loads(prompt.split("ITEMS_JSON:", 1)[1])
        self.calls.append(payload)
        if self.mode == "failure":
            raise LLMError("timeout", "가짜 시간 초과")
        selected = payload if self.mode == "all" else payload[:1] if self.mode == "partial" else []
        items = [{"id": p["id"], "ko": f"관측 원문 해석 {p['id']}"} for p in selected]
        items += [{"id": "not_requested", "ko": "꾸며낸 항목"}]
        return LLMResult(text="", data={"items": items}, usage=Usage(input_tokens=11, output_tokens=7), duration_ms=1)


def setup(monkeypatch, lines, *, max_items=40, mode="all", max_chars=1000):
    session = Session(lines)

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(db, "session", fake_session)
    cfg = AppConfig()
    cfg.assist.interpret_enabled = True
    cfg.assist.interpret_max_items = max_items
    cfg.assist.interpret_max_chars = max_chars
    provider = Provider(mode)
    return session, provider, Interpreter(cfg, provider)


def test_duplicate_row_and_bundle_texts_use_one_slot_and_keep_agent_conclusion(monkeypatch):
    lines = [f"unknown line {i}" for i in range(30)]
    session, provider, interpreter = setup(monkeypatch, lines)
    before = incident_items(session, 1)
    assert len(before) == 61, "공통 로그 30개 + 행 로그 30개 + 결론 1개"
    result = interpreter.run(1)
    sent = provider.calls[0]
    assert len(sent) == 31
    assert len({p["text"] for p in sent}) == 31
    assert any(p["id"] == "agent:300" for p in sent)
    cache = session.rows[Interpretation]
    assert result == len(cache) == 61, "단일 해석을 자기 키·원문 hash의 캐시 행으로 각각 저장"
    assert len({(r.item_key, r.raw_sha256) for r in cache}) == 61
    after = readings(session, 1, before)
    assert all(i["ko"] and i["ko_source"] == "llm" for i in after)
    legacy = {i["raw"]: i["ko"] for i in after if i["id"].startswith("E7_")}
    assert all(legacy[i["raw"]] == i["ko"] for i in after if i.get("evidence_id") is not None)
    assert interpreter.run(1) == 0 and len(provider.calls) == 1
    assert session.rows[AgentRun][-1].input_tokens == 11, "해석 비용을 중복 계상하지 않는다"


@pytest.mark.parametrize("mode,known", [("partial", 1), ("empty", 0), ("failure", 0)])
def test_only_valid_returned_representatives_are_fanned_out(monkeypatch, mode, known):
    session, provider, interpreter = setup(monkeypatch, ["unknown first", "unknown second"], mode=mode)
    result = interpreter.run(1)
    requested = {p["id"] for p in provider.calls[0]}
    assert "not_requested" not in requested
    cache = session.rows[Interpretation]
    assert result == len(cache) == known, "부분 응답은 우선한 Agent 결론 1개만, 빈 응답·실패는 아무 캐시도 없음"
    assert all(c.item_key == "agent:300" for c in cache)


def test_item_limit_applies_to_unique_inputs_and_never_marks_unsent_items(monkeypatch):
    session, provider, interpreter = setup(monkeypatch, ["unknown first", "unknown second"], max_items=2)
    assert interpreter.run(1) == 3, "결론 1개 + 로그 1개의 공통/행 캐시 2개"
    sent = provider.calls[0]
    assert len(sent) == 2 and sent[0]["id"] == "agent:300"
    assert len({p["text"] for p in sent}) == 2
    cache = {r.item_key: r.raw_sha256 for r in session.rows[Interpretation]}
    assert cache == {"agent:300": sha("agent conclusion X"), "E7_error_logs#0": sha("unknown first"),
                     "evidence:1:E7_error_logs#0": sha("unknown first")}


def test_equal_truncated_prefix_does_not_merge_different_full_raw_hashes(monkeypatch):
    prefix = "unknown prefix " * 10
    session, provider, interpreter = setup(monkeypatch, [prefix + "first", prefix + "second"], max_chars=50)
    assert interpreter.run(1) == 5
    sent = provider.calls[0]
    logs = [p for p in sent if p["id"] != "agent:300"]
    assert len(logs) == 2 and logs[0]["text"] == logs[1]["text"]
    hashes = {r.raw_sha256 for r in session.rows[Interpretation] if r.item_key != "agent:300"}
    assert hashes == {sha(prefix + "first"), sha(prefix + "second")}


def test_same_text_with_distinct_reading_kinds_is_not_merged(monkeypatch):
    session, provider, interpreter = setup(monkeypatch, ["agent conclusion X"])
    assert interpreter.run(1) == 3
    assert len(provider.calls[0]) == 2, "Agent 결론과 로그는 서로 다른 문맥"
    assert len(session.rows[Interpretation]) == 3
