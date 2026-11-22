"""Stage card summaries: what a screen can show, the summary cutover and Kubernetes quote readings."""
from datetime import datetime, timezone
from types import SimpleNamespace as N

from app import reading_era
from app.interpret import agent_items, quote_entries, readings, sha
from app.models import AgentRun, Incident, Interpretation
from app.readings_ko import display_safe, summary_ok


def test_display_safe_mirrors_the_dashboard_filter():
    assert display_safe("재고 예약 요청에서 0으로 나누기 예외가 발생했습니다.")
    assert display_safe("src/main/java/App.java 파일의 trim 호출이 실패합니다.")
    for text in ("재고 조회 중 IllegalStateException이 기록됐습니다.", "{\"a\": 1} 오류", "https://x.y 문서를 보세요",
                 "Traceback 이후 실패", "10.0.0.1 연결 실패", "connection refused", "", None):
        assert not display_safe(text), text


def test_a_summary_keeps_identifiers_but_must_read_as_korean():
    assert summary_ok("AppFaultBusiness의 quantity==7 분기가 0 나눗셈을 일으킵니다.")
    assert summary_ok("inventory 서비스의 Pod가 메모리 한도를 넘어 다시 시작됐습니다.")
    assert not summary_ok("OOMKilled inventory 재시작")          # too few Hangul letters
    assert not summary_ok("Pod inventory-7f9 OOMKilled 확인")     # mostly identifiers
    assert not summary_ok("재고 조회 중 IllegalStateException 발생")  # hidden by the screen filter


def test_cutover_reads_floats_datetimes_and_iso_strings(monkeypatch):
    monkeypatch.setattr(reading_era, "READINGS_V2_SINCE", 1_000.0)
    assert reading_era.legacy_at(999.0) and not reading_era.legacy_at(1_000.0)
    assert reading_era.legacy_at(datetime(1970, 1, 1, 0, 0, 10, tzinfo=timezone.utc))
    assert reading_era.legacy_at("1970-01-01T00:00:10")            # naive means UTC
    assert not reading_era.legacy_at(None) and not reading_era.legacy_at("unknown")


def _run(created_at):
    output = {"summary": "memory limit", "evidence": [
        {"ref": "E7_error_logs", "quote": "OOMKilled"}, {"ref": "E2_pods", "quote": ""}, "not a dict",
        {"ref": "E3_service", "quote": "no endpoints"}]}
    return {"id": 41, "role": "cause_reasoner", "output": output, "created_at": created_at}


def test_quotes_are_summarised_only_for_runs_after_the_cutover(monkeypatch):
    monkeypatch.setattr(reading_era, "READINGS_V2_SINCE", datetime(2026, 11, 22, tzinfo=timezone.utc).timestamp())
    new = [i["id"] for i in agent_items([_run("2026-11-22T01:00:00+00:00")])]
    old = [i["id"] for i in agent_items([_run("2026-11-20T01:00:00+00:00")])]
    assert [k for k in new if ":quote:" in k] == ["agent:41:quote:0", "agent:41:quote:1"]
    assert not [k for k in old if ":quote:" in k], "history gains no new reading work"
    # The card lists the same excerpts in the same order, so quote:i joins the i-th quote of the card.
    assert [q["quote"] for q in quote_entries(_run(None)["output"])] == ["OOMKilled", "no endpoints"]


class Session:
    def __init__(self, created, attempted=(), cached=()):
        self.rows = {Incident: [N(id=1, created_at=created)], AgentRun: [N(role="interpreter", output={"unread": dict(attempted)})],
                     Interpretation: [N(item_key=k, raw_sha256=sha(raw), ko=ko) for k, raw, ko in cached]}

    def get(self, cls, key):
        return next((r for r in self.rows.get(cls, []) if r.id == key), None)

    def scalars(self, query):
        selection = query.column_descriptions[0]
        if selection["expr"] is AgentRun.output:
            return [r.output for r in self.rows[AgentRun]]
        return list(self.rows.get(selection["entity"], []))


def test_past_gaps_read_legacy_and_current_gaps_keep_their_state(monkeypatch):
    since = datetime(2026, 11, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(reading_era, "READINGS_V2_SINCE", since.timestamp())
    item = lambda key, at: {"id": key, "ref": "decision", "kind": "agent_detail", "raw": "raw " + key, "at": at}  # noqa: E731
    old, new = item("agent:1:quote:0", "2026-11-20T00:00:00+00:00"), item("agent:2:quote:0", "2026-11-23T00:00:00+00:00")
    tried = item("agent:3:quote:0", "2026-11-23T00:00:00+00:00")
    session = Session(datetime(2026, 11, 23, tzinfo=timezone.utc), attempted=[(tried["id"], sha(tried["raw"]))])
    states = {i["id"]: i["reading_state"] for i in readings(session, 1, [old, new, tried])}
    assert states == {old["id"]: "legacy", new["id"]: "missing", tried["id"]: "failed"}
    # An item without a time takes the incident's own creation time.
    undated = {**item("agent:4:quote:0", None)}
    history = Session(datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert readings(history, 1, [undated])[0]["reading_state"] == "legacy"
    cached = Session(datetime(2026, 11, 1, tzinfo=timezone.utc), cached=[(old["id"], old["raw"], "메모리 부족으로 종료됐습니다.")])
    ready = readings(cached, 1, [old])[0]
    assert (ready["reading_state"], ready["ko"]) == ("ready", "메모리 부족으로 종료됐습니다.")
