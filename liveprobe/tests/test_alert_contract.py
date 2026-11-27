"""KAN-66 alert identity contract: only named target alerts become LiveProbe signals."""
from types import SimpleNamespace

import pytest

from app.main import ALERT_KIND, alert_signal
from app.incidents import PROXY_LDS_REJECTED
from conftest import family1_kube, family4_kube


@pytest.mark.parametrize("name,labels,expected", [
    ("TargetWorkloadImagePullFailing", {"pod": "inventory-bbb-1"}, ("Deployment", "inventory")),
    ("TargetWorkloadCrashLooping", {"pod": "inventory-bbb-1"}, ("Deployment", "inventory")),
    ("TargetWorkloadOOMKilled", {"pod": "inventory-bbb-1"}, ("Deployment", "inventory")),
    ("TargetServiceNoEndpoints", {"endpoint": "inventory"}, ("Service", "inventory")),
    ("OrderHigh5xxRate", {"app": "order"}, ("App", "order")),
    ("InventoryHigh5xxRate", {"app": "inventory"}, ("App", "inventory")),
    ("TargetHigh5xxRate", {"app": "inventory", "method": "GET", "uri": "/api/stock/{item}"}, ("App", "inventory")),
    ("TargetHighLatency", {"app": "inventory"}, ("App", "inventory")),
    ("TargetRouteSlowRequests", {"app": "inventory", "method": "GET", "uri": "/api/stock/{sku}"}, ("App", "inventory")),
    ("TargetConnectionPoolExhausted", {"app": "inventory", "pool": "inventory"}, ("App", "inventory")),
])
def test_each_alert_maps_to_expected_workload(name, labels, expected):
    assert name in ALERT_KIND
    signal = alert_signal({"labels": {"alertname": name, "namespace": "liveprobe-target", **labels}},
                          SimpleNamespace(target_namespaces=["liveprobe-target"]),
                          family4_kube() if name == "TargetServiceNoEndpoints" else family1_kube())
    assert signal is not None
    assert (signal.kind, signal.name) == expected
    assert signal.reason == name


@pytest.mark.parametrize("labels", [
    {"alertname": "TargetRouteSlowRequests", "namespace": "liveprobe-target", "app": "inventory"},
    {"alertname": "TargetRouteSlowRequests", "namespace": "liveprobe-target", "app": "inventory", "method": "GET",
     "uri": "/api/stock/a?token=hidden"},
    {"alertname": "TargetHigh5xxRate", "namespace": "liveprobe-target", "app": "inventory", "method": "GET",
     "uri": "/actuator/health/readiness"},
    {"alertname": "TargetHighLatency", "namespace": "kube-system", "app": "inventory"},
    {"alertname": "UnrecognizedAlert", "namespace": "liveprobe-target", "app": "inventory"},
    {"alertname": "TargetWorkloadOOMKilled", "namespace": "liveprobe-target", "pod": "missing"},
])
def test_unrecognized_or_unresolvable_alert_ignored(labels):
    assert alert_signal({"labels": labels}, SimpleNamespace(target_namespaces=["liveprobe-target"]),
                        family1_kube()) is None


@pytest.mark.parametrize("label_uid,phase,terminating,accepted", [
    ("current", "Running", False, True),
    ("replaced", "Running", False, False),
    (None, "Running", False, False),
    ("current", "Pending", False, False),
    ("current", "Running", True, False),
])
def test_listener_rejection_requires_current_running_proxy(label_uid, phase, terminating, accepted):
    kube = family1_kube()
    pod = kube.pods[0]
    pod["metadata"]["uid"] = "current"
    pod["status"]["phase"] = phase
    if terminating:
        pod["metadata"]["deletionTimestamp"] = "2026-11-24T10:00:00Z"
    signal = alert_signal({"labels": {"alertname": PROXY_LDS_REJECTED, "namespace": "dev",
                                     "pod": pod["metadata"]["name"], "pod_uid": label_uid}},
                          SimpleNamespace(target_namespaces=["dev"]), kube)
    assert (signal is not None) == accepted
    if signal:
        assert (signal.kind, signal.name) == ("Deployment", "inventory")
        assert signal.detail["listener_rejection_observed"] is True
        assert signal.detail["route_impact_observed"] is None
