"""Prepare isolated dev pulse Services and their order dependency."""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json

from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation import k8s_dev_image_trial as live
from evaluation.dev_endpoint_pulse_case import INVENTORY_CALLER, INVENTORY_FIXTURE
from evaluation.dev_pool_case import load_pool_case
from evaluation.k8s_dev_pool_trial import SCOPE, PoolFixture, idle, state
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text


NAME = pulse.ORDER_FIXTURE
CANDIDATE = "liveprobe/order:dev-pulse-reset-20261124"
INVENTORY_CANDIDATE = "liveprobe/inventory:dev-pulse-reset-20261124"
STOCK_IMAGE = "postgres@sha256:d4bb0a8c1b7bb2e29f976d099e7bfb9a5d8858cffe9e46b35cd302cd1f1f8168"
STOCK_BOOTSTRAP = """cat > /docker-entrypoint-initdb.d/01-stock.sql <<'SQL'
CREATE ROLE inventory_eval LOGIN;
CREATE SCHEMA inventory;
CREATE TABLE inventory.stock (item text PRIMARY KEY, quantity integer NOT NULL CHECK (quantity >= 0));
INSERT INTO inventory.stock VALUES ('a',42);
GRANT USAGE ON SCHEMA inventory TO inventory_eval;
GRANT SELECT ON TABLE inventory.stock TO inventory_eval;
ALTER ROLE inventory_eval SET default_transaction_read_only = on;
SQL
exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c shared_buffers=16MB -c max_connections={db_connections}
"""
ORDER_BOOTSTRAP = """cat > /docker-entrypoint-initdb.d/01-order.sql <<'SQL'
CREATE ROLE order_eval LOGIN;
ALTER ROLE order_eval SET default_transaction_read_only = on;
SQL
exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c shared_buffers=16MB -c max_connections={db_connections}
"""
IMAGE_RECEIPT = live.EVIDENCE / "dev-pulse-java-image-candidates.json"


def configure_fixture_stock(pod, container, connections):
    stock_env = {
        "INVENTORY_DB_URL": "jdbc:postgresql://127.0.0.1:5432/inventory_eval",
        "INVENTORY_DB_USER": "inventory_eval",
        "INVENTORY_DB_PASSWORD": "",
        "INVENTORY_DB_POOL_MAX": str(connections),
    }
    attach_private_db(pod, container, stock_env, "stock", "inventory_eval",
                      STOCK_BOOTSTRAP.format(db_connections=connections + 6),
                      "SELECT 1 / CASE WHEN EXISTS (SELECT 1 FROM inventory.stock WHERE item='a' AND quantity=42) THEN 1 ELSE 0 END")


def attach_private_db(pod, container, environment, label, role, bootstrap, readiness_query):
    name = f"verification-{label}"
    container["env"] = [e for e in container["env"] if e["name"] not in environment]
    container["env"].extend({"name": key, "value": value} if value else {"name": key}
                            for key, value in environment.items())
    pod.setdefault("volumes", []).append({"name": name, "emptyDir": {"sizeLimit": "128Mi"}})
    pod["containers"].append({
        "name": name, "image": STOCK_IMAGE, "imagePullPolicy": "Never",
        "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
        "command": ["/bin/sh", "-eu", "-c", bootstrap],
        "env": [{"name": "POSTGRES_DB", "value": role},
                {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"}],
        "volumeMounts": [{"name": name, "mountPath": "/var/lib/postgresql/data"}],
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "150m", "memory": "64Mi"}},
        "readinessProbe": {"exec": {"command": [
            "psql", "-Atq", "-h", "127.0.0.1", "-U", role, "-d", role,
            "-v", "ON_ERROR_STOP=1", "-c", readiness_query,
        ]}, "periodSeconds": 5, "timeoutSeconds": 2, "successThreshold": 1, "failureThreshold": 3},
    })


