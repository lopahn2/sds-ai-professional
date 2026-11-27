"""P15 operations overview: raw resource metrics + topology + incident location, never inferred zero/health."""
from datetime import datetime, timezone

from conftest import needs_db


def inventory():
    return {
        "nodes": [{"metadata": {"name": "node-1"}, "status": {"capacity": {"cpu": "4", "memory": "8Gi"},
                   "conditions": [{"type": "Ready", "status": "True"}]}}],
        "namespaces": [{"metadata": {"name": "empty"}}],
        "deployments": [{"metadata": {"namespace": "apps", "name": "api"},
                         "spec": {"replicas": 2}, "status": {"readyReplicas": 1}},
                        {"metadata": {"namespace": "empty", "name": "asleep"},
                         "spec": {"replicas": 0}, "status": {"readyReplicas": 0}}],
        "pods": [{"metadata": {"namespace": "apps", "name": "api-abc-1",
                               "ownerReferences": [{"kind": "ReplicaSet", "name": "api-abc"}]},
                  "spec": {"nodeName": "node-1"}, "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}},
                 {"metadata": {"namespace": "apps", "name": "api-abc-2",
                               "ownerReferences": [{"kind": "ReplicaSet", "name": "api-abc"}]},
                  "spec": {"nodeName": "node-1"}, "status": {"phase": "Pending"}},
                 {"metadata": {"namespace": "apps", "name": "finished",
                               "ownerReferences": [{"kind": "Job", "name": "one-shot"}]},
                  "spec": {"nodeName": "node-1"}, "status": {"phase": "Succeeded"}}],
        "services": [], "endpoints": [], "ingresses": [], "errors": [],
    }


def sample(labels, value):
    return {"metric": labels, "value": [1, str(value)]}


def test_overview_raw_metrics_and_incident_location():
    from app.overview import assemble
    inv = inventory()
    inc = {"id": 55, "namespace": "apps", "workload": "api", "status": "analyzing", "severity_level": "NORMAL",
           "stage": {"key": "analyzing", "label": "Agent 분석 중"}, "created_at": "2026-10-19T00:00:00Z"}
    values = {
        "pod_cpu": [sample({"namespace": "apps", "pod": "api-abc-1"}, 0.123)],
        "pod_mem": [sample({"namespace": "apps", "pod": "api-abc-1"}, 123456)],
        "pod_restarts": [sample({"namespace": "apps", "pod": "api-abc-1"}, 3)],
        "node_cpu": [sample({"node": "node-1"}, 1.23)],
        "node_mem": [sample({"node": "node-1"}, 456789)],
        "req_rate": [sample({"namespace": "apps", "app": "api"}, 2.4)],
        "err_rate": [sample({"namespace": "apps", "app": "api"}, 0.25)],
        "p95_ms": [sample({"namespace": "apps", "app": "api"}, "NaN")],
    }
    ev = [{"metadata": {"namespace": "apps"}, "type": "Warning", "reason": "Failed", "count": 2,
           "involvedObject": {"kind": "Pod", "name": "api-abc-2"}, "lastTimestamp": "2026-10-19T00:00:01Z",
           "message": 'Failed to pull image "verbatim:bad"'}]
    d = assemble(inv, values, events=ev, incidents=[inc], today={"total": 1, "resolved": 0, "handed_off": 0, "failed": 0})
    w = next(x for x in d["workloads"] if x["id"] == "apps/Deployment/api")
    assert w["desired"] == 2 and w["ready"] == 1 and w["incident"]["id"] == 55
    assert w["cpu"] == 0.123 and w["mem"] == 123456 and w["restarts_1h"] == 3
    assert w["req_rate"] == 2.4 and w["err_rate"] == 0.25 and w["p95_ms"] is None
    n = d["cluster"]["nodes"][0]
    assert n["cpu_cores"] == 4 and n["mem_bytes"] == 8 * 1024**3
    assert n["cpu_used_cores"] == 1.23 and n["mem_used_bytes"] == 456789 and n["pods"] == 2
    ns = next(n for n in d["cluster"]["namespaces"] if n["name"] == "apps")
    assert ns["pods_running"] == 1 and ns["pods_pending"] == 1 and ns["pods_failed"] == 0
    assert ns["workloads_unhealthy"] == 1          # completed Job is NOT unhealthy
    empty = next(w for w in d["workloads"] if w["name"] == "asleep")
    assert empty["desired"] == 0 and empty["ready"] == 0   # zero-replica workloads must not disappear
    assert empty["cpu"] is None and empty["restarts_1h"] is None
    assert d["events"][0]["message"] == ev[0]["message"]
    assert next(n for n in d["topology"]["nodes"] if n["id"] == w["id"])["warnings"][0]["incident_id"] == 55


def test_missing_metrics_are_null_and_source_failure_is_visible():
    from app.overview import assemble
    d = assemble(inventory(), {}, errors={"prometheus": "HTTPStatusError", "events": "TimeoutError"})
    assert d["sources"]["prometheus"] != "ok" and "TimeoutError" in d["sources"]["kubernetes"]
    assert d["cluster"]["nodes"][0]["cpu_used_cores"] is None
    assert all(w["cpu"] is None for w in d["workloads"])
    assert d["sources"]["hubble"] != "connected" and d["sources"]["envoy"] != "ok"


def test_atlas_view_preserves_displayed_health_and_incoming_traffic_without_raw_details():
    from app.overview import atlas_view

    api = "dev/Deployment/api"
    ingress = "kube-system/Ingress/gateway"
    service = {"id": api, "namespace": "dev", "name": "api", "kind": "Deployment", "role": "service", "status": "ready",
               "services": [{"name": "api", "endpoints_ready": 1, "ports": [8080]}],
               "network_warnings": [{"peer": ingress, "errors": 1}]}
    warning = {"message": "raw diagnostic"}
    edge = {"from": ingress, "to": api, "last_seen": 120, "req_rate": 4.0, "err_rate": None, "ports": {"8080": 8}}
    full = {"generated_at": "2026-11-24T00:00:00Z", "sources": {"kubernetes": "error: events:TimeoutError"},
            "workloads": [{**service, "req_rate": None, "cpu": .2}, {"id": "job", "namespace": "dev", "kind": "Job"}],
            "incidents": {"open": [{"id": 8}], "today": {"total": 1}}, "events": [warning],
            "topology": {"nodes": [service, {"id": ingress, "namespace": "kube-system", "kind": "Ingress"}],
                         "contract_edges": [{"from": api, "to": ingress}], "observed_edges": [edge]}}
    result = atlas_view(full)
    assert result["sources"] == full["sources"] and result["incidents"] == {"open": [{"id": 8}]}
    assert "events" not in result and full["events"] == [warning]
    assert len(result["workloads"]) == 1 and result["workloads"][0]["req_rate"] is None
    node, = result["topology"]["nodes"]
    assert node["warning"] is True and node["services"] == [{"name": "api", "endpoints_ready": 1}]
    assert result["topology"]["observed_edges"] == [{k: value for k, value in edge.items() if k != "ports"}]
    assert result["topology"]["contract_edges"] == []


def test_atlas_assembly_matches_full_projection_for_mixed_health_and_traffic(monkeypatch):
    from app import overview
    from app.config import AppConfig

    inv = inventory()
    for rows in inv.values():
        for row in rows:
            metadata = row.get("metadata", {})
            for key in ("namespace", "name"):
                if metadata.get(key) in ("apps", "empty"):
                    metadata[key] = {"apps": "dev", "empty": "prd"}[metadata[key]]
    inv["services"] = [{"metadata": {"namespace": "dev", "name": "api"},
                        "spec": {"selector": {"app": "api"}}}]
    inv["pods"][0]["metadata"]["labels"] = {"app": "api"}
    labels = {"source_workload_namespace": "prd", "source_workload": "asleep",
              "destination_workload_namespace": "dev", "destination_workload": "api"}
    app = {"namespace": "dev", "app": "api"}
    values = {"req_rate": [sample(app, 6)], "err_rate": [sample(app, "NaN")], "p95_ms": [sample(app, 33)],
              "edge_req": [sample(labels, 6)], "edge_err": [sample(labels, .25)]}
    events = [{"metadata": {"namespace": "dev"}, "reason": "Failed", "message": "기존 원문"}]
    incidents = [{"id": 1, "namespace": "dev", "workload": "api", "status": "analyzing"},
                 {"id": 2, "namespace": "stg", "workload": "api", "status": "analyzing"}]
    options = dict(events=events, incidents=incidents, errors={"events": "TimeoutError"},
                   cfg=AppConfig(target_namespaces=["dev", "prd"], historical_namespaces=["stg"]))
    projected = overview.atlas_view(overview.assemble(inv, values, **options))

    def format_warning(_):
        raise AssertionError("raw Warning formatted")

    monkeypatch.setattr(overview, "_event", format_warning)
    candidate = overview.assemble(inv, values, atlas=True, **options)
    candidate.pop("generated_at")
    projected.pop("generated_at")
    assert candidate == projected
    assert [i["id"] for i in candidate["incidents"]["open"]] == [1]
    assert candidate["topology"]["observed_edges"][0]["err_rate"] == .25
    assert {w["id"] for w in candidate["workloads"]} == {"dev/Deployment/api", "prd/Deployment/asleep"}


def test_metrics_reuse_one_transport_and_keep_each_source_outcome(monkeypatch):
    from types import SimpleNamespace
    import httpx
    from app import overview

    address = "http://prometheus.invalid"
    started, queried, closed = [], [], []
    vector = [sample({"namespace": "dev"}, 2)]

    class Client:
        def __init__(self, *, timeout, limits):
            started.append((timeout, limits.max_connections))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)

        def get(self, url, *, params):
            query = params["query"]
            assert url == f"{address}/api/v1/query"
            queried.append(query)
            req = httpx.Request("GET", url)
            if query == overview.QUERIES["pod_mem"]:
                return httpx.Response(503, request=req)
            result_type = "scalar" if query == overview.QUERIES["edge_err"] else "vector"
            return httpx.Response(200, request=req, json={
                "status": "success", "data": {"resultType": result_type, "result": vector},
            })

    monkeypatch.setattr(overview.httpx, "Client", Client)
    values, errors = overview.fetch_metrics(SimpleNamespace(prometheus_url=address))

    assert started == [(4, 5)] and closed == [True]
    assert sorted(queried) == sorted(overview.QUERIES.values())
    assert values["pod_cpu"] == vector
    assert values["pod_mem"] == values["edge_err"] == []
    assert errors == {"prometheus": "pod_mem:HTTPStatusError, edge_err:invalid_response"}


