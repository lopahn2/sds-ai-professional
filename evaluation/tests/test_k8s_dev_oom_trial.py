from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from evaluation import k8s_dev_oom_trial as trial
from evaluation import oom_isolated_images as images
from evaluation import oom_isolated_preflight as isolated
from evaluation import oom_isolated_receipts as receipts
from evaluation.comparison_summary import dev_oom_record
from evaluation.dev_oom_case import (
    ALERT, ALERT_FRESHNESS_SECONDS, CASES, MIB, PROTOCOL, RECOVERY_OBSERVER, first_error, load_case, oom_sample,
    operation_window,
)
from evaluation.live_repair import TASK


START = datetime(2026, 11, 24, 2, 0, 0, 100_000, tzinfo=timezone.utc)
OP = "01000000-0000-0000-0000-000000000002"


def test_qualification_reports_content_outside_stream_budget_without_streaming(monkeypatch, capsys):
    shared = {"digest": "sha256:" + "1" * 64, "size": 8}
    extra = {"digest": "sha256:" + "2" * 64, "size": 16}
    metadata = {}

    def register(payload):
        raw = json.dumps(payload).encode()
        digest = sha256(raw).hexdigest()
        metadata[digest] = raw
        return {"digest": "sha256:" + digest, "size": len(raw)}

    config = register(images.ARCH)
    fixture = register({"schemaVersion": 2, "mediaType": "manifest", "config": config, "layers": [shared]})
    receiver = register({"schemaVersion": 2, "mediaType": "manifest", "config": config, "layers": [shared, extra]})
    monkeypatch.setattr(images, "IMAGES", {"fixture": fixture["digest"][7:]})
    monkeypatch.setattr(images, "RECEIVER_IMAGES", {"receiver": receiver["digest"][7:]})
    monkeypatch.setattr(images, "MAX_BLOB", 10)
    monkeypatch.setattr(images, "read_metadata", lambda digest, _size: metadata[digest[7:]])
    monkeypatch.setattr(images, "stream_layout", lambda *_args: pytest.fail("qualification streamed image layers"))
    monkeypatch.setattr(sys, "argv", ["oom_isolated_images", "--describe-qualification"])

    images.main()
    captured = capsys.readouterr()
    report = json.loads(captured.err)
    assert not captured.out
    assert report["additional_bytes"] == extra["size"] + receiver["size"]
    assert report["fixture_and_incident_receiver"]["linux_layers"] == 2
    assert report["largest_layer_bytes"] == 16
    assert report["within_stream_limits"] is False
    assert images.layout()[3] == report["fixture"]
    monkeypatch.setattr(images, "IMAGES", {"fixture": fixture["digest"][7:], "other": receiver["digest"][7:]})
    monkeypatch.setattr(sys, "argv", ["oom_isolated_images", "--describe", "--image", "fixture"])
    images.main()
    assert json.loads(capsys.readouterr().err) == report["fixture"]
    monkeypatch.setattr(images, "IMAGES", images.RECEIVER_IMAGES)
    with pytest.raises(ValueError, match="layer exceeds"):
        images.layout()


