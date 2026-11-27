from copy import deepcopy

import pytest

from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation import k8s_dev_order_pulse_fixture as fixture


def source_objects(app="order"):
    labels = {"app": app, "app.kubernetes.io/part-of": "samsung-trade", "liveprobe.io/environment": "dev"}
    deployment = {"metadata": {"name": app, "namespace": "dev", "uid": "original-deployment"}, "spec": {
        "template": {"metadata": {"labels": labels}, "spec": {
            "containers": [{"name": app, "image": "original", "env": [
                {"name": "SELF_TRAFFIC_INTERVAL_SECONDS", "value": "3"},
                {"name": "CHAOS_OPERATOR_TOKEN", "valueFrom": {"secretKeyRef": {"name": "fixture", "key": "token"}}},
            ]}], "volumes": [{"name": "chaos-state", "emptyDir": {}}],
        }},
    }}
    service = {"metadata": {"name": app, "namespace": "dev", "uid": "original-service"}, "spec": {
        "type": "ClusterIP", "clusterIP": "10.43.0.11", "selector": labels,
        "ports": [{"port": 8080, "targetPort": 8080}],
    }}
    return deployment, service


def test_order_fixture_has_distinct_routes_and_discards_original_resource_identity():
    original = source_objects()
    snapshot = deepcopy(original)
    service, deployment = fixture.fixture_objects(*original, fixture.CANDIDATE)["items"]
    assert original == snapshot
    assert all(item["metadata"]["name"] == fixture.NAME and "uid" not in item["metadata"]
               and item["metadata"]["namespace"] == "dev" for item in (service, deployment))
    pod = deployment["spec"]["template"]
    assert service["spec"]["selector"]["app"] == pod["metadata"]["labels"]["app"] == fixture.NAME
    assert "clusterIP" not in service["spec"] and pod["spec"]["automountServiceAccountToken"] is False
    container, = pod["spec"]["containers"]
    assert container["name"] == "order" and container["imagePullPolicy"] == "Never"
    assert container["image"] == fixture.CANDIDATE
    assert {e["name"]: e.get("value") for e in container["env"]}["SELF_TRAFFIC_INTERVAL_SECONDS"] == "0"


def test_order_fixture_rejects_a_persistent_mount_and_keeps_the_pinned_case():
    deployment, service = source_objects()
    deployment["spec"]["template"]["spec"]["volumes"] = [
        {"name": "order-data", "persistentVolumeClaim": {"claimName": "keep"}},
    ]
    with pytest.raises(fixture.live.Refused, match="expected isolated fixture shape"):
        fixture.fixture_objects(deployment, service, fixture.CANDIDATE)
    original = pulse.load_case("F4-08")
    copy = pulse.load_case("F4-08", isolated_order=True)
    assert (copy.application, copy.duration, copy.case_sha256) == ("order", original.duration, original.case_sha256)
    assert copy.workload == fixture.NAME and copy.errors[0].endswith('"lp-eval-order-pulse"')
    with pytest.raises(ValueError, match="pinned order cases"):
        pulse.load_case("F4-02", isolated_order=True)


