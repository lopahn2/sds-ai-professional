"""KAN-39: signal deduplication, ownership and endpoint grace."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import db
from app.config import AppConfig
from app.evidence import deployment_summary
from app.incidents import Signal, fingerprint, ingest
from app.k8s import deployment_for_pod
from app.models import Incident
from app.watcher import Watchers
from conftest import FakeKube, family1_kube, family4_kube, needs_db

NS = "liveprobe-target"


def test_caller_5xx_merges_to_callee_fingerprint():
    cfg = AppConfig(target_namespace="liveprobe-target", )
    assert fingerprint(Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"), cfg) == (
        f"{NS}/inventory", "inventory")


@needs_db
def test_stale_caller_5xx_does_not_open_new_incident(db_url):
    with db.session() as s:
        inc, outcome = ingest(s, AppConfig(target_namespace="liveprobe-target", ), Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"))
        assert inc is None and outcome == "suppressed"


@needs_db
def test_signal_dedup_and_window(db_url):
    cfg = AppConfig(target_namespace="liveprobe-target", )
    now = datetime.now(timezone.utc)
    with db.session() as s:
        a, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff", "pod_watch"), now)
        first = a.id
        assert outcome == "created"
        b, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ErrImagePull", "event_watch"),
                            now + timedelta(seconds=1))
        assert outcome == "merged" and b.id == first and b.signal_count == 2
        assert s.scalar(select(Incident).where(Incident.id == first)).signal_reasons == [
            "ImagePullBackOff", "ErrImagePull"]
        # P15 live run: one OOM fault whose analysis stalled (quota) opened 14 incidents, one per Alertmanager repeat.
        # While the earlier incident is still open, the same reason class keeps merging past the window.
        c, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff", "pod_watch"),
                            now + timedelta(seconds=cfg.merge_window_seconds + 1))
        assert outcome == "merged" and c.id == first
        # A different reason class past the window is a new occurrence.
        d, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "TargetWorkloadOOMKilled", "alertmanager"),
                            now + timedelta(seconds=cfg.merge_window_seconds + 2))
        assert outcome == "created" and d.id != first


@needs_db
def test_restart_interruption_suppresses_same_fault_beyond_normal_cooldown(db_url):
    cfg = AppConfig(target_namespace="liveprobe-target", )
    now = datetime.now(timezone.utc)
    sig = Signal(NS, "Service", "inventory", "NoEndpoints", "endpoint_poller")
    with db.session() as s:
        inc, outcome = ingest(s, cfg, sig, now)
        assert outcome == "created"
        inc.status = "failed"
        inc.status_reason = "LiveProbe restarted while incident was 'analyzing'; marked failed (no replay)"
        inc.closed_at = now
        same, outcome = ingest(s, cfg, sig, now + timedelta(seconds=cfg.close_cooldown_seconds + 20))
        assert outcome == "suppressed" and same.id == inc.id


def test_pod_owner_resolution_and_signal():
    k = family1_kube()
    pod = k.pods[1]
    assert deployment_for_pod(k, NS, pod) == "inventory"
    emitted = []
    w = Watchers(k, AppConfig(target_namespace="liveprobe-target", ), emitted.append)
    w.handle_pod(pod)
    assert len(emitted) == 1 and emitted[0].reason == "ImagePullBackOff"
    pod["metadata"]["deletionTimestamp"] = "2026-09-25T11:00:00Z"
    w.handle_pod(pod)
    assert len(emitted) == 1


@pytest.mark.parametrize("case", [
    "corroborated", "repeated", "stale", "new_pod", "other_node", "recovered", "unrelated",
    "not_kubelet", "outside_scope", "duplicate_event",
])
def test_cni_warning_requires_current_pending_pods_on_the_reporting_node(case):
    from app.incidents import CNI_EVENT_SOURCE, CNI_VERSION_MISMATCH
    from app.safety import needs_cluster_authority

    kube, signals = FakeKube(), []
    cfg = AppConfig(target_namespaces=["dev"], historical_namespaces=["prd"])
    watcher = Watchers(kube, cfg, signals.append)
    now = datetime.now(timezone.utc)
    warnings = []
    for name in ("first", "second"):
        pod = {
            "metadata": {"namespace": "dev", "name": name, "uid": name + "-uid",
                         "creationTimestamp": (now - timedelta(minutes=5)).isoformat()},
            "spec": {"nodeName": "worker-a"}, "status": {"phase": "Pending"},
        }
        kube.pods.append(pod)
        warnings.append({
            "metadata": {"namespace": "dev", "uid": name + "-warning"}, "type": "Warning",
            "involvedObject": {**{k: pod["metadata"][k] for k in ("namespace", "name", "uid")}, "kind": "Pod"},
            "source": {"component": "kubelet", "host": "worker-a"}, "reason": "FailedCreatePodSandBox",
            "lastTimestamp": (now - timedelta(seconds=1)).isoformat(), "count": 1,
            "message": ('Failed to create pod sandbox: failed to setup network for sandbox "local-id": '
                        'plugin type="bridge" failed (add): incompatible CNI versions; '
                        'config is "1.0.0", plugin supports ["0.1.0" "0.2.0" "0.3.0" "0.3.1" "0.4.0"]'),
        })
    first, second = warnings
    watcher.handle_event(first)
    assert not signals
    if case == "repeated":
        second = {**first, "count": 2}
    elif case == "stale":
        second["lastTimestamp"] = (now - timedelta(seconds=121)).isoformat()
    elif case == "new_pod":
        kube.pods[1]["metadata"]["uid"] = "replacement"
    elif case == "other_node":
        second["source"]["host"] = kube.pods[1]["spec"]["nodeName"] = "worker-b"
    elif case == "recovered":
        kube.pods[0]["status"] = {"phase": "Running", "podIP": "192.0.2.1"}
    elif case == "unrelated":
        second["message"] = 'Failed to create pod sandbox: CNI configuration was not initialized'
    elif case == "not_kubelet":
        second["source"]["component"] = "other"
    elif case == "outside_scope":
        second["involvedObject"]["namespace"] = second["metadata"]["namespace"] = "prd"
    elif case == "duplicate_event":
        second["metadata"]["uid"] = first["metadata"]["uid"]
    watcher.handle_event(second)
    if case not in ("corroborated", "repeated"):
        assert not signals
        return
    signal, = signals
    assert (signal.kind, signal.name, signal.reason, signal.source) == (
        "Node", "worker-a", CNI_VERSION_MISMATCH, CNI_EVENT_SOURCE)
    assert signal.detail["configured_version"] == "1.0.0"
    assert "0.4.0" in signal.detail["supported_versions"]
    assert signal.detail["warning_count"] == 2
    assert [p["pod"] for p in signal.detail["pods"]] == (["first", "second"] if case == "corroborated" else ["first"])
    assert "local-id" not in repr(signal)
    assert fingerprint(signal, cfg) == ("dev/Node/worker-a", "worker-a")
    assert fingerprint(Signal("dev", "App", "worker-a", "High5xx", "alertmanager"), cfg)[0] == "dev/worker-a"
    assert needs_cluster_authority(Incident(signal_reasons=[signal.reason]))
    watcher.handle_event(second)
    assert len(signals) == 1


def workload_fixture():
    kube = FakeKube()
    deployment = {"metadata": {"name": "api", "namespace": NS, "uid": "deployment-1", "generation": 2},
                  "spec": {"replicas": 1}, "status": {"observedGeneration": 2}}
    rs = {"metadata": {"name": "api-1", "namespace": NS, "uid": "rs-1", "ownerReferences": [
        {"kind": "Deployment", "name": "api", "uid": "deployment-1", "controller": True},
    ]}}
    pod = {"metadata": {"name": "api-1-abc", "namespace": NS, "uid": "pod-1", "ownerReferences": [
        {"kind": "ReplicaSet", "name": "api-1", "uid": "rs-1", "controller": True},
    ]}, "spec": {}, "status": {"phase": "Pending"}}
    kube.deployments["api"] = deployment
    kube.replicasets.append(rs)
    kube.pods.append(pod)
    return kube, deployment, pod


def test_workload_poll_scheduling_continuity_and_duplicate_suppression():
    kube, _, pod = workload_fixture()
    pod["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                                     "lastTransitionTime": "2026-11-23T00:00:00Z"}]
    emitted = []
    watcher = Watchers(kube, AppConfig(target_namespace=NS, workload_state_signals_enabled=True), emitted.append)
    for now in range(0, 120, 15):
        watcher.poll_endpoints_once(now)
    assert not emitted
    pod["metadata"]["uid"] = "pod-replacement"
    for now in range(120, 240, 15):
        watcher.poll_endpoints_once(now)
    assert not emitted
    watcher.poll_endpoints_once(240)
    watcher.poll_endpoints_once(255)
    assert [(s.name, s.reason, s.detail["observed_for_s"]) for s in emitted] == [("api", "FailedScheduling", 120)]


def test_workload_poll_discards_stale_owner_and_does_not_export_reference_names(monkeypatch):
    kube, _, pod = workload_fixture()
    pod["status"]["initContainerStatuses"] = [{"name": "init", "state": {"waiting": {
        "reason": "CreateContainerConfigError", "message": 'secret "application-credential" not found',
    }}}]
    emitted = []
    watcher = Watchers(kube, AppConfig(target_namespace=NS, workload_state_signals_enabled=True), emitted.append)
    for now in (0, 15, 30, 45):
        watcher.poll_endpoints_once(now)
    with monkeypatch.context() as patch:
        def failed_list(_namespace):
            raise TimeoutError()
        patch.setattr(kube, "list_deployments", failed_list)
        watcher.poll_endpoints_once(60)
    kube.replicasets[0]["metadata"]["uid"] = "rs-replacement"
    for now in (75, 90, 105, 120, 135):
        watcher.poll_endpoints_once(now)
    assert not emitted
    kube.replicasets[0]["metadata"]["uid"] = "rs-1"
    for now in (150, 165, 180, 195, 210):
        watcher.poll_endpoints_once(now)
    assert len(emitted) == 1 and emitted[0].reason == "RequiredContainerReferenceMissing"
    assert emitted[0].detail["reference_kind"] == "secret"
    assert emitted[0].detail["container_kind"] == "init"
    assert "application-credential" not in repr(emitted[0])


def test_workload_poll_checks_rollout_generation_and_separates_availability():
    kube, deployment, _ = workload_fixture()
    status = deployment["status"]
    status.update(observedGeneration=1, availableReplicas=1, updatedReplicas=0,
                  conditions=[{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}])
    emitted = []
    watcher = Watchers(kube, AppConfig(target_namespace=NS, workload_state_signals_enabled=True), emitted.append)
    watcher.poll_endpoints_once(0)
    status["observedGeneration"] = 2
    deployment["spec"]["paused"] = True
    watcher.poll_endpoints_once(15)
    assert not emitted
    deployment["spec"]["paused"] = False
    watcher.poll_endpoints_once(30)
    watcher.poll_endpoints_once(45)
    assert len(emitted) == 1 and emitted[0].reason == "ProgressDeadlineExceeded"
    assert emitted[0].detail["availability_reduced"] is False
    assert emitted[0].detail["updated_replicas"] == 0


def test_workload_poll_detects_current_replicaset_creation_block_without_pods():
    kube, deployment, _ = workload_fixture()
    kube.pods.clear()
    condition = {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
                 "lastTransitionTime": "2026-11-23T00:00:00Z", "message": 'serviceaccount "internal-account" not found'}
    deployment["status"].update(updatedReplicas=0, conditions=[condition])
    deployment["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "2"}
    replica_set = kube.replicasets[0]
    replica_set["metadata"].update(generation=1, annotations={"deployment.kubernetes.io/revision": "1"})
    replica_set["spec"] = {"replicas": 1}
    replica_set["status"] = {"observedGeneration": 1, "replicas": 0, "conditions": [condition]}
    emitted = []
    watcher = Watchers(kube, AppConfig(target_namespace=NS, workload_state_signals_enabled=True), emitted.append)
    for now in (0, 15, 30, 45, 60, 75):
        watcher.poll_endpoints_once(now)
    assert not emitted
    replica_set["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "2"
    for now in (90, 105, 120, 135, 150, 165):
        watcher.poll_endpoints_once(now)
    assert len(emitted) == 1 and emitted[0].reason == "ReplicaCreationFailed"
    assert emitted[0].detail["failure_class"] == "service_account_missing"
    assert emitted[0].detail["observed_for_s"] == 60
    assert "internal-account" not in repr(emitted[0])


@pytest.mark.parametrize("selected, expected", [({"api"}, 1), ({"lp-eval-kw04"}, 0), (set(), 0)])
def test_workload_poll_limits_candidate_signals_to_selected_workloads(selected, expected):
    kube, deployment, pod = workload_fixture()
    deployment["status"].update(observedGeneration=2, availableReplicas=1, updatedReplicas=0,
                                conditions=[{"type": "Progressing", "status": "False",
                                             "reason": "ProgressDeadlineExceeded"}])
    emitted = []
    config = AppConfig(target_namespace=NS, workload_state_signals_enabled=True,
                       workload_state_signal_workloads=selected)
    Watchers(kube, config, emitted.append).poll_endpoints_once(0)
    assert len(emitted) == expected


@pytest.mark.parametrize("variant", ["confirmed", "too_few", "before_window", "different_uid", "different_probe",
                                    "wrong_reporter", "without_killing", "without_restart", "older_revision", "outside_scope"])
def test_liveness_signal_requires_current_ownership_and_confirmed_kubelet_restart(variant, monkeypatch):
    from app.workload_signals import LIVENESS, WorkloadSignals

    kube, deployment, pod = workload_fixture()
    deployment["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "2"}
    kube.replicasets[0]["metadata"]["annotations"] = {
        "deployment.kubernetes.io/revision": "1" if variant == "older_revision" else "2",
    }
    pod["spec"]["containers"] = [{"name": "api", "livenessProbe": {"httpGet": {"path": "/ready", "port": 8080}}}]
    pod["status"].update(phase="Running", containerStatuses=[{
        "name": "api", "restartCount": 0 if variant == "without_restart" else 1,
        "lastState": {"terminated": {"finishedAt": "2026-11-24T00:01:40Z"}},
    }])
    involved = {"kind": "Pod", "namespace": NS, "name": pod["metadata"]["name"],
                "uid": pod["metadata"]["uid"], "fieldPath": "spec.containers{api}"}
    warning = {"reason": "Unhealthy", "type": "Warning", "involvedObject": dict(involved),
               "reportingComponent": "kubelet", "firstTimestamp": "2026-11-24T00:01:20Z",
               "lastTimestamp": "2026-11-24T00:01:37Z", "count": 3,
               "message": "Liveness probe failed: HTTP probe failed with statuscode: 404"}
    killing = {"reason": "Killing", "type": "Normal", "involvedObject": involved, "reportingComponent": "kubelet",
               "lastTimestamp": "2026-11-24T00:01:37Z", "message": "Container api failed liveness probe, will be restarted"}
    if variant == "too_few":
        warning["count"] = 2
    elif variant == "before_window":
        warning["firstTimestamp"] = "2026-11-23T23:59:01Z"
    elif variant == "different_uid":
        warning["involvedObject"]["uid"] = "previous-pod"
    elif variant == "different_probe":
        warning["message"] = warning["message"].replace("Liveness", "Readiness")
    elif variant == "wrong_reporter":
        warning["reportingComponent"] = "external-controller"
    from unittest.mock import Mock

    event_read = Mock(return_value=[warning] if variant == "without_killing" else [warning, killing])
    monkeypatch.setattr(kube, "list_events", event_read)
    observed = datetime(2026, 11, 24, 0, 2, tzinfo=timezone.utc)
    emitted = []
    detector = WorkloadSignals(NS, kube, emitted.append, max_gap=45,
                               workloads={"other"} if variant == "outside_scope" else {"api"})
    assert detector.observe([pod], [deployment], now=30, observed_at=observed) == (variant == "confirmed")
    assert detector.observe([pod], [deployment], now=31, observed_at=observed) == 0
    if variant == "confirmed":
        signal, = emitted
        assert (signal.name, signal.reason, signal.detail["warnings_last_two_minutes"]) == ("api", LIVENESS, 3)
        assert "HTTP probe failed" not in repr(signal)
        event_read.assert_called_once_with(NS)
    else:
        assert not emitted
        if variant in ("older_revision", "outside_scope", "without_restart"):
            event_read.assert_not_called()


@pytest.mark.parametrize("variant", ["matched", "under_120_seconds", "old", "other_pod", "projection",
                                    "not_bound", "running", "wrong_reporter", "old_revision"])
def test_mount_signal_requires_a_current_scheduled_pod_and_repeated_pvc_mount_failure(variant, monkeypatch):
    from unittest.mock import Mock

    from app.workload_signals import VOLUME_MOUNT, WorkloadSignals

    kube, deployment, pod = workload_fixture()
    deployment["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "2"}
    kube.replicasets[0]["metadata"]["annotations"] = {
        "deployment.kubernetes.io/revision": "1" if variant == "old_revision" else "2",
    }
    pod["metadata"]["creationTimestamp"] = "2026-11-24T00:00:00Z"
    pod["spec"].update(nodeName="node", containers=[{"name": "api", "volumeMounts": [{"name": "orders"}]}],
                       volumes=[{"name": "orders", "persistentVolumeClaim": {"claimName": "orders-pvc"}},
                                {"name": "settings", "configMap": {"name": "configuration"}}])
    if variant == "running":
        pod["status"]["phase"] = "Running"
    reason_volume = "settings" if variant == "projection" else "old-orders-pv"
    warning = {"reason": "FailedMount", "type": "Warning", "count": 8,
               "reportingComponent": "other" if variant == "wrong_reporter" else "kubelet",
               "involvedObject": {"kind": "Pod", "namespace": NS, "name": pod["metadata"]["name"],
                                  "uid": "old-pod" if variant == "other_pod" else pod["metadata"]["uid"]},
               "firstTimestamp": "2026-11-23T23:59:59Z" if variant == "old" else "2026-11-24T00:00:03Z",
               "lastTimestamp": "2026-11-24T00:02:02Z" if variant == "under_120_seconds" else "2026-11-24T00:02:04Z",
               "message": f'MountVolume.NewMounter initialization failed for volume "{reason_volume}" : path "/data/missing" does not exist'}
    monkeypatch.setattr(kube, "list_events", Mock(return_value=[warning]))
    pvc = {"metadata": {"name": "orders-pvc", "namespace": NS, "uid": "pvc-uid"},
           "spec": {"volumeName": "old-orders-pv"},
           "status": {"phase": "Pending" if variant == "not_bound" else "Bound"}}
    pvc_read = Mock(return_value=pvc)
    monkeypatch.setattr(kube, "get_pvc", pvc_read, raising=False)
    observed = datetime(2026, 11, 24, 0, 2, 6, tzinfo=timezone.utc)
    emitted = []
    observer = WorkloadSignals(NS, kube, emitted.append, max_gap=45, workloads={"api"})
    assert observer.observe([pod], [deployment], now=126, observed_at=observed) == (variant == "matched")
    assert observer.observe([pod], [deployment], now=127, observed_at=observed) == 0
    if variant == "matched":
        signal, = emitted
        assert (signal.name, signal.reason, signal.detail["observed_for_s"]) == ("api", VOLUME_MOUNT, 121)
        assert signal.detail["claim"] == "orders-pvc"
        assert "/data/missing" not in repr(signal)
        pvc_read.assert_called_once_with(NS, "orders-pvc")
    elif variant != "not_bound":
        pvc_read.assert_not_called()


@pytest.mark.parametrize("variant", ["current", "old_rollout", "wrong_owner", "wrong_account", "no_capacity",
                                    "mixed_error", "network_risk", "fixed_baseline"])
def test_replica_account_handoff_requires_current_isolated_evidence(variant):
    from app.evidence import Bundle
    from app.router import decide

    kube, deployment, _ = workload_fixture()
    deployment["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "2"}
    template = {"spec": {"serviceAccountName": "missing-account", "containers": [{"name": "api", "image": "api:v1"}]}}
    deployment["spec"]["template"] = template
    deployment["status"]["availableReplicas"] = 1
    replica = kube.replicasets[0]
    replica["metadata"].update(generation=3, annotations={"deployment.kubernetes.io/revision": "2"})
    replica["spec"] = {"replicas": 1, "template": template}
    missing = f'error looking up service account {NS}/missing-account: serviceaccount "missing-account" not found'
    condition = {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": missing}
    replica["status"] = {"observedGeneration": 3, "replicas": 0, "conditions": [condition]}
    if variant == "old_rollout":
        replica["status"]["observedGeneration"] = 2
    elif variant == "wrong_owner":
        replica["metadata"]["ownerReferences"][0]["uid"] = "old-deployment"
    elif variant == "wrong_account":
        condition["message"] = missing.replace("missing-account", "different-account")
    elif variant == "no_capacity":
        deployment["status"]["availableReplicas"] = 0

    bundle = Bundle(NS, "api")
    bundle.deployment = deployment_summary(kube, NS, "api")
    bundle.pods = [{"name": "api-old", "containers": [{"waiting_reason": None, "ready": True}]}]
    signals = ["ReplicaCreationFailed"]
    if variant == "mixed_error":
        bundle.logs = ["database connection failed"]
    if variant == "network_risk":
        signals.append("NetworkDrop")
    calls = []

    def classifier(prompt):
        calls.append(prompt)
        return {"level": "COMPLEX", "rationale": "other evidence requires analysis", "confidence": 1.0}

    result = decide(bundle, 15, classifier, signal_reasons=signals, allow_authority_shortcut=variant != "fixed_baseline")
    if variant == "current":
        assert result.decided_by == "rules_authority_boundary" and result.policy["handoff_only"] is True
        assert result.features["evidence_state"] == "fault_observed" and not calls
        assert "missing-account" not in repr(result.features["replica_account_failure"])
    else:
        assert result.decided_by != "rules_authority_boundary"
        if variant == "network_risk":
            assert result.level == "HIGH_RISK"


def test_deployment_summary_tracks_reused_replicaset_rollout():
    k = family1_kube(created_bad="2026-09-24T10:00:00Z")
    k.deployments["inventory"]["status"]["conditions"] = [
        {"type": "Progressing", "lastUpdateTime": "2026-09-25T13:31:37Z"}]
    d = deployment_summary(k, NS, "inventory")
    assert d["recent_rollout_at"] == "2026-09-25T13:31:37+00:00"


def test_endpoint_grace_then_signal_and_restore():
    k = family4_kube()
    k.services["inventory"]["spec"]["selector"] = {"app": "inventory"}
    k.pods[0]["status"]["containerStatuses"][0]["ready"] = False
    emitted = []
    # Existing configs with a short grace still respect the new ten-minute floor.
    w = Watchers(k, AppConfig(target_namespace=NS, endpoint_grace_seconds=30), emitted.append)
    for now in range(0, 600, 15):
        w.poll_endpoints_once(now)
    w.poll_endpoints_once(599)
    assert not emitted
    # A replacement Pod may still be starting after ten minutes of no ready backend.
    k.pods[0]["metadata"]["creationTimestamp"] = datetime.now(timezone.utc).isoformat()
    w.poll_endpoints_once(600)
    assert len(emitted) == 1 and emitted[0].reason == "NoEndpoints"
    assert emitted[0].detail["empty_for_s"] == 600
    assert emitted[0].detail["matching_pods"] == 1
    k.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
    w.poll_endpoints_once(615)
    assert "inventory" not in w._empty_since


@pytest.mark.parametrize("interruption", ["rollout_recovered", "unknown", "new_service", "api_error", "poll_gap"])
def test_endpoint_grace_restarts_after_interruption(interruption, monkeypatch):
    k = family4_kube()
    service = k.services["inventory"]
    service["metadata"]["uid"] = "original"
    emitted = []
    watcher = Watchers(k, AppConfig(target_namespace=NS), emitted.append)
    for now in range(0, 555, 15):
        watcher.poll_endpoints_once(now)

    if interruption == "rollout_recovered":
        k.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
        watcher.poll_endpoints_once(555)
    elif interruption == "unknown":
        del k.endpoints["inventory"]
        watcher.poll_endpoints_once(555)
    elif interruption == "new_service":
        service["metadata"]["uid"] = "replacement"
    elif interruption == "api_error":
        def timeout(*_args):
            raise TimeoutError("endpoints read timed out")

        with monkeypatch.context() as context:
            context.setattr(k, "get_endpoints", timeout)
            with pytest.raises(TimeoutError):
                watcher.poll_endpoints_once(555)

    k.endpoints["inventory"] = {"subsets": []}
    start = 615 if interruption == "poll_gap" else 570
    for now in range(start, start + 600, 15):
        watcher.poll_endpoints_once(now)
    assert not emitted
    watcher.poll_endpoints_once(start + 600)
    assert len(emitted) == 1


@pytest.mark.parametrize("intervening", [1, 55])
def test_stale_open_merge_finds_matching_older_reason(db_url, intervening):
    """독립 검토 재현: 최신 열린 사건만 보면 그보다 오래된 같은 원인 사건을 놓친다."""
    from app import db
    from app.config import AppConfig
    from app.incidents import Signal, ingest
    cfg = AppConfig(target_namespaces=["apps"])
    now = datetime.now(timezone.utc)
    image = Signal("apps", "Deployment", "api", "ImagePullBackOff", "pod_watch")
    oom = Signal("apps", "Deployment", "api", "TargetWorkloadOOMKilled", "alertmanager")
    with db.session() as s:
        a, _ = ingest(s, cfg, image, now)
        assert a is not None
        b, status = ingest(s, cfg, oom, now + timedelta(seconds=cfg.merge_window_seconds + 1))
        assert status == "created" and b.id != a.id
        for i in range(intervening - 1):
            s.add(Incident(fingerprint=a.fingerprint, namespace=a.namespace, workload=a.workload,
                           title="다른 원인", status="open", signal_reasons=[f"different-{i}"],
                           created_at=now + timedelta(seconds=cfg.merge_window_seconds + 1)))
        s.flush()
        again, status = ingest(s, cfg, image, now + timedelta(seconds=2 * cfg.merge_window_seconds + 2))
        assert (again.id, status) == (a.id, "merged")