@pytest.mark.parametrize("architecture", ["arm64", "amd64"])
def test_isolated_preflight_requires_recorded_node_architecture(monkeypatch, architecture):
    node = {"metadata": {"name": isolated.NODE}, "status": {"nodeInfo": {
        "architecture": architecture, "kubeletVersion": isolated.KUBERNETES,
    }}}
    responses = {
        "namespace": {"metadata": {"labels": {"liveprobe.io/evaluation": "native-oom-isolated"}}},
        "--raw=/version": {"gitVersion": isolated.KUBERNETES, "platform": f"linux/{architecture}"},
        "nodes": {"items": [node]},
    }
    monkeypatch.setattr(isolated.subprocess, "check_output", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(isolated, "kube", lambda _get, resource, *_args: json.dumps(responses[resource]))
    if architecture == "arm64":
        assert isolated.isolated_node() == node
    else:
        with pytest.raises(RuntimeError, match="cluster identity"):
            isolated.isolated_node()


def test_isolated_preflight_rejects_headroom_lost_during_verification(monkeypatch, capsys):
    checks = []

    class NodeGateStop(RuntimeError):
        breaches = ["memory_headroom"]
        snapshot = {"mem_mi": 1700, "alloc_mi": 3600}

    def gate():
        checks.append("gate")
        if checks == ["gate"]:
            return {"mem_mi": 1600}
        raise NodeGateStop()

    def business(scope):
        assert scope == "all"
        checks.append("business")
        return {"order_lookup": 200}

    monkeypatch.setattr(isolated, "isolated_node", dict)
    monkeypatch.setattr(isolated, "gate_source", lambda: SimpleNamespace(node_gate=gate, NodeGateStop=NodeGateStop))
    monkeypatch.setattr(isolated, "verified_images", dict)
    monkeypatch.setattr(isolated, "workload", lambda *_: {})
    monkeypatch.setattr(isolated, "business_checks", business)
    with pytest.raises(RuntimeError, match="memory_headroom.*1700.*3600"):
        isolated.main()
    assert checks == ["gate", "business", "gate"]
    assert not capsys.readouterr().out


def test_isolated_preflight_checks_running_image_against_verified_source(monkeypatch, tmp_path):
    monkeypatch.setattr(isolated, "ROOT", tmp_path)
    path = tmp_path / "verified-oom-images.json"
    receipts = {name: {"source_digest": "sha256:" + "1" * 64,
                       "linux_arm64_manifest": "sha256:" + "2" * 64,
                       "configuration_digest": "sha256:" + "3" * 64}
                for name in isolated.SOURCE_IMAGES.values()}
    path.write_text(json.dumps({"images": receipts}))
    with pytest.raises(RuntimeError, match="completed verified OCI stream"):
        isolated.verified_images()
    path.write_text(json.dumps({"images": receipts, "streamed_and_verified": True}))
    images = isolated.verified_images()
    isolated.require_image("order", "docker-pullable://docker.io/liveprobe/order@sha256:" + "2" * 64, images)
    isolated.require_image("verification-order", "sha256:" + "3" * 64, images)
    with pytest.raises(RuntimeError, match="differs from the verified source"):
        isolated.require_image("inventory", "sha256:" + "4" * 64, images)


def test_isolated_business_preflight_rejects_wrong_dependency_payload(monkeypatch):
    stock = {"item": "a", "quantity": 42, "available": True}
    order = {"id": 1}
    dependency = {"item": "a", "orderable": True, "inventory": deepcopy(stock)}

    def kube(_get, _raw, path):
        if path.endswith("/api/stock/a"):
            return json.dumps(stock)
        return json.dumps(order if path.endswith("/api/orders/1") else dependency)

    monkeypatch.setattr(isolated, "kube", kube)
    assert all(value == 200 for value in isolated.business_checks().values())
    dependency["inventory"]["quantity"] = 41
    with pytest.raises(RuntimeError, match="same approved fixture"):
        isolated.business_checks()


@pytest.mark.parametrize(("scope", "services"), [
    ("inventory", [isolated.INVENTORY_FIXTURE, isolated.INVENTORY_CALLER]),
    ("order", [isolated.INVENTORY_FIXTURE, isolated.ORDER_FIXTURE, isolated.ORDER_FIXTURE]),
])
def test_scoped_business_preflight_reads_the_target_and_its_real_dependency(monkeypatch, scope, services):
    calls = []
    stock = {"item": "a", "quantity": 42, "available": True}

    def kube(_get, _raw, path):
        calls.append(path.split("/services/", 1)[1].split(":", 1)[0])
        if path.endswith("/api/stock/a"):
            return json.dumps(stock)
        if path.endswith("/api/orders/1"):
            return json.dumps({"id": 1})
        return json.dumps({"item": "a", "orderable": True, "inventory": stock})

    monkeypatch.setattr(isolated, "kube", kube)
    assert isolated.business_checks(scope) == dict.fromkeys(isolated.business_requests(scope), 200)
    assert calls == services


@pytest.mark.parametrize("problem", [None, "external_volume", "wrong_image", "nonserving_revision"])
def test_receiver_pod_pins_runtime_image_and_private_storage(monkeypatch, problem):
    digest = "sha256:" + "a" * 64
    dep = {"metadata": {"generation": 1}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "postgres"}}},
           "status": {"observedGeneration": 1, "replicas": 1, "readyReplicas": 1,
                      "currentRevision": "current", "updateRevision": "current"}}
    pod = {"metadata": {"name": "postgres-0", "uid": "new-pod"},
           "spec": {"nodeName": isolated.NODE, "volumes": [{"emptyDir": {}}]},
           "status": {"containerStatuses": [{"name": "postgres", "imageID": digest, "ready": True,
                                             "state": {"running": {"startedAt": "time"}}}]}}
    if problem == "external_volume":
        pod["spec"]["volumes"] = [{"persistentVolumeClaim": {"claimName": "known"}}]
    elif problem == "wrong_image":
        pod["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "b" * 64
    elif problem == "nonserving_revision":
        dep["status"]["updateRevision"] = "soon"
    monkeypatch.setattr(isolated, "kube", lambda _verb, kind, *args, **kwargs: json.dumps(
        dep if kind == "statefulset" else {"items": [pod]}))
    if problem:
        with pytest.raises(RuntimeError):
            isolated.receiver_pod("statefulset", isolated.SYSTEM, "postgres", "postgres", {"postgres": {digest}})
    else:
        assert isolated.receiver_pod("statefulset", isolated.SYSTEM, "postgres", "postgres", {"postgres": {digest}})["uid"] == "new-pod"


@pytest.mark.parametrize("problem", [None, "old_rule", "firing", "missing_scrape", "wrong_alertmanager"])
def test_isolated_oom_metrics_check_rejects_ambiguous_or_stale_monitoring(monkeypatch, problem):
    rule = {"name": isolated.OOM_ALERT, "health": "ok", "duration": 0, "query": isolated.OOM_RULE, "alerts": []}
    target = f"alertmanager.{isolated.OBSERVABILITY}.svc"
    samples = [{"metric": {"job": "kube-state-metrics"}, "value": [10, "1"]}]
    if problem == "old_rule":
        rule["query"] = isolated.OOM_REASON
    elif problem == "firing":
        rule["alerts"] = [{"state": "firing"}]
    elif problem == "missing_scrape":
        samples[0]["value"] = [10, "0"]
    elif problem == "wrong_alertmanager":
        target = "another-service.invalid"
    requests = []

    def get(_namespace, service, _port, path):
        requests.append((service, path))
        if path == "/-/ready":
            return "OK"
        if path.endswith("rules"):
            data = {"groups": [{"name": "p18-environments", "rules": [rule]}]}
        elif path.endswith("alertmanagers"):
            data = {"activeAlertmanagers": [{"url": f"http://{target}:9093/api/v2/alerts"}]}
        else:
            from urllib.parse import unquote_plus

            assert 'timestamp(up{job="kube-state-metrics"}) > time() - 45' in unquote_plus(path)
            data = {"result": samples}
        return json.dumps({"status": "success", "data": data})

    monkeypatch.setattr(isolated, "receiver_get", get)
    if problem:
        with pytest.raises(RuntimeError):
            isolated.verify_oom_metrics()
    else:
        isolated.verify_oom_metrics()
        assert requests[-1] == ("alertmanager", "/-/ready")


@pytest.mark.parametrize("problem", [None, "read_only", "prd", "degraded_watch", "empty_retention", "wrong_retention_file"])
@pytest.mark.parametrize("cached", [False, True])
def test_receiver_qualification_pins_dev_and_keeps_database_probes_read_only(monkeypatch, problem, cached):
    database = {"primary": True, "read_only_default": False, "incident_schema": True}
    state = {"ready": True, "target_namespaces": ["dev"], "llm": {"provider": "codex_cli"},
             "watchers": {name + "/dev": {"state": "running", "since": "recorded"}
                          for name in ("pod_watch", "event_watch", "endpoint_poller")}}
    if problem == "read_only":
        database["read_only_default"] = True
    elif problem == "prd":
        state["target_namespaces"].append("prd")
    elif problem == "degraded_watch":
        state["watchers"]["pod_watch/dev"]["state"] = "reconnecting"
    statements = []
    retention = "e" * 64 if problem in {"empty_retention", "wrong_retention_file"} else None

    def kube(verb, *args, namespace=isolated.NAMESPACE):
        if verb == "get":
            return json.dumps({"metadata": {"labels": {"liveprobe.io/evaluation": isolated.MARKER}}})
        assert verb == "exec" and namespace == isolated.SYSTEM
        if args[0] == "pod/liveprobe-api":
            if args[-1] == isolated.RETENTION_FILES:
                return ("b" * 64 if problem == "wrong_retention_file" else retention) + "\n"
            assert (verb, *args) == isolated.product_read_args("liveprobe-api", args[-1])
            return json.dumps({"db": True, "background": True} if args[-1] == "/api/readyz" else state)
        assert args[0] == "pod/postgres"
        statements.append(args[-1])
        return json.dumps(database)

    def verified_images(sources, filename):
        if cached:
            assert sources["liveprobe-api"] == isolated.CACHED_RECEIVER_IMAGE
            assert filename == "verified-oom-cached-receiver.json"
        else:
            assert sources["liveprobe-api"] == "docker.io/liveprobe/liveprobe:oom-dev-receiver-compact"
            assert filename == "verified-oom-receiver.json"
        return {}

    monkeypatch.setattr(isolated, "verified_images", verified_images)
    monkeypatch.setattr(isolated, "receiver_pod", lambda _kind, _ns, name, *_: {
        "name": name, "rag_retention_sha256": retention if name == "liveprobe-api" else None})
    monkeypatch.setattr(isolated, "kube", kube)
    monkeypatch.setattr(isolated, "verify_oom_metrics", lambda: None)
    if problem:
        with pytest.raises(RuntimeError):
            isolated.receiver_checks(cached=cached, rag_retention_sha256=retention)
    else:
        report = isolated.receiver_checks(cached=cached)
        assert report["model_round_trip"] == "unverified"
        assert report["watcher_epochs"] == {"event_watch": "recorded", "pod_watch": "recorded", "endpoint_poller": "recorded"}
    if problem == "wrong_retention_file":
        assert not statements
        return
    assert len(statements) == 1 and statements[0].startswith("BEGIN READ ONLY; SELECT")
    assert statements[0].endswith("ROLLBACK;")


@pytest.mark.parametrize("status", [200, 303])
def test_isolated_status_probe_keeps_read_key_in_receiver(monkeypatch, tmp_path, capsys, status):
    import http.client

    key = tmp_path / "read"
    key.write_text("local-isolated-test-key-" + "x" * 32)
    monkeypatch.setenv("LIVEPROBE_LOCAL_READ_KEY_FILE", str(key))
    monkeypatch.setattr(sys, "argv", ["-c", "/api/status"])
    args = isolated.product_read_args("pinned-api", "/api/status")
    assert key.read_text() not in args

    def request(method, path, *, headers):
        assert method == "GET" and path == "/api/status"
        assert headers == {"X-LiveProbe-Local-Read": key.read_text()}

    def connect(host, port, *, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8000, 5)
        return SimpleNamespace(request=request, close=lambda: None,
                               getresponse=lambda: SimpleNamespace(status=status, read=lambda: b'{"ready":true}'))

    monkeypatch.setattr(http.client, "HTTPConnection", connect)
    if status == 200:
        exec(isolated.PRODUCT_PROBE, {})
        assert capsys.readouterr().out == '{"ready":true}'
    else:
        with pytest.raises(SystemExit, match="HTTP 303"):
            exec(isolated.PRODUCT_PROBE, {})
        assert capsys.readouterr().out == ""
    with pytest.raises(ValueError, match="unexpected"):
        isolated.product_read_args("pinned-api", "/api/action")


@pytest.mark.parametrize("failure", [None, "unfinished", "duplicate", "wrong_receiver", "missing"])
def test_full_isolated_imports_require_each_verified_source_in_any_order(tmp_path, failure):
    workload_names = sorted(receipts.WORKLOADS)
    receiver_names = sorted(receipts.RECEIVER)

    def report(name):
        root = receipts.IMAGES.get(name, "d" * 64)
        return {field: "sha256:" + value for field, value in zip(receipts.FIELDS, (root, root, "c" * 64))}

    approved = {"streamed_and_verified": True, "images": {name: report(name) for name in receiver_names}}
    interleaved = [name for pair in zip(workload_names, receiver_names) for name in pair]
    if failure == "missing":
        interleaved.pop()
    if failure == "duplicate":
        interleaved[-1] = interleaved[0]
    for index, name in enumerate(interleaved, 1):
        data = {"streamed_and_verified": failure != "unfinished" or index != 1, "images": {name: report(name)}}
        if name == receipts.COMPACT:
            data = {"streamed_and_verified": True, "compact_name": name,
                    "compact_manifest": {"digest": report(name)["linux_arm64_manifest"]},
                    "compact_config": {"digest": "sha256:" + "0" * 64 if failure == "wrong_receiver"
                                       else report(name)["configuration_digest"]}}
        (tmp_path / f"{index}.json").write_text(json.dumps(data))
    if failure:
        with pytest.raises(ValueError):
            receipts.read_imports(tmp_path, approved)
    else:
        result = receipts.read_imports(tmp_path, approved)
        assert result["streamed_and_verified"] is True
        assert result["images"].keys() == set(interleaved)
        assert result["images"][receipts.COMPACT] == approved["images"][receipts.COMPACT]


def at(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def acknowledgement(case):
    return {"operation_id": OP, "mode": "oom", "effect": "pending", "allocation_cap_mib": 576,
            "accepted_at": at(0), "expires_at": at(case.duration)}


@pytest.mark.parametrize("case_id", CASES)
def test_oom_cases_are_pinned_to_catalog_duration_and_native_limit(case_id):
    case = load_case(case_id)
    accepted = acknowledgement(case)
    assert operation_window(case, accepted) == (START, START + timedelta(seconds=case.duration))
    with pytest.raises(ValueError, match="isolated limit"):
        operation_window(case, {**accepted, "allocation_cap_mib": 640})


def test_generic_137_and_stale_kernel_termination_are_not_native_oom():
    before = {"containerID": "one", "restartCount": 4}
    ordinary = {"containerID": "one", "reason": "Error", "exitCode": 137, "finishedAt": at(1)}
    after = {"containerID": "two", "restartCount": 5, "lastState": {"terminated": ordinary}}
    end = START + timedelta(seconds=6)
    assert not oom_sample(after, before, START, end)["terminations"]
    after["lastState"]["terminated"] = {**ordinary, "reason": "OOMKilled", "finishedAt": at(-2)}
    assert not oom_sample(after, before, START, end)["terminations"]
    after["lastState"]["terminated"]["finishedAt"] = at(1)
    event, = oom_sample(after, before, START, end)["terminations"]
    assert event["container_id"] == "one"
    assert first_error(event) == '{"reason":"OOMKilled","exitCode":137}'


def test_prometheus_matches_fresh_exact_pod_and_container(monkeypatch):
    dev = {"namespace": "dev", "pod": "pinned", "container": "inventory"}
    samples = [{"metric": {**dev, "namespace": "prd"}, "value": [START.timestamp() + 3, "1"]},
               {"metric": dev, "value": [START.timestamp() + 3, str(START.timestamp() - 2)]}]
    monkeypatch.setattr(trial, "metric", lambda *_: samples)
    assert trial.fresh_metric("timestamp", "pinned", "inventory", since=START) is None
    samples[-1]["value"][-1] = str(START.timestamp() + 1)
    assert trial.fresh_metric("timestamp", "pinned", "inventory", since=START)["sample_timestamp"] == START.timestamp() + 1


def test_previous_oom_is_eligible_only_after_matching_native_timestamp_and_quiet_alert(monkeypatch):
    case = load_case("F2-01")
    finished = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=20)
    baseline = {"endpoint_uid": "pinned-uid"}
    before = {"state": {"running": {"startedAt": (finished + timedelta(seconds=55)).isoformat()}},
              "containerID": "original-container", "restartCount": 1, "ready": True,
              "lastState": {"terminated": {"reason": "OOMKilled", "finishedAt": finished.isoformat()}}}
    monkeypatch.setattr(trial.pulse, "preflight", lambda case: (baseline, "pinned-pod", [], None))
    monkeypatch.setattr(trial, "current_fixture", lambda case: {})
    monkeypatch.setattr(trial.pulse, "verify_fixture", lambda *args: None)
    monkeypatch.setattr(trial.restart, "verify_boundary", lambda *args: None)
    monkeypatch.setattr(trial.restart, "pod_snapshot", lambda *args: (before, {}))
    monkeypatch.setattr(trial, "cgroup_boundary", lambda *args: {})
    monkeypatch.setattr(trial.restart, "watcher_epochs", lambda: {})
    monkeypatch.setattr(trial.live.CHAOS, "node_gate", dict)
    checked = []

    def guard(case, pod, observed, *, standalone):
        assert observed == baseline and standalone
        checked.append(pod)
        return "observed rule"

    authority = type("Authority", (), {"check": lambda self: None})()
    monkeypatch.setattr(trial, "signal_preflight", guard)
    monkeypatch.setattr(trial, "fresh_metric", lambda *args: finished.timestamp() - 1)
    with pytest.raises(trial.live.Refused, match="previous native OOM"):
        trial.preflight(case, authority, standalone=True)
    assert not checked

    def fresh_metric(query, pod, container):
        assert 'uid="pinned-uid"' in query
        assert (pod, container) == ("pinned-pod", "inventory")
        return finished.timestamp()

    monkeypatch.setattr(trial, "fresh_metric", fresh_metric)
    *_, gate = trial.preflight(case, authority, standalone=True)
    assert checked == ["pinned-pod"] and gate["rule_expression"] == "observed rule"


def receipt():
    case = load_case("F2-01")
    native = {"reason": "OOMKilled", "exit_code": 137, "container_id": "one", "finished_at": at(2)}
    error = first_error(native)
    probes = [{"endpoint_uid": "pod", "ready": 200, "business": 200, "order_dependency": 200, "contract_ok": True} for _ in range(2)]
    return {"case": case.case_id, "arm": "liveprobe", "target": case.target, "case_sha256": case.case_sha256, "protocol": PROTOCOL,
            "status": "oom_observed", "fixture_sha256": "0" * 64, "kubernetes_lease_released": True,
            "operation": acknowledgement(case), "observations": [{"terminations": [native]}],
            "gate_evidence": {"running_since": at(-12 * 60), "restart_backoff_eligible_at": at(-2 * 60),
                              "cgroups": {"inventory": {"limit_bytes": 512 * MIB}, "db": {"limit_bytes": 64 * MIB}}},
            "healthy_before": {"probes": probes, "container_id": "one", "restarts": 4, "contract_ok": True},
            "restored": {"probes": probes, "container_id": "two", "restarts": 5},
            "fault_before": {"endpoint_uid": "pod", "contract_ok": False, "ready_endpoints": 0, "http_status": 503,
                             "service_error": case.errors[0], "first_termination": native, "first_error_at": at(2),
                             "unavailable_at": at(3), "error": error},
            "input_sha256": sha256((TASK + error).encode()).hexdigest(), "recovered_at": at(11),
            "termination_events": [{"event": "expired", "timestamp": at(8)}], "observation_seconds": 75,
            "signal_samples": {name: {"sample_timestamp": START.timestamp() + 14, "evaluated_at": START.timestamp() + 15}
                               for name in ("kube_state_oom", "firing_oom_alert")},
            "incidents": [{"status": "recovery_review", "signal_reasons": [ALERT], "executed_actions": 0,
                           "first_oom_signal_at": at(18), "calls": [{"estimated_usd": .01, "input_tokens": 50,
                                                                    "output_tokens": 20, "duration_ms": 500}]}],
            "product_terminal_observed_at": at(35)}


def test_oom_receipt_requires_kernel_to_incident_chain_and_restore():
    saved = receipt()
    result = dev_oom_record(saved, "liveprobe")
    assert result["status"] == "verified_natural_recovery" and result["first_error_to_operation_end_ms"] == 6000
    assert 'first_error_to_verified_recovery_ms' not in result
    broken = deepcopy(saved)
    broken["signal_samples"]["firing_oom_alert"]["sample_timestamp"] = START.timestamp() - 1
    assert dev_oom_record(broken, "liveprobe")["status"] == "product_observation_only"
    broken = deepcopy(saved)
    broken["restored"]["probes"][0]["endpoint_uid"] = "other"
    with pytest.raises(ValueError, match="isolated cgroup"):
        dev_oom_record(broken, "liveprobe")
    assert json.loads(result["fault_before"]["error"]) == {"reason": "OOMKilled", "exitCode": 137}


def independent_receipt():
    saved = receipt()
    saved['independent_recovery'] = {
        'observer': RECOVERY_OBSERVER, 'armed_at': at(3.1), 'probe_started_at': at(8.2), 'verified_at': at(9),
        'checks': deepcopy(saved['restored']),
    }
    return saved


def test_oom_verified_workload_time_uses_independent_probe_instead_of_cleanup():
    result = dev_oom_record(independent_receipt(), 'liveprobe')
    assert result['first_error_to_verified_recovery_ms'] == 7000
    assert result['first_error_to_operation_end_ms'] == 6000
    assert result['wall_duration_ms'] == 33000
    legacy = {**receipt(), 'first_error_to_verified_recovery_ms': 1}
    assert 'first_error_to_verified_recovery_ms' not in dev_oom_record(legacy, 'liveprobe')


def test_standalone_oom_timing_requires_the_observer_to_be_armed_before_the_model():
    saved = {**independent_receipt(), 'arm': 'single_sol', 'incidents': [], 'lease_released': True,
             'calls': [{'model': 'gpt-6.2-sol', 'effort': 'high', 'status': 'ok'}],
             'investigation': [{'request': {'tool': 'finish'}, 'result': {'finished': True}}],
             'model_evaluation_duration_ms': 10000, 'standalone_alert_settled_at': at(190),
             'model_evaluation_started_at': at(3.2)}
    assert dev_oom_record(saved, 'single_sol')['first_error_to_verified_recovery_ms'] == 7000
    saved['model_evaluation_started_at'] = at(3)
    with pytest.raises(ValueError, match='invalid independent OOM recovery evidence'):
        dev_oom_record(saved, 'single_sol')


@pytest.mark.parametrize('difference', ['early', 'after_cleanup', 'different_pod', 'dependency_failed', 'more_restarts'])
def test_oom_independent_timing_rejects_evidence_outside_the_same_recovery(difference):
    saved = independent_receipt()
    observation = saved['independent_recovery']
    if difference == 'early':
        observation['probe_started_at'], observation['verified_at'] = at(6), at(7)
    elif difference == 'after_cleanup':
        observation['verified_at'] = at(12)
    elif difference == 'different_pod':
        observation['checks']['probes'][0]['endpoint_uid'] = 'replacement'
    elif difference == 'dependency_failed':
        observation['checks']['probes'][0]['order_dependency'] = 503
    else:
        observation['checks']['restarts'] += 1
    with pytest.raises(ValueError, match='invalid independent OOM recovery evidence'):
        dev_oom_record(saved, 'liveprobe')


def test_independent_oom_observer_can_complete_while_model_is_waiting(monkeypatch):
    first_read, service_recovered, model_finished, stop = (Event() for _ in range(4))
    checks = receipt()['restored']

    def probe(*_):
        first_read.set()
        assert service_recovered.wait(2)
        assert not model_finished.is_set()
        return checks

    monkeypatch.setattr(trial.restart, 'healthy', probe)
    armed = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=1) as pool:
        observer = pool.submit(trial.observe_independent_recovery, None, 'same-pod', {}, {}, stop, armed)
        try:
            assert first_read.wait(2)
            service_recovered.set()
            reading = observer.result(timeout=2)
            assert reading['checks'] == checks and reading['observer'] == RECOVERY_OBSERVER
            assert armed <= datetime.fromisoformat(reading['verified_at']) <= datetime.now(timezone.utc)
        finally:
            model_finished.set()
            stop.set()