def test_metrics_report_unobserved_series_if_transport_cannot_start(monkeypatch):
    from types import SimpleNamespace
    from app import overview

    def fail(**_):
        raise OSError("transport setup")

    monkeypatch.setattr(overview.httpx, "Client", fail)
    values, errors = overview.fetch_metrics(SimpleNamespace(prometheus_url="http://prometheus.invalid"))

    assert values == {name: [] for name in overview.QUERIES}
    assert errors == {"prometheus": ", ".join(f"{name}:OSError" for name in overview.QUERIES)}


def test_atlas_metrics_share_results_with_detail_but_respect_age_and_source(monkeypatch):
    from types import SimpleNamespace
    from app import overview

    calls, cache = [], {}
    clock = [1.0]
    monkeypatch.setattr(overview, "monotonic", lambda: clock[0])

    def query(url, names):
        calls.append((url, tuple(names)))
        return {name: ([sample({"namespace": "dev"}, clock[0])], None)
                if name != "pod_cpu" else ([], "unavailable") for name in names}

    monkeypatch.setattr(overview, "_query_metrics", query)
    first = SimpleNamespace(prometheus_url="http://first.invalid")
    rows, errors = overview.fetch_metrics(first, atlas=True, cache=cache)
    assert tuple(rows) == overview.ATLAS_METRICS and not errors
    clock[0] = 4.0
    full, errors = overview.fetch_metrics(first, cache=cache)
    assert set(full) == set(overview.QUERIES) and errors == {"prometheus": "pod_cpu:unavailable"}
    assert all(full[name] == rows[name] for name in rows)
    assert set(calls[-1][1]) == set(overview.QUERIES) - set(rows)
    assert overview.fetch_metrics(first, atlas=True, cache=cache) == (rows, {})
    assert len(calls) == 2
    clock[0] = 12.0
    overview.fetch_metrics(first, cache=cache)
    assert calls[-1][1] == overview.ATLAS_METRICS
    other = SimpleNamespace(prometheus_url="http://second.invalid")
    overview.fetch_metrics(other, atlas=True, cache=cache)
    assert calls[-1] == ("http://second.invalid/api/v1/query", overview.ATLAS_METRICS)
    assert set(cache["samples"]) == set(overview.ATLAS_METRICS)


