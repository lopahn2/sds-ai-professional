"""Render the formal-run kit: Kubernetes objects for one scenario in a LiveProbe-watched namespace.

    python -m evaluation.compound.k8s --scenario C1 [--namespace dev] > c1.yaml
    kubectl --context rancher-desktop apply --dry-run=server -f c1.yaml     # validation only

The kit deploys the same shopfront JAR, database and traffic as the local reproduction, with the platform
loops now real (kubelet probes, Service endpoints, LimitRange admission). Faults are applied by the
operator-side steps printed with --steps; nothing here carries the answer key, and every name avoids test
vocabulary because LiveProbe and the single model read these objects and logs.
Apply only inside an exclusive, approved fault window: the dev namespace is shared with other trials.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

FIXTURE = Path(__file__).resolve().parent / "fixture"
STOCK_IMAGE = "registry.shop.internal/shopfront/stock:1.4.2"
ORDER_IMAGE = "registry.shop.internal/shopfront/order:1.4.2-b8"
PG_IMAGE = "postgres:17-alpine"
LOADGEN_IMAGE = "python:3.12-slim-bookworm"
PREFIX = "shop-"   # names: shop-stock, shop-order, shop-db ...


def meta(name: str, ns: str, app: str, extra: dict | None = None) -> dict:
    labels = {"app": app, "app.kubernetes.io/part-of": "shopfront", **(extra or {})}
    return {"name": name, "namespace": ns, "labels": labels}


def db_statefulset(ns: str, name: str, replicas: int, replication: bool, profile: str = "reporting") -> list[dict]:
    """PostgreSQL; with replication, ordinal 0 starts as primary and the others clone it as hot standbys."""
    start = (
        "set -e\n"
        "ord=${HOSTNAME##*-}\n"
        "if [ \"$ord\" != 0 ] && [ ! -s \"$PGDATA/PG_VERSION\" ]; then\n"
        f"  until pg_basebackup -h {name}-0.{name}-hl -U replicator -D \"$PGDATA\" -R -X stream; do sleep 2; done\n"
        "  chmod 700 \"$PGDATA\"\n"
        f"  # {profile}-replica access profile: only the {profile} role may connect over TCP\n"
        f"  sed -i 's/^host all all all scram-sha-256/host all {profile} all scram-sha-256/' \"$PGDATA/pg_hba.conf\"\n"
        "fi\n"
        "exec docker-entrypoint.sh postgres -c 'log_line_prefix=%m [%p] %q%u@%d '\n"
    ) if replication else "exec docker-entrypoint.sh postgres -c 'log_line_prefix=%m [%p] %q%u@%d '\n"
    init = (FIXTURE / "bootstrap.sql").read_text()
    if replication:
        init += ("\\connect postgres\nRESET ROLE;\nCREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replica-local';\n")
    hba = "echo 'host replication replicator all scram-sha-256' >> \"$PGDATA/pg_hba.conf\"\n"
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(f"{name}-init", ns, name),
         "data": {"10-shop.sql": init, "20-replication.sh": hba if replication else "true\n", "start.sh": start}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta(f"{name}-hl", ns, name),
         "spec": {"clusterIP": "None", "selector": {"app": name}, "ports": [{"name": "pg", "port": 5432}]}},
        *([] if replication else [  # single node: the client Service the applications use
            {"apiVersion": "v1", "kind": "Service", "metadata": meta(name, ns, name),
             "spec": {"selector": {"app": name}, "ports": [{"name": "pg", "port": 5432}]}}]),
        {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": meta(name, ns, name),
         "spec": {"serviceName": f"{name}-hl", "replicas": replicas, "selector": {"matchLabels": {"app": name}},
                  "template": {"metadata": {"labels": {"app": name, "app.kubernetes.io/part-of": "shopfront"}},
                               "spec": {"containers": [{
                                   "name": "postgres", "image": PG_IMAGE, "command": ["sh", "/init/start.sh"],
                                   "env": [{"name": "POSTGRES_PASSWORD", "value": "platform-local"},
                                           {"name": "PGPASSWORD", "value": "replica-local"},
                                           {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}],
                                   "ports": [{"containerPort": 5432, "name": "pg"}],
                                   "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "postgres"]},
                                                      "periodSeconds": 5},
                                   "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                 "limits": {"cpu": "500m", "memory": "384Mi"}},
                                   "volumeMounts": [{"name": "start", "mountPath": "/init"},
                                                    {"name": "initdb", "mountPath": "/docker-entrypoint-initdb.d"},
                                                    {"name": "data", "mountPath": "/var/lib/postgresql/data"}]}],
                                   "volumes": [
                                       {"name": "start", "configMap": {"name": f"{name}-init",
                                                                       "items": [{"key": "start.sh", "path": "start.sh"}]}},
                                       {"name": "initdb", "configMap": {"name": f"{name}-init", "items": [
                                           {"key": "10-shop.sql", "path": "10-shop.sql"},
                                           {"key": "20-replication.sh", "path": "20-replication.sh"}]}},
                                       {"name": "data", "emptyDir": {}}]}}}},
    ]


def app(ns: str, role: str, image: str, env: dict, probes: dict, resources: dict, annotations: dict | None = None) -> list[dict]:
    name = PREFIX + role
    container = {"name": role, "image": image, "imagePullPolicy": "IfNotPresent",
                 "args": [f"--spring.profiles.active={role}"],
                 "env": [{"name": k, "value": v} for k, v in env.items()],
                 "ports": [{"containerPort": 8080, "name": "http"}], **probes, "resources": resources}
    template_meta = {"labels": {"app": name, "app.kubernetes.io/part-of": "shopfront"},
                     "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": "8080",
                                     "prometheus.io/path": "/actuator/prometheus", **(annotations or {})}}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(name, ns, name),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                  "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                  "template": {"metadata": template_meta, "spec": {"containers": [container]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta(name, ns, name),
         "spec": {"selector": {"app": name}, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
    ]


LOADGEN = r'''
import json, os, random, threading, time, urllib.request
plan = json.loads(os.environ["PLAN"])  # [[seconds_from_start, [[rate, channel, [skus]], ...]], ...]
start = time.time()
def shoot(channel, sku):
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://shop-order:8080/api/orders?sku={sku}&channel={channel}", method="POST"), timeout=15).read()
    except Exception:
        pass
rng = random.Random()
while True:
    elapsed = time.time() - start
    mix = [m for at, m in plan if at <= elapsed][-1]
    for rate, channel, skus in mix:
        if rate > 0 and rng.random() < rate / 20:
            threading.Thread(target=shoot, args=(channel, rng.choice(skus)), daemon=True).start()
    time.sleep(0.05)
'''


def network_policy(ns: str) -> dict:
    """Namespaces with default-deny need the shopfront pods to reach each other, DNS and Prometheus."""
    part = {"app.kubernetes.io/part-of": "shopfront"}
    traffic = {"app": PREFIX + "traffic"}
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta("shopfront-internal", ns, "shopfront"),
            "spec": {"podSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": [
                         PREFIX + "stock", PREFIX + "order", PREFIX + "traffic", PREFIX + "db", PREFIX + "db-node", "partner-feed-sync"]}]},
                     "policyTypes": ["Ingress", "Egress"],
                     "ingress": [{"from": [{"podSelector": {"matchLabels": part}}, {"podSelector": {"matchLabels": traffic}},
                                           {"podSelector": {"matchLabels": {"app": "partner-feed-sync"}}},
                                           {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "liveprobe-observability"}}}]}],
                     "egress": [{"to": [{"podSelector": {"matchLabels": part}}, {"podSelector": {"matchLabels": traffic}}]},
                                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                                 "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}]}}


def loadgen(ns: str, plan: list) -> dict:
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(PREFIX + "traffic", ns, PREFIX + "traffic"),
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": PREFIX + "traffic"}},
                     "template": {"metadata": {"labels": {"app": PREFIX + "traffic"}},
                                  "spec": {"containers": [{"name": "traffic", "image": LOADGEN_IMAGE,
                                                           "command": ["python", "-c", LOADGEN],
                                                           "env": [{"name": "PLAN", "value": json.dumps(plan)}],
                                                           "resources": {"limits": {"cpu": "200m", "memory": "96Mi"}}}]}}}}


WEB = [f"SKU-{i:04d}" for i in range(11, 41)]
PARTNER = [f"SKU-{i:04d}" for i in range(1, 11)]


def db_env(host: str, extra: dict | None = None) -> dict:
    return {"SPRING_DATASOURCE_URL": f"jdbc:postgresql://{host}:5432/shop", "SPRING_DATASOURCE_USERNAME": "shop_app",
            "SPRING_DATASOURCE_PASSWORD": "shop-app-local", **(extra or {})}


def scenario(name: str, ns: str) -> tuple[list[dict], list[str], list[dict]]:
    order = app(ns, "order", ORDER_IMAGE, {"SHOPFRONT_ORDER_STOCK_URL": f"http://{PREFIX}stock:8080"},
                {"readinessProbe": {"httpGet": {"path": "/actuator/health/readiness", "port": "http"}}},
                {"requests": {"cpu": "200m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "384Mi"}})
    readiness = {"readinessProbe": {"httpGet": {"path": "/actuator/health/readiness", "port": "http"}, "periodSeconds": 5}}
    if name in ("C1", "C1c"):
        # C1c: the same leak on another surface (other frozen SKUs, at T0 in liveprobe_arm, and a pool of 5)
        pool = {"SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE": "5", "SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE": "5"} \
            if name == "C1c" else {}
        stock = app(ns, "stock", STOCK_IMAGE, db_env(PREFIX + "db", {**pool, "SHOPFRONT_STOCK_SCORING_ROUNDS": "2000"}), {
            **readiness,
            "livenessProbe": {"httpGet": {"path": "/actuator/health", "port": "http"}, "initialDelaySeconds": 20,
                              "periodSeconds": 10, "timeoutSeconds": 1, "failureThreshold": 3}},
            {"requests": {"cpu": "500m", "memory": "384Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}})
        sync = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": meta("partner-feed-sync", ns, "partner-feed-sync"),
                "spec": {"schedule": "*/3 * * * *", "concurrencyPolicy": "Forbid",
                         "jobTemplate": {"spec": {"template": {"metadata": {"labels": {"app": "partner-feed-sync",
                                                                                            "app.kubernetes.io/part-of": "shopfront"}},
                                                               "spec": {"restartPolicy": "Never", "containers": [{
                             "name": "sync", "image": PG_IMAGE,
                             "env": [{"name": "PGPASSWORD", "value": "shop-app-local"},
                                     {"name": "HOLDS", "valueFrom": {"configMapKeyRef": {"name": "partner-gateway-state",
                                                                                         "key": "frozen"}}}],
                             "command": ["sh", "-c",
                                         "echo \"$(date -u +%FT%TZ) INFO  partner-feed-sync: fetched 10 allocations from partner gateway\"; "
                                         f"ok=0; for attempt in 1 2 3 4 5 6 7 8; do psql -h {PREFIX}db -U shop_app -d shop -qAt -c \"update partner_allocation set status = "
                                         "case when sku = any(string_to_array('$HOLDS', ',')) then 'HOLD' else 'OPEN' end, "
                                         "allocatable = 1000000, synced_at = now()\" 2>/dev/null && { ok=1; break; }; sleep 2; done; "
                                         "[ $ok = 1 ] && echo \"$(date -u +%FT%TZ) INFO  partner-feed-sync: upserted 10 rows into partner_allocation\""]}]}}}}}}
        state = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("partner-gateway-state", ns, "partner-feed-sync"),
                 "data": {"frozen": ""}}
        objects = [*db_statefulset(ns, PREFIX + "db", 1, False), *stock, *order, state, sync,
                   loadgen(ns, [[0, [[6.0, "web", WEB], [0.25, "partner", PARTNER]]]])]
        fault = []
        steps = [
            "wait for shop-stock/shop-order Ready and 3 minutes of clean traffic",
            f"decoy: kubectl -n {ns} rollout restart deployment/{PREFIX}order   (unrelated rollout ~80 s before T0)",
            f"T0: kubectl -n {ns} patch configmap partner-gateway-state -p '{{\"data\":{{\"frozen\":\"SKU-0003,SKU-0007\"}}}}'"
            "   (the next */3 sync writes HOLD)",
            "observe; collect pod logs (current+previous), namespace events and Job logs for the bundle",
        ]
    elif name in ("C2", "C2b", "C2c"):
        # C2b is the memory-round variant: the same stale-primary routing on another surface (another access
        # profile on the promoted node and another pool size).
        pool = "5" if name == "C2b" else "6"
        profile = {"C2b": "analytics", "C2c": "etl"}.get(name, "reporting")
        stock = app(ns, "stock", STOCK_IMAGE, db_env(f"{PREFIX}db", {"SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE": pool,
                                                                     "SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE": pool,
                                                                     "SHOPFRONT_STOCK_SCORING_ROUNDS": "2000"}),
                    readiness, {"requests": {"cpu": "500m", "memory": "384Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}})
        db = db_statefulset(ns, PREFIX + "db-node", 2, True, profile)
        svc = {"apiVersion": "v1", "kind": "Service", "metadata": meta(PREFIX + "db", ns, PREFIX + "db-node"),
               "spec": {"selector": {"app": PREFIX + "db-node", "role": "primary"}, "ports": [{"name": "pg", "port": 5432}]}}
        objects = [*db, svc, *stock, *order, loadgen(ns, [[0, [[6.0, "web", WEB], [0.25, "partner", PARTNER]]]])]
        fault = []
        steps = [
            f"kubectl -n {ns} label pod {PREFIX}db-node-0 role=primary; wait until {PREFIX}db-node-1 streams (pg_stat_replication)",
            "decoy: run the nightly backup Job against the replica (logs a 1.2 s replay-lag warning)",
            f"T0 (switchover Job, as the platform's): ALTER SYSTEM SET default_transaction_read_only=on + pg_reload_conf() on "
            f"{PREFIX}db-node-0; wait replay; SELECT pg_promote() on {PREFIX}db-node-1; "
            f"kubectl label pod {PREFIX}db-node-1 role=primary; leave {PREFIX}db-node-0 labelled role=primary and log the "
            "write-conflict line with 'will retry in 300s'; the Job then completes",
            "observe; collect stock/order/db pod logs, events and the Job log",
        ]
    elif name in ("C3", "C3b", "C3c"):
        # C3b is the memory-round variant: the same mechanism on another surface (a different LimitRange name
        # and value, and a certificate-rotation restart instead of a log-level reload).
        variant = name == "C3b"
        lr_name, lr_cpu, lr_req = {"C3b": ("team-quota-defaults", "300m", "150m"),
                                   "C3c": ("workload-defaults", "280m", "100m")}.get(
                                       name, ("namespace-defaults", "250m", "100m"))
        # the heavy allocation plan (1.5M rounds) is the image's own stock-profile default
        stock = app(ns, "stock", STOCK_IMAGE, db_env(PREFIX + "db", {"SHOPFRONT_STOCK_PARTNER_LEDGER": "false"}),
                    readiness, {"requests": {"cpu": "250m", "memory": "384Mi"}, "limits": {"memory": "512Mi"}},
                    annotations={"configmap.reloader.stakater.com/reload": "shop-stock-config"})
        limits = {"apiVersion": "v1", "kind": "LimitRange",
                  "metadata": meta(lr_name, ns, "platform-policy"),
                  "spec": {"limits": [{"type": "Container", "default": {"cpu": lr_cpu},
                                       "defaultRequest": {"cpu": lr_req}}]}}
        cfg = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("shop-stock-config", ns, PREFIX + "stock"),
               "data": {"LOGGING_LEVEL_COM_ACME": "INFO"}}
        objects = [*db_statefulset(ns, PREFIX + "db", 1, False), cfg, *stock, *order,
                   loadgen(ns, [[0, [[10.0, "web", WEB]]], [300, [[15.0, "web", WEB]]], [390, [[10.0, "web", WEB]]]])]
        fault = [limits]
        steps = [
            "deploy everything except the LimitRange; wait for 3 minutes of clean traffic",
            "the morning before T0: apply the LimitRange `namespace-defaults` (no pod restarts, no events)",
            f"T0: edit ConfigMap shop-stock-config (log level) and roll the Deployment as Reloader does "
            f"(kubectl -n {ns} rollout restart deployment/{PREFIX}stock); the new pod is admitted with cpu limit 250m",
            "the traffic Deployment steps to 1.5x for 90 s at +300 s; observe; collect logs and events",
        ]
    else:
        raise SystemExit(f"unknown scenario {name}")
    return [network_policy(ns), *objects], steps, fault


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("C1", "C1c", "C2", "C2b", "C2c", "C3", "C3b", "C3c"))
    parser.add_argument("--namespace", default="dev")
    parser.add_argument("--steps", action="store_true", help="print the operator-side fault steps instead of YAML")
    parser.add_argument("--fault-objects", action="store_true", help="print objects the fault steps apply (C3 LimitRange)")
    args = parser.parse_args()
    objects, steps, fault = scenario(args.scenario, args.namespace)
    if args.fault_objects:
        print(yaml.safe_dump_all(fault, sort_keys=False, width=140))
    elif args.steps:
        print("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
    else:
        print(yaml.safe_dump_all(objects, sort_keys=False, width=140))


if __name__ == "__main__":
    main()
