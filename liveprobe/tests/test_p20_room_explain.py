"""P20: explainability fields on the incident-room step cards (pure functions, no database)."""
from __future__ import annotations

from types import SimpleNamespace

from app.flowgraph import (_agent_card, attach_escalations, escalation_reason_ko, explain_fields, source_labels)

OUTPUT = {
    "summary": "inventory container OOMKilled at the 512Mi limit",
    "confidence": 0.82,
    "evidence": [
        {"ref": "E2_pods", "quote": "lastState.terminated.reason: OOMKilled", "supports": "Memory limit too low"},
        {"ref": "R1", "quote": "raise the limit before restarting", "supports": "unrelated note"},
        {"ref": "E99_made_up", "quote": "does not exist", "supports": "Memory limit too low"},
        {"ref": "E5", "quote": "", "supports": "empty quotes are dropped"},
    ],
    "root_cause_candidates": [{"cause": "memory limit too low", "cause_ko": "메모리 한도가 낮습니다", "confidence": 0.8,
                               "evidence_refs": ["E2_pods", "E6_metrics"], "verifier": "supported"}],
    "counter_evidence": ["Readiness probe failed before the OOM kill", "상태 점검이 먼저 실패했습니다", "  "],
    "verdict": "supported",
    "candidate_verdicts": [{"index": 0, "verdict": "supported", "reason": "quote matches the pod status"}],
    "next_action": "act",
}
EVIDENCE = [SimpleNamespace(source="pod_status"), SimpleNamespace(source="prometheus"), SimpleNamespace(source="code_refs")]
CITES = [{"chunk_id": 7, "doc": "oom-runbook.md", "source": "runbook", "chunk_ix": 0, "score": 0.71,
          "section": "Recovery", "locator": "L10-L20", "source_uri": None, "content": "raise the limit"}]


def test_source_labels_cover_long_and_short_refs_of_collected_sources_only():
    labels = source_labels(EVIDENCE)
    assert labels["E2_pods"] == labels["E2"]
    assert "E6_metrics" in labels and "E6" in labels
    assert "E1_deployment" not in labels      # not collected for this incident
    assert all(k.startswith("E") for k in labels)


def test_quotes_keep_agent_text_and_link_to_the_candidate_they_support():
    f = explain_fields(OUTPUT, source_labels(EVIDENCE), CITES)
    assert [q["ref"] for q in f["quotes"]] == ["E2_pods", "R1", "E99_made_up"]     # empty quote dropped
    first, runbook, unknown = f["quotes"]
    assert first["quote"] == "lastState.terminated.reason: OOMKilled"
    assert first["candidate_index"] == 0 and first["ref_known"] is True and first["ref_ko"]
    assert runbook["ref_known"] is True and runbook["ref_ko"] == "참고 문서" and runbook["candidate_index"] is None
    assert unknown["ref_known"] is False and unknown["ref_ko"] is None
    # an R# quote without any stored citation for the run cannot be tied to a document
    assert explain_fields(OUTPUT, source_labels(EVIDENCE), [])["quotes"][1]["ref_known"] is False


def test_counter_evidence_and_verdicts_are_exposed_with_korean_only_when_written_in_korean():
    f = explain_fields(OUTPUT, {}, [])
    assert [c["text_raw"] for c in f["counter_evidence"]] == ["Readiness probe failed before the OOM kill",
                                                              "상태 점검이 먼저 실패했습니다"]
    assert f["counter_evidence"][0]["text_ko"] is None
    assert f["counter_evidence"][1]["text_ko"] == "상태 점검이 먼저 실패했습니다"
    assert f["candidate_verdicts"] == [{"index": 0, "verdict": "supported", "verdict_ko": "근거로 지지",
                                        "reason_raw": "quote matches the pod status", "reason_ko": None}]
    assert f["acceptance"] is None