def test_oom_receipt_rejects_release_of_model_scope_before_alerts_settle():
    standalone = {**receipt(), "arm": "single_sol", "lease_released": True, "incidents": [], "calls": [{}],
                  "standalone_alert_settled_at": at(6 + ALERT_FRESHNESS_SECONDS + 59)}
    with pytest.raises(ValueError, match="standalone OOM attempt was not isolated"):
        dev_oom_record(standalone, "single_sol")


def deadline_abort_receipt():
    saved = receipt()
    saved["termination_events"] = [{"event": "oom_not_killed", "timestamp": at(6.2)}]
    saved["abort_readback"] = {"allocations": [at(5)], "control_off": True,
                               "intent": {"v": 1, "operation_id": OP, "mode": "oom", "aborted": True,
                                          "created_ms": round(START.timestamp() * 1000),
                                          "armed_until_ms": round((START + timedelta(seconds=6)).timestamp() * 1000)}}
    return saved


def test_oom_receipt_accepts_deadline_abort_only_after_pinned_native_restart():
    saved = deadline_abort_receipt()
    result = dev_oom_record(saved, "liveprobe")
    assert (result["status"], result["first_error_to_operation_end_ms"]) == ("verified_natural_recovery", 4200)
    for field, value in (("allocations", [at(1)]), ("allocations", []), ("control_off", False),
                         ("intent", {**saved["abort_readback"]["intent"], "operation_id": "another"})):
        broken = deepcopy(saved)
        broken["abort_readback"][field] = value
        with pytest.raises(ValueError, match="did not prove reset or expiry"):
            dev_oom_record(broken, "liveprobe")
    saved["termination_events"][0]["timestamp"] = at(5.8)
    with pytest.raises(ValueError, match="did not prove reset or expiry"):
        dev_oom_record(saved, "liveprobe")


