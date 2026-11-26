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
