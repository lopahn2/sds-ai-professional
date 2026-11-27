"""Render a credential-free copy of the three established dev OOM test workloads."""
from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


CONTEXT = "rancher-desktop"
NS = "dev"
STOCK = "lp-eval-inventory-pulse"
ORDER = "lp-eval-order-pulse"
CALLER = "lp-eval-order-inventory"
FIXTURES = (ORDER, STOCK, CALLER)
TOKEN_REF = {"secretKeyRef": {"name": "oom-isolated-operator", "key": "token"}}
DB_IMAGE = "postgres@sha256:d4bb0a8c1b7bb2e29f976d099e7bfb9a5d8858cffe9e46b35cd302cd1f1f8168"
APP_IMAGES = {
    "order": "liveprobe/order:dev-pulse-reset-20261124",
    "inventory": "liveprobe/inventory:dev-pulse-reset-20261124",
}
APP_ENV = {
    "SELF_TRAFFIC_INTERVAL_SECONDS", "ORDER_DB_ENABLED", "ORDER_DB_URL", "ORDER_DB_USER", "ORDER_DB_PASSWORD",
    "ORDER_DB_POOL_MAX", "INVENTORY_URL", "INVENTORY_DB_URL", "INVENTORY_DB_USER", "INVENTORY_DB_PASSWORD",
    "INVENTORY_DB_POOL_MAX",
}
APP_CONFIG = {
    "APP_ENV", "APP_VERSION", "CHAOS_STATE_DIR", "INVENTORY_CONNECT_TIMEOUT_MS", "INVENTORY_MAX_RETRIES",
    "INVENTORY_RETRY_BACKOFF_MS", "INVENTORY_TIMEOUT_MS", "INVENTORY_DB_CONNECTION_TIMEOUT_MS",
    "INVENTORY_DB_POOL_MAX", "JAVA_TOOL_OPTIONS", "SELF_TRAFFIC", "SERVICE_NAME", "SPRING_PROFILES_ACTIVE", "SYSTEM_NAME",
}
JVM_OPTIONS = "-XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError -Dhttp.keepAlive=false -XX:ActiveProcessorCount=2"
PORTABLE_TEXT = re.compile(r"[A-Za-z0-9 _.-]{1,100}\Z")
JDBC = {"jdbc:postgresql://127.0.0.1:5432/order_eval", "jdbc:postgresql://127.0.0.1:5432/inventory_eval"}


def read(*args):
    command = ["kubectl", "--context", CONTEXT, "-n", NS, "get", *args, "-o", "json"]
    return json.loads(subprocess.check_output(command, text=True, timeout=12))


def json_digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_setting(name, value):
    if name.endswith("_PASSWORD"):
        if value:
            raise ValueError("nonempty database password in the source test template")
        return ""
    if name == "INVENTORY_URL":
        url = urlsplit(value)
        if (url.scheme, url.netloc, url.path, url.query, url.fragment) != ("http", f"{STOCK}:8080", "", "", ""):
            raise ValueError("unexpected inventory route in source test template")
        return value
    if name.endswith("_DB_URL"):
        if value not in JDBC:
            raise ValueError("source test database is not the approved loopback database")
        return value
    if name == "CHAOS_STATE_DIR" and value == "/var/run/liveprobe-chaos":
        return value
    if name == "JAVA_TOOL_OPTIONS" and value == JVM_OPTIONS:
        return value
    if name not in {"CHAOS_STATE_DIR", "JAVA_TOOL_OPTIONS"} and PORTABLE_TEXT.fullmatch(value):
        return value
    raise ValueError(f"unexpected value for {name}")


def app_env(values):
    result = []
    for env in values:
        name = env["name"]
        if name == "CHAOS_OPERATOR_TOKEN" and set(env.get("valueFrom", {})) == {"secretKeyRef"}:
            result.append({"name": name, "valueFrom": TOKEN_REF})
        elif name in APP_ENV and "valueFrom" not in env:
            result.append({"name": name, "value": validate_setting(name, env.get("value", ""))})
        else:
            raise ValueError(f"unexpected environment binding {name}")
    if sum(e["name"] == "CHAOS_OPERATOR_TOKEN" for e in result) != 1:
        raise ValueError("the application still needs the isolated authorization token")
    return result