def test_oom_standalone_expired_lease_requires_alert_settling_before_its_deadline():
    saved = {**deadline_abort_receipt(), "arm": "single_sol", "incidents": [],
             "calls": [{"model": "gpt-6.2-sol", "effort": "high", "status": "ok"}],
             "investigation": [{"request": {"tool": "finish"}, "result": {"finished": True}}],
             "model_evaluation_duration_ms": 10_000, "standalone_alert_settled_at": at(190),
             "lease_expiry_verified": {"deadline": at(200), "observed_at": at(210), "inactive": True}}
    assert dev_oom_record(saved, "single_sol")["status"] == "verified_natural_recovery"
    saved["lease_expiry_verified"]["deadline"] = at(189)
    with pytest.raises(ValueError, match="standalone OOM attempt was not isolated"):
        dev_oom_record(saved, "single_sol")


def test_last_node_gate_clears_leases_without_sending_a_fault(tmp_path, monkeypatch):
    case = load_case("F2-01")
    baseline = {"endpoint_uid": "pinned"}
    before = {"containerID": "original", "restartCount": 1, "ready": True}
    state = (baseline, "pinned-pod", [], None, before, {}, {})
    events = []
    lease = SimpleNamespace(uid="trial-uid", acquire=lambda: events.append("acquire"),
                            release=lambda: events.append("release"), assert_owned=lambda: None)
    authority = SimpleNamespace(lease=lease, check=lambda *, owned: None)
    monkeypatch.setattr(trial.restart, "pod_snapshot", lambda *args: (before, {}))
    monkeypatch.setattr(trial.pulse, "request", lambda *args: {"mode": "off", "readiness": "ACCEPTING_TRAFFIC"})
    monkeypatch.setattr(trial.service, "checked_deployment", lambda *args: {})

    def stop():
        raise trial.live.CHAOS.NodeGateStop({"cpu_m": 3000}, ["cpu"])

    def unexpected_fault(*_):
        pytest.fail("fault request reached after the stop gate")

    monkeypatch.setattr(trial.live.CHAOS, "node_gate", stop)
    monkeypatch.setattr(trial.pulse, "control_response", unexpected_fault)
    raw, journal = tmp_path / "raw.json", tmp_path / "active.json"
    outcome = trial.execute(case, "liveprobe", authority, state, raw, journal, None)
    assert "node stop gate breached" in outcome["trial_error"]
    assert outcome["node_gate_rejection"] == {"node": {"cpu_m": 3000}, "breaches": ["cpu"]}
    assert events == ["acquire", "release"] and not journal.exists()
    assert json.loads(raw.read_text())["kubernetes_lease_released"] is True