def fixture_objects(source_deployment, source_service, image, *, name=NAME):
    application, pinned = {
        NAME: ("order", CANDIDATE), INVENTORY_FIXTURE: ("inventory", INVENTORY_CANDIDATE),
        INVENTORY_CALLER: ("order", CANDIDATE),
    }[name]
    expected = ("dev", application)
    if any((item["metadata"]["namespace"], item["metadata"]["name"]) != expected
           for item in (source_deployment, source_service)) or image != pinned:
        raise live.Refused("only the pinned dev application can be copied to the assessment fixture")
    selector = source_service["spec"]["selector"]
    template = deepcopy(source_deployment["spec"]["template"])
    pod = template["spec"]
    labels = template["metadata"]["labels"]
    container, = pod["containers"]
    if (selector.get("app") != application or labels.get("app") != application or container["name"] != application
            or source_service["spec"].get("type") != "ClusterIP"
            or not all(labels.get(key) == value for key, value in selector.items())
            or any(set(resource) != {"name", "emptyDir"} for resource in pod.get("volumes") or [])
            or any(pod.get(key) for key in ("hostNetwork", "hostPID", "hostIPC", "initContainers"))
            or container.get("securityContext", {}).get("privileged")):
        raise live.Refused("the original dev order does not have the expected isolated fixture shape")
    label = {**labels, "app": name, "liveprobe.io/verification-case": "order-pulse" if name == NAME else "inventory-pulse"}
    template["metadata"]["labels"] = label
    pod["automountServiceAccountToken"] = False
    container["image"] = image
    container["imagePullPolicy"] = "Never"
    container["env"] = [e for e in container.get("env", []) if e.get("name") != "SELF_TRAFFIC_INTERVAL_SECONDS"]
    container["env"].append({"name": "SELF_TRAFFIC_INTERVAL_SECONDS", "value": "0"})
    if name == INVENTORY_CALLER:
        container["env"] = [e for e in container["env"] if e["name"] != "INVENTORY_URL"]
        container["env"].append({"name": "INVENTORY_URL", "value": f"http://{INVENTORY_FIXTURE}:8080"})
    elif name == INVENTORY_FIXTURE:
        configure_fixture_stock(pod, container, SCOPE.case.connections)

    def object_for(api, kind, spec):
        return {"apiVersion": api, "kind": kind, "metadata": {"namespace": "dev", "name": name, "labels": label},
                "spec": spec}

    deployment = object_for("apps/v1", "Deployment", {"replicas": 1, "revisionHistoryLimit": 2,
                           "selector": {"matchLabels": {"app": name}}, "template": template})
    ports = [{k: v for k, v in port.items() if k != "nodePort"} for port in source_service["spec"]["ports"]]
    service = object_for("v1", "Service", {"type": "ClusterIP", "selector": {**selector, "app": name}, "ports": ports})
    return {"apiVersion": "v1", "kind": "List", "items": [service, deployment]}


def prepare(write, case):
    images = json.loads(IMAGE_RECEIPT.read_text())["images"]
    if case.application == "inventory":
        profiles = (("F4-02", case.workload), ("F4-08", case.dependency_caller))
    else:
        profiles = (("F4-08", NAME),)
    original_baselines, resources = {}, {"apiVersion": "v1", "kind": "List", "items": []}
    for case_id, target in profiles:
        source = pulse.load_case(case_id)
        image = images[source.application]
        baseline, _, _, _ = pulse.preflight(source)
        deployment = live.get("deployment", source.workload)
        route = live.get("service", source.workload)
        if (deployment["spec"]["template"]["spec"]["containers"][0]["image"] != image["original_image"]
                or image.get("offline_file_verified") is not True or not live.is_settled(deployment)
                or deployment["metadata"]["uid"] != baseline["deployment_uid"]
                or live.fixture_digest(deployment) != baseline["deployment_sha256"]
                or route["metadata"]["uid"] != baseline["service_uid"] or route["spec"] != baseline["service_spec"]):
            raise live.Refused("the original ready application no longer matches the tested candidate base")
        original_baselines[case_id] = baseline
        resources["items"].extend(fixture_objects(deployment, route, image["candidate"], name=target)["items"])

    image = images[case.application]
    source_baseline = original_baselines[profiles[0][0]]
    body = json.dumps(resources)
    checked = live.command("create", "-f", "-", "--dry-run=server", "-o", "name", stdin=body).splitlines()
    if len(checked) != len(resources["items"]):
        raise live.Refused("all isolated objects must pass server dry-run")
    if not write:
        return {"server_dry_run": True, "objects": checked, "candidate": image["candidate"],
                "source_uid": source_baseline["deployment_uid"], "source_sha256": source_baseline["deployment_sha256"]}
    gate = live.CHAOS.node_gate()
    live.command("create", "-f", "-", "-o", "name", stdin=body)
    for _, target in profiles:
        live.command("rollout", "status", f"deployment/{target}", "--timeout=105s", timeout=110)
    baseline, pod, probes, _ = pulse.preflight(case)
    original_proofs = {}
    for case_id, target in profiles:
        source = pulse.load_case(case_id)
        after, _, source_probes, _ = pulse.preflight(source)
        if after != original_baselines[case_id]:
            raise live.Refused("an original deployment, service or endpoint changed while preparing the fixture")
        original_proofs[source.workload] = {"baseline": after, "probes": source_probes}
        uid = baseline["endpoint_uid"] if target == case.workload else baseline["dependency_caller"]["endpoint_uid"]
        pods = live.get("pods", selector=f"app={target}")["items"]
        matching, = [p for p in pods if p["metadata"]["uid"] == uid]
        status, = [c for c in matching["status"]["containerStatuses"] if c["name"] == source.application and c.get("ready")]
        if not status["imageID"].endswith(images[source.application]["candidate_id"]):
            raise live.Refused("an isolated running container did not use the verified candidate")
    result = {"at": datetime.now(timezone.utc).isoformat(), "namespace": "dev", "workload": case.workload, "ready": True,
              "image": image, "baseline": baseline, "fixture_sha256": hash_text(json.dumps(baseline, sort_keys=True)),
              "baseline_http": probes, "original_workloads": original_proofs, "node_gate": gate}
    live.write_once(pulse.FIXTURE_RECEIPTS[case.workload], result)
    return {"ready": True, "target": case.target, "fixture_sha256": result["fixture_sha256"]}


