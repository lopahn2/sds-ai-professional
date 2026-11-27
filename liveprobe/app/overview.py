"""P15 cluster operations overview. Measured series and Kubernetes facts, not a synthetic health score."""
from __future__ import annotations

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import monotonic
from typing import Any

import httpx
from kubernetes.utils.quantity import parse_quantity

from .observation_sources import adapter_url
from .flows import FlowStore, _wl_name, build_topology
from .k8s import parse_ts
from .systems import current_inventory


def _atlas_environment(environment, namespace):
    return (environment or namespace) in ("dev", "prd")


# Preserve missing series as None. A ratio's denominator may be zero; NaN is not a healthy zero.
_POD_CPU = ('sum by(namespace,pod)(rate(container_cpu_usage_seconds_total{pod!="",container!="",container!="POD"}[2m])) '
            'or on(namespace,pod) max by(namespace,pod)(rate(container_cpu_usage_seconds_total{pod!="",container=""}[2m]))')
_POD_MEM = ('sum by(namespace,pod)(container_memory_working_set_bytes{pod!="",container!="",container!="POD"}) '
            'or on(namespace,pod) max by(namespace,pod)(container_memory_working_set_bytes{pod!="",container=""})')
_PAIR = "source_workload_namespace,source_workload,destination_workload_namespace,destination_workload"
QUERIES = {
    "pod_cpu": _POD_CPU,
    "pod_mem": _POD_MEM,
    "pod_restarts": 'sum by(namespace,pod)(increase(kube_pod_container_status_restarts_total[1h]))',
    "node_cpu": 'max by(node)(rate(container_cpu_usage_seconds_total{id="/"}[2m]))',
    "node_mem": 'max by(node)(container_memory_working_set_bytes{id="/"})',
    "req_rate": 'sum by(namespace,app)(rate(http_server_requests_seconds_count[2m]))',
    "err_rate": ('sum by(namespace,app)(rate(http_server_requests_seconds_count{status=~"5.."}[2m])) / '
                 'sum by(namespace,app)(rate(http_server_requests_seconds_count[2m]))'),
    "p95_ms": ('1000 * histogram_quantile(0.95,sum by(namespace,app,le)('
               'rate(http_server_requests_seconds_bucket{uri!~"/actuator.*"}[2m]))) '
               # Fallback only where the app exposes no histogram: server-side Envoy latency (raw, milliseconds).
               'or on(namespace,app) histogram_quantile(0.95,sum by(namespace,app,le)('
               'label_replace(rate(istio_request_duration_milliseconds_bucket{reporter="destination"}[2m]),'
               '"app","$1","destination_app","(.+)")))'),
    "edge_req": f'sum by({_PAIR})(rate(istio_requests_total{{reporter="destination"}}[2m]))',
    "edge_err": (f'sum by({_PAIR})(rate(istio_requests_total{{reporter="destination",response_code=~"5.."}}[2m])) / '
                 f'sum by({_PAIR})(rate(istio_requests_total{{reporter="destination"}}[2m]))'),
}
ATLAS_METRICS = ("req_rate", "err_rate", "edge_req", "edge_err")
METRIC_TTL_SECONDS = 10


def _query_metrics(url, names) -> dict:
    workers = min(5, len(names))
    try:
        transport = httpx.Client(timeout=4, limits=httpx.Limits(max_connections=workers, max_keepalive_connections=workers))
    except Exception as exc:  # noqa: BLE001 - retain per-series unobserved status on shared startup failure
        return {key: ([], type(exc).__name__) for key in names}

    def query(key):
        try:
            r = transport.get(url, params={"query": QUERIES[key]})
            r.raise_for_status()
            b = r.json()
            if b.get("status") != "success" or b.get("data", {}).get("resultType") != "vector":
                return key, [], "invalid_response"
            return key, b["data"]["result"], None
        except Exception as e:  # noqa: BLE001 - one failed source cannot blank the cluster map
            return key, [], type(e).__name__
    with transport, ThreadPoolExecutor(max_workers=workers, thread_name_prefix="overview-prom") as pool:
        return {key: (rows, err) for key, rows, err in pool.map(query, names)}


