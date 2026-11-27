"""Inject an upstream Service outage and verify that a new caller process recovers."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from time import monotonic, sleep
from urllib.error import URLError

from evaluation import k8s_dev_image_trial as cluster
from evaluation import matched_k8s_fixtures as fixture
from evaluation.dev_image_case import DevImageCase
from evaluation.pool_config_observe import service_proxy
from evaluation.verify_jdbc_fixture import get


CASE = DevImageCase("K8S-UPSTREAM-RESTART", fixture.UPSTREAM, "")
CALL = "/api/orders/check?item=a"
STOCK = "/api/stock/a"
DISABLED = "liveprobe.io/matched-upstream-disconnected"
RESTART = "kubectl.kubernetes.io/restartedAt"


@contextmanager
def origin():
    with service_proxy() as pool_address:
        yield pool_address.split("/api/v1/namespaces/dev/services/", 1)[0]


def request(address, service, route):
    status, raw = get(address, f"/api/v1/namespaces/dev/services/{service}:8080/proxy{route}")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        body = None
    return status, body


def stock(address, service=fixture.ROUTE):
    status, data = request(address, service, STOCK)
    if (status != 200 or not isinstance(data, dict) or data.get("item") != "a"
            or type(data.get("quantity")) is not int or type(data.get("available")) is not bool):
        raise cluster.Refused("the independent upstream inventory contract is unavailable")
    return data


def healthy_call(address, expected):
    status, data = request(address, fixture.UPSTREAM, CALL)
    return status == 200 and data == {"item": "a", "orderable": expected["available"], "inventory": expected}


def change_selector(expected, before, after):
    alias = cluster.get("service", fixture.ROUTE)
    meta = alias["metadata"]
    if meta["uid"] != expected or alias["spec"]["selector"] != before:
        raise cluster.Refused("the upstream alias changed outside the trial")
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": expected},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/spec/selector", "value": before},
        {"op": "replace", "path": "/spec/selector", "value": after},
    ]
    cluster.command("patch", "service", fixture.ROUTE, "--type=json", "-p", json.dumps(ops))


def pod():
    uid = cluster.ready_endpoints(CASE)
    pods = cluster.get("pods", selector=f"app={CASE.workload}")["items"]
    current, = [item for item in pods if item["metadata"]["uid"] == uid]
    return {"uid": uid, "name": current["metadata"]["name"]}


def restart(deployment_uid):
    deployment = cluster.get("deployment", fixture.UPSTREAM)
    fixture.require_owner(deployment, fixture.UPSTREAM)
    meta = deployment["metadata"]
    if meta["uid"] != deployment_uid:
        raise cluster.Refused("caller Deployment identity changed")
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": deployment_uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "add", "path": "/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt",
         "value": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
    ]
    cluster.command("patch", "deployment", fixture.UPSTREAM, "--type=json", "-p", json.dumps(ops))


def induce(address, expected, first_pod):
    alias = cluster.get("service", fixture.ROUTE)
    baseline = cluster.get("service", fixture.STOCK)
    selectors = alias["spec"]["selector"]
    if DISABLED in selectors or selectors != baseline["spec"]["selector"] or stock(address) != expected:
        raise cluster.Refused("upstream alias does not match the isolated stock fixture")
    alias_uid = alias["metadata"]["uid"]
    disconnected = {**selectors, DISABLED: "true"}
    try:
        change_selector(alias_uid, selectors, disconnected)
        began = monotonic()
        injected_at = datetime.now(timezone.utc).isoformat()
        marker = None
        deadline = began + 25
        while monotonic() < deadline:
            status, data = request(address, fixture.UPSTREAM, CALL)
            if status == 503 and data == {"error": "inventory check unavailable"}:
                logs = cluster.command("logs", first_pod["name"], "-c", fixture.UPSTREAM, "--tail=100")
                marker = next((line for line in logs.splitlines()
                               if line == "ERROR inventory check unavailable item=a cause=URLError"), None)
                if marker:
                    break
            sleep(0.5)
        if marker is None:
            raise cluster.Refused("the caller did not record the actual upstream connection failure")
    finally:
        current = cluster.get("service", fixture.ROUTE)
        if current["spec"]["selector"] == disconnected:
            change_selector(alias_uid, disconnected, selectors)
        elif current["metadata"]["uid"] != alias_uid or current["spec"]["selector"] != selectors:
            raise cluster.Refused("a concurrent Service change prevented upstream restoration")

    while monotonic() < began + 44:
        try:
            if stock(address) == expected:
                status, payload = request(address, fixture.UPSTREAM, CALL)
                if status == 503 and payload == {"error": "inventory check unavailable"} and pod() == first_pod:
                    return began, {"error": marker, "upstream_independently_restored": True,
                                   "same_caller": first_pod, "injected_at": injected_at}
        except (cluster.Refused, URLError):
            pass
        sleep(0.5)
    raise cluster.Refused("the original caller failure did not survive the independently verified upstream recovery")


def independent_recovery(address, expected, first_pod, timeout):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            candidate = pod()
            if (candidate["uid"] != first_pod["uid"] and stock(address) == expected
                    and healthy_call(address, expected) and healthy_call(address, expected) and pod() == candidate):
                return {"caller": candidate, "upstream_equal": True, "responses": [200, 200]}
        except (URLError, cluster.Refused, ValueError):
            pass
        sleep(1)
    raise cluster.Refused("a new caller could not be independently confirmed healthy")