@pytest.mark.parametrize("reply", ["lost", "invalid"])
def test_unconfirmed_oom_request_keeps_lease_journal_and_receipt(tmp_path, monkeypatch, reply):
    case = load_case("F2-07")
    baseline = {"endpoint_uid": "pinned"}
    before = {"containerID": "original", "restartCount": 1, "ready": True}
    state = (baseline, "pinned-pod", [], None, before, {}, {})
    calls = []
    lease = SimpleNamespace(uid="trial-uid", acquire=lambda: calls.append("acquire"),
                            release=lambda: calls.append("release"))
    authority = SimpleNamespace(lease=lease)
    monkeypatch.setattr(trial, "ready_to_write", lambda *args: ({"cpu_m": 900}, 0))

    def post(target, pod, action, body, expected_status):
        calls.append(action)
        assert (target, pod, json.loads(body), expected_status) == (
            case, "pinned-pod", {"mode": "oom", "duration_seconds": case.duration}, "202")
        if reply == "lost":
            raise trial.live.Refused("test-only workload control did not accept the operation")
        return {**acknowledgement(case), "allocation_cap_mib": 640}

    def unexpected(*args):
        pytest.fail("unconfirmed request advanced to post-injection verification")

    monkeypatch.setattr(trial.pulse, "control_response", post)
    monkeypatch.setattr(trial, "observe", unexpected)
    monkeypatch.setattr(trial, "sleep", unexpected)
    monkeypatch.setattr(trial.restart, "healthy", unexpected)
    raw, journal = tmp_path / "raw.json", tmp_path / "active.json"
    outcome = trial.execute(case, "liveprobe", authority, state, raw, journal, None)
    assert calls == ["acquire", "POST"]
    assert outcome["status"] == "trial_unverified"
    assert outcome["cleanup_error"] == "OOM request acceptance could not be verified"
    assert "operation" not in outcome and "kubernetes_lease_released" not in outcome
    assert datetime.fromisoformat(outcome["request_started_at"]).tzinfo is not None
    assert json.loads(raw.read_text()) == outcome
    assert json.loads(journal.read_text())["raw_receipt"] == str(raw)


