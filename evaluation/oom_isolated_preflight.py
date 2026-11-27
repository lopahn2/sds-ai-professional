"""Read the dedicated dev node with the same resource gate and cgroup bounds."""
from argparse import ArgumentParser
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode, urlsplit


ROOT = Path("/tmp/lp-oom-k3s")
NODE = "oom-dev-verify"
NAMESPACE = "dev"
SYSTEM = "liveprobe-system"
OBSERVABILITY = "liveprobe-observability"
MARKER = "native-oom-isolated"
FIXTURE_SCOPE_LABEL = "liveprobe.io/evaluation-fixtures"
KUBERNETES = "v1.36.2+k3s1"
MIB = 1024 ** 2
ORDER_FIXTURE = "lp-eval-order-pulse"
INVENTORY_FIXTURE = "lp-eval-inventory-pulse"
INVENTORY_CALLER = "lp-eval-order-inventory"
FIXTURES = {
    ORDER_FIXTURE: ("order", "verification-order"),
    INVENTORY_FIXTURE: ("inventory", "verification-stock"),
    INVENTORY_CALLER: ("order",),
}
FIXTURE_SCOPES = {"all": tuple(FIXTURES), "order": (INVENTORY_FIXTURE, ORDER_FIXTURE),
                  "inventory": (INVENTORY_FIXTURE, INVENTORY_CALLER)}
BUSINESS_REQUESTS = {
    "inventory_lookup": (INVENTORY_FIXTURE, "api/stock/a"),
    "order_lookup": (ORDER_FIXTURE, "api/orders/1"),
    "order_stock_check": (ORDER_FIXTURE, "api/orders/check?item=a"),
    "dependent_order_check": (INVENTORY_CALLER, "api/orders/check?item=a"),
}
SOURCE_IMAGES = {
    "order": "docker.io/liveprobe/order:dev-pulse-reset-20261124",
    "inventory": "docker.io/liveprobe/inventory:dev-pulse-reset-20261124",
    "verification-order": "docker.io/library/postgres:lp-oom-source",
    "verification-stock": "docker.io/library/postgres:lp-oom-source",
}
RECEIVER_COMPONENTS = (
    ("deployment", SYSTEM, "liveprobe-api", "api", "docker.io/liveprobe/liveprobe:oom-dev-receiver-compact"),
    ("statefulset", SYSTEM, "postgres", "postgres", "docker.io/pgvector/pgvector:0.8.6-pg17-bookworm"),
    ("deployment", OBSERVABILITY, "prometheus", "prometheus", "docker.io/prom/prometheus:v3.15.0"),
    ("deployment", OBSERVABILITY, "alertmanager", "alertmanager", "docker.io/prom/alertmanager:v0.34.1"),
    ("deployment", OBSERVABILITY, "kube-state-metrics", "kube-state-metrics",
     "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0"),
)
CACHED_RECEIVER_IMAGE = "docker.io/liveprobe/liveprobe:dev-prd-scope-api-20261124"
RETENTION_ANNOTATION = "liveprobe.io/rag-retention-sha256"
RETENTION_FILES = """from hashlib import sha256
import json
from pathlib import Path

paths = ('main.py', 'rag_model.py')
files = {name: sha256((Path('/app/app') / name).read_bytes()).hexdigest() for name in paths}
print(sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
"""
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
OOM_ALERT = "TargetWorkloadOOMKilled"
OOM_REASON = 'kube_pod_container_status_last_terminated_reason{namespace=~"dev|prd",reason="OOMKilled"}'
OOM_AGE = 'time() - kube_pod_container_status_last_terminated_timestamp{namespace=~"dev|prd"}'
OOM_RULE = (f"max by (namespace,pod,container) (({OOM_REASON} > 0) and on (namespace,pod,uid,container)"
            f" ({OOM_AGE} >= 0 and {OOM_AGE} < 120))")
