"""Prepare the current scoped dev workloads for the matched upstream and backlog trials."""

from copy import deepcopy
import re

from evaluation.jdbc_dev_manifest import manifest


NS = "dev"
UPSTREAM = "lp-process"
BACKLOG = "lp-cpu"
STOCK = "lp-eval-inventory-pulse"
ROUTE = "lp-eval-inventory-route"
CAPACITY_DATA = "lp-eval-capacity"
OWNER = "distinct-five-20261116"
PROBE = {"prometheus.io/scrape": "true", "prometheus.io/path": "/actuator/prometheus",
         "prometheus.io/port": "8080"}


def require_owner(deployment, name):
    meta = deployment["metadata"]
    labels = meta.get("labels") or {}
    app = deployment["spec"]["template"]["spec"]["containers"]
    if ((meta.get("namespace"), meta.get("name"), labels.get("liveprobe.io/campaign"),
         labels.get("app.kubernetes.io/part-of"), labels.get("liveprobe.io/managed")) !=
            (NS, name, OWNER, "liveprobe-verification", "true")
            or len(app) != 1 or app[0]["name"] != name or deployment["spec"]["replicas"] != 1):
        raise ValueError("the approved existing dev trial workload is unavailable")


def upstream_template(deployment, image):
    require_owner(deployment, UPSTREAM)
    if not re.fullmatch(r"liveprobe/eval-caller@sha256:[0-9a-f]{64}", image):
        raise ValueError("upstream image must use the sealed fixture repository")
    template = deepcopy(deployment["spec"]["template"])
    template["metadata"].setdefault("annotations", {}).update(PROBE)
    spec = template["spec"]
    spec["automountServiceAccountToken"] = False
    application = spec["containers"][0]
    application.update(image=image, imagePullPolicy="IfNotPresent", command=["python", "/fixture/matched-caller.py"],
                       args=[], readinessProbe={"httpGet": {"path": "/actuator/health/readiness", "port": 8080},
                                               "periodSeconds": 2})
    return template


def backlog_template(deployment, image):
    require_owner(deployment, BACKLOG)
    blueprint = next(x for x in manifest(image)["items"] if x["kind"] == "Deployment")["spec"]["template"]
    template = deepcopy(deployment["spec"]["template"])
    template["metadata"].setdefault("annotations", {}).update(PROBE)
    # Keep the identity and workload-level controls on the previously authorized test target.
    spec = template["spec"]
    recipe = blueprint["spec"]
    application, storage = recipe["containers"]
    application["name"] = BACKLOG
    storage["restartPolicy"] = "Always"
    storage["startupProbe"] = {**storage["readinessProbe"], "failureThreshold": 30}
    volumes = recipe["volumes"]
    for volume in volumes:
        if volume["name"] == "schema":
            volume["configMap"]["name"] = CAPACITY_DATA + "-schema"
    for container in (application, storage):
        for setting in container["env"]:
            reference = setting.get("valueFrom", {}).get("secretKeyRef")
            if reference is not None:
                reference["name"] = CAPACITY_DATA + "-db"
    spec.update(containers=[application], initContainers=[storage], volumes=volumes, automountServiceAccountToken=False)
    return template


def update_template(deployment, replacement):
    meta = deployment["metadata"]
    return [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/spec/template", "value": deployment["spec"]["template"]},
        {"op": "replace", "path": "/spec/template", "value": replacement},
    ]


def policy(name, app, **direction):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name, "namespace": NS},
            "spec": {"podSelector": {"matchLabels": {"app": app}},
                     "policyTypes": [key.capitalize() for key in direction], **direction}}


def selector(namespace, app, *, key="app"):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": {key: app}}}


def connectivity(stock_service):
    stock = stock_service["spec"]
    if (stock_service["metadata"]["name"] != STOCK or stock_service["metadata"]["namespace"] != NS
            or stock.get("selector", {}).get("app") != STOCK or len(stock["ports"]) != 1 or stock["ports"][0]["port"] != 8080):
        raise ValueError("inventory fixture Service identity does not match")
    endpoint = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": ROUTE, "namespace": NS},
                "spec": {"selector": stock["selector"], "ports": [{"port": 8080, "targetPort": stock["ports"][0]["targetPort"]}]}}
    http_port = [{"port": 8080, "protocol": "TCP"}]
    metrics = {"from": [selector("liveprobe-observability", "prometheus")], "ports": http_port}
    callers = [
        {"to": [selector(NS, STOCK)], "ports": http_port},
        {"to": [selector("kube-system", "kube-dns", key="k8s-app")],
         "ports": [{"port": 53, "protocol": protocol} for protocol in ("TCP", "UDP")]},
    ]
    return {"apiVersion": "v1", "kind": "List", "items": [
        endpoint,
        policy("lp-matched-upstream-caller", UPSTREAM, ingress=[metrics], egress=callers),
        policy("lp-matched-upstream-stock", STOCK, ingress=[{"from": [selector(NS, UPSTREAM)], "ports": http_port}]),
        policy("lp-matched-backlog-scrape", BACKLOG, ingress=[metrics]),
    ]}