def test_resource_wait_revalidates_target_before_accepting_fresh_sample(monkeypatch):
    case = load_case("F2-01")
    events = []
    samples = iter([None, "cpu", None, None])

    def node_gate():
        stopped = next(samples)
        events.append("capacity")
        if stopped:
            raise trial.live.CHAOS.NodeGateStop({"cpu_m": 3100}, [stopped])
        return {"cpu_m": 2600}

    monkeypatch.setattr(trial.live.CHAOS, "node_gate", node_gate)
    monkeypatch.setattr(trial, "sleep", lambda _: events.append("wait"))
    before = {"containerID": "original", "restartCount": 1, "ready": True}
    monkeypatch.setattr(trial.restart, "pod_snapshot", lambda *args: (before, {}))
    monkeypatch.setattr(trial.pulse, "request", lambda *args: {"mode": "off", "readiness": "ACCEPTING_TRAFFIC"})
    monkeypatch.setattr(trial.service, "checked_deployment", lambda *args: events.append("workload"))
    lease = SimpleNamespace(assert_owned=lambda: events.append("ownership"))
    authority = SimpleNamespace(lease=lease, check=lambda *, owned: events.append("authority"))
    gate, _ = trial.ready_to_write(case, {"endpoint_uid": "pinned"}, "pod", "original", 1, {}, authority, None, 10)
    assert gate == {"cpu_m": 2600}
    assert events == ["capacity", "authority", "workload", "capacity", "wait",
                      "capacity", "authority", "workload", "capacity", "ownership"]