def test_stage_projection_preserves_legacy_stage_labels_and_skips_fixed_incidents():
    from types import SimpleNamespace
    from sqlalchemy.dialects.postgresql import dialect
    from app.narrative import stage_of
    from app.overview import incident_stages

    incidents = [
        SimpleNamespace(id=1, status="analyzing", handoff=None, status_reason=None),
        SimpleNamespace(id=2, status="open", handoff=None, status_reason=None),
        SimpleNamespace(id=3, status="action_pending", handoff=None, status_reason=None),
        SimpleNamespace(id=4, status="resolved", handoff={"observed_recovery": True}, status_reason="정상 · 회복 관측"),
        SimpleNamespace(id=5, status="analyzing", handoff=None, status_reason=None),
    ]
    rows = [
        SimpleNamespace(incident_id=1, kind="agent_started", detail={"role": "group_verifier"}),
        SimpleNamespace(incident_id=1, kind="agent_started", detail=["malformed"]),
        SimpleNamespace(incident_id=2, kind="evidence_collected", detail={}),
        SimpleNamespace(incident_id=5, kind="agent_started", detail={"role": "k8s_analyst"}),
    ]

    class Session:
        def scalars(self, statement):
            assert list(statement.compile().params.values()) == [[1, 2, 5]]
            return [1, 5]

        def execute(self, statement):
            compiled = statement.compile(dialect=dialect())
            assert [1, 2, 5] in compiled.params.values()
            assert "PARTITION BY run_events.incident_id, run_events.kind ORDER BY run_events.seq DESC" in str(compiled)
            assert 1 in compiled.params.values()
            return rows

    expected = {
        incident.id: stage_of(incident, [object()] if incident.id in (1, 5) else [], [],
                              [row for row in rows if row.incident_id == incident.id])
        for incident in incidents
    }
    assert incident_stages(Session(), incidents) == expected


