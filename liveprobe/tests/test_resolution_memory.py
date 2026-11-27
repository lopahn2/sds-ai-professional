"""Verified resolution memory: deterministic signatures, documents and the precedent match."""
from types import SimpleNamespace

import pytest

from app import resolution_memory as M
from app.rag_ingest import Document, _validate

CPU = [{"kind": "cpu_limit_binding", "limit_in_deployment_template": False, "pod": "p"},
       {"kind": "namespace_default_limits", "limit_ranges": []}]
LEAK = [{"kind": "recent_restart", "liveness_failures": 4},
        {"kind": "db_pool_state", "active": 6, "max": 6, "active_trend": {"resets_to_minimum": 2}}]


def test_signature_uses_structured_evidence_only():
    assert M.signature(CPU, [], ["TargetConnectionPoolExhausted"]) == [
        "cpu_limit_binding", "limit_from_admission", "namespace_default_limits", "signal:TargetConnectionPoolExhausted"]
    assert M.signature(LEAK, [], []) == ["liveness_restart", "pool_resets", "pool_saturated", "recent_restart"]
    deps = [{"ready_endpoints": 2, "pods": [{"error_logs": ["[db-1] FATAL"]}]}]
    assert M.signature([], deps, []) == ["dependency_errors", "dependency_multi_endpoint"]


def test_similarity_needs_shared_findings_and_compatible_signals():
    a = M.signature(CPU, [], ["TargetConnectionPoolExhausted"])
    assert M.similarity(a, a) == 1.0
    assert M.similarity(a, M.signature(LEAK, [], ["TargetConnectionPoolExhausted"])) == 0.0
    assert M.similarity(a, M.signature(CPU, [], ["OrderHigh5xxRate"])) == 0.0      # other signal family
    assert M.similarity(["signal:X"], ["signal:X"]) == 0.0                        # signals alone never match


def test_resolution_record_is_screened_and_requires_steps():
    rec = M.resolution_record("CPU limit from namespace default", ["set cpu limit 2000m <script>", " "],
                              "soak passed", "op", ["cpu_limit_binding"])
    assert rec["verified"] and rec["steps"] == ["set cpu limit 2000m <script>"] and rec["signature"] == ["cpu_limit_binding"]
    with pytest.raises(ValueError):
        M.resolution_record("x", [" "], "", "op", [])


def test_memory_text_and_precedent_round_trip():
    inc = SimpleNamespace(id=7, namespace="dev", workload="shop-stock", signal_reasons=["TargetConnectionPoolExhausted"])
    sig = M.signature(CPU, [], inc.signal_reasons)
    rec = M.resolution_record("namespace default cpu limit binds", ["set explicit cpu limit"], "soak", "op", sig)
    text = M.memory_text(inc, rec)
    hits = [{"ref": "R1", "source": "runbook", "content": text},
            {"ref": "R2", "source": "verified-resolution", "content": text, "doc": "resolution-7.md"}]
    match = M.precedent(hits, sig)
    assert match["ref"] == "R2" and match["similarity"] == 1.0
    assert M.precedent(hits, M.signature(LEAK, [], inc.signal_reasons)) is None


def test_eval_memory_kind_is_a_valid_internal_document():
    doc = Document(key="resolution-7.md", title="t", kind="eval_memory", source="verified-resolution",
                   license="internal", source_uri="incident://7", incident_id=7, text="# t\nbody\n")
    _validate(doc)
    with pytest.raises(ValueError):
        _validate(Document(key="k", title="t", kind="eval_memory", source="s", license="internal",
                           source_uri="incident://8", incident_id=None, text="x"))


def test_signature_from_stored_evidence_matches_live_signature(monkeypatch):
    from datetime import datetime, timezone
    from app import evidence_depth as D
    stamp = datetime(2026, 11, 26, 11, 34, tzinfo=timezone.utc)
    rows = [SimpleNamespace(source="pod_status", error=None, collected_at=stamp, payload=[
                {"name": "p", "containers": [], "resources": {"app": {"limits": {"cpu": "250m"}, "requests": {}}},
                 "admission_defaults": "LimitRanger plugin set: cpu limit for container app"}]),
            SimpleNamespace(source="deployment", error=None, collected_at=stamp,
                            payload={"resources": {"app": {"limits": {}, "requests": {}}}, "probes": {}}),
            SimpleNamespace(source="prometheus", error=None, collected_at=stamp,
                            payload={"pod_cpu_usage_cores": {"p": 0.24}, "pod_cpu_throttled_ratio": {"p": 0.9}, "_query": {}}),
            SimpleNamespace(source="limitrange", error=None, collected_at=stamp, payload=[{"name": "d", "limits": []}]),
            SimpleNamespace(source="pod_status", error=None, collected_at=stamp, payload=[])]   # later re-read is ignored

    class S:
        def scalars(self, _query):
            return rows
    inc = SimpleNamespace(id=1, namespace="dev", workload="w", signal_reasons=["TargetConnectionPoolExhausted"])
    assert M.signature_from_evidence(S(), inc) == [
        "cpu_limit_binding", "limit_from_admission", "namespace_default_limits", "signal:TargetConnectionPoolExhausted"]



def test_static_source_facts_do_not_enter_the_signature():
    static = [{"kind": "source_connection_leak"}, {"kind": "caller_retry_without_backoff"},
              {"kind": "dependency_access_config"}]
    assert M.signature(static + CPU, [], ["X"]) == M.signature(CPU, [], ["X"])


