"""정돈-03/04: K8s 신규 변경 생성 차단(기존 이력 보존)과 전체 이력 서버 필터·페이지·출처·검토 원장 CSV.

격리 SQLite만 사용한다. 클러스터·모델·운영 DB 접속 없음. lifespan은 실행하지 않는다.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

from isolated_sqlite import isolated_k8s_db, table_counts

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


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
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with isolated_k8s_db(tmp_path) as url:
        yield url
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()


def _client():
    from fastapi.testclient import TestClient
    from app import main
    return TestClient(main.app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"})


def _incidents(rows):
    """rows: (namespace, fingerprint, system, status). Returns ids oldest→newest."""
    from app import db
    from app.models import Incident
    ids = []
    with db.session() as s:
        for n, (ns, fp, system, status) in enumerate(rows):
            inc = Incident(fingerprint=fp, namespace=ns, workload=f"w{n}", status=status, title=f"Signal {n}",
                           system=system, signal_reasons=["X"], created_at=T0 + timedelta(minutes=n),
                           updated_at=T0 + timedelta(minutes=n))
            s.add(inc)
            s.flush()
            ids.append(inc.id)
    return ids


# ------------------------------------------------------------------ 정돈-03
class _Kube:
    def __init__(self):
        self.calls = 0

    def get_deployment(self, *a, **k):
        self.calls += 1
        raise AssertionError("change creation must not read the cluster any more")


def test_new_k8s_change_request_is_refused_without_writes_and_existing_history_stays_readable(k8s, operator_gateway):
    from app import db
    from app.changes import ChangeError, ChangeService, auto_create_for_incident
    from app.change_models import ChangeRequest
    from app.config import AppConfig
    from app.models import AgentRun, AuditLog
    iid, old = _incidents([("prd", "f1", "samsung-trade", "handed_off"), ("prd", "f2", "samsung-trade", "handed_off")])
    with db.session() as s:
        s.add(ChangeRequest(incident_id=old, system="samsung-trade", service="order", namespace_origin="prd",
                            status="stg_verified", created_by="liveprobe", stages={"dev": {"status": "verified"}},
                            source_files=[], tests={}, build={}))
        s.add(AgentRun(incident_id=iid, role="cause_reasoner", provider="p", model="m", status="succeeded", attempts=1,
                       output={"remediation": {"kind": "code_fix", "summary": "fix"}}))
    kube = _Kube()
    service = ChangeService(AppConfig(), None, None, kube)
    before = table_counts(["change_requests", "runner_jobs"])
    with pytest.raises(ChangeError) as e:
        service.create_for_incident(iid, "local-operator")
    assert (e.value.code, e.value.status_code) == ("change_creation_moved_to_application", 410)
    # Existing request: returned unchanged (in-progress work is preserved, not cancelled or moved).
    existing = service.create_for_incident(old)
    assert existing["status"] == "stg_verified"
    # Engine path: a code-fix conclusion records only a routing receipt.
    engine = type("E", (), {"capture": None, "cfg": AppConfig(), "changes": service})()
    auto_create_for_incident(engine, iid)
    assert table_counts(["change_requests", "runner_jobs"]) == before and kube.calls == 0
    with db.session() as s:
        assert [a.event for a in s.query(AuditLog).filter_by(incident_id=iid)] == ["code_fix_routed_to_application"]
    # Read paths still list the stored history.
    from app import main
    main.STATE["changes"] = service
    try:
        c = _client()
        assert [x["id"] for x in c.get(f"/api/changes?incident_id={old}").json()["items"]] == [existing["id"]]
        promotion = c.post(f"/api/changes/{existing['id']}/promote", json={
            "stage": "prd", "plan_sha256": "a" * 64, "confirm": True})
        assert promotion.status_code == 410
        assert table_counts(["change_requests", "runner_jobs"]) == before
        r = c.post(f"/api/incidents/{iid}/changes")
        assert r.status_code == 410 and r.json()["detail"] == "change_creation_moved_to_application"
    finally:
        main.STATE.pop("changes", None)


# ------------------------------------------------------------------ 정돈-04
def test_environment_origin_and_system_filters_run_on_the_whole_history_before_paging(k8s):
    rows = [("prd", "eval/c/a/1", "samsung-trade", "resolved")] + \
           [("dev", f"f{n}", "samsung-trade", "resolved") for n in range(60)] + \
           [("prd", "f-old", "legacy-system", "failed")]
    ids = _incidents(rows)
    c = _client()
    # 60 newer dev incidents: the prd ones are outside the first 50 and still found.
    prd = c.get("/api/incidents?environment=prd&limit=50").json()
    assert prd["total"] == 2 and {i["id"] for i in prd["items"]} == {ids[0], ids[-1]}
    ev = c.get("/api/incidents?origin=evaluation").json()
    assert ev["total"] == 1 and ev["items"][0]["origin"] == "evaluation"
    assert c.get("/api/incidents?origin=unconfirmed").json()["total"] == 61
    legacy = c.get("/api/incidents?system=legacy-system").json()
    assert legacy["total"] == 1 and legacy["items"][0]["system"] == "legacy-system"   # past system names stay searchable
    # Same filter: total equals the union of its pages.
    p1 = c.get("/api/incidents?environment=dev&limit=50").json()
    p2 = c.get(f"/api/incidents?environment=dev&limit=50&offset=50&as_of={p1['as_of']}").json()
    assert p1["total"] == p2["total"] == 60 and len({i["id"] for i in p1["items"] + p2["items"]}) == 60
    # Empty, unknown and invalid filters.
    assert c.get("/api/incidents?environment=qa").json()["total"] == 0
    assert c.get("/api/incidents?environment=prd&status=open").json()["items"] == []
    assert c.get("/api/incidents?origin=guess").status_code == 422
    assert c.get("/api/incidents?as_of=-1").status_code == 422


def test_as_of_keeps_later_pages_stable_while_new_incidents_arrive(k8s):
    _incidents([("dev", f"f{n}", None, "resolved") for n in range(5)])
    c = _client()
    first = c.get("/api/incidents?limit=2").json()
    _incidents([("dev", "new", None, "open")])                      # concurrent new incident
    second = c.get(f"/api/incidents?limit=2&offset=2&as_of={first['as_of']}").json()
    third = c.get(f"/api/incidents?limit=2&offset=4&as_of={first['as_of']}").json()
    seen = [i["id"] for i in first["items"] + second["items"] + third["items"]]
    assert len(seen) == len(set(seen)) == 5 and second["total"] == 5
    assert second["newer_since_as_of"] == 1


def test_list_cost_uses_the_shared_contract(k8s):
    from app import db
    from app.models import AgentRun
    a, b, z, none = _incidents([("dev", f"f{n}", None, "resolved") for n in range(4)])
    with db.session() as s:
        for iid, cost in ((a, 0.1), (a, 0.2), (b, 0.2), (b, None), (z, 0.0)):
            s.add(AgentRun(incident_id=iid, role="router", provider="p", model="m", status="succeeded", attempts=1,
                           estimated_cost_usd=cost))
    items = {i["id"]: i for i in _client().get("/api/incidents").json()["items"]}
    assert items[a]["cost_status"] == "observed" and items[a]["total_cost_usd"] == pytest.approx(0.3)
    assert items[b]["cost_status"] == "partial" and items[b]["total_cost_usd"] is None and items[b]["known_cost_usd"] == 0.2
    assert items[z]["cost_status"] == "observed" and items[z]["total_cost_usd"] == 0.0
    assert items[none]["cost_status"] == "no_runs" and items[none]["agent_run_count"] == 0


def test_review_ledger_environment_filter_paging_and_full_csv(k8s):
    from app import db
    from app.models import ReviewLedger
    dev, prd = _incidents([("dev", "f1", None, "handed_off"), ("prd", "f2", None, "handed_off")])
    with db.session() as s:
        for n in range(1205):
            s.add(ReviewLedger(incident_id=prd if n % 2 else dev, reason_code="low_confidence" if n % 3 else "high_risk_level",
                               profile="poc", rationale=f"=r{n}" if n == 1 else f"r{n}", created_at=T0))
    c = _client()
    page = c.get("/api/review-ledger?environment=prd&limit=200&offset=400").json()
    assert page["total"] == 602 and len(page["items"]) == 200 and all(e["environment"] == "prd" for e in page["items"])
    assert sum(page["by_reason"].values()) == 602
    assert c.get("/api/review-ledger?environment=qa").json()["total"] == 0
    r = c.get("/api/review-ledger.csv?environment=prd")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert r.headers["X-LiveProbe-Rows"] == "602" and len(rows) == 602        # no 1000-row cap, same filter as the page
    assert {row["environment"] for row in rows} == {"prd"}
    assert any(row["rationale"] == "'=r1" for row in rows)                    # formula neutralisation preserved
    assert len(list(csv.DictReader(io.StringIO(c.get("/api/review-ledger.csv").text)))) == 1205


def test_calendar_days_and_exact_workload_before_paging(k8s):
    from app import db
    from app.models import Incident
    rows = _incidents([("dev", f"event/{n}", "legacy-system", "resolved") for n in range(3)])
    with db.session() as s:
        for i, minute in zip(rows, (-1, 0, 1440)):
            s.get(Incident, i).created_at = T0 + timedelta(hours=15, minutes=minute)
    query = dict(start="2026-10-02", end="2026-10-02", system="legacy-system")
    client = _client()
    payload = client.get("/api/incidents", params=query).json()
    assert payload["total"] == 1 and payload["items"][0]["id"] == rows[1]
    assert client.get("/api/incidents", params={**query, "service":"w0"}).json()["total"] == 0
    assert client.get("/api/incidents", params={**query, "end":"2026-09-30"}).status_code == 422