def configmap(data, name):
    bound = {key: validate_setting(key, value) for key, value in data.items() if key in APP_CONFIG}
    if (bound.get("APP_ENV"), bound.get("SPRING_PROFILES_ACTIVE"), bound.get("JAVA_TOOL_OPTIONS"),
        bound.get("SELF_TRAFFIC")) != ("dev", "chaos", JVM_OPTIONS, "0"):
        raise ValueError("the source application is not the established chaos test profile")
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NS}, "data": bound}


def fixture(pg_bootstrap, deployment, service):
    name = deployment["metadata"]["name"]
    application = "inventory" if name == STOCK else "order"
    source = deployment["spec"]
    blueprint = source["template"]
    pod = blueprint["spec"]
    if name not in FIXTURES or source["replicas"] != 1 or pod["automountServiceAccountToken"] is not False:
        raise ValueError("unexpected source workload")
    if any(set(volume) != {"name", "emptyDir"} for volume in pod["volumes"]):
        raise ValueError("only emptyDir fixture volumes may be rendered")
    if set(pod) - {"automountServiceAccountToken", "containers", "dnsPolicy", "restartPolicy",
                   "schedulerName", "securityContext", "terminationGracePeriodSeconds", "volumes"}:
        raise ValueError("unexpected privileges in source test template")
    app, *sidecars = pod["containers"]
    if (app["name"], app["image"], app["resources"]["limits"]["memory"]) != (application, APP_IMAGES[application], "512Mi"):
        raise ValueError("unexpected application image or cgroup memory limit")
    if (name == CALLER and sidecars) or (name != CALLER and len(sidecars) != 1):
        raise ValueError("unexpected database sidecar count")
    for db in sidecars:
        if (db["image"], db["resources"]["limits"]["memory"]) != (DB_IMAGE, "64Mi"):
            raise ValueError("unexpected sidecar image or memory limit")
        if db["command"] != pg_bootstrap[name]:
            raise ValueError("unexpected fixture database bootstrap")
        if db["env"] != [{"name": "POSTGRES_DB", "value": "inventory_eval" if name == STOCK else "order_eval"},
                         {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"}]:
            raise ValueError("unexpected fixture database environment")
    for container in (app, *sidecars):
        security = container.get("securityContext", {})
        if security.get("privileged") or security.get("allowPrivilegeEscalation") is True:
            raise ValueError("unexpected container permissions")
    app["env"] = app_env(app["env"])
    if app["envFrom"] != [{"configMapRef": {"name": f"{application}-config"}}]:
        raise ValueError("unexpected application config reference")
    spec = service["spec"]
    if spec["type"] != "ClusterIP" or spec["selector"].get("app") != name:
        raise ValueError("unexpected source fixture service route")
    service_spec = {"type": "ClusterIP", "selector": spec["selector"], "ports": spec["ports"]}
    return [
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS}, "spec": service_spec},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS},
         "spec": {"replicas": 1, "selector": source["selector"], "template": blueprint}},
    ]


def render():
    from evaluation.k8s_dev_order_pulse_fixture import ORDER_BOOTSTRAP, STOCK_BOOTSTRAP

    pg = {ORDER: ["/bin/sh", "-eu", "-c", ORDER_BOOTSTRAP.format(db_connections=14)],
          STOCK: ["/bin/sh", "-eu", "-c", STOCK_BOOTSTRAP.format(db_connections=12)]}
    source = read("deployment", *FIXTURES)["items"]
    services = {item["metadata"]["name"]: item for item in read("service", *FIXTURES)["items"]}
    items = [configmap(read("configmap", name)["data"], name) for name in ("order-config", "inventory-config")]
    for deployment in source:
        items.extend(fixture(pg, deployment, services[deployment["metadata"]["name"]]))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = render()
    with args.output.open("x") as report:
        json.dump(manifest, report, indent=2)
        report.write("\n")
    print(json.dumps({"objects": len(manifest["items"]), "manifest_sha256": json_digest(manifest),
                      "operator_secret_replaced": True}))


if __name__ == "__main__":
    main()
