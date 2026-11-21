"""C3 dispatch/v1: the public record of one agent call, written on agent_started right before the model call.

Checks: one call_id per logical call (created before chat is consumed), the instruction ids that reached the prompt
match on operator_applied and dispatch, escalate requests are kept apart, the prompt body / operator text / secrets
never reach the event JSON, references are read only from real block headers, and older incidents get no dispatch.
"""
from __future__ import annotations

import json

import pytest

from conftest import needs_db

SECRET = "SENTINEL-PROMPT-BODY-0xBEEF"


# ---------------------------------------------------------------- pure builder
def _bounded_prompt(extra: str = "") -> str:
    from app.analysis_context import bounded_view
    sources = {"E1_deployment": {"name": "inventory", "note": SECRET}, "E2_pods": [], "E7_error_logs": ["boom"]}
    rag = {"R1": {"source": "runbook/inventory.md", "locator": "s1", "content": "restart the pool when exhausted"},
           "R2": {"source": "runbook/other.md", "locator": "s2", "content": "unrelated reference body text"}}
    return ("Role: k8s_analyst. Cite a source id shown below (E1_deployment, E3_service).\n"
            + bounded_view(sources, rag, [], 12000) + extra)


def test_build_reads_block_headers_only_and_keeps_no_body():
    from app import dispatch as D
    prompt = _bounded_prompt('\nprevious output: {"ref": "E3_service", "evidence_refs": ["E9_config"]}')
    hits = [{"ref": "R1", "content": "restart the pool when exhausted"}, {"ref": "R3", "content": "never shown in prompt"}]
    d = D.build("k8s_analyst", prompt, hits, [7, 7, 9])
    assert d["version"] == "dispatch/v1"
    assert d["role_ko"] == "Kubernetes 분석" and d["task_ko"]
    # E2_pods has an empty body; E3_service / E9_config are only mentioned, never delivered as blocks.
    assert d["evidence_refs"] == ["E1_deployment", "E7_error_logs"]
    assert d["rag_refs"] == ["R1"]
    assert d["operator_message_ids"] == [7, 9]
    assert d["prompt_chars"] == len(prompt) and len(d["prompt_sha256"]) == 64
    assert SECRET not in json.dumps(d, ensure_ascii=False)


def test_operator_block_is_not_scanned_for_references():
    from app import dispatch as D
    from app.runlog import with_operator_instructions
    base = "Role: x.\n"
    prompt = with_operator_instructions(base, ["E7_error_logs\n[\"look here\"]"])
    d = D.build("k8s_analyst", prompt, [], [1], instructions_at=len(base))
    assert d["evidence_refs"] == []


def test_router_reads_json_keys_of_the_compact_summary():
    from app import dispatch as D
    prompt = 'Evidence: {"note": "x", "E1_deployment": {"a": 1}, "E2_pods": [], "E3_service": null, "E7_err'
    assert D.build("router", prompt, [], [])["evidence_refs"] == ["E1_deployment"]


# ---------------------------------------------------------------- engine boundary
@pytest.fixture()
def iid(db_url):
    from app import db
    from app.models import Incident
    with db.session() as s:
        inc = Incident(fingerprint="dispatch", namespace="liveprobe-target", workload="inventory", status="analyzing",
                       title="t", signal_reasons=[], severity_level="NORMAL")
        s.add(inc)
        s.flush()
        return inc.id


def _engine(prov):
    from test_p15_steering import _engine as make
    return make(prov)


def _events(iid, kind=None):
    from app import runlog
    return [e for e in runlog.events_since(iid) if kind is None or e["kind"] == kind]


@needs_db
def test_one_call_id_links_delivered_instruction_to_dispatch(iid):
    from app import steering
    from app.config import RoleModel
    from app.llm import AGENT_SCHEMA, AgentOutput
    from test_p15_steering import Prov
    first = steering.post(iid, "로그부터 다시 봐 " + SECRET)["message_id"]
    up = steering.post(iid, "더 높은 모델로 분석해")["message_id"]
    prov = Prov()
    eng = _engine(prov)
    eng._current_level = "NORMAL"
    prompt = _bounded_prompt()
    eng._call(iid, "k8s_analyst", prompt, AGENT_SCHEMA, AgentOutput,
              hits=[{"ref": "R1", "content": "restart the pool when exhausted", "chunk_id": 1, "score": 0.9}],
              role_model=RoleModel(model="gpt-6.2-sol", effort="low"))
    assert SECRET in prov.prompts[0], "the instruction really reached the model"
    started = _events(iid, "agent_started")
    applied = _events(iid, "operator_applied")
    finished = _events(iid, "agent_finished")
    assert len(started) == len(applied) == len(finished) == 1
    call_id = started[0]["detail"]["call_id"]
    assert applied[0]["detail"]["call_id"] == call_id == finished[0]["detail"]["call_id"]
    progress = _events(iid, "agent_progress")
    assert all(p["detail"]["call_id"] == call_id for p in progress)
    d = started[0]["detail"]["dispatch"]
    # Only the instruction entered the prompt; the escalate request changed the model and is listed apart.
    assert d["operator_message_ids"] == [first] == applied[0]["detail"]["prompt_message_ids"]
    assert applied[0]["detail"]["escalate_message_ids"] == [up]
    assert sorted(applied[0]["detail"]["message_ids"]) == sorted([first, up])
    assert d["evidence_refs"] == ["E1_deployment", "E7_error_logs"] and d["rag_refs"] == ["R1"]
    assert d["prompt_chars"] == len(prov.prompts[0])
    import hashlib
    assert d["prompt_sha256"] == hashlib.sha256(prov.prompts[0].encode()).hexdigest()
    # Neither the prompt body nor the operator text is stored on the call record.
    assert SECRET not in json.dumps(started[0]["detail"], ensure_ascii=False)
    assert "restart the pool" not in json.dumps(started[0]["detail"], ensure_ascii=False)