CONTROL_READ = """set -eu
printf 'Authorization: Bearer %s\n' "$CHAOS_OPERATOR_TOKEN" | \
  curl -fsS --max-time 5 -H @- http://127.0.0.1:8080/internal/chaos/v1/faults
"""
PRODUCT_READS = {"/api/status", "/api/readyz"}
PRODUCT_PROBE = """from http.client import HTTPConnection
import os
from pathlib import Path
import sys

key = Path(os.environ["LIVEPROBE_LOCAL_READ_KEY_FILE"]).read_text().strip()
connection = HTTPConnection("127.0.0.1", 8000, timeout=5)
try:
    connection.request("GET", sys.argv[1], headers={"X-LiveProbe-Local-Read": key})
    response = connection.getresponse()
    if response.status != 200:
        raise SystemExit(f"Isolated status returned HTTP {response.status}")
    sys.stdout.buffer.write(response.read())
finally:
    connection.close()
"""


class KubernetesReadError(RuntimeError):
    pass


def kube(*args, timeout=20, stdin=None, namespace=NAMESPACE):
    result = subprocess.run(
        ["/opt/isolated/k3s-arm64", "kubectl", "--kubeconfig", str(ROOT / "kubeconfig"),
         "-n", namespace, "--request-timeout=12s", *args],
        env={**os.environ, "K3S_DATA_DIR": str(ROOT / "state")},
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        raise KubernetesReadError(f"isolated Kubernetes {args[0]} failed with exit {result.returncode}")
    return result.stdout


def gate_source():
    spec = importlib.util.spec_from_file_location("oom_node_gate", ROOT / "node-gate.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

    def read(*args, **kwargs):
        try:
            return kube(*args, **kwargs)
        except KubernetesReadError as error:
            raise adapter.Stop(str(error)) from error

    adapter.kubectl = read
    return adapter


def admit_node(limits):
    try:
        return limits.node_gate()
    except limits.NodeGateStop as error:
        conditions = ", ".join(error.breaches)
        raise RuntimeError(f"isolated node admission denied ({conditions}): {json.dumps(error.snapshot)}") from None


def isolated_node():
    for family in ("-4", "-6"):
        routes = subprocess.check_output(["ip", family, "route", "show", "default"], text=True)
        if routes.strip():
            raise RuntimeError("default network route is present inside the dedicated node")
    namespace = json.loads(kube("get", "namespace", NAMESPACE, "-o", "json"))
    version = json.loads(kube("get", "--raw=/version"))
    nodes = json.loads(kube("get", "nodes", "-o", "json"))["items"]
    node, = nodes
    node_info = node["status"]["nodeInfo"]
    if (namespace["metadata"]["labels"].get("liveprobe.io/evaluation") != MARKER
            or version["gitVersion"] != KUBERNETES or node["metadata"]["name"] != NODE
            or node_info["kubeletVersion"] != KUBERNETES or node_info["architecture"] != "arm64"
            or version["platform"] != "linux/arm64"):
        raise RuntimeError("unexpected dedicated dev cluster identity")
    return node


def verified_images(sources=None, filename="verified-oom-images.json"):
    receipt = json.loads((ROOT / filename).read_text())
    if receipt.get("streamed_and_verified") is not True:
        raise RuntimeError("the expected images require a completed verified OCI stream")
    fields = ("source_digest", "linux_arm64_manifest", "configuration_digest")
    expected = {}
    for role, name in (SOURCE_IMAGES if sources is None else sources).items():
        digests = frozenset(receipt["images"][name][field] for field in fields)
        if not all(DIGEST.fullmatch(digest) for digest in digests):
            raise RuntimeError(f"the source image has invalid digests: {role}")
        expected[role] = digests
    return expected


def require_image(container, image_id, images):
    # CRI status can report the original index, the selected manifest or the
    # image configuration. The verified OCI stream ties the three together.
    digest = DIGEST.search(image_id)
    if not digest or digest.group() not in images[container]:
        raise RuntimeError(f"dedicated workload image differs from the verified source: {container}")


def workload(name, containers, images):
    deployment = json.loads(kube("get", "deployment", name, "-o", "json"))
    service = json.loads(kube("get", "service", name, "-o", "json"))
    result = json.loads(kube("get", "pod", "-l", f"app={name}", "-o", "json"))["items"]
    pod, = result
    uid, pod_name = (pod["metadata"][key] for key in ("uid", "name"))
    status = {row["name"]: row for row in pod["status"]["containerStatuses"]}
    if (deployment["status"].get("readyReplicas") != 1 or pod["spec"]["nodeName"] != NODE
            or set(status) != set(containers) or not all(row["ready"] for row in status.values())):
        raise RuntimeError(f"isolated workload is not ready: {name}")
    slices = json.loads(kube("get", "endpointslice", "-l", f"kubernetes.io/service-name={name}", "-o", "json"))["items"]
    endpoints = {end["targetRef"]["uid"] for item in slices for end in item.get("endpoints", [])
                 if end["conditions"].get("ready")}
    service_uid = service["metadata"]["uid"]
    if (endpoints != {uid} or not all(any(ref.get("kind") == "Service" and ref.get("uid") == service_uid
            for ref in group["metadata"].get("ownerReferences", [])) for group in slices)):
        raise RuntimeError("the fixture Service has an unexpected endpoint")
    groups = {}
    for container in containers:
        image_id = status[container]["imageID"]
        require_image(container, image_id, images)
        original = kube("exec", f"pod/{pod_name}", "-c", container, "--", "cat",
                        "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current",
                        "/sys/fs/cgroup/memory.oom.group")
        limit, current, mode = map(int, original.split())
        expected = 512 * MIB if container == containers[0] else 64 * MIB
        if limit != expected or not 0 < current < limit:
            raise RuntimeError("the dedicated cgroup does not match the approved limit")
        groups[container] = {"memory_limit_bytes": limit, "memory_current_bytes": current, "oom_group": mode,
                             "image_id": image_id, "restarts": status[container]["restartCount"]}
    raw = kube("exec", f"pod/{pod_name}", "-c", containers[0], "--", "sh", "-ec", CONTROL_READ)
    control = json.loads(raw)
    if (control.get("mode"), control.get("operation_id"), control.get("readiness")) != ("off", None, "ACCEPTING_TRAFFIC"):
        raise RuntimeError("the isolated workload fault control is not idle")
    return {"pod_uid": uid, "pod_name": pod_name, "containers": groups, "control_idle": True,
            "service_uid": service_uid, "service_spec": service["spec"],
            "deployment_uid": deployment["metadata"]["uid"],
            "deployment_sha256": sha256(json.dumps(deployment["spec"], sort_keys=True).encode()).hexdigest()}


def business_requests(scope):
    return {key: route for key, route in BUSINESS_REQUESTS.items() if route[0] in FIXTURE_SCOPES[scope]}


def business_checks(scope="all"):
    responses = {key: json.loads(kube("get", "--raw", f"/api/v1/namespaces/{NAMESPACE}/services/{name}:8080/proxy/{path}"))
                 for key, (name, path) in business_requests(scope).items()}
    stock = responses["inventory_lookup"]
    if (stock.get("item"), stock.get("quantity"), stock.get("available")) != ("a", 42, True):
        raise RuntimeError("the private database fixtures did not return the approved examples")
    if "order_lookup" in responses and responses["order_lookup"].get("id") != 1:
        raise RuntimeError("the private database fixtures did not return the approved examples")
    for key in responses.keys() & {"order_stock_check", "dependent_order_check"}:
        dependent = responses[key]
        if (dependent.get("item") != "a" or dependent.get("orderable") is not True or dependent.get("inventory") != stock):
            raise RuntimeError("the order-to-stock request did not report the same approved fixture")
    return dict.fromkeys(responses, 200)


def matching_fixture_scope(read, namespace, scope):
    if scope == "all":
        return namespace.get("labels", {}).get(FIXTURE_SCOPE_LABEL, "all") == scope
    if namespace.get("labels", {}).get(FIXTURE_SCOPE_LABEL) != scope:
        return False
    deployments = json.loads(read("get", "deployments", "-o", "json"))["items"]
    return {row["metadata"]["name"] for row in deployments} == set(FIXTURE_SCOPES[scope])


def receiver_pod(kind, namespace, name, container, images):
    resource = json.loads(kube("get", kind, name, "-o", "json", namespace=namespace))
    state = resource["status"]
    labels = resource["spec"]["selector"]["matchLabels"]
    if (not labels or state.get("observedGeneration") != resource["metadata"]["generation"]
            or state.get("replicas") != 1 or state.get("readyReplicas") != 1
            or resource["spec"].get("replicas") != 1
            or kind == "statefulset" and state.get("currentRevision") != state.get("updateRevision")
            or kind == "deployment" and state.get("updatedReplicas") != 1):
        raise RuntimeError(f"the isolated {name} workload is not settled")
    selector = ",".join(f"{key}={value}" for key, value in labels.items())
    pod, = json.loads(kube("get", "pods", "-l", selector, "-o", "json", namespace=namespace))["items"]
    meta = pod["metadata"]
    spec = pod["spec"]
    status, = pod["status"]["containerStatuses"]
    if (meta.get("deletionTimestamp") or spec.get("nodeName") != NODE or status["name"] != container
            or status.get("ready") is not True or not status.get("state", {}).get("running")
            or not meta.get("uid")):
        raise RuntimeError(f"the isolated {name} Pod is not running on the expected node")
    if any("hostPath" in volume or "persistentVolumeClaim" in volume for volume in spec.get("volumes", [])):
        raise RuntimeError(f"the isolated {name} Pod refers to external storage")
    require_image(name, status["imageID"], images)
    receipt = {"name": meta["name"], "uid": meta["uid"], "image_id": status["imageID"]}
    retention = meta.get("annotations", {}).get(RETENTION_ANNOTATION)
    if name == "liveprobe-api" and retention:
        receipt["rag_retention_sha256"] = retention
    return receipt


def receiver_get(namespace, service, port, path):
    route = f"/api/v1/namespaces/{namespace}/services/{service}:{port}/proxy{path}"
    return kube("get", "--raw", route, namespace=namespace)


def product_read_args(pod, path):
    if path not in PRODUCT_READS:
        raise ValueError("unexpected isolated product observation")
    return ("exec", f"pod/{pod}", "-c", "api", "--", "python", "-c", PRODUCT_PROBE, path)


def verify_oom_metrics():
    def prom(path):
        response = json.loads(receiver_get(OBSERVABILITY, "prometheus", 9090, "/api/v1/" + path))
        if response.get("status") != "success":
            raise RuntimeError("the isolated Prometheus query did not succeed")
        return response["data"]

    rule, = [rule for group in prom("rules")["groups"] if group["name"] == "p18-environments"
             for rule in group["rules"] if rule["name"] == OOM_ALERT]
    if (rule["health"] != "ok" or rule["duration"] != 0
            or "".join(rule["query"].split()) != "".join(OOM_RULE.split())
            or rule.get("alerts")):
        raise RuntimeError("the isolated node has a different or already active OOM rule")
    expr = 'up{job="kube-state-metrics"}'
    query = urlencode({"query": f"{expr} and (timestamp({expr}) > time() - 45)"})
    metric, = prom("query?" + query)["result"]
    if metric["metric"].get("job") != "kube-state-metrics" or float(metric["value"][1]) != 1:
        raise RuntimeError("the isolated kube-state-metrics scrape is not current")
    alerts, = prom("alertmanagers")["activeAlertmanagers"]
    destination = urlsplit(alerts["url"])
    if (destination.scheme, destination.hostname, destination.port, destination.path) != (
            "http", f"alertmanager.{OBSERVABILITY}.svc", 9093, "/api/v2/alerts"):
        raise RuntimeError("Prometheus is not bound to the isolated Alertmanager")
    receiver_get(OBSERVABILITY, "alertmanager", 9093, "/-/ready")


def receiver_checks(*, cached=False, rag_retention_sha256=None):
    expected = {name: image for _, _, name, _, image in RECEIVER_COMPONENTS}
    if cached:
        expected["liveprobe-api"] = CACHED_RECEIVER_IMAGE
    receipt_name = "verified-oom-cached-receiver.json" if cached else "verified-oom-receiver.json"
    images = verified_images(expected, receipt_name)
    for namespace in (SYSTEM, OBSERVABILITY):
        meta = json.loads(kube("get", "namespace", namespace, "-o", "json"))["metadata"]
        if meta.get("labels", {}).get("liveprobe.io/evaluation") != MARKER:
            raise RuntimeError(f"the {namespace} runtime is outside the isolated trial")
    pods = {name: receiver_pod(kind, namespace, name, container, images)
            for kind, namespace, name, container, _ in RECEIVER_COMPONENTS}
    api = pods["liveprobe-api"]["name"]
    if pods["liveprobe-api"].get("rag_retention_sha256") != rag_retention_sha256:
        raise RuntimeError("the isolated receiver RAG retention provenance differs")
    if rag_retention_sha256 and kube("exec", f"pod/{api}", "-c", "api", "--", "python", "-c",
                                   RETENTION_FILES, namespace=SYSTEM).strip() != rag_retention_sha256:
        raise RuntimeError("the isolated receiver RAG retention files differ")
    probe_sql = ("BEGIN READ ONLY; SELECT json_build_object('primary', NOT pg_is_in_recovery(),"
                 "'read_only_default', current_setting('default_transaction_read_only') = 'on',"
                 "'incident_schema', to_regclass('public.incidents') IS NOT NULL); ROLLBACK;")
    database = json.loads(kube("exec", f"pod/{pods['postgres']['name']}", "-c", "postgres", "--", "psql",
                               "-U", "liveprobe", "-d", "liveprobe", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                               "-c", probe_sql, namespace=SYSTEM))
    if database != {"primary": True, "read_only_default": False, "incident_schema": True}:
        raise RuntimeError("the isolated incident database is not the expected primary with initialized schema")
    verify_oom_metrics()
    ready = json.loads(kube(*product_read_args(api, "/api/readyz"), namespace=SYSTEM))
    status = json.loads(kube(*product_read_args(api, "/api/status"), namespace=SYSTEM))
    if (ready.get("db") is not True or ready.get("background") is not True or status.get("ready") is not True
            or status.get("target_namespaces") != [NAMESPACE] or status.get("llm", {}).get("provider") != "codex_cli"):
        raise RuntimeError("the isolated receiver is not ready to monitor dev with Codex")
    if rag_retention_sha256 and (type(ready.get("rag_chunks")) is not int or ready["rag_chunks"] <= 0):
        raise RuntimeError("the isolated receiver did not load the RAG corpus after the retention change")
    epochs = {}
    for key in ("endpoint_poller", "event_watch", "pod_watch"):
        watches = status["watchers"]
        watch = watches.get(f"{key}/{NAMESPACE}") or watches.get(key) or {}
        if watch.get("state") != "running" or not watch.get("since"):
            raise RuntimeError(f"the isolated receiver has no running {key}")
        epochs[key] = watch["since"]
    return {"pods": pods, "database": database, "watcher_epochs": epochs,
            "oom_rule": OOM_ALERT, "model_round_trip": "unverified"}


def main(*, receiver=False, cached_receiver=False, fixture_scope="all", rag_retention_sha256=None):
    if rag_retention_sha256 is not None and (not cached_receiver or not re.fullmatch(r"[0-9a-f]{64}", rag_retention_sha256)):
        raise ValueError("RAG retention is scoped to the verified cached receiver")
    node = isolated_node()
    limits = gate_source()
    initial_gate = admit_node(limits)
    images = verified_images()
    samples = {name: workload(name, FIXTURES[name], images) for name in FIXTURE_SCOPES[fixture_scope]}
    business = business_checks(fixture_scope)
    receiver_observation = {"receiver": receiver_checks(cached=cached_receiver,
                                                       rag_retention_sha256=rag_retention_sha256)} if receiver else {}
    # The business and cgroup reads take time and may themselves change load.
    # A passed admission sample must be newer than those reads.
    gate = admit_node(limits)
    namespace = json.loads(kube("get", "namespace", NAMESPACE, "-o", "json"))["metadata"]
    if not matching_fixture_scope(kube, namespace, fixture_scope):
        raise RuntimeError("the declared dev fixture scope differs from the running workloads")
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "node": NODE,
                      "node_uid": node["metadata"]["uid"], "namespace_uid": namespace["uid"],
                      "kubernetes": KUBERNETES, "kubelet": node["status"]["nodeInfo"]["kubeletVersion"],
                      "runtime": node["status"]["nodeInfo"]["containerRuntimeVersion"],
                      "initial_gate": initial_gate, "gate": gate, "fixture_scope": fixture_scope,
                      "workloads": samples, "business": business,
                      "fault_injections": 0, **receiver_observation}))


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--receiver", action="store_true", help="include the isolated monitor and compact incident receiver")
    scope.add_argument("--cached-receiver", action="store_true", help="check the frozen original receiver from the Docker cache")
    parser.add_argument("--fixture-scope", choices=FIXTURE_SCOPES, default="all")
    parser.add_argument("--rag-retention-sha256")
    args = parser.parse_args()
    main(receiver=args.receiver or args.cached_receiver, cached_receiver=args.cached_receiver,
         fixture_scope=args.fixture_scope, rag_retention_sha256=args.rag_retention_sha256)
