import json
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiException

from app.config import AppConfig
from app.endpoint_slices import SERVICE_LABEL, current_slice_addresses
from app.k8s import Kube
from app.service_health import read_backend
from app.watcher import Watchers


NS = "dev"


def service(name, uid):
    return {"metadata": {"namespace": NS, "name": name, "uid": uid},
            "spec": {"type": "ClusterIP", "selector": {"app": name}}}


def endpoint_slice(name, owner, *endpoints, namespace=NS):
    return {"metadata": {"namespace": namespace, "labels": {SERVICE_LABEL: name},
                         "ownerReferences": [{"kind": "Service", "name": name, "uid": owner}]},
            "addressType": "IPv4", "endpoints": list(endpoints)}


def endpoint(ip, **conditions):
    return {"addresses": [ip], "conditions": conditions}


def test_endpoint_slice_snapshot_uses_current_owners_without_legacy_truncation():
    target, other, unavailable = service("api", "new"), service("empty", "empty"), service("unknown", "unknown")
    addresses = [endpoint(f"10.{index // 256}.{index % 256}.2", ready=True) for index in range(1001)]
    chunks = [endpoint_slice("api", "new", *addresses[index:index + 100]) for index in range(0, 1001, 100)]
    slices = [
        *chunks, endpoint_slice("api", "new", addresses[0], endpoint("10.90.0.5", terminating=True, ready=True),
                                endpoint("10.90.0.6", ready=False)),
        endpoint_slice("api", "old", endpoint("10.90.0.7")),
        endpoint_slice("api", "new", endpoint("10.90.0.8"), namespace="prd"),
        endpoint_slice("empty", "empty"),
    ]
    snapshot = current_slice_addresses([target, other, unavailable], slices)
    assert len(snapshot[(NS, "api")]) == 1001
    assert not snapshot[(NS, "empty")]
    assert (NS, "unknown") not in snapshot


def test_endpoint_slice_client_reads_all_pages_with_service_label():
    kube = Kube.__new__(Kube)
    kube.api_client = object()
    kube.to_dict = lambda data: data
    releases = []
    replies = iter([
        {"items": [{"name": "first"}], "metadata": {"continue": "next"}},
        {"items": [{"name": "second"}], "metadata": {}},
    ])
    calls = []

    def load(ns, **params):
        calls.append((ns, params))
        data = json.dumps(next(replies)).encode()
        return SimpleNamespace(read=lambda *, cache_content: data, release_conn=lambda: releases.append(True))

    kube.client = SimpleNamespace(DiscoveryV1Api=lambda _: SimpleNamespace(list_namespaced_endpoint_slice=load),
                                  ApiException=ApiException)
    assert list(kube.list_endpoint_slices(NS, name="api")) == [{"name": "first"}, {"name": "second"}]
    assert releases == [True, True]
    assert [params["_continue"] for _, params in calls] == [None, "next"]
    assert all(ns == NS and params["label_selector"] == SERVICE_LABEL + "=api" and params["_request_timeout"] == 8
               for ns, params in calls)


def kube_listing(pages):
    kube = Kube.__new__(Kube)
    kube.api_client = object()
    kube.client = SimpleNamespace(ApiException=ApiException, DiscoveryV1Api=lambda _: SimpleNamespace(
        list_namespaced_endpoint_slice=lambda *_, **kwargs: pages(kwargs['_continue'])))
    kube._list_document = lambda load, *args, **kwargs: load(*args, **kwargs)
    return kube


def test_expired_endpoint_slice_token_restarts_and_discards_partial_addresses():
    calls = []

    def pages(token):
        calls.append(token)
        if token == 'expired':
            raise ApiException(status=410, reason='ResourceExpired')
        if len(calls) == 1:
            return {'metadata': {'resourceVersion': '100', 'continue': 'expired'},
                    'items': [endpoint_slice('api', 'new', endpoint('10.1.0.1'))]}
        return {'metadata': {'resourceVersion': '123'}, 'items': [endpoint_slice('api', 'new')]}

    kube = kube_listing(pages)
    assert kube.service_endpoint_counts([service('api', 'new')], NS) == {(NS, 'api'): 0}
    assert calls == [None, 'expired', None]


