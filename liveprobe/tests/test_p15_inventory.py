"""P15 overview inventory reads only bounded, non-secret Kubernetes resource summaries."""
import json
from types import SimpleNamespace as N

import pytest

@pytest.mark.parametrize('ingress_denied', [False, True])
def test_inventory_keeps_zero_pod_deployments_and_namespace_names_without_templates(ingress_denied):
    from threading import Barrier

    from app.k8s import Kube
    k = Kube.__new__(Kube)
    k.api_client = object()
    calls = []
    releases = []
    simultaneous_reads = []
    first_wave = Barrier(2)
    def read(name, rows):
        def f(**kw):
            calls.append((name, kw))
            if name in ("ingresses", "deployments"):
                first_wave.wait(timeout=2)
                simultaneous_reads.append(name)
            if name == "ingresses" and ingress_denied:
                raise PermissionError("unavailable ingress list")
            def body(*, cache_content):
                assert cache_content is False
                return json.dumps({"items": rows}).encode()
            return N(read=body, release_conn=lambda: releases.append(name))
        return f
    node = {"metadata": {"name": "n1", "annotations": {"omit": "x"}}, "spec": {"podCIDRs": ["10.0.0.0/24"]},
            "status": {"addresses": [{"type": "InternalIP", "address": "192.0.2.1"}], "images": ["unused"]}}
    k.core = N(list_node=read("nodes", [node]), list_pod_for_all_namespaces=read("pods", []),
               list_service_for_all_namespaces=read("services", []), list_endpoints_for_all_namespaces=read("endpoints", []),
               list_namespace=read("namespaces", [{"metadata": {"name": "empty", "annotations": {"omit": "x"}}}]))
    k.apps = N(list_deployment_for_all_namespaces=read("deployments", [{"metadata": {"namespace": "empty", "name": "asleep"},
               "spec": {"replicas": 0, "template": {"spec": {"containers": [{"env": [{"value": "not-for-overview"}]}]}}},
               "status": {"readyReplicas": 0}}]))
    k.client = N(NetworkingV1Api=lambda api: N(list_ingress_for_all_namespaces=read("ingresses", [])))
    out = k.cluster_inventory()
    assert out["namespaces"] == [{"metadata": {"name": "empty"}}]
    assert out["deployments"][0]["spec"] == {"replicas": 0}
    assert out["nodes"][0]["metadata"] == {"name": "n1"}
    assert out["nodes"][0]["spec"]["podCIDRs"] == ["10.0.0.0/24"]
    assert out["nodes"][0]["status"]["addresses"] == node["status"]["addresses"]
    assert "images" not in out["nodes"][0]["status"]
    assert "not-for-overview" not in str(out)
    assert out['errors'] == (['ingresses: PermissionError'] if ingress_denied else [])
    assert all(opts == {"_preload_content": False, "_request_timeout": 8} for _, opts in calls)
    assert sorted(simultaneous_reads) == ["deployments", "ingresses"]
    assert sorted(releases) == sorted(name for name, _ in calls if name != 'ingresses' or not ingress_denied)


def test_raw_list_releases_the_connection_on_read_or_payload_failure():
    from app.k8s import Kube

    actions = []
    def interrupted(**_):
        raise TimeoutError
    def response(read):
        return N(read=read, close=lambda: actions.append("closed"), release_conn=lambda: actions.append("released"))
    with pytest.raises(TimeoutError):
        Kube._list_document(lambda **_: response(interrupted))
    assert actions == ["closed", "released"]
    with pytest.raises(ValueError, match="missing its items"):
        Kube._list_document(lambda **_: response(lambda **_: b'{"items": null}'))
    assert actions[-1] == "released" and len(actions) == 3


def test_concurrent_overview_reads_share_one_inventory_refresh(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from app import main

    barrier, entered, finish = Barrier(3), Event(), Event()
    calls = []
    snapshot = {"pods": []}
    def fetch():
        calls.append(True)
        entered.set()
        assert finish.wait(2)
        return snapshot
    kube = N(cluster_inventory=fetch)
    monkeypatch.setattr(main, "_TOPO_CACHE", {"at": 0.0, "inventory": None})
    def get():
        barrier.wait(timeout=2)
        return main._inventory(kube)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reads = [pool.submit(get), pool.submit(get)]
        barrier.wait(timeout=2)
        assert entered.wait(2)
        finish.set()
        assert all(result.result(timeout=2) is snapshot for result in reads)
    assert len(calls) == 1 and main._TOPO_CACHE["observed_at"]


def test_warning_events_read_is_bounded_and_excludes_normal_events_at_source():
    import json

    from app.k8s import Kube
    from app.overview import _event

    k = Kube.__new__(Kube)
    calls = []
    released = []
    raw_event = {"reason": "Failed", "message": "original raw text",
                 "series": {"count": 3, "lastObservedTime": "2026-11-24T08:15:02.410Z"}}

    def read(**kw):
        calls.append(kw)
        return N(read=lambda *, cache_content: json.dumps({"items": [raw_event]}).encode(),
                 release_conn=lambda: released.append(True))

    k.core = N(list_event_for_all_namespaces=read)
    event, = k.cluster_warning_events()
    assert event == raw_event
    assert _event(event)["at"] == "2026-11-24T08:15:02.410000+00:00"
    assert _event(event)["count"] == 3
    assert released == [True]
    assert calls == [{"field_selector": "type=Warning", "limit": 500,
                      "_request_timeout": 8, "_preload_content": False}]
    assert k.cluster_warning_events(limit=1) == [raw_event]
    assert calls[-1] == {"field_selector": "type=Warning", "limit": 1,
                         "_request_timeout": 8, "_preload_content": False}
