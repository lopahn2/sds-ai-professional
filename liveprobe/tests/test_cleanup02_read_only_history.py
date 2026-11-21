"""정돈-02/07: 기록 열람은 해석 작업·DB 쓰기를 만들지 않고, 비용은 전부/일부/미수집/실제 0을 구분한다.

격리 SQLite(tests/isolated_sqlite.py)만 사용한다. conftest·운영/시험 DB·모델·클러스터에 접속하지 않는다.
lifespan을 실행하지 않도록 TestClient를 context manager 없이 쓴다(마이그레이션·백그라운드 기동 없음).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from isolated_sqlite import isolated_k8s_db, table_counts

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class Recorder:
    """InterpretWorker stand-in that records every scheduling call."""

    def __init__(self):
        self.requests, self.views, self.explicit = [], [], []
        self.jobs: dict[int, str] = {}

    def request(self, iid, idle=False):
        self.requests.append((iid, idle))

    def seen(self, iid):
        self.views.append(iid)

    def status(self, iid):
        return self.jobs.get(iid)

    def request_explicit(self, iid):
        self.explicit.append(iid)
        if iid in self.jobs:
            return self.jobs[iid]
        self.jobs[iid] = "queued"
        return "accepted"


@pytest.fixture()
def k8s(tmp_path, monkeypatch):
    from app import config
    for k in list(__import__("os").environ):
        if k.startswith("LIVEPROBE_") and k != "LIVEPROBE_CONFIG_FILE":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIVEPROBE_CONFIG_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with isolated_k8s_db(tmp_path) as url:
        yield url
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()


def _seed(status, costs, *, fingerprint="f"):
    from app import db
    from app.models import AgentRun, Evidence, Incident
    with db.session() as s:
        inc = Incident(fingerprint=fingerprint, namespace="liveprobe-target", workload="inventory", status=status,
                       severity_level="NORMAL", title="TargetWorkloadOOMKilled on Deployment/inventory",
                       signal_reasons=["TargetWorkloadOOMKilled"], created_at=T0, updated_at=T0)
        s.add(inc)
        s.flush()
        s.add(Evidence(incident_id=inc.id, source="loki", target="liveprobe-target/loki",
                       payload={"lines": ["[inventory] mysterious internal widget hiccup code=77"]}, collected_at=T0))
        for i, cost in enumerate(costs):
            s.add(AgentRun(incident_id=inc.id, role="signal_analyst" if i else "router", provider="codex_cli", model="m",
                           status="succeeded", attempts=1, estimated_cost_usd=cost,
                           output={"summary": "The inventory container was OOMKilled at 512Mi."}, created_at=T0))
        return inc.id


def _client():
    from fastapi.testclient import TestClient
    from app import main
    return main, TestClient(main.app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"})


def test_cost_contract_distinguishes_observed_partial_unobserved_and_real_zero():
    from app.accounting import cost_of
    assert cost_of([0.1, 0.2]) == {"total_cost_usd": 0.3, "known_cost_usd": 0.3, "cost_missing_runs": 0, "cost_status": "observed"}
    assert cost_of([0.0, 0.0])["total_cost_usd"] == 0.0 and cost_of([0.0])["cost_status"] == "observed"
    partial = cost_of([0.2, None])
    assert partial == {"total_cost_usd": None, "known_cost_usd": 0.2, "cost_missing_runs": 1, "cost_status": "partial"}
    assert cost_of([None, None]) == {"total_cost_usd": None, "known_cost_usd": None, "cost_missing_runs": 2, "cost_status": "unobserved"}
    assert cost_of([])["cost_status"] == "no_runs"
    assert cost_of([float("nan")])["cost_status"] == "invalid"


def test_repeated_views_of_a_closed_incident_schedule_nothing_and_write_nothing(k8s):
    iid = _seed("handed_off", [0.2, None])
    main, c = _client()
    rec = Recorder()
    main.STATE["interpret"] = rec
    try:
        before = table_counts()
        for _ in range(3):
            r = c.get(f"/api/incidents/{iid}/narrative")
            assert r.status_code == 200
        assert table_counts() == before                 # no AgentRun / Interpretation / audit rows
        assert rec.requests == [] and rec.views == [] and rec.explicit == []
        d = r.json()
        assert d["interpreting"] is False
        assert d["interpret_state"]["status"] == "not_requested" and "비용" in d["interpret_state"]["reason_ko"]
        inc = d["incident"]
        assert inc["total_cost_usd"] is None and inc["known_cost_usd"] == 0.2 and inc["cost_status"] == "partial"
        assert inc["origin"] == "unconfirmed"
    finally:
        main.STATE.pop("interpret", None)


def test_active_incident_view_leaves_scheduling_to_pipeline(k8s):
    iid = _seed("analyzing", [0.1])
    main, c = _client()
    rec = Recorder()
    main.STATE["interpret"] = rec
    try:
        before = table_counts()
        d = c.get(f"/api/incidents/{iid}/narrative").json()
        assert table_counts() == before
        assert rec.views == [] and rec.requests == []
        assert d["interpret_state"]["status"] == "pipeline"
        rec.jobs[iid] = "running"
        d2 = c.get(f"/api/incidents/{iid}/narrative").json()
        assert d2["interpreting"] is True and d2["interpret_state"]["status"] == "pending"
    finally:
        main.STATE.pop("interpret", None)


def test_explicit_interpretation_requires_confirmation_and_is_not_duplicated(k8s, operator_gateway):
    iid = _seed("resolved", [None], fingerprint="eval/c1/a1/1")
    main, c = _client()
    rec = Recorder()
    main.STATE["interpret"] = rec
    try:
        assert c.post(f"/api/incidents/{iid}/interpretations", json={}).status_code == 422
        assert c.post(f"/api/incidents/{iid}/interpretations", json={"confirm": True},
                      headers={"X-LiveProbe-Client": ""}).status_code == 403
        assert rec.explicit == []
        first = c.post(f"/api/incidents/{iid}/interpretations", json={"confirm": True})
        assert first.status_code == 202 and first.json()["status"] == "accepted"
        second = c.post(f"/api/incidents/{iid}/interpretations", json={"confirm": True})
        assert second.json()["status"] == "running"             # already queued: reported, not re-queued
        assert rec.explicit == [iid]
        from app import db
        from app.models import AuditLog
        with db.session() as s:
            assert [a.event for a in s.query(AuditLog).filter_by(incident_id=iid)] == ["interpretation_requested"]
        d = c.get(f"/api/incidents/{iid}/narrative").json()
        assert d["incident"]["origin"] == "evaluation" and d["incident"]["cost_status"] == "unobserved"
        assert c.post("/api/incidents/999999/interpretations", json={"confirm": True}).status_code == 404
    finally:
        main.STATE.pop("interpret", None)
