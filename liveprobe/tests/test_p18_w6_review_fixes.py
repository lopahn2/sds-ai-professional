"""독립 검토 P2: 예정 단계 ID 연결과 행별 근거 원문 보존 회귀."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as N

import pytest

from app.flowgraph import _evidence_card, build_flow
from app.interpret import bundle_from_evidence, raw_items, rule_ko
from app.models import (Action, AgentRun, AuditLog, Evidence, Incident, Interpretation,
                        OperatorCommand, OperatorMessage, RoutingDecision, RunEvent)

T = datetime(2026, 10, 25, 3, tzinfo=timezone.utc)


class Rows(list):
    def all(self):
        return list(self)


class Session:
    def __init__(self):
        self.rows = {cls: [] for cls in (Action, AgentRun, AuditLog, Evidence, Incident, Interpretation,
                                        OperatorCommand, OperatorMessage, RoutingDecision, RunEvent)}
        self.rows[Incident] = [N(id=1, fingerprint="fixture/k8s", namespace="시험", workload="서비스", system=None, signal_reasons=[],
                                 signal_count=1, status="analyzing", severity_level="COMPLEX", handoff=None,
                                 created_at=T, updated_at=T, status_reason=None, title="시험", closed_at=None)]
        self.rows[RoutingDecision] = [decision(10, 0)]

    def get(self, cls, key):
        return next((r for r in self.rows[cls] if r.id == key), None)

    def scalars(self, query):
        return Rows(self.rows[query.column_descriptions[0]["entity"]])

    def scalar(self, query):
        return next(iter(self.scalars(query)), None)

    def execute(self, query):
        # P20: build_flow joins citations to reference chunks; these scenarios record none.
        return []


def decision(key, second):
    return N(id=key, level="COMPLEX", workflow="complex_group", decided_by="rules", rationale="",
             confidence=.9, features={}, policy={}, created_at=T + timedelta(seconds=second))


def run(key, second, role="cause_reasoner"):
    return N(id=key, role=role, status="succeeded", output={"summary": "시험 결론"},
             created_at=T + timedelta(seconds=second), model="시험", reasoning_effort=None,
             input_tokens=None, output_tokens=None, estimated_cost_usd=None, duration_ms=1, error=None)


def message(key, target):
    return N(id=key, body="확인 지시", intent="instruction", intent_detail={"target_role": target.split(":")[-1],
             "target_node": target}, created_at=T, applied_at=None)


@pytest.mark.parametrize("escalated", [False, True])
def test_plan_alias_belongs_only_to_first_run_in_each_segment(escalated):
    s = Session()
    s.rows[RoutingDecision].append(decision(20, 10))
    s.rows[AgentRun] = [run(101, 1), run(102, 2), run(201, 11), run(202, 12)]
    if escalated:
        s.rows[RunEvent] = [N(kind="escalated", detail={"role": "cause_reasoner"}, created_at=T, seq=1)]
    s.rows[OperatorMessage] = [message(7, "plan:10:cause_reasoner")]
    flow = build_flow(s, 1, items=[])
    by = {n["id"]: n for n in flow["nodes"]}
    assert by["run:101"].get("aliases") == ["plan:10:cause_reasoner"]
    assert by["run:201"].get("aliases") == ["plan:20:cause_reasoner"]
    assert not by["run:102"].get("aliases") and not by["run:202"].get("aliases")
    assert next(e for e in flow["edges"] if e["source"] == "steer:7")["target"] == "run:101"


@pytest.mark.parametrize("old_group", [False, True])
def test_targeted_unexecuted_plan_survives_as_skipped(old_group):
    s = Session()
    if old_group:
        s.rows[RoutingDecision].append(decision(20, 10))
    else:
        s.rows[Incident][0].status = "handed_off"
    s.rows[OperatorMessage] = [message(7, "plan:10:challenger")]
    flow = build_flow(s, 1, items=[])
    target = next((n for n in flow["nodes"] if n["id"] == "plan:10:challenger"), None)
    assert target is not None and target["status"] == "skipped"
    assert target["card"]["accepts_steering"] is False
    assert next(e for e in flow["edges"] if e["source"] == "steer:7")["target"] == target["id"]


def evidence(key, source, payload, *, target="시험/서비스", error=None):
    return N(id=key, source=source, target=target, payload=payload, error=error,
             collected_at=T + timedelta(seconds=key))


def test_card_uses_own_snapshots_even_with_only_legacy_latest_items():
    ev = [evidence(1, "deployment", {"replicas": 1, "ready_replicas": 0}),
          evidence(2, "deployment", {"replicas": 1, "ready_replicas": 1}),
          evidence(3, "deployment", {"replicas": 3, "ready_replicas": 2}, target="시험/다른서비스")]
    legacy = [{**i, "ko": rule_ko(i)} for i in raw_items(bundle_from_evidence(ev))]
    rows = _evidence_card(ev, legacy)["items"]
    assert "준비된 Pod 0개" in rows[0]["summary_ko"]
    assert "준비된 Pod 1개" in rows[1]["summary_ko"]
    assert "목표 Pod 3개 중 준비된 Pod 2개" in rows[2]["summary_ko"]
    for e, row in zip(ev, rows):
        assert row["raw_items"][0]["id"] == f"evidence:{e.id}:E1_deployment#0"
        assert row["raw_items"][0]["evidence_id"] == e.id
        assert row["raw_items"][0]["at"] == e.collected_at.isoformat()
        assert row["raw_items"][0]["ref"] == "E1_deployment"
        assert row["raw_items"][0]["ko"]
    assert '"ready_replicas": 0' in rows[0]["raw_items"][0]["raw"]
    assert '"ready_replicas": 1' in rows[1]["raw_items"][0]["raw"]


@pytest.mark.parametrize("source,payload,fragment", [
    ("pod_status", [{"name": "이전", "phase": "Pending"}, {"name": "다른", "phase": "Running"}], "이전"),
    ("k8s_event", [{"reason": "FailedScheduling", "message": "Insufficient cpu", "type": "Warning"}], "Insufficient cpu"),
    ("loki", {"lines": ["connection refused", "알 수 없는 과거 로그"]}, "connection refused"),
    ("prometheus", {"count": 0}, '"count": 0'),
    ("node", {"name": "이전노드", "conditions": {"Ready": {"status": "False"}}}, "이전노드"),
    ("configmap", {"name": "이전설정", "sha256": "이전해시"}, "이전해시"),
    ("endpoints", {"addresses": 0}, '"addresses": 0'),
    ("code_refs", {"files": ["이전.java"]}, "이전.java"),
    ("deployment", {}, "{}"), ("pod_status", [], "[]"), ("k8s_event", [], "[]"),
    ("loki", {}, "{}"), ("loki", {"lines": []}, '"lines": []'),
    ("prometheus", {}, "{}"), ("endpoints", [], "[]"), ("code_refs", {}, "{}"),
])
def test_row_raw_survives_arrays_and_sources_not_in_legacy_bundle(source, payload, fragment):
    ev = evidence(1, source, payload)
    row = _evidence_card([ev], [])["items"][0]
    assert row["ok"] is True
    assert row.get("raw_items"), "빈 payload도 수집 원문을 유지해야 함"
    assert any(fragment in i["raw"] for i in row["raw_items"])
    assert all(i["id"].startswith("evidence:1:") and i["evidence_id"] == 1 for i in row["raw_items"])
    assert all("ko" in i for i in row["raw_items"])


@pytest.mark.parametrize("payload,error", [(None, None), (None, "수집 실패"),
                                              ({"replicas": 1, "ready_replicas": 1}, "수집 실패")])
def test_failed_or_null_rows_never_borrow_normal_summary(payload, error):
    latest = {**raw_items({"E1_deployment": {"replicas": 1, "ready_replicas": 1}})[0], "ko": "정상 최신값"}
    row = _evidence_card([evidence(1, "deployment", payload, error=error)], [latest])["items"][0]
    assert row["ok"] is False and row["summary_ko"] is None and row["error"] == error
    assert row.get("raw_items") and all(i["ko"] is None for i in row["raw_items"])


def test_incident_row_readings_use_own_cache_and_do_not_pollute_legacy_raw():
    from app.interpret import incident_items, sha
    from app.narrative import build_narrative
    s = Session()
    s.rows[Evidence] = [evidence(1, "loki", {"lines": ["과거 불명 로그"]}),
                        evidence(2, "loki", {"lines": ["최신 불명 로그"]})]
    all_items = incident_items(s, 1)
    assert {i.get("evidence_id") for i in all_items if i.get("evidence_id")} == {1, 2}
    s.rows[Interpretation] = [N(item_key="evidence:1:E7_error_logs#0", raw_sha256=sha("과거 불명 로그"), ko="과거 행의 해석입니다."),
                              N(item_key="E7_error_logs#0", raw_sha256=sha("최신 불명 로그"), ko="최신 공통 원문 해석입니다.")]
    narrative = build_narrative(s, 1)
    assert narrative is not None
    rows = next(n for n in narrative["flow"]["nodes"] if n["id"] == "evidence")["card"]["items"]
    assert rows[0]["summary_ko"] == "과거 행의 해석입니다."
    assert rows[0]["raw_items"][0]["ko_source"] == "llm"
    assert rows[1]["summary_ko"] is None and rows[1]["raw_items"][0]["ko"] is None
    assert len(narrative["raw"]) == 1
    assert narrative["raw"][0]["raw"] == "최신 불명 로그"
    assert narrative["raw"][0]["ko"] == "최신 공통 원문 해석입니다."
    assert not any(i.get("evidence_id") for i in narrative["raw"])
    s.rows[Evidence][0].payload = {"lines": ["변경된 과거 로그"]}
    changed = build_narrative(s, 1)
    assert changed is not None
    rows = next(n for n in changed["flow"]["nodes"] if n["id"] == "evidence")["card"]["items"]
    assert rows[0]["raw_items"][0]["ko"] is None, "원문 hash가 바뀌면 과거 캐시를 재사용하면 안 됨"


def test_present_row_without_ko_never_borrows_another_rows_reading():
    from app.interpret import evidence_items
    ev = [evidence(1, "loki", {"lines": ["불명 로그"]}), evidence(2, "loki", {"lines": ["다른 로그"]})]
    items = evidence_items(ev)
    items[1]["ko"] = "다른 행은 정상입니다."
    rows = _evidence_card(ev, items)["items"]
    assert rows[0]["summary_ko"] is None and rows[0]["raw_items"][0]["ko"] is None
    assert rows[1]["summary_ko"] == "다른 행은 정상입니다."


@pytest.mark.parametrize("source,old,new", [
    ("loki", {"lines": ["connection refused", "이전 고유 로그"]}, {"lines": ["최신 고유 로그"]}),
    ("configmap", {"name": "이전설정", "sha256": "이전해시"}, {"name": "최신설정", "sha256": "최신해시"}),
    ("node", {"name": "이전노드", "conditions": {"Ready": {"status": "False"}}},
             {"name": "최신노드", "conditions": {"Ready": {"status": "True"}}}),
])
def test_log_config_node_recollections_keep_their_own_text(source, old, new):
    ev = [evidence(1, source, old), evidence(2, source, new)]
    legacy = [{**i, "ko": rule_ko(i)} for i in raw_items(bundle_from_evidence(ev))]
    rows = _evidence_card(ev, legacy)["items"]
    first = " ".join(i["raw"] for i in rows[0]["raw_items"])
    second = " ".join(i["raw"] for i in rows[1]["raw_items"])
    assert "이전" in first and "최신" not in first
    assert "최신" in second and "이전" not in second
    assert {i["id"] for i in rows[0]["raw_items"]}.isdisjoint(i["id"] for i in rows[1]["raw_items"])


def test_plan_running_first_execution_preserves_selection_alias():
    s = Session()
    before = build_flow(s, 1, items=[])
    assert any(n["id"] == "plan:10:cause_reasoner" and n["status"] == "planned" for n in before["nodes"])
    s.rows[RunEvent] = [N(kind="agent_started", detail={"role": "cause_reasoner"}, created_at=T, seq=1)]
    during = build_flow(s, 1, items=[])
    assert any(n["id"] == "plan:10:cause_reasoner" and n["status"] == "running" for n in during["nodes"])
    s.rows[AgentRun] = [run(101, 1)]
    after = build_flow(s, 1, items=[])
    assert not any(n["id"] == "plan:10:cause_reasoner" for n in after["nodes"])
    assert next(n for n in after["nodes"] if n["id"] == "run:101")["aliases"] == ["plan:10:cause_reasoner"]


def test_row_raw_retains_existing_redaction_boundary():
    row = _evidence_card([evidence(1, "loki", {"lines": ["password=hidden123 connection refused"]})], [])["items"][0]
    assert "hidden123" not in row["raw_items"][0]["raw"]
    assert "connection refused" in row["raw_items"][0]["raw"]


def test_unobserved_row_does_not_reuse_cached_positive_reading():
    from app.interpret import evidence_items, readings, sha
    s = Session()
    item = evidence_items([evidence(1, "loki", {"lines": ["connection refused"]}, error="수집 실패")])[0]
    s.rows[Interpretation] = [N(item_key=item["id"], raw_sha256=sha(item["raw"]), ko="정상입니다.")]
    assert readings(s, 1, [item])[0]["ko"] is None


def test_persisted_snapshots_and_display_interpreter_stay_isolated(db_url):
    import json
    from app import db
    from app.config import AppConfig
    from app.interpret import Interpreter
    from app.llm.base import LLMResult, Usage
    from app.narrative import build_narrative
    with db.session() as s:
        inc = Incident(fingerprint="행별-회귀", namespace="시험", workload="서비스", title="시험", status="analyzing")
        s.add(inc)
        s.flush()
        iid = inc.id
        rows = [Evidence(incident_id=iid, source="deployment", payload={"replicas": 1, "ready_replicas": ready},
                         collected_at=T + timedelta(seconds=ready)) for ready in (0, 1)]
        rows += [Evidence(incident_id=iid, source="loki", payload={"lines": ["과거 불명 로그"]}, collected_at=T),
                 Evidence(incident_id=iid, source="loki", payload={"lines": ["최신 불명 로그"]}, collected_at=T),
                 Evidence(incident_id=iid, source="loki", payload={"lines": ["실패한 수집 로그"]}, error="수집 실패", collected_at=T)]
        s.add_all(rows)
        s.flush()
        old_log_id, failed_id = rows[2].id, rows[4].id

    class Provider:
        name = "단위시험"

        def __init__(self):
            self.items = []

        def complete(self, role, prompt, schema, model, effort, timeout):
            payload = json.loads(prompt.split("ITEMS_JSON:", 1)[1])
            self.items.extend(payload)
            return LLMResult(text="", data={"items": [{"id": i["id"], "ko": f"자기 해석 {i['id']}"} for i in payload]},
                             usage=Usage(input_tokens=1, output_tokens=1), duration_ms=1)

    p = Provider()
    cfg = AppConfig()
    cfg.assist.interpret_max_items = 100
    assert Interpreter(cfg, p).run(iid) > 0
    assert all(not i["id"].startswith(f"evidence:{failed_id}:") for i in p.items)
    with db.session() as s:
        narrative = build_narrative(s, iid)
    assert narrative is not None
    cards = next(n for n in narrative["flow"]["nodes"] if n["id"] == "evidence")["card"]["items"]
    assert "준비된 Pod 0개" in cards[0]["summary_ko"] and "준비된 Pod 1개" in cards[1]["summary_ko"]
    assert cards[2]["summary_ko"] == f"자기 해석 evidence:{old_log_id}:E7_error_logs#0"
    assert cards[4]["summary_ko"] is None
    assert not any(i.get("evidence_id") for i in narrative["raw"])
    assert len([i for i in narrative["raw"] if i["ref"] == "E1_deployment"]) == 1
