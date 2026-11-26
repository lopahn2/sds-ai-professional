"""Deeper read-only evidence and the restart/unknown-log recovery guards (compound incident gaps)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import evidence_depth as D
from app.evidence import Bundle, compact_summary
from app.observed_recovery import recovery_proof

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)


def pod(name="shop-stock-1", cpu_limit=None, annotation=None, labels=None):
    limits = {"cpu": cpu_limit} if cpu_limit else {}
    md = {"name": name, "labels": labels or {"app": "shop-stock"}}
    if annotation:
        md["annotations"] = {"kubernetes.io/limit-ranger": annotation}
    return {"metadata": md, "spec": {"containers": [{"name": "app", "resources": {"limits": limits, "requests": {}}}]},
            "status": {"phase": "Running", "containerStatuses": [{"name": "app", "ready": True, "restartCount": 0}]}}


def test_pod_runtime_reports_effective_limits_and_admission_note():
    view = D.pod_runtime(pod(cpu_limit="250m", annotation="LimitRanger plugin set: cpu limit for container app"))
    assert view["resources"] == {"app": {"limits": {"cpu": "250m"}, "requests": {}}}
    assert view["admission_defaults"].startswith("LimitRanger plugin set")
    assert "admission_defaults" not in D.pod_runtime(pod())


def test_probe_view_keeps_http_target_and_hides_exec_arguments():
    template = {"spec": {"containers": [{"name": "app",
        "livenessProbe": {"httpGet": {"path": "/actuator/health", "port": "http"}, "timeoutSeconds": 1, "failureThreshold": 3},
        "readinessProbe": {"exec": {"command": ["sh", "-c", "PGPASSWORD=secret pg_isready"]}}}]}}
    view = D.probe_view(template)["app"]
    assert view["liveness"] == {"timeoutSeconds": 1, "failureThreshold": 3,
                                "http_get": {"path": "/actuator/health", "port": "http"}}
    assert view["readiness"] == {"exec": "command not shown"}
    assert "secret" not in repr(view)


def test_cpu_quantities():
    assert D.cpu_cores("250m") == 0.25 and D.cpu_cores("2") == 2.0 and D.cpu_cores(None) is None
    assert D.cpu_cores("garbage") is None


def test_resource_findings_flag_binding_limit_from_admission_default():
    pods = [{"name": "shop-stock-1", **D.pod_runtime(pod(cpu_limit="250m", annotation="LimitRanger plugin set: cpu limit"))}]
    metrics = {"pod_cpu_usage_cores": {"shop-stock-1": 0.24}, "pod_cpu_throttled_ratio": {"shop-stock-1": 0.62}}
    deployment = {"resources": {"app": {"limits": {}, "requests": {}}}}
    ranges = [{"name": "team-defaults", "limits": [{"type": "Container", "default": {"cpu": "250m"}}]}]
    out = D.resource_findings(pods, metrics, deployment, ranges)
    assert out[0]["kind"] == "cpu_limit_binding" and out[0]["utilization_of_limit"] == 0.96
    assert out[0]["limit_in_deployment_template"] is False and out[0]["throttled_period_ratio"] == 0.62
    assert out[1] == {"kind": "namespace_default_limits", "limit_ranges": ranges}


def test_resource_findings_stay_quiet_for_declared_unsaturated_limits():
    pods = [{"name": "p", **D.pod_runtime(pod(name="p", cpu_limit="2"))}]
    deployment = {"resources": {"app": {"limits": {"cpu": "2"}, "requests": {}}}}
    assert D.resource_findings(pods, {"pod_cpu_usage_cores": {"p": 0.3}}, deployment, []) == []
    unbounded = [{"name": "p", **D.pod_runtime(pod(name="p"))}]
    assert D.resource_findings(unbounded, {"pod_cpu_usage_cores": {"p": 3.0}}, {}, []) == []


def containers(restarts, ended, reason="Error"):
    return [{"name": "app", "restart_count": restarts, "last_terminated_at": ended.isoformat(),
             "last_terminated_reason": reason, "last_exit_code": 143}]


def liveness_event(at, pod_name="shop-stock-1", count=3):
    return {"type": "Warning", "reason": "Unhealthy", "object": f"Pod/{pod_name}", "count": count,
            "message": "Liveness probe failed: Get \"http://10.0.0.1:8080/actuator/health\": context deadline exceeded",
            "last": at.isoformat()}


def test_probe_driven_restart_is_masking_but_plain_history_is_not():
    pods = [{"name": "shop-stock-1", "containers": containers(2, NOW - timedelta(minutes=3))}]
    masked = D.probe_restart_masking(pods, [liveness_event(NOW - timedelta(minutes=4))], NOW)
    assert masked and masked[0]["liveness_failures"] == 3
    assert D.probe_restart_masking(pods, [], NOW) == []                                     # restart, no probe kills
    assert D.probe_restart_masking(pods, [liveness_event(NOW - timedelta(hours=1))], NOW) == []   # old probe kills
    old = [{"name": "shop-stock-1", "containers": containers(2, NOW - timedelta(hours=2))}]
    assert D.probe_restart_masking(old, [liveness_event(NOW - timedelta(minutes=4))], NOW) == []
    other = [liveness_event(NOW - timedelta(minutes=4), pod_name="shop-order-1")]
    assert D.probe_restart_masking(pods, other, NOW) == []


def test_pool_trend_separates_climb_from_steady_use():
    climb = D.pool_trend([(i * 30.0, float(min(4, i // 2))) for i in range(12)])
    assert climb["never_released"] and climb["max"] == 4 and climb["step_falls"] == 0
    sawtooth = D.pool_trend([(i * 30.0, v) for i, v in enumerate([0, 1, 2, 3, 4, 4, 0, 1, 2, 3, 4, 4])])
    assert sawtooth["resets_to_minimum"] == 1 and not sawtooth["never_released"]
    assert D.pool_trend([(0, 1.0), (30, 1.0)]) is None


def test_error_lines_group_and_strip_kubelet_and_app_timestamps():
    text = "\n".join([
        "2026-11-26T10:00:00.1Z 2026-11-26T10:00:00.100Z ERROR reservation failed: pool timeout",
        "2026-11-26T10:00:01.1Z 2026-11-26T10:00:01.100Z ERROR reservation failed: pool timeout",
        "2026-11-26T10:00:02.1Z 2026-11-26T10:00:02.100Z INFO request served",
        "2026-11-26T10:00:03.1Z FATAL:  no pg_hba.conf entry for host \"10.42.0.9\", user \"app\"",
    ])
    lines = D.error_lines(text, "db", 5)
    assert lines[0] == '[db] FATAL:  no pg_hba.conf entry for host "10.42.0.9", user "app"'
    assert lines[1] == "[db] ERROR reservation failed: pool timeout (repeated 2 times)"
    assert len(lines) == 2


class FakeCore:
    def __init__(self, configmaps=None, logs=None):
        self.configmaps, self.logs, self.reads = configmaps or {}, logs or {}, []

    def read_namespaced_config_map(self, name, ns, **kw):
        self.reads.append(("cm", ns, name))
        return {"metadata": {"name": name}, "data": self.configmaps[name]}

    def read_namespaced_pod_log(self, name, ns, **kw):
        self.reads.append(("log", ns, name, kw.get("previous")))
        return self.logs.get((name, bool(kw.get("previous"))), "")


class FakeKube:
    def __init__(self, core, endpoints=None):
        self.core, self._endpoints = core, endpoints or {}

    def to_dict(self, obj):
        return obj

    def get_endpoints(self, ns, name):
        return self._endpoints.get(name)


def test_dependency_names_scan_env_and_configmaps_without_returning_values():
    core = FakeCore(configmaps={"stock-config": {"DB_URL": "jdbc:postgresql://shop-db.dev.svc.cluster.local:5432/shop",
                                                 "LOG": "INFO"}})
    template = {"spec": {"containers": [{"name": "app",
        "env": [{"name": "CACHE", "value": "redis://cache:6379"}, {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "s"}}},
                {"name": "REMOTE", "value": "http://payments.prd.svc:80"}],
        "envFrom": [{"configMapRef": {"name": "stock-config"}}]}]}}
    names = D.dependency_names(FakeKube(core), "dev", template, {"shop-db", "cache", "payments", "shop-stock"}, "shop-stock")
    assert names == ["cache", "shop-db"] or names == ["shop-db", "cache"]
    assert "payments" not in names   # a Service in another namespace is not this one
    assert all(r[0] == "cm" for r in core.reads)


def test_dependency_view_reports_endpoints_labels_and_backing_logs():
    db0 = pod("db-0", labels={"app": "db", "role": "primary"})
    db1 = pod("db-1", labels={"app": "db", "role": "primary"})
    core = FakeCore(logs={("db-0", False): "2026-11-26T10:00:00Z ERROR:  cannot execute UPDATE in a read-only transaction\n",
                          ("db-1", False): "2026-11-26T10:00:00Z FATAL:  no pg_hba.conf entry for host \"10.1.1.1\"\n"})
    endpoints = {"db": {"subsets": [{"addresses": [{"targetRef": {"kind": "Pod", "name": "db-0"}},
                                                   {"targetRef": {"kind": "Pod", "name": "db-1"}}]}]}}
    svc = {"spec": {"selector": {"app": "db", "role": "primary"}, "ports": [{"port": 5432, "targetPort": 5432}]}}
    view = D.dependency_view(FakeKube(core, endpoints), "dev", "db", svc, [db0, db1], NOW - timedelta(minutes=5), None)
    assert view["ready_endpoints"] == 2 and view["selector_matches"] == ["db-0", "db-1"]
    assert [p["labels"]["role"] for p in view["pods"]] == ["primary", "primary"]
    assert "read-only transaction" in view["pods"][0]["error_logs"][0]
    assert "pg_hba.conf" in view["pods"][1]["error_logs"][0]


def test_collect_depth_skips_clients_without_core_api():
    b = D.collect_depth(SimpleNamespace(), SimpleNamespace(dependencies={}), None, "dev", "shop-stock", None, 10**12)
    assert b.items == [] and b.dependencies == []


def healthy_pair(signals_restart=False):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=2)
    b = Bundle("dev", "shop-stock")
    b.deployment = {"replicas": 1, "ready_replicas": 1, "available_replicas": 1, "unavailable_replicas": 0,
                    "images": ["img"], "revisions": [], "current_revision": 1}
    ended = stamp - timedelta(minutes=2)
    b.pods = [{"name": "shop-stock-1", "uid": "u1", "containers": [{"name": "app", "ready": True, "restart_count": 2,
               "waiting_reason": None, "last_terminated_at": ended.isoformat(), "last_terminated_reason": "Error",
               "last_exit_code": 143}]}]
    b.service = {"ready_endpoints": 1, "matching_pods": ["shop-stock-1"], "selector": {"app": "shop-stock"}}
    b.metrics = {"mean_latency_ms_1m": {"shop-stock": 10}}
    for source, payload in [("deployment", b.deployment), ("pod_status", b.pods), ("service", b.service),
                            ("endpoints", {"ready_addresses": 1}), ("k8s_event", []), ("prometheus", b.metrics)]:
        b.add(source, "dev/Deployment/shop-stock", deepcopy(payload), collected_at=stamp)
    second = deepcopy(b)
    for it in second.items:
        it.collected_at += timedelta(seconds=1)
    return b, second


def test_recovery_refused_right_after_liveness_restart():
    first, second = healthy_pair()
    assert recovery_proof(first, second, ["TargetHighLatency"])          # control: history alone does not block
    second.events = [liveness_event(datetime.now(timezone.utc) - timedelta(minutes=3))]
    assert recovery_proof(first, second, ["TargetHighLatency"]) is None


def test_recovery_refused_when_logs_could_not_be_read():
    first, second = healthy_pair()
    second.add("loki", "dev/shop-stock", error="ReadTimeout: timed out")
    assert recovery_proof(first, second, ["TargetHighLatency"]) is None
    second.add("pod_log", "dev/pods(app=shop-stock)", {"lines": []})
    assert recovery_proof(first, second, ["TargetHighLatency"])


def test_compact_summary_carries_findings_and_dependencies():
    b = Bundle("dev", "shop-stock")
    b.findings = [{"kind": "cpu_limit_binding", "pod": "p"}]
    b.dependencies = [{"service": "shop-db", "ready_endpoints": 2}]
    s = compact_summary(b)
    assert s["E10_findings"] == b.findings and s["E11_dependencies"] == b.dependencies


def test_dependency_logs_come_from_the_log_store_when_available(monkeypatch):
    seen = {}

    def fake_loki(settings, ns, pods, since, deadline, keep=D.DEPENDENCY_LOG_KEEP):
        seen["pods"] = pods
        return {"db-1": ['[db-1] FATAL:  no pg_hba.conf entry for host "10.1.1.1"']}

    monkeypatch.setattr(D, "loki_pod_errors", fake_loki)
    db0, db1 = pod("db-0", labels={"app": "db"}), pod("db-1", labels={"app": "db"})
    core = FakeCore()
    svc = {"spec": {"selector": {"app": "db"}}}
    view = D.dependency_view(FakeKube(core, {}), "dev", "db", svc, [db0, db1], None, None, settings=object())
    assert seen["pods"] == ["db-0", "db-1"] and view["log_source"] == "loki"
    assert view["pods"][0]["error_logs"] == [] and "pg_hba" in view["pods"][1]["error_logs"][0]
    assert not [r for r in core.reads if r[0] == "log"]   # no kubelet reads once the store answered


def test_retry_amplification_compares_inbound_with_caller_requests():
    load = {"workload": "stock", "callers": ["order"],
            "rates": {"stock": {"requests_per_s": 38.0, "errors_5xx_per_s": 37.5},
                      "order": {"requests_per_s": 10.0, "errors_5xx_per_s": 9.9}}}
    f = D.retry_findings(load)[0]
    assert f["kind"] == "caller_retry_amplification" and f["requests_per_caller_request"] == 3.8
    healthy = {**load, "rates": {"stock": {"requests_per_s": 10.2}, "order": {"requests_per_s": 10.0}}}
    assert D.retry_findings(healthy) == [] and D.retry_findings(None) == []
    from app import resolution_memory as M
    assert "caller_retry_amplification" in M.signature([f], [], [])


def test_caller_names_find_deployments_that_name_the_service():
    core = FakeCore(configmaps={"order-cfg": {"STOCK_URL": "http://shop-stock:8080"}})

    class Kube(FakeKube):
        def list_deployments(self, ns):
            def dep(name, env=None, env_from=None):
                c = {"name": name, "env": env or [], "envFrom": env_from or []}
                return {"metadata": {"name": name}, "spec": {"template": {"spec": {"containers": [c]}}}}
            return [dep("shop-stock", env=[{"name": "SELF", "value": "http://shop-stock"}]),
                    dep("shop-order", env_from=[{"configMapRef": {"name": "order-cfg"}}]),
                    dep("billing", env=[{"name": "DB", "value": "jdbc:postgresql://billing-db/x"}])]
    cache = {}
    assert D.caller_names(Kube(core), "dev", "shop-stock", {"shop-stock"}, None, cache) == ["shop-order"]
    assert cache == {"order-cfg": {"STOCK_URL": "http://shop-stock:8080"}}


def test_loki_pod_query_uses_plain_names(monkeypatch):
    import httpx
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"result": [{"stream": {"pod": "shop-db-node-1"},
                                         "values": [["1", 'FATAL:  no pg_hba.conf entry for host "10.0.0.9"']]}]}}

    def get(url, timeout, params):
        seen.update(params)
        return R()
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr("app.observation_sources.adapter_url", lambda settings, name: "http://loki")
    out = D.loki_pod_errors(object(), "dev", ["shop-db-node-0", "shop-db-node-1"], None, None)
    assert '\\' not in seen["query"] and 'pod=~"shop-db-node-0|shop-db-node-1"' in seen["query"]
    assert "pg_hba" in out["shop-db-node-1"][0]


def test_dependency_view_lists_unselected_siblings_with_their_logs():
    def db(name, role):
        p = pod(name, labels={"app": "db", "role": role})
        p["metadata"]["ownerReferences"] = [{"kind": "StatefulSet", "name": "db", "controller": True}]
        return p
    core = FakeCore(logs={("db-0", False): "2026-11-26T10:00:00Z ERROR:  cannot execute UPDATE in a read-only transaction\n"})
    endpoints = {"db": {"subsets": [{"addresses": [{"targetRef": {"kind": "Pod", "name": "db-1"}}]}]}}
    svc = {"spec": {"selector": {"app": "db", "role": "primary"}}}
    view = D.dependency_view(FakeKube(core, endpoints), "dev", "db", svc, [db("db-0", "replica"), db("db-1", "primary")],
                             None, None)
    rows = {r["name"]: r for r in view["pods"]}
    assert rows["db-1"]["selected_by_service"] is True and rows["db-0"]["selected_by_service"] is False
    assert "read-only" in rows["db-0"]["error_logs"][0]
    from app import resolution_memory as M
    assert "dependency_unselected_errors" in M.signature([], [view], [])