def test_repeated_endpoint_slice_expiry_is_a_failed_observation():
    calls = []

    def pages(token):
        calls.append(token)
        if token:
            raise ApiException(status=410, reason='ResourceExpired')
        return {'metadata': {'resourceVersion': '100', 'continue': 'expired'},
                'items': [endpoint_slice('api', 'new')]}

    with pytest.raises(ApiException) as caught:
        kube_listing(pages).service_endpoint_counts([service('api', 'new')], NS)
    assert caught.value.status == 410
    assert calls == [None, 'expired', None, 'expired']


def test_endpoint_slice_discovery_loss_after_expiry_does_not_switch_sources():
    calls = []

    def pages(token):
        calls.append(token)
        if token:
            raise ApiException(status=410, reason='ResourceExpired')
        if len(calls) > 1:
            raise ApiException(status=404, reason='NotFound')
        return {'metadata': {'resourceVersion': '100', 'continue': 'expired'},
                'items': [endpoint_slice('api', 'new')]}

    kube = kube_listing(pages)
    kube.get_endpoints = lambda *_: pytest.fail('did not complete an observed EndpointSlice snapshot')
    with pytest.raises(RuntimeError, match='EndpointSlice API became unavailable'):
        kube.service_endpoint_counts([service('api', 'new')], NS)
    assert calls == [None, 'expired', None]


@pytest.mark.parametrize('defect', ['changed_version', 'cyclic_token'])
def test_endpoint_slice_pagination_does_not_publish_inconsistent_lists(defect):
    pages = {None: {'resourceVersion': '100', 'continue': 'a'},
             'a': {'resourceVersion': '100', 'continue': 'b'},
             'b': {'resourceVersion': '100', 'continue': 'a'}}
    if defect == 'changed_version':
        pages['a'] = {'resourceVersion': '101'}
    kube = kube_listing(lambda token: {'metadata': pages[token], 'items': [endpoint_slice('api', 'new')]})
    with pytest.raises(RuntimeError, match='EndpointSlice pagination'):
        kube.service_endpoint_counts([service('api', 'new')], NS)


def test_forbidden_discovery_is_not_a_legacy_empty_result():
    kube = Kube.__new__(Kube)
    kube.api_client = object()

    def forbidden(*args, **kwargs):
        raise ApiException(status=403)

    kube.client = SimpleNamespace(DiscoveryV1Api=lambda _: SimpleNamespace(list_namespaced_endpoint_slice=forbidden),
                                  ApiException=ApiException)
    with pytest.raises(ApiException) as caught:
        kube.service_endpoint_counts([service("api", "new")], NS)
    assert caught.value.status == 403


def test_legacy_fallback_does_not_count_truncated_addresses():
    kube = Kube.__new__(Kube)
    kube.list_endpoint_slices = lambda *args, **kwargs: None
    target = service("api", "new")
    endpoint = {"metadata": {"name": "api", "namespace": NS},
                "subsets": [{"addresses": [{"ip": "10.10.10.1"}]}]}
    kube.get_endpoints = lambda ns, name: endpoint

    assert kube.service_endpoint_counts([target], NS) == {(NS, "api"): 1}
    endpoint["metadata"]["annotations"] = {"endpoints.kubernetes.io/over-capacity": "truncated"}
    assert kube.service_endpoint_counts([target], NS) == {}


def test_watcher_batches_service_backends_and_keeps_the_ten_minute_gate():
    services = [service("api", "api"), service("stable", "stable")]
    calls = []
    slices = [endpoint_slice("api", "api"), endpoint_slice("stable", "stable", endpoint("10.10.10.1"))]

    def snapshot(targets, ns):
        calls.append((len(targets), ns))
        return {key: len(addresses) for key, addresses in current_slice_addresses(targets, slices).items()}

    kube = SimpleNamespace(list_pods=lambda ns: [], list_services=lambda ns: services,
                           list_deployments=lambda ns: [], service_endpoint_counts=snapshot)
    emitted = []
    watcher = Watchers(kube, AppConfig(target_namespace=NS, workload_state_signals_enabled=False), emitted.append)
    for second in range(0, 600, 30):
        watcher.poll_endpoints_once(second)
    assert emitted == []
    watcher.poll_endpoints_once(600)
    assert len(emitted) == 1 and emitted[0].name == "api" and emitted[0].reason == "NoEndpoints"
    assert calls == [(2, NS)] * 21
    assert read_backend(kube, NS, services[1])["ready_endpoints"] == 1