def test_rejected_output_reports_why_in_korean():
    f = explain_fields({**OUTPUT, "_acceptance_rejected": "2 quote(s) not found in cited sources"}, {}, [])
    assert f["acceptance"] == {"rejected": True, "reason_raw": "2 quote(s) not found in cited sources",
                               "reason_ko": "인용문 2건이 원본 근거에서 확인되지 않았습니다."}


def test_long_text_is_clipped_and_lists_are_bounded():
    big = {"evidence": [{"ref": "E2", "quote": "x" * 900, "supports": "s"}] * 30, "counter_evidence": ["y" * 900] * 30}
    f = explain_fields(big, {}, [])
    assert len(f["quotes"]) == 12 and len(f["quotes"][0]["quote"]) == 240
    assert len(f["counter_evidence"]) == 10 and len(f["counter_evidence"][0]["text_raw"]) == 300


def test_missing_output_gives_empty_fields():
    assert explain_fields(None, {}, []) == {"quotes": [], "counter_evidence": [], "candidate_verdicts": [],
                                            "citations": [], "acceptance": None}


def test_escalation_reasons_have_fixed_korean_readings():
    assert "반박" in escalation_reason_ko("conclusion contradicted by reviewer")
    assert escalation_reason_ko("3 quote(s) not found in cited sources").startswith("인용문 3건")
    assert "근거 번호" in escalation_reason_ko("unknown evidence refs ['E42']")
    assert "원인 후보" in escalation_reason_ko("no root-cause candidate backed by a verified E# quote (insufficient evidence)")
    assert escalation_reason_ko("confidence 0.31 below 0.5") == "신뢰도 0.31이(가) 기준 0.5보다 낮습니다."
    assert "응답" in escalation_reason_ko("agent failed (timeout)")
    assert escalation_reason_ko("something new") == "근거 검사를 통과하지 못했습니다."


def test_escalation_events_attach_to_the_escalated_node_and_its_successor_in_order():
    nodes = [{"id": "a", "role": "k8s_analyst"}, {"id": "b", "role": "k8s_analyst"}, {"id": "c", "role": "challenger"},
             {"id": "d", "role": "challenger"}, {"id": "e", "role": "log_summarizer"}, {"id": "f", "role": "log_summarizer"}]
    edges = [{"source": "a", "target": "b", "kind": "escalate"}, {"source": "c", "target": "d", "kind": "escalate"},
             {"source": "e", "target": "f", "kind": "escalate"}, {"source": "b", "target": "c", "kind": "flow"}]
    ev = lambda role, reason: SimpleNamespace(kind="escalated", detail={
        "role": role, "reason": reason, "from_model": "luna", "from_effort": "low", "to_model": "sol", "to_effort": "high"})
    events = [ev("challenger", "conclusion contradicted by reviewer"), ev("k8s_analyst", "confidence 0.2 below 0.5"),
              SimpleNamespace(kind="agent_started", detail={"role": "k8s_analyst"})]
    attach_escalations(nodes, edges, events)
    by = {n["id"]: n for n in nodes}
    assert by["a"]["escalation"]["reason_ko"].startswith("신뢰도 0.2")
    assert by["b"]["escalated_from"] == by["a"]["escalation"]
    assert by["a"]["escalation"]["to_model"] == "sol" and by["a"]["escalation"]["from_effort"] == "low"
    assert "반박" in by["c"]["escalation"]["reason_ko"] and "escalated_from" in by["d"]
    assert "escalation" not in by["e"] and "escalated_from" not in by["f"]     # no recorded event: nothing invented


def test_agent_card_carries_explain_fields_and_candidate_refs():
    run = SimpleNamespace(id=11, output=OUTPUT, error=None)
    card = _agent_card({"status": "done", "role": "group_verifier", "conclusion_ko": "근거와 일치합니다"}, run, False, False,
                       source_labels(EVIDENCE), CITES)
    assert card["confidence"] == 0.82
    assert card["candidates"][0]["evidence_refs"] == ["E2_pods", "E6_metrics"]
    assert card["citations"] == CITES and len(card["quotes"]) == 3
    assert card["counter_evidence_count"] == 3 and len(card["counter_evidence"]) == 2
    # older callers that pass no sources/citations still work
    assert _agent_card({"status": "planned", "role": "x"}, None, True, False)["quotes"] == []