def fetch_metrics(settings, *, atlas: bool = False, cache: dict | None = None) -> tuple[dict, dict]:
    """Read each source series once per freshness window. The caller serializes use of a shared cache."""
    names = ATLAS_METRICS if atlas else tuple(QUERIES)
    failures = {}
    try:
        url = f"{adapter_url(settings, 'prometheus')}/api/v1/query"
    except Exception as exc:  # noqa: BLE001 - invalid adapter settings leave metrics unobserved
        failures = {name: type(exc).__name__ for name in names}
        rows = {name: [] for name in names}
    else:
        if cache is None:
            cache = {}
        if cache.get("url") != url:
            cache.clear()
            cache.update(url=url, samples={})
        samples = cache["samples"]
        now = monotonic()
        missing = [name for name in names if name not in samples or now - samples[name][0] > METRIC_TTL_SECONDS]
        if missing:
            measured = _query_metrics(url, missing)
            received_at = monotonic()
            samples.update((name, (received_at, data, err)) for name, (data, err) in measured.items())
        rows = {name: samples[name][1] for name in names}
        failures = {name: samples[name][2] for name in names if samples[name][2]}
    return rows, ({"prometheus": ", ".join(f"{key}:{value}" for key, value in failures.items())} if failures else {})


def incident_stages(session, incidents) -> dict[int, dict]:
    from sqlalchemy import case, func, or_, select

    from .models import INCIDENT_CLOSED_STATES, RoutingDecision, RunEvent
    from .narrative import stage_of

    fixed = {*INCIDENT_CLOSED_STATES, "action_pending", "acting"}
    active_ids = [incident.id for incident in incidents if incident.status not in fixed]
    routed, events = set(), defaultdict(list)
    if active_ids:
        routed = set(session.scalars(select(RoutingDecision.incident_id)
                                     .where(RoutingDecision.incident_id.in_(active_ids)).distinct()))
        # The overview needs only whether evidence arrived and the latest role, not the event history.
        role = case((RunEvent.kind == "agent_started", func.jsonb_build_object("role", RunEvent.detail["role"])))
        rank = func.row_number().over(partition_by=(RunEvent.incident_id, RunEvent.kind), order_by=RunEvent.seq.desc())
        latest = select(RunEvent.incident_id, RunEvent.kind, role.label("detail"), rank.label("rank")).where(
            RunEvent.incident_id.in_(active_ids), or_(
                RunEvent.kind == "evidence_collected",
                (RunEvent.kind == "agent_started") & (func.jsonb_typeof(RunEvent.detail) == "object"),
            ),
        ).subquery()
        query = select(latest.c.incident_id, latest.c.kind, latest.c.detail).where(latest.c.rank == 1)
        for event in session.execute(query):
            events[event.incident_id].append(event)
    return {i.id: stage_of(i, i.id in routed, [], events.get(i.id, [])) for i in incidents}


def _number(v: Any) -> float | None:
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _quantity(v: Any) -> float | None:
    try:
        return float(parse_quantity(v)) if v is not None else None
    except (ValueError, TypeError, ArithmeticError):
        return None


def _index(rows: list, labels: tuple[str, ...], *, field: int = 1) -> dict:
    out = {}
    for r in rows:
        metric = r.get("metric") or {}
        val = r.get("value") or []
        if len(val) == 2:
            out[tuple(metric.get(k, "") for k in labels)] = _number(val[field])
    return out


def _sum_seen(values) -> float | None:
    seen = [v for v in values if v is not None]
    return sum(seen) if seen else None