def test_envoy_edge_metrics_join_only_observed_workloads():
    from app.overview import assemble
    labels = {"source_workload_namespace": "apps", "source_workload": "api",
              "destination_workload_namespace": "empty", "destination_workload": "asleep"}
    d = assemble(inventory(), {"edge_req": [sample(labels, 1.1)], "edge_err": [sample(labels, 0.4)]})
    e = next(e for e in d["topology"]["observed_edges"] if e.get("req_rate") is not None)
    assert e["from"] == "apps/Deployment/api" and e["to"] == "empty/Deployment/asleep"
    assert e["req_rate"] == 1.1 and e["err_rate"] == 0.4
    assert e["state"] == "degraded" and d["sources"]["envoy"] == "ok"


@needs_db
def test_overview_endpoint_loads_real_incident_stage(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, db, main, overview
    from app.models import Incident, RoutingDecision, RunEvent
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with db.session() as s:
        i = Incident(fingerprint="overview", namespace="apps", workload="api", title="bad image", status="analyzing",
                     handoff={"claimed_by": "local-operator", "summary": "preserved handoff body"})
        s.add(i)
        s.flush()
        iid = i.id
        s.add(RoutingDecision(incident_id=iid, level="NORMAL", workflow="normal_group",
                              features={}, rationale="probe", decided_by="rules"))
        s.add_all(RunEvent(incident_id=iid, seq=index, kind="agent_started", summary="prior event", detail=detail)
                  for index, detail in enumerate(({"role": "k8s_analyst"}, {"role": "group_verifier"}, ["legacy detail"]), 1))
    monkeypatch.setattr(overview, "fetch_metrics", lambda *args, **kwargs: ({}, {}))
    class K:
        event_limits = []
        deny_warnings = False

        def cluster_inventory(self):
            return inventory()

        def cluster_warning_events(self, *, limit=500):
            self.event_limits.append(limit)
            if self.deny_warnings:
                raise PermissionError("warning collection unavailable")
            return []
    with TestClient(main.app, base_url="http://liveprobe.localhost") as c:
        kube = K()
        monkeypatch.setitem(main.STATE, "kube", kube)
        monkeypatch.setitem(main.STATE, "flows", None)
        main._TOPO_CACHE.clear()
        r = c.get("/api/overview")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["incidents"]["open"][0]["id"] == iid
        pin = d["incidents"]["open"][0]
        assert pin["claimed"] is True
        assert pin["stage"] == {"key": "verifying", "label": "결과 확인 중 — 결과 확인"}
        with db.session() as session:
            assert session.get(Incident, iid).handoff["summary"] == "preserved handoff body"
        assert d["incidents"]["today"]["total"] == 1
        assert d["cluster"]["namespaces"] and d["sources"]["kubernetes"] == "ok"
        projected = c.get("/api/overview", headers={"X-LiveProbe-Overview-View": "atlas"})
        assert projected.status_code == 200
        assert kube.event_limits == [500, 1]
        assert "X-LiveProbe-Overview-View" in r.headers["Vary"] and "X-LiveProbe-Overview-View" in projected.headers["Vary"]
        expected = overview.atlas_view(d)
        assert {k: v for k, v in projected.json().items() if k != "generated_at"} == {
            k: v for k, v in expected.items() if k != "generated_at"}
        kube.deny_warnings = True
        unavailable = c.get("/api/overview", headers={"X-LiveProbe-Overview-View": "atlas"}).json()
        assert "events:PermissionError" in unavailable["sources"]["kubernetes"]
        assert unavailable["incidents"]["open"][0]["id"] == iid
    config.get_settings.cache_clear()


def test_p95_falls_back_to_destination_envoy_latency_only_when_app_histogram_missing():
    """앱 히스토그램이 없으면 서버측 Envoy(destination) 지연 분포에서 p95를 읽는다. 둘 다 없으면 미관측(None)."""
    from app.overview import QUERIES, assemble
    assert "istio_request_duration_milliseconds_bucket" in QUERIES["p95_ms"] and 'reporter="destination"' in QUERIES["p95_ms"]
    assert "http_server_requests_seconds_bucket" in QUERIES["p95_ms"]
    inv = inventory()
    values = {"p95_ms": [sample({"namespace": "apps", "app": "api"}, 42.5)]}
    d = assemble(inv, values, events=[], incidents=[], today={"total": 0, "resolved": 0, "handed_off": 0, "failed": 0})
    assert next(x for x in d["workloads"] if x["id"] == "apps/Deployment/api")["p95_ms"] == 42.5