# ------------------------------------------------------------------ database-backed: the full narrative path
from datetime import datetime, timedelta, timezone  # noqa: E402

from conftest import needs_db  # noqa: E402


@needs_db
def test_narrative_cards_expose_quotes_citations_and_escalation_reason(db_url):
    from app import db
    from app.models import AgentRun, Citation, Evidence, Incident, RagChunk, RoutingDecision, RunEvent
    from app.narrative import build_narrative
    t0 = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
    with db.session() as s:
        inc = Incident(fingerprint="p20", namespace="liveprobe-target", workload="inventory", status="handed_off",
                       severity_level="NORMAL", title="OOM on Deployment/inventory", signal_count=1,
                       signal_reasons=["OOMKilled"], created_at=t0, updated_at=t0 + timedelta(minutes=3))
        s.add(inc)
        s.flush()
        iid = inc.id
        s.add(Evidence(incident_id=iid, source="pod_status", target="liveprobe-target/inventory", payload=[{"name": "p"}],
                       collected_at=t0 + timedelta(seconds=2)))
        s.add(RoutingDecision(incident_id=iid, level="NORMAL", workflow="normal_group", features={}, policy={},
                              rationale="rule", confidence=0.9, decided_by="rules", created_at=t0 + timedelta(seconds=5)))
        first = AgentRun(incident_id=iid, role="k8s_analyst", provider="t", model="gpt-6-luna", reasoning_effort="low",
                         status="succeeded", attempts=1, created_at=t0 + timedelta(seconds=10),
                         output={**OUTPUT, "confidence": 0.2})
        second = AgentRun(incident_id=iid, role="k8s_analyst", provider="t", model="gpt-6.1-sol", reasoning_effort="high",
                          status="succeeded", attempts=1, created_at=t0 + timedelta(seconds=20), output=OUTPUT)
        s.add_all([first, second])
        s.flush()
        s.add(RunEvent(incident_id=iid, seq=1, kind="escalated", summary="", created_at=t0 + timedelta(seconds=15),
                       detail={"role": "k8s_analyst", "reason": "confidence 0.2 below 0.5", "from_model": "gpt-6-luna",
                               "from_effort": "low", "to_model": "gpt-6.1-sol", "to_effort": "high"}))
        chunk = RagChunk(doc="oom-runbook.md", source="runbook", chunk_ix=0, content="Raise the memory limit, then verify.",
                         content_hash="p20-" + "0" * 60, embedding=[0.0] * RagChunk.embedding.type.dim)
        s.add(chunk)
        s.flush()
        s.add(Citation(agent_run_id=second.id, chunk_id=chunk.id, score=0.66))
        first_id, second_id = first.id, second.id
    with db.session() as s:
        nodes = build_narrative(s, iid)["flow"]["nodes"]
    old = next(n for n in nodes if n.get("agent_run_id") == first_id)
    new = next(n for n in nodes if n.get("agent_run_id") == second_id)
    assert old["status"] == "escalated"
    assert old["escalation"]["reason_ko"] == "신뢰도 0.2이(가) 기준 0.5보다 낮습니다."
    assert old["escalation"]["to_model"] == "gpt-6.1-sol" and new["escalated_from"] == old["escalation"]
    card = new["card"]
    assert card["quotes"][0]["ref_known"] is True and card["quotes"][0]["ref_ko"]      # E2_pods was collected
    assert card["quotes"][2]["ref_known"] is False                                        # E99_made_up was not
    assert card["quotes"][1]["ref_known"] is True                                         # R1 and a stored citation
    assert [c["doc"] for c in card["citations"]] == ["oom-runbook.md"]
    assert card["citations"][0]["content"].startswith("Raise the memory limit")
    assert old["card"]["citations"] == [] and old["card"]["quotes"][1]["ref_known"] is False
    assert card["candidate_verdicts"][0]["verdict_ko"] == "근거로 지지"