def pool_capacity_patch(deployment, previous, desired, strategy):
    meta = deployment["metadata"]
    expected = deepcopy(deployment["spec"])
    template = expected["template"]
    pod = template["spec"]
    if ((meta["namespace"], meta["name"], expected["replicas"]) != ("dev", INVENTORY_FIXTURE, 1)
            or {previous, desired} != {2, 6} or expected.get("paused")
            or template["metadata"]["labels"].get("liveprobe.io/verification-case") != "inventory-pulse"
            or any(set(volume) != {"name", "emptyDir"} for volume in pod.get("volumes", []))
            or not any(volume["name"] == "verification-stock" for volume in pod.get("volumes", []))):
        raise live.Refused("only the isolated, ephemeral stock fixture can change capacity")
    inventory, stock = pod["containers"]
    slot, = [i for i, env in enumerate(inventory["env"]) if env["name"] == "INVENTORY_DB_POOL_MAX"]
    old_command = ["/bin/sh", "-eu", "-c", STOCK_BOOTSTRAP.format(db_connections=previous + 6)]
    if ((inventory["name"], inventory["image"], stock["name"], stock["image"])
            != ("inventory", INVENTORY_CANDIDATE, "verification-stock", STOCK_IMAGE)
            or inventory["env"][slot] != {"name": "INVENTORY_DB_POOL_MAX", "value": str(previous)}
            or stock["command"] != old_command):
        raise live.Refused("the pinned pool setting or loopback database command changed")
    inventory["env"][slot]["value"] = str(desired)
    stock["command"][-1] = STOCK_BOOTSTRAP.format(db_connections=desired + 6)
    expected["strategy"] = strategy
    path = "/spec/template/spec/containers"
    patch = [{"op": "test", "path": f"/metadata/{key}", "value": meta[key]} for key in ("uid", "resourceVersion")]
    patch += [
        {"op": "replace", "path": f"{path}/0/env/{slot}/value", "value": str(desired)},
        {"op": "replace", "path": f"{path}/1/command", "value": stock["command"]},
        {"op": "replace", "path": "/spec/strategy", "value": strategy},
    ]
    return patch, expected


def apply_capacity(deployment, previous, desired, strategy, *, dry_run=False):
    operations, expected = pool_capacity_patch(deployment, previous, desired, strategy)
    dry = ("--dry-run=server",) if dry_run else ()
    actual = json.loads(live.command("patch", "deployment", INVENTORY_FIXTURE, "--type=json", "-p",
                                     json.dumps(operations), "-o", "json", *dry))
    if actual["spec"] != expected:
        raise live.Refused("server did not keep the intended fixture specification")
    return actual


