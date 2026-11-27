"""Render the bounded dev application trial resources for one blinded JDBC image."""

from argparse import ArgumentParser
import json
from pathlib import Path
import re


NAME = "lp-eval-reservation"
POOL_NAME = "lp-eval-pool"
POOL_IMAGE = "liveprobe/eval-pool:matched-20261126"
POOL_CONFIG = POOL_NAME + "-settings"
POOL_VERSION = POOL_CONFIG + "-v1"
POOL_KEY = "RESERVATION_POOL_MAX"
POOL_READY = {POOL_KEY: "4"}
POOL_BROKEN = {POOL_KEY: "1"}
PG_IMAGE = "postgres@sha256:d4bb0a8c1b7bb2e29f976d099e7bfb9a5d8858cffe9e46b35cd302cd1f1f8168"
BOOTSTRAP = Path(__file__).resolve().parent / "fixtures/jdbc-recovery/bootstrap.sql"
IMAGE = re.compile(r"(?:localhost:[0-9]{2,5}/)?liveprobe/eval-reservation@sha256:[0-9a-f]{64}")


def manifest(image: str, *, pool: bool = False) -> dict:
    if not (image == POOL_IMAGE if pool else IMAGE.fullmatch(image)):
        raise ValueError("the trial reservation image must be digest pinned in its designated repository")
    workload = POOL_NAME if pool else NAME
    labels = {"app": workload, "app.kubernetes.io/part-of": "liveprobe-verification", "liveprobe.io/environment": "dev"}
    schema_name = workload + "-schema"

    def resource(api, kind, name, **body):
        return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": "dev", "labels": labels}, **body}

    def secret_env(name):
        return {"name": name, "valueFrom": {"secretKeyRef": {"name": workload + "-db", "key": "password"}}}

    schema = resource("v1", "ConfigMap", schema_name, immutable=True, data={"10-reservations.sql": BOOTSTRAP.read_text()})
    service = resource("v1", "Service", workload, spec={"selector": labels,
        "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]})
    postgres = {"name": "postgres", "image": PG_IMAGE, "imagePullPolicy": "IfNotPresent",
                "args": ["-c", "listen_addresses=127.0.0.1", "-c", "max_connections=12", "-c", "shared_buffers=24MB"],
                "env": [{"name": "POSTGRES_USER", "value": "reservation"}, {"name": "POSTGRES_DB", "value": "reservation"},
                        {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}, secret_env("POSTGRES_PASSWORD")],
                "readinessProbe": {"exec": {"command": ["pg_isready", "-q", "-h", "127.0.0.1", "-U", "reservation"]},
                                   "periodSeconds": 2},
                "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "250m", "memory": "128Mi"}},
                "volumeMounts": [{"name": "schema", "mountPath": "/docker-entrypoint-initdb.d", "readOnly": True},
                                 {"name": "data", "mountPath": "/var/lib/postgresql/data"}]}
    application = {"name": workload, "image": image, "imagePullPolicy": "IfNotPresent",
                   "ports": [{"name": "http", "containerPort": 8080}],
                   "env": [{"name": "SPRING_DATASOURCE_URL", "value": "jdbc:postgresql://127.0.0.1:5432/reservation"},
                           {"name": "SPRING_DATASOURCE_USERNAME", "value": "reservation"},
                           {"name": "JAVA_TOOL_OPTIONS", "value": "-Xmx128m"}, secret_env("SPRING_DATASOURCE_PASSWORD")],
                   "readinessProbe": {"httpGet": {"path": "/actuator/health/readiness", "port": "http"}, "periodSeconds": 2},
                   "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "400m", "memory": "320Mi"}},
                   "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                       "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True, "runAsUser": 10001},
                   "volumeMounts": [{"name": "temporary", "mountPath": "/tmp"}]}
    deployment = resource("apps/v1", "Deployment", workload, spec={
        "replicas": 1, "strategy": {"type": "Recreate"}, "revisionHistoryLimit": 3,
        "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [application, postgres], "volumes": [
                {"name": "schema", "configMap": {"name": schema_name}},
                {"name": "data", "emptyDir": {"sizeLimit": "192Mi"}},
                {"name": "temporary", "emptyDir": {"sizeLimit": "32Mi"}},
            ],
        }},
    })
    template = deployment["spec"]["template"]
    template["metadata"]["annotations"] = {"prometheus.io/scrape": "true", "prometheus.io/port": "8080",
                                             "prometheus.io/path": "/actuator/prometheus"}
    scrape = resource("networking.k8s.io/v1", "NetworkPolicy", workload + "-scrape", spec={
        "podSelector": {"matchLabels": {"app": workload}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "liveprobe-observability",
            }}, "podSelector": {"matchLabels": {"app": "prometheus"}}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }],
    })
    items = [schema, service, deployment, scrape]
    if pool:
        labels["liveprobe.io/managed"] = "true"
        application["envFrom"] = [{"configMapRef": {"name": POOL_CONFIG}}]
        postgres["restartPolicy"] = "Always"
        postgres["startupProbe"] = {**postgres["readinessProbe"], "failureThreshold": 30}
        template["spec"]["containers"] = [application]
        template["spec"]["initContainers"] = [postgres]
        items.extend((resource("v1", "ConfigMap", POOL_CONFIG, data=POOL_READY),
                      resource("v1", "ConfigMap", POOL_VERSION, immutable=True, data=POOL_READY)))
    return {"apiVersion": "v1", "kind": "List", "items": items}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--pool", action="store_true")
    args = parser.parse_args()
    print(json.dumps(manifest(args.image, pool=args.pool), indent=2))