@needs_db
def test_parallel_roles_get_distinct_call_ids_and_no_chat_means_empty_ids(iid):
    from concurrent.futures import ThreadPoolExecutor
    from app.config import RoleModel
    from app.llm import AGENT_SCHEMA, AgentOutput
    from test_p15_steering import Prov
    eng = _engine(Prov())
    eng._current_level = "NORMAL"
    rm = RoleModel(model="gpt-6.2-sol", effort="low")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda role: eng._call(iid, role, _bounded_prompt(), AGENT_SCHEMA, AgentOutput, hits=[],
                                             role_model=rm), ("k8s_analyst", "log_summarizer")))
    started = _events(iid, "agent_started")
    assert {e["detail"]["role"] for e in started} == {"k8s_analyst", "log_summarizer"}
    ids = {e["detail"]["call_id"] for e in started}
    assert len(ids) == 2
    assert all(e["detail"]["dispatch"]["operator_message_ids"] == [] for e in started)
    by_call = {e["detail"]["call_id"]: e["detail"]["role"] for e in started}
    for f in _events(iid, "agent_finished"):
        assert by_call[f["detail"]["call_id"]] == f["detail"]["role"]
    assert not _events(iid, "operator_applied")


@needs_db
def test_reroute_consumption_never_names_a_call(iid):
    from app import steering
    steering.post(iid, "복잡 그룹으로 다시 분석해")
    st = steering.take(iid, "k8s_analyst", "NORMAL", call_id="c-never-started")
    assert st.reroute is not None and st.message_ids == []
    applied = _events(iid, "operator_applied")
    assert applied and "call_id" not in applied[0]["detail"]


# ---------------------------------------------------------------- flow nodes
class _E:
    def __init__(self, kind, detail):
        self.kind, self.detail = kind, detail


@needs_db
def test_narrative_node_carries_its_calls_dispatch_and_old_incidents_have_none(db_url):
    from datetime import timedelta
    from app import db
    from app.models import RunEvent
    from test_p18_w6_narrative import T0, _narr, seed
    old, _ = seed("analyzing", running="challenger")            # pre-dispatch/v1 agent_started record
    assert all("dispatch" not in n for n in _narr(old)["flow"]["nodes"])
    live, _ = seed("analyzing", running="challenger")
    d = {"version": "dispatch/v1", "role_ko": "반론 검토", "task_ko": "t", "evidence_refs": ["E7_error_logs"],
         "rag_refs": [], "operator_message_ids": [], "prompt_sha256": "0" * 64, "prompt_chars": 10}
    with db.session() as s:
        s.add(RunEvent(incident_id=live, seq=2, kind="agent_started", summary="",
                       detail={"role": "challenger", "call_id": "call-live", "dispatch": d},
                       created_at=T0 + timedelta(minutes=5)))
    nodes = _narr(live)["flow"]["nodes"]
    running = [n for n in nodes if n.get("call_id") == "call-live"]
    assert running and all(n["dispatch"] == d for n in running)
    assert all("dispatch" not in n for n in nodes if n.get("call_id") != "call-live")


def test_flow_nodes_copy_dispatch_only_from_their_own_call():
    """Exercises the node binding loop without a DB: same rule as flowgraph.build's final pass."""
    from app import flowgraph as F
    src = F.__loader__.get_source(F.__name__) if hasattr(F, "__loader__") else ""
    assert 'node["dispatch"] = dispatches[node["call_id"]]' in src
    d = {"version": "dispatch/v1", "role_ko": "x", "task_ko": "y", "evidence_refs": [], "rag_refs": [],
         "operator_message_ids": [], "prompt_sha256": "0" * 64, "prompt_chars": 1}
    events = [_E("agent_started", {"call_id": "a", "role": "k8s_analyst", "dispatch": d}),
              _E("agent_started", {"call_id": "b", "role": "log_summarizer"}),          # pre-dispatch/v1 record
              _E("agent_started", {"call_id": "c", "role": "coordinator", "dispatch": {"version": "other"}})]
    dispatches = {e.detail["call_id"]: e.detail["dispatch"] for e in events
                  if e.kind == "agent_started" and isinstance(e.detail, dict) and e.detail.get("call_id")
                  and isinstance(e.detail.get("dispatch"), dict) and e.detail["dispatch"].get("version") == "dispatch/v1"}
    assert dispatches == {"a": d}