def switch_to_six(write, case):
    before, pod, _, _ = pulse.preflight(SCOPE)
    pulse.verify_fixture(SCOPE, before, pod)
    if not idle(state(pod), SCOPE):
        raise live.Refused("the old stock fixture is not idle at its pinned capacity")
    deployment = live.get("deployment", INVENTORY_FIXTURE)
    if (deployment["metadata"]["uid"] != before["deployment_uid"]
            or live.fixture_digest(deployment) != before["deployment_sha256"]):
        raise live.Refused("stock fixture changed after preflight")
    previous, desired = SCOPE.case.connections, case.case.connections
    strategy = {"type": "Recreate"}
    dry = apply_capacity(deployment, previous, desired, strategy, dry_run=True)
    prepared = {"at": datetime.now(timezone.utc).isoformat(), "case": case.case_id, "target": case.target,
                "before": before, "after_deployment_sha256": live.fixture_digest(dry),
                "previous_strategy": deployment["spec"]["strategy"], "non_surge": True, "server_dry_run": True}
    if not write:
        return prepared
    prepared["node_gate"] = live.CHAOS.node_gate()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt = live.EVIDENCE / f"dev-isolated-inventory-pool-six-switch-{stamp}.json"
    live.write_once(attempt, prepared)
    rollout = ("rollout", "status", f"deployment/{INVENTORY_FIXTURE}", "--timeout=105s")
    try:
        apply_capacity(deployment, previous, desired, strategy)
        live.command(*rollout, timeout=110)
        after, next_pod, probes, _ = pulse.preflight(case)
        old = json.loads(pulse.INVENTORY_FIXTURE_RECEIPT.read_text())
        if (any(after[key] != before[key] for key in ("service_uid", "deployment_uid", "dependency_caller"))
                or after["deployment_sha256"] != prepared["after_deployment_sha256"]
                or after["endpoint_uid"] == before["endpoint_uid"] or not idle(state(next_pod, case), case)):
            raise live.Refused("the new capacity did not preserve the isolated Service and caller")
        result = {**prepared, "switch_receipt": attempt.name, "namespace": "dev", "workload": case.workload, "ready": True,
                  "image": old["image"], "baseline": after, "baseline_http": probes,
                  "fixture_sha256": hash_text(json.dumps(after, sort_keys=True))}
        pulse.verify_fixture(case, after, next_pod, approval=result)
        live.write_once(pulse.SIX_POOL_RECEIPT, result)
        return {"ready": True, "target": case.target, "fixture_sha256": result["fixture_sha256"]}
    except Exception:
        current = live.get("deployment", INVENTORY_FIXTURE)
        # Restore only the exact deployment specification admitted for this switch.
        if (current["metadata"]["uid"] == before["deployment_uid"]
                and live.fixture_digest(current) == prepared["after_deployment_sha256"]):
            apply_capacity(current, desired, previous, strategy)
            live.command(*rollout, timeout=110)
            restored = live.get("deployment", INVENTORY_FIXTURE)
            expected = {**restored["spec"], "strategy": prepared["previous_strategy"]}
            if (restored["metadata"]["uid"] != before["deployment_uid"]
                    or live.fixture_digest({"spec": expected}) != before["deployment_sha256"]):
                raise live.Refused("cannot restore rollout strategy on a changed fixture")
            ops = [{"op": "test", "path": f"/metadata/{key}", "value": restored["metadata"][key]}
                   for key in ("uid", "resourceVersion")]
            ops.append({"op": "replace", "path": "/spec/strategy", "value": prepared["previous_strategy"]})
            live.command("patch", "deployment", INVENTORY_FIXTURE, "--type=json", "-p", json.dumps(ops))
        raise


def order_pool_spec(deployment, connections):
    meta = deployment["metadata"]
    expected = deepcopy(deployment["spec"])
    template = expected["template"]
    pod = template["spec"]
    order, = pod["containers"]
    if ((meta["namespace"], meta["name"], expected["replicas"], connections) != ("dev", NAME, 1, 8)
            or expected.get("paused") or order["name"] != "order" or order["image"] != CANDIDATE
            or template["metadata"]["labels"].get("liveprobe.io/verification-case") != "order-pulse"
            or any(set(volume) != {"name", "emptyDir"} for volume in pod.get("volumes", []))):
        raise live.Refused("only the isolated, ephemeral order fixture can receive a private pool")
    database = "order_eval"
    environment = {"ORDER_DB_ENABLED": "true", "ORDER_DB_URL": f"jdbc:postgresql://127.0.0.1:5432/{database}",
                   "ORDER_DB_USER": database, "ORDER_DB_PASSWORD": "", "ORDER_DB_POOL_MAX": str(connections),
                   "INVENTORY_URL": f"http://{INVENTORY_FIXTURE}:8080"}
    attach_private_db(pod, order, environment, "order", database, ORDER_BOOTSTRAP.format(db_connections=connections + 6),
                      "SELECT 1")
    expected["strategy"] = {"type": "Recreate"}
    return expected


