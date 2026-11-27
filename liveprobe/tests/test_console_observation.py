"""An operator sees measured service routing, HTTP impact and actual configured collectors separately."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import evidence, observation_sources as sources
from app.config import AppConfig, Settings
from app.flows import FlowStore, build_topology
from app.main import alert_signal
from app.narrative import _impact
from app.router import extract_features
from app.watcher import Watchers
from conftest import family4_kube, needs_db


@pytest.mark.parametrize("mode,state", [
    ("no_service", "unknown"), ("manual", "manual"), ("external", "external_name"),
    ("scaled_down", "scaled_down"), ("alternate_owner", "missing"), ("absent_endpoint", "unknown"),
    ("empty", "missing"), ("ready", "ready")])
def test_endpoint_intent_at_normal_entry_points(mode, state):
    k = family4_kube()
    dep, svc = k.deployments["inventory"], k.services["inventory"]
    ns = "dev"
    svc["metadata"]["namespace"] = ns
    dep["metadata"]["namespace"] = ns
    svc["spec"]["selector"] = {"app": "inventory"}
    if mode == "manual":
        svc["spec"].pop("selector")
    elif mode == "external":
        svc["spec"].update(type="ExternalName", externalName="db.example")
    elif mode in ("scaled_down", "alternate_owner"):
        dep["spec"]["replicas"] = 0
        if mode == "alternate_owner":
            dep["spec"]["template"]["metadata"]["labels"] = {"app": "another"}
    elif mode == "absent_endpoint":
        k.endpoints.clear()
    elif mode == "ready":
        k.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "127.0.0.1"}]}]}
    elif mode == "no_service":
        k.services.clear()

    cfg = AppConfig(target_namespaces=[ns])
    summary = evidence.service_summary(k, ns, "inventory")
    f = extract_features(evidence.Bundle(namespace=ns, workload="inventory", service=summary))
    assert f["service_backend_state"] == state
    assert f["service_has_no_endpoints"] is (True if state == "missing" else False if state == "ready" else None)
    emitted = []
    watcher = Watchers(k, cfg, emitted.append)
    for now in range(0, 601, 15):
        watcher.poll_endpoints_once(now)
    assert bool(emitted) == (state == "missing")
    alert = alert_signal({"labels": {"alertname": "TargetServiceNoEndpoints", "namespace": ns,
                                     "endpoint": "inventory"}}, cfg, k)
    assert bool(alert) == (state == "missing")
    inventory = {"services": list(k.services.values()), "deployments": [dep], "pods": [],
                 "endpoints": [{**ep, "metadata": {"namespace": ns, "name": n}} for n, ep in k.endpoints.items()]}
    topo = build_topology(inventory, [], FlowStore().freshness())
    nodes = {n["kind"]: n for n in topo["nodes"]}
    if mode == "no_service":
        assert "Service" not in nodes and not nodes["Deployment"].get("no_endpoints")
    else:
        assert nodes["Service"]["backend_state"] == state
    if mode == "scaled_down":
        assert nodes["Deployment"]["status"] == "scaled_down"


def test_no_http_samples_cannot_prove_success_or_failure(monkeypatch):
    monkeypatch.setattr(evidence, "_prom", lambda *args: [])
    b = evidence._collect_prom(Settings(), "dev", "inventory", ["inventory"], float("inf"))
    assert b.metrics["http_5xx_rate_per_s"] == {} and b.metrics["http_rate_per_s"] == {}
    svc = {"ref": "E3_service", "ko": "준비된 연결 대상 0개", "severity": "error"}
    inc = SimpleNamespace(workload="inventory", namespace="dev")
    assert "실제 요청 실패는 미확인" in _impact([svc], inc)
    measured = {"ref": "E6_metrics", "raw": '{"http_5xx_rate_per_s":{"inventory": 0.25}}'}
    assert "HTTP 5xx 요청 실패가 초당 0.25건" in _impact([svc, measured], inc)


def test_tag_errors_are_distinct_from_empty_data_and_failed_connection(monkeypatch):
    monkeypatch.setattr(sources, "_get_json", lambda *a: {"status": "success", "data": []})
    assert sources.check_location("loki", "http://approved", None, ["dev"])["state"] == "no_samples"
    rows = [{"namespace": "dev", "app": "pay", "system": s} for s in ("one", "two")]
    rows += [{"namespace": "dev", "app": "other"}]
    monkeypatch.setattr(sources, "_get_json", lambda *a: {"status": "success", "data": rows})
    result = sources.check_location("loki", "http://approved", None, ["dev"])
    assert result["state"] == "observed"
    assert result["tags"] == {"examined": 3, "unassigned": 1, "missing": {"namespace": 0, "app": 0, "system": 1},
                              "ambiguous_services": 1}
    monkeypatch.setattr(sources, "_get_json", lambda *a: {})
    assert sources.check_location("loki", "http://approved", None, ["dev"])["state"] == "unavailable"


@pytest.mark.parametrize(("values", "state"), [(["0", "1"], "partial"), (["0"], "scrape_failed")])
def test_prometheus_reachable_but_targets_not_collected(monkeypatch, values, state):
    rows = [{"metric": {"namespace": "dev", "app": "order", "system": "shop"}, "value": [0, v]} for v in values]
    monkeypatch.setattr(sources, "_get_json", lambda *a: {"status": "success", "data": {"resultType": "vector", "result": rows}})
    actual = sources.check_location("prometheus", "http://approved", None, ["dev"])
    assert actual["state"] == state and actual["failed_scrapes"] == values.count("0")


@needs_db
def test_operator_uses_only_provisioned_location_and_existing_reader_uses_it(operator_gateway, db_url, monkeypatch):
    cfg = Settings(observation_locations={"loki": {"alternate": "http://approved-observer:3100"}})
    monkeypatch.setattr(sources, "get_settings", lambda: cfg)
    monkeypatch.setattr(sources, "_get_json", lambda url, p: {"status": "success", "data": []})
    app = FastAPI()
    app.include_router(sources.source_router({}))
    api = TestClient(app, base_url="http://liveprobe.localhost", headers={"x-liveprobe-client": "dashboard"})
    root = "/api/settings/observation-sources"
    refused = {"location": "http://169.254.169.254"}
    assert api.put(root + "/loki", json=refused).status_code == 422
    assert api.post(root + "/loki/check", json={"location": "unknown"}).status_code == 422
    assert api.put(root + "/loki", json={"location": "alternate", "url": "http://unregistered"}).status_code == 422
    assert api.put(root + "/loki", json={"location": "alternate"}).json() == {"active": "alternate"}
    assert api.post(root + "/loki/check", json={"location": "alternate"}).json()["state"] == "no_samples"
    row = next(i for i in api.get(root).json()["sources"] if i["adapter"] == "loki")
    assert row["active"] == "alternate" and row["check"]["location"] == "alternate"
    # Restart a reader with a fresh Settings object. Resolve from the stored ID and local allowlist.
    assert sources.adapter_url(cfg.model_copy(), "loki") == "http://approved-observer:3100"
