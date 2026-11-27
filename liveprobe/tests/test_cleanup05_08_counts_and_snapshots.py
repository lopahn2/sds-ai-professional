"""정돈-05/08: 공통 사건 집계, K8s·Application 읽기 전용 해시 스냅샷과 evaluation.offline 연결.

격리 tmp SQLite만 사용한다. 모델·클러스터·운영 DB 접속 없음.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from isolated_sqlite import isolated_k8s_db, table_counts

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
RAW = "raw-trace token=synthetic-secret-value stacktrace"
WORK = Path(__file__).resolve().parents[2]


@pytest.fixture()
def k8s(tmp_path, monkeypatch):
    import os
    from app import config
    for k in list(os.environ):
        if k.startswith("LIVEPROBE_"):
            monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("target_namespaces: [dev, stg, prd]\n")
    monkeypatch.setenv("LIVEPROBE_CONFIG_FILE", str(cfg))
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with isolated_k8s_db(tmp_path) as url:
        yield url
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()


def seed():
    from app import db
    from app.models import AgentRun, Evidence, Incident, RoutingDecision
    with db.session() as s:
        rows = [("prd", "eval/c/1", "resolved", None), ("prd", "f-live", "handed_off", None),
                ("dev", "f-claimed", "handed_off", {"claimed_by": "op"}), ("dev", "f-run", "analyzing", None),
                ("stg", "f-wait", "action_pending", None), ("prd", "f-closed", "closed", None)]
        ids = []
        for n, (ns, fp, status, handoff) in enumerate(rows):
            inc = Incident(fingerprint=fp, namespace=ns, workload=f"w{n}", status=status, title=f"{RAW} {n}",
                           system="samsung-trade", signal_reasons=[RAW], handoff=handoff,
                           created_at=T0 + timedelta(minutes=n), updated_at=T0 + timedelta(minutes=n))
            s.add(inc)
            s.flush()
            ids.append(inc.id)
        s.add(RoutingDecision(incident_id=ids[0], level="NORMAL", workflow="w", features={"raw": RAW}, rationale=RAW,
                              decided_by="rules", confidence=0.9))
        s.add(Evidence(incident_id=ids[0], source="pod_log", target="prd/w0", payload={"log": RAW}, error=None))
        s.add(Evidence(incident_id=ids[1], source="k8s_event", target="prd/w1", payload=None, error=RAW))
        s.add(AgentRun(incident_id=ids[0], role="router", provider="codex", model="m-old", status="succeeded", attempts=1,
                       estimated_cost_usd=0.1, input_tokens=10, output_tokens=5, output={"text": RAW}))
        s.add(AgentRun(incident_id=ids[0], role="cause_reasoner", provider="codex", model="m-old", status="failed", attempts=2,
                       estimated_cost_usd=None, output=None, error=RAW))
    return ids


# ------------------------------------------------------------------ 정돈-05
def test_shared_counts_separate_progress_approval_and_unclaimed_handoff(k8s):
    from app import db
    from app.incident_counts import incident_counts
    seed()
    before = table_counts()
    with db.session() as s:
        c = incident_counts(s, ["dev", "stg", "prd"])
    assert (c["automatic"], c["awaiting_approval"], c["unclaimed_handoff"], c["in_progress"]) == (1, 1, 1, 2)
    assert c["closed_history"] == 2                       # resolved + closed; never counted as live work
    assert c["by_environment"]["prd"]["unclaimed_handoff"] == 1 and c["by_environment"]["dev"]["unclaimed_handoff"] == 0
    assert c["by_environment"]["stg"]["in_progress"] == 1
    assert c["map_handoff_window_hours"] == 6
    assert table_counts() == before                       # counting writes nothing


def test_shared_counts_preserve_truthiness_of_legacy_claim_markers(k8s):
    from app import db
    from app.incident_counts import incident_counts
    from app.models import Incident

    unclaimed = [None, False, "", 0, [], {}]
    claimed = [True, "operator", 1, ["operator"], {"name": "operator"}]
    with db.session() as s:
        for index, marker in enumerate(unclaimed + claimed, 1):
            s.add(Incident(id=index, fingerprint=f"legacy-claim-{index}", namespace="dev", workload="order",
                           status="handed_off", handoff={"claimed_by": marker}))
    with db.session() as s:
        result = incident_counts(s, ["dev"])
    assert result["unclaimed_handoff"] == len(unclaimed)
    assert result["by_environment"]["dev"]["unclaimed_handoff"] == len(unclaimed)


# ------------------------------------------------------------------ 정돈-08 K8s
def test_k8s_snapshot_is_read_only_hashed_reproducible_and_keeps_unknowns(k8s, tmp_path):
    from app import db
    from app.snapshot_export import build_snapshot, export
    ids = seed()
    before = table_counts()
    out = tmp_path / "exports" / "k8s.json"
    receipt = export(out, as_of=ids[-1])
    assert table_counts() == before
    text = out.read_text("utf-8")
    assert RAW not in text and "synthetic-secret-value" not in text          # no raw trace / token text
    snap = json.loads(text)
    assert snap["content_sha256"] == receipt["content_sha256"]
    first = snap["incidents"][0]
    assert first["origin"] == "evaluation" and snap["incidents"][1]["origin"] == "unconfirmed"
    assert first["cost"]["cost_status"] == "partial" and first["cost"]["total_cost_usd"] is None
    assert first["cost"]["known_cost_usd"] == pytest.approx(0.1)
    assert first["accounting"]["cost_usd"] == {"total": None, "known_subtotal": pytest.approx(0.1), "missing_rows": 1}
    assert [r["model"] for r in first["runs"]] == ["m-old", "m-old"]           # stored model kept as recorded
    assert first["runs"][1]["output_sha256"] is None and first["runs"][1]["error_sha256"]
    assert snap["incidents"][1]["evidence"][0]["failed"] is True
    # Same state + same as_of → same content hash; a new incident after as_of does not change it.
    with db.session() as s:
        again = build_snapshot(s, as_of=ids[-1], environment_of=lambda ns: ns)
        s.rollback()
    assert again["content_sha256"] == snap["content_sha256"]
    from app.models import Incident
    with db.session() as s:
        s.add(Incident(fingerprint="later", namespace="dev", workload="new", status="open", title="new"))
    with db.session() as s:
        assert build_snapshot(s, as_of=ids[-1], environment_of=lambda ns: ns)["content_sha256"] == snap["content_sha256"]
        assert build_snapshot(s, origin="evaluation")["counts"]["incidents"] == 1
        s.rollback()
    with pytest.raises(FileExistsError):
        export(out)                                                            # never overwrites an export


def test_k8s_snapshot_records_feed_existing_offline_utility_without_inventing_values(k8s, tmp_path):
    from app.snapshot_export import export
    seed()
    out = tmp_path / "k8s.json"
    export(out)
    result_path = tmp_path / "utility.json"

    def temp_alias(path):
        # Pytest resolves the Mac system aliases. The offline CLI deliberately accepts
        # the public aliases and keeps direct private paths denied.
        for alias in (Path("/tmp"), Path("/var/folders")):
            if alias.exists() and path.is_relative_to(alias.resolve()):
                return alias / path.relative_to(alias.resolve())
        return path

    proc = subprocess.run([sys.executable, "-m", "evaluation.offline", "utility", "--records", str(temp_alias(out)),
                           "--out", str(temp_alias(result_path))],
                          cwd=WORK, capture_output=True, text=True, env={"PYTHONPATH": f"{WORK}:{WORK / 'liveprobe'}", "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    result = json.loads(result_path.read_text("utf-8"))
    assert result["schema"] == "liveprobe-role-utility/v1"
    router = next(r for r in result["roles"] if r["role"] == "router")
    reasoner = next(r for r in result["roles"] if r["role"] == "cause_reasoner")
    assert router["complete_cost_usd"] == pytest.approx(0.1)
    assert reasoner["complete_cost_usd"] is None                               # missing cost stays unknown
    assert all(r["scored"] == 0 and r["mean_quality"] is None for r in result["roles"])   # no labels invented
    assert result["calibration"]["scored"] == 0


# ------------------------------------------------------------------ 정돈-08 Application
def test_application_snapshot_is_read_only_and_hashed(tmp_path):
    import hashlib
    import sqlite3
    from app.application.settings import ApplicationSettings
    from app.application.snapshot_export import export
    from app.application.storage import Incident, Store
    db_path = tmp_path / "app.db"
    store = Store(ApplicationSettings(database_url=f"sqlite:///{db_path}", testing=True))
    store.initialize()
    try:
        with store.transaction() as session:
            session.add(Incident(id="a1", state="handoff", created_at=100.0, document={
                "environment": "stg", "service": "order", "system": "samsung-trade", "reason": RAW,
                "stages": [{"state": "handoff", "occurred_at": 101.0, "details": {"code": "model_transport_failed"}}],
                "error": {"code": "model_transport_failed", "message_ko": "x"},
                "data": {"collection": {"logs": [{"text": RAW}]}, "analysis": {"roles": [
                    {"role": "classifier", "model": "m1", "effort": "low", "provider": "codex", "usage": {"input_tokens": 3},
                     "occurred_at": 100.5, "result": {"summary_ko": RAW}, "output_sha256": "a" * 64}]},
                         "patch": {"diff": RAW, "diff_sha256": "b" * 64, "tests_added": ["t1"]}}}))

        def digest():
            with sqlite3.connect(db_path) as c:
                return hashlib.sha256(repr([c.execute(f'select * from "{t}"').fetchall() for (t,) in
                                            c.execute("select name from sqlite_master where type='table' order by name")]).encode()).hexdigest()
        before = digest()
        out = tmp_path / "app.json"
        receipt = export(store, out)
        assert digest() == before
        text = out.read_text("utf-8")
        assert RAW not in text and "synthetic-secret-value" not in text
        snap = json.loads(text)
        assert receipt["incidents"] == 1 and snap["incidents"][0]["origin"] == "unconfirmed"
        assert snap["incidents"][0]["error_code"] == "model_transport_failed"
        assert snap["incidents"][0]["patch"] == {"diff_sha256": "b" * 64, "tests_added": 1}
        assert snap["records"][0]["cost_usd"] is None and snap["records"][0]["model"] == "m1"
    finally:
        store.engine.dispose()