def patch_order_fixture(deployment, expected, *, dry_run=False):
    meta = deployment["metadata"]
    if (meta["namespace"], meta["name"]) != ("dev", NAME):
        raise live.Refused("the order fixture identity changed")
    original = deployment["spec"]
    keys = {key for key in original.keys() | expected.keys() if original.get(key) != expected.get(key)}
    if not keys <= {"template", "strategy"}:
        raise live.Refused("the order fixture patch extends beyond its template and rollout strategy")
    operations = [{"op": "test", "path": f"/metadata/{key}", "value": meta[key]}
                  for key in ("uid", "resourceVersion")]
    operations.extend({"op": "replace", "path": f"/spec/{key}", "value": expected[key]} for key in sorted(keys))
    dry = ("--dry-run=server",) if dry_run else ()
    actual = json.loads(live.command("patch", "deployment", NAME, "--type=json", "-p", json.dumps(operations),
                                     "-o", "json", *dry))
    if actual["spec"] != expected:
        raise live.Refused("server did not keep the intended order fixture specification")
    return actual


def switch_to_eight(write, case):
    archived = pulse.load_case("F4-08", isolated_order=True)
    before, pod, _, _ = pulse.preflight(archived)
    pulse.verify_fixture(archived, before, pod)
    pulse.preflight(SCOPE)  # Its read-only stock contract is the only upstream dependency.
    original = live.get("deployment", NAME)
    if (original["metadata"]["uid"] != before["deployment_uid"]
            or live.fixture_digest(original) != before["deployment_sha256"]):
        raise live.Refused("order fixture changed after preflight")
    changed = order_pool_spec(original, case.case.connections)
    planned = patch_order_fixture(original, changed, dry_run=True)
    digest = live.fixture_digest(planned)
    prepared = {"at": datetime.now(timezone.utc).isoformat(), "case": case.case_id, "target": case.target,
                "before": before, "after_deployment_sha256": digest, "server_dry_run": True, "non_surge": True}
    if not write:
        return prepared
    prepared["node_gate"] = live.CHAOS.node_gate()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    attempt = live.EVIDENCE / f"dev-isolated-order-pool-eight-switch-{stamp}.json"
    live.write_once(attempt, prepared)
    rollout = ("rollout", "status", f"deployment/{NAME}", "--timeout=105s")
    try:
        patch_order_fixture(original, changed)
        live.command(*rollout, timeout=110)
        after, next_pod, probes, _ = pulse.preflight(case)
        if (any(after[key] != before[key] for key in ("service_uid", "service_spec", "deployment_uid"))
                or after["deployment_sha256"] != digest or after["endpoint_uid"] == before["endpoint_uid"]
                or not idle(state(next_pod, case), case)):
            raise live.Refused("the new order pool did not preserve the verified Service identity")
        old = json.loads(pulse.ORDER_FIXTURE_RECEIPT.read_text())
        result = {**prepared, "switch_receipt": attempt.name, "namespace": "dev", "workload": case.workload,
                  "ready": True, "image": old["image"], "baseline": after, "baseline_http": probes,
                  "fixture_sha256": hash_text(json.dumps(after, sort_keys=True))}
        pulse.verify_fixture(case, after, next_pod, approval=result)
        live.write_once(pulse.ORDER_POOL_RECEIPT, result)
        return {"ready": True, "target": case.target, "fixture_sha256": result["fixture_sha256"]}
    except Exception:
        active = live.get("deployment", NAME)
        if active["metadata"]["uid"] == before["deployment_uid"] and live.fixture_digest(active) == digest:
            restored = {**original["spec"], "strategy": {"type": "Recreate"}}
            patch_order_fixture(active, restored)
            live.command(*rollout, timeout=110)
            current = live.get("deployment", NAME)
            if current["metadata"]["uid"] != before["deployment_uid"] or current["spec"] != restored:
                raise live.Refused("cannot restore rollout strategy on a changed order fixture")
            patch_order_fixture(current, original["spec"])
        raise


def main():
    parser = ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--prepare", action="store_true")
    fixture = parser.add_mutually_exclusive_group()
    fixture.add_argument("--inventory", action="store_true")
    fixture.add_argument("--pool-six", action="store_true")
    fixture.add_argument("--pool-eight", action="store_true")
    args = parser.parse_args()
    if args.pool_eight:
        case = PoolFixture(load_pool_case("F3-08"))
        provision = switch_to_eight
    elif args.pool_six:
        case = PoolFixture(load_pool_case("F3-14"))
        provision = switch_to_six
    elif args.inventory:
        case = pulse.load_case("F4-02", isolated_inventory=True)
        provision = prepare
    else:
        case = pulse.load_case("F4-08", isolated_order=True)
        provision = prepare
    with (live.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (live.EVIDENCE / "dev-k8s-fault-active.json").exists() or pulse.fixture_receipt(case).exists():
            raise live.Refused("an active trial or an existing isolated fixture requires review")
        result = provision(args.prepare, case)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False,
                          "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