def _event(e: dict) -> dict:
    md = e.get("metadata") or {}
    ob = e.get("involvedObject") or e.get("involved_object") or e.get("regarding") or {}
    at = (e.get("lastTimestamp") or e.get("last_timestamp") or e.get("eventTime") or e.get("event_time") or
          (e.get("series") or {}).get("lastObservedTime") or md.get("creationTimestamp") or md.get("creation_timestamp"))
    timestamp = parse_ts(at)
    return {"at": timestamp.isoformat() if timestamp else (str(at) if at else None), "namespace": md.get("namespace"),
            "object": f"{ob.get('kind', '')}/{ob.get('name', '')}", "reason": e.get("reason"), "type": e.get("type"),
            "message": e.get("message") or e.get("note") or "", "count": e.get("count") or (e.get("series") or {}).get("count") or 1}


def atlas_view(overview: dict) -> dict:
    """Keep only fields drawn by the service atlas. Infrastructure details use the full overview."""
    def pick(item, *fields):
        return {key: item[key] for key in fields if key in item}

    def active(item):
        return _atlas_environment(item.get("environment"), item.get("namespace"))

    workloads = [pick(w, "id", "name", "system", "environment", "namespace", "kind", "ready", "desired",
                      "req_rate", "err_rate", "metric_at") for w in overview["workloads"]
                 if active(w) and w["kind"] not in ("Job", "CronJob", "Pod")]
    result = {**pick(overview, "generated_at", "inventory_observed_at", "preview_notice", "sources"),
              "workloads": workloads, "incidents": pick(overview["incidents"], "open", "counts"), "topology": None}
    topology = overview.get("topology")
    if topology is None:
        return result

    nodes = []
    for resource in topology["nodes"]:
        if (not active(resource) or resource.get("lifecycle") == "retired"
                or (resource.get("role") != "service" and resource["kind"] != "Service")):
            continue
        node = pick(resource, "id", "name", "system", "environment", "namespace", "kind", "role", "status", "endpoints_ready")
        node["warning"] = bool(resource.get("network_warnings") or resource.get("no_endpoints"))
        if resource.get("services"):
            node["services"] = [pick(s, "name", "endpoints_ready") for s in resource["services"]]
        nodes.append(node)
    ids = {item["id"] for item in workloads + nodes}
    result["topology"] = {
        "nodes": nodes,
        "contract_edges": [pick(e, "from", "to") for e in topology.get("contract_edges", []) if e["from"] in ids and e["to"] in ids],
        # Traffic from a non-displayed source can still be evidence about a displayed service.
        "observed_edges": [pick(e, "from", "to", "last_seen", "req_rate", "err_rate", "metric_at")
                           for e in topology["observed_edges"] if e["from"] in ids or e["to"] in ids],
    }
    return result