def _hit(ref, source, sig, steps=("set explicit cpu limit",)):
    body = "\n".join([f"# r {ref}", f"signature: {' '.join(sig)}", "verified fix:",
                      *[f"{i}. {s}" for i, s in enumerate(steps, 1)]])
    return {"ref": ref, "source": source, "content": body + "\n", "doc": f"{ref}.md"}


def test_precedent_accepts_observed_memory_and_prefers_operator_verified():
    sig = M.signature(CPU, [], ["X"])
    hits = [_hit("R1", "observed-resolution", sig), _hit("R2", "verified-resolution", sig)]
    best = M.precedent(hits, sig)
    assert best["ref"] == "R2" and best["verified"] is True
    only_observed = M.precedent(hits[:1], sig)
    assert only_observed["ref"] == "R1" and only_observed["verified"] is False
    assert M.precedent([_hit("R3", "closed-incident", sig)], sig) is None
    partial = sig[:1] + ["pool_saturated"]
    assert M.precedent(hits, partial) is None and M.precedent(hits, partial, threshold=0.2)["ref"] == "R2"


def test_precedent_checks_carry_the_verified_steps_as_items_to_confirm():
    sig = M.signature(CPU, [], ["X"])
    hits = [_hit("R1", "verified-resolution", sig, ["re-point the writer service", "allow the app role on the promoted node"])]
    checks = M.precedent_checks(M.precedent(hits, sig), hits)
    assert checks["items"] == ["re-point the writer service", "allow the app role on the promoted node"]
    assert "never justifies an action" in checks["note"] and checks["inherited"] is False
    assert M.precedent_checks(None, hits) is None
    assert M.steps_of("no steps here") == []


def test_continuity_inherits_at_a_lower_bar_and_only_visible_kinds():
    past = ["cpu_limit_binding", "limit_from_admission", "namespace_default_limits"]
    current = ["cpu_limit_binding", "recent_restart", "pool_saturated", "liveness_restart"]   # 1 / 6 shared
    assert M.similarity(current, past) < M.MATCH_JACCARD
    prev = [{"incident_id": 41, "precedent": {"doc": "resolution-7.md", "signature": past}, "kind": "past_incident"}]
    assert M.inherited_precedent(prev, current, ["runbook", "past_incident"]) is None          # 0.17 < 0.25
    closer = ["cpu_limit_binding", "limit_from_admission", "recent_restart"]
    got = M.inherited_precedent(prev, closer, ["runbook", "past_incident"])
    assert got == {"doc": "resolution-7.md", "similarity": 0.5, "signature": past, "verified": True,
                   "inherited": True, "from_incident": 41}
    eval_prev = [{**prev[0], "kind": "eval_memory"}]
    assert M.inherited_precedent(eval_prev, closer, ["runbook", "public_doc", "past_incident"]) is None
    assert M.inherited_precedent(eval_prev, closer, ["runbook", "eval_memory"])["from_incident"] == 41


def test_observed_memory_is_fixed_vocabulary_and_stays_in_one_chunk():
    from app.rag import chunk_markdown
    inc = SimpleNamespace(id=1501, namespace="liveprobe-target", workload="shop-inventory-service-long-name",
                          signal_reasons=["ImagePullBackOff", "bad name; rm -rf"], family_hint="image_pull")
    sig = ["cpu_limit_binding", "dependency_errors", "dependency_multi_endpoint", "liveness_restart",
           "namespace_default_limits", "pool_resets", "pool_saturated", "recent_restart", "signal:ImagePullBackOff"]
    text = M.observed_memory_text(inc, "rollout_undo", sig)
    assert "1. roll the Deployment back to its previous revision" in text
    assert "image_pull" not in text and "rm -rf" not in text and "signals: ImagePullBackOff" in text
    assert text.index("signature:") < text.index("verified fix:") < text.index("signals:")
    chunks = chunk_markdown("observed-1501.md", text)
    assert len(chunks) == 1 and "verified fix:" in chunks[0].content
    hit = {"ref": "R1", "source": "observed-resolution", "content": chunks[0].content}
    assert M.precedent([hit], sig)["similarity"] == 1.0 and M.steps_of(chunks[0].content) == [
        "roll the Deployment back to its previous revision"]
    assert set(M.ACTION_TEXT) >= {"rollout_undo", "rollout_restart", "set_memory_limit", "configmap_restore", "pod_delete"}


def test_memory_channel_matches_by_signature_only():
    cur = ["cpu_limit_binding", "limit_from_admission", "namespace_default_limits", "signal:X"]
    docs = [{"doc": "observed-3.md", "kind": "past_incident", "source": "observed-resolution", "signature": cur},
            {"doc": "resolution-7.md", "kind": "past_incident", "source": "verified-resolution", "signature": cur},
            {"doc": "incident-9.md", "kind": "past_incident", "source": "closed-incident", "signature": cur},
            {"doc": "observed-4.md", "kind": "past_incident", "source": "observed-resolution",
             "signature": ["pool_saturated", "signal:X"]}]
    assert M.best_memory(docs, cur)["doc"] == "resolution-7.md"              # verified wins the tie
    assert M.best_memory(docs[:1], cur)["doc"] == "observed-3.md"
    assert M.best_memory(docs[2:], cur) is None                              # summaries and non-matching memory
    assert M.best_memory(docs, ["signal:X"]) is None                         # signals alone never match