def test_inventory_pulse_uses_a_separate_read_only_order_caller_and_exact_duration():
    case = pulse.load_case("F4-02", isolated_inventory=True)
    stock_service, stock_deployment = fixture.fixture_objects(
        *source_objects("inventory"), fixture.INVENTORY_CANDIDATE, name=case.workload,
    )["items"]
    order_service, order_deployment = fixture.fixture_objects(
        *source_objects(), fixture.CANDIDATE, name=case.dependency_caller,
    )["items"]
    for service, deployment in ((stock_service, stock_deployment), (order_service, order_deployment)):
        template = deployment["spec"]["template"]
        assert service["spec"]["selector"]["app"] == template["metadata"]["labels"]["app"] == service["metadata"]["name"]
        container = template["spec"]["containers"][0]
        assert {"name": "SELF_TRAFFIC_INTERVAL_SECONDS", "value": "0"} in container["env"]
    assert (case.workload, case.dependency_caller, case.duration) == (fixture.INVENTORY_FIXTURE, fixture.INVENTORY_CALLER, 7)
    stock_pod = stock_deployment["spec"]["template"]["spec"]
    inventory, postgres = stock_pod["containers"]
    env = {item["name"]: item.get("value") for item in inventory["env"]}
    assert env["INVENTORY_DB_URL"] == "jdbc:postgresql://127.0.0.1:5432/inventory_eval"
    assert {"name": "INVENTORY_DB_PASSWORD"} in inventory["env"]
    assert postgres["name"] == "verification-stock" and "listen_addresses=127.0.0.1" in postgres["command"][-1]
    assert all(port["port"] != 5432 for port in stock_service["spec"]["ports"])
    assert all("emptyDir" in volume for volume in stock_pod["volumes"])
    assert {"name": "INVENTORY_URL", "value": f"http://{case.workload}:8080"} in (
        order_deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    )
    with pytest.raises(ValueError, match="pinned inventory case"):
        pulse.load_case("F4-08", isolated_inventory=True)


def test_six_slot_inventory_reuses_the_stock_fixture_without_surge():
    case = fixture.PoolFixture(fixture.load_pool_case("F3-14"))
    _, deployment = fixture.fixture_objects(
        *source_objects("inventory"), fixture.INVENTORY_CANDIDATE, name=case.workload,
    )["items"]
    deployment["metadata"].update(uid="fixture", resourceVersion="4")
    deployment["spec"]["strategy"] = {"type": "RollingUpdate"}
    original = deepcopy(deployment)
    operations, expected = fixture.pool_capacity_patch(deployment, 2, 6, {"type": "Recreate"})
    inventory, postgres = expected["template"]["spec"]["containers"]
    assert deployment == original
    assert case.workload == fixture.INVENTORY_FIXTURE and case.dependency_caller == fixture.INVENTORY_CALLER
    assert pulse.fixture_receipt(case) != pulse.fixture_receipt(fixture.SCOPE)
    assert expected["replicas"] == 1 and expected["strategy"] == {"type": "Recreate"}
    assert {"name": "INVENTORY_DB_POOL_MAX", "value": "6"} in inventory["env"]
    assert "listen_addresses=127.0.0.1" in postgres["command"][-1]
    assert "max_connections=12" in postgres["command"][-1]
    assert operations[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "fixture"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "4"},
    ]
    _, restored = fixture.pool_capacity_patch({**deployment, "spec": expected}, 6, 2, original["spec"]["strategy"])
    assert restored == original["spec"]
    deployment["spec"]["template"]["spec"]["volumes"].append({"name": "persist", "persistentVolumeClaim": {}})
    with pytest.raises(fixture.live.Refused, match="only the isolated"):
        fixture.pool_capacity_patch(deployment, 2, 6, {"type": "Recreate"})


def test_eight_slot_order_fixture_has_only_a_loopback_database_and_private_stock():
    _, deployment = fixture.fixture_objects(*source_objects(), fixture.CANDIDATE)["items"]
    original = deepcopy(deployment)
    expected = fixture.order_pool_spec(deployment, 8)
    pod = expected["template"]["spec"]
    order, postgres = pod["containers"]
    env = {item["name"]: item.get("value") for item in order["env"]}
    assert deployment == original
    assert expected["strategy"] == {"type": "Recreate"}
    assert env["ORDER_DB_POOL_MAX"] == "8"
    assert env["ORDER_DB_URL"] == "jdbc:postgresql://127.0.0.1:5432/order_eval"
    assert {"name": "ORDER_DB_PASSWORD"} in order["env"]
    assert env["INVENTORY_URL"] == f"http://{fixture.INVENTORY_FIXTURE}:8080"
    assert "listen_addresses=127.0.0.1" in postgres["command"][-1]
    assert "max_connections=14" in postgres["command"][-1]
    assert "default_transaction_read_only" in postgres["command"][-1]
    assert all("emptyDir" in volume for volume in pod["volumes"])
    with pytest.raises(fixture.live.Refused, match="only the isolated"):
        fixture.order_pool_spec(deployment, 7)