def assemble(inventory: dict, values: dict, *, events: list | None = None, incidents: list | None = None,
             today: dict | None = None, errors: dict | None = None, store: FlowStore | None = None, cfg=None,
             atlas: bool = False) -> dict:
    """Assemble one response. `inventory` has no Secret values; raw Warning messages are preserved verbatim."""
    from .config import AppConfig
    cfg = cfg or AppConfig()
    historical = set(cfg.historical_namespaces)
    incidents = [{**i, "system": i.get("system"), "environment": cfg.environment(i.get("namespace"))}
                 for i in incidents or [] if i.get("namespace") not in historical]
    errors = errors or {}
    store = store or FlowStore()
    freshness = store.freshness()
    # Incident pins come from the shared incident rows on the atlas. The detailed graph adds its own warnings.
    topo = build_topology(inventory, store.snapshot(), freshness, None if atlas else incidents, cfg=cfg)
    inventory, _ = current_inventory(inventory, historical)
    tnodes = {n["id"]: n for n in topo["nodes"]}
    # Inventory also includes deployments with no pods (scaled to zero or still waiting for scheduling).
    for d in inventory.get("deployments", []):
        md, sp, st = d.get("metadata") or {}, d.get("spec") or {}, d.get("status") or {}
        wid = f"{md.get('namespace')}/Deployment/{md.get('name')}"
        ready = st.get("readyReplicas", st.get("ready_replicas", 0))
        desired = sp.get("replicas")
        n = tnodes.setdefault(wid, {"id": wid, "namespace": md.get("namespace"), "name": md.get("name"),
                                   "kind": "Deployment", "role": "service", "pods": 0, "ready": ready})
        n["desired"], n["ready"] = desired, ready
        n["status"] = "unknown" if desired is None else "scaled_down" if desired == 0 else (
            "ready" if ready >= desired else "degraded" if ready else "down")
    podmap: dict[str, list[tuple]] = defaultdict(list)
    pods_by_ns: dict[str, list[dict]] = defaultdict(list)
    pods_by_node: dict[str, int] = defaultdict(int)
    indexes = {}
    latest = {}
    if not atlas:
        for p in inventory.get("pods", []):
            md, st = p.get("metadata") or {}, p.get("status") or {}
            ns, pod = md.get("namespace"), md.get("name")
            pods_by_ns[ns].append(p)
            wid = f"{ns}/{_wl_name(p)}"
            if st.get("phase") not in ("Succeeded", "Failed"):
                pods_by_node[(p.get("spec") or {}).get("nodeName")] += 1
            podmap[wid].append((ns, pod))
        indexes = {k: _index(values.get(k) or [], ("namespace", "pod")) for k in ("pod_cpu", "pod_mem", "pod_restarts")}
        for inc in incidents:
            latest.setdefault((inc.get("namespace"), inc.get("workload")), inc)
    app_metrics = ("req_rate", "err_rate") if atlas else ("req_rate", "err_rate", "p95_ms")
    apps = {k: _index(values.get(k) or [], ("namespace", "app")) for k in app_metrics}
    request_times = _index(values.get("req_rate") or [], ("namespace", "app"), field=0)
    workloads = []
    for n in sorted(tnodes.values(), key=lambda x: x["id"]):
        if n["kind"] not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"):
            continue
        wid, pair = n["id"], (n.get("namespace"), n.get("name"))
        environment = cfg.environment(pair[0])
        if atlas and (n["kind"] in ("Job", "Pod") or not _atlas_environment(environment, pair[0])):
            continue
        metric = {k: series.get(pair) for k, series in apps.items()}
        if not atlas:
            metric = {"cpu": _sum_seen(indexes["pod_cpu"].get(p) for p in podmap[wid]),
                      "mem": _sum_seen(indexes["pod_mem"].get(p) for p in podmap[wid]),
                      "restarts_1h": _sum_seen(indexes["pod_restarts"].get(p) for p in podmap[wid]), **metric}
        w = {"metric_at": request_times.get(pair) if metric["req_rate"] is not None else None, "id": wid, "namespace": n.get("namespace"), "name": n["name"], "kind": n["kind"],
             "system": n.get("system"), "environment": environment,
             "ready": n.get("ready"), "desired": n.get("desired", n.get("pods")), **metric}
        workloads.append(w)
        if not atlas:
            inc = latest.get(pair)
            w["incident"] = {"id": inc["id"], "level": inc.get("severity_level"), "status": inc["status"],
                             "stage": inc.get("stage"), "system": inc.get("system"),
                             "environment": cfg.environment(inc.get("namespace"))} if inc else None
            n.update(metric)
            if inc and not n.get("warnings"):
                n["warnings"] = [{"incident_id": inc["id"], "level": inc.get("severity_level"), "status": inc["status"], "title": inc.get("title")}]
    labels = ("source_workload_namespace", "source_workload", "destination_workload_namespace", "destination_workload")
    er, ee = _index(values.get("edge_req") or [], labels), _index(values.get("edge_err") or [], labels)
    edge_times = _index(values.get("edge_req") or [], labels, field=0)
    observed = {(e["from"], e["to"]): e for e in topo["observed_edges"]}
    pair_nodes = {(n.get("namespace"), n.get("name")): n["id"] for n in tnodes.values() if n["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Pod")}
    for pair, req in er.items():
        src, dst = pair_nodes.get(pair[:2]), pair_nodes.get(pair[2:])
        if not src or not dst or src == dst or req is None:
            continue
        e = observed.setdefault((src, dst), {"from": src, "to": dst, "type": "observed_network", "flows": 0, "ok": 0, "dropped": 0, "errors": 0,
                                            "last_seen": None, "ports": {}, "drop_reasons": {}, "via_service": {},
                                            "state": "unknown"})
        e.update(req_rate=req, err_rate=ee.get(pair), metric_at=edge_times.get(pair))
        if (ee.get(pair) or 0) > 0:
            e["state"] = "degraded"
    topo["nodes"] = sorted(tnodes.values(), key=lambda n: n["id"])
    topo["observed_edges"] = list(observed.values())
    kube_errors = list(inventory.get("errors") or []) + [f"{k}:{v}" for k, v in errors.items() if k != "prometheus"]
    sources = {"prometheus": f"error: {errors['prometheus']}" if errors.get("prometheus") else "ok",
               "kubernetes": "error: " + "; ".join(kube_errors) if kube_errors else "ok",
               "hubble": freshness.get("stream_state") or freshness.get("state") or "never_connected",
               "envoy": "ok" if er else "미관측"}
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "workloads": workloads,
              "incidents": {"open": incidents}, "topology": topo, "sources": sources}
    if atlas:
        return atlas_view(result)
    all_ns = set(pods_by_ns) | {n.get("namespace") for n in tnodes.values() if n.get("namespace")} | {
        (n.get("metadata") or {}).get("name") for n in inventory.get("namespaces", [])}
    workloads_by_ns = defaultdict(list)
    for w in workloads:
        workloads_by_ns[w["namespace"]].append(w)
    ns_rows = []
    for ns in sorted(n for n in all_ns if n):
        ps, ws = pods_by_ns[ns], workloads_by_ns[ns]
        ns_rows.append({"name": ns,
                        **{f"pods_{phase.lower()}": sum((p.get("status") or {}).get("phase") == phase for p in ps)
                           for phase in ("Running", "Pending", "Failed")},
                        "restarts_1h": _sum_seen(w["restarts_1h"] for w in ws),
                        "cpu_cores": _sum_seen(w["cpu"] for w in ws), "mem_bytes": _sum_seen(w["mem"] for w in ws),
                        "workloads": len(ws), "workloads_unhealthy": sum(tnodes[w["id"]].get("status") in ("down", "degraded", "not_ready") for w in ws)})
    node_cpu = _index(values.get("node_cpu") or [], ("node",))
    node_mem = _index(values.get("node_mem") or [], ("node",))
    nodes = []
    for n in inventory.get("nodes", []):
        name, st = (n.get("metadata") or {}).get("name"), n.get("status") or {}
        conditions = st.get("conditions") or []
        ready = next((c.get("status") == "True" for c in conditions if c.get("type") == "Ready"), None)
        capacity = st.get("capacity") or {}
        nodes.append({"name": name, "ready": ready, "cpu_cores": _quantity(capacity.get("cpu")),
                      "mem_bytes": _quantity(capacity.get("memory")), "cpu_used_cores": node_cpu.get((name,)),
                      "mem_used_bytes": node_mem.get((name,)), "pods": pods_by_node[name]})
    environments = []
    for order, ns in enumerate(cfg.target_namespaces, 1):
        missing = ns not in all_ns
        ps = pods_by_ns[ns]
        environments.append({"name": ns, "order": order, "missing": missing,
            "workloads": None if missing else len(workloads_by_ns[ns]),
            "pods_ready": None if missing else sum((p.get("status") or {}).get("phase") == "Running" and bool((p.get("status") or {}).get("containerStatuses")) and all(c.get("ready") for c in p["status"]["containerStatuses"]) for p in ps),
            "pods_total": None if missing else len(ps),
            "open_incidents": sum(i.get("namespace") == ns for i in incidents)})
    current_events = (_event(e) for e in events or [] if (e.get("metadata") or {}).get("namespace") not in historical)
    result["cluster"] = {"nodes": nodes, "namespaces": ns_rows, "environments": environments}
    result["events"] = sorted(current_events, key=lambda e: e["at"] or "", reverse=True)[:80]
    result["incidents"]["today"] = today or {"total": 0, "resolved": 0, "handed_off": 0, "failed": 0}
    return result
