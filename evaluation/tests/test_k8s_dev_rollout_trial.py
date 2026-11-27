import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from evaluation import k8s_dev_rollout_trial as trial
from evaluation import k8s_dev_volume_fixture as volume_fixture
from evaluation.comparison_summary import dev_rollout_record


CASE = trial.SCHEDULING
ERROR = "0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector."


def test_scheduling_patch_touches_only_the_original_selector(monkeypatch):
    deployment = {
        "metadata": {"uid": "fixture", "resourceVersion": "2"},
        "spec": {"template": {"spec": {"containers": [{"name": "verification"}],
                                       "nodeSelector": {"kubernetes.io/os": CASE.healthy}}}},
    }
    baseline = {"uid": "fixture", "fixture_sha256": trial.live.fixture_digest(deployment)}
    write = Mock()
    monkeypatch.setattr(trial.live, "command", write)
    trial.patch_path(deployment, baseline, CASE.healthy, CASE.broken, CASE)
    assert json.loads(write.call_args.args[-1]) == [
        {"op": "test", "path": "/metadata/uid", "value": "fixture"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "2"},
        {"op": "test", "path": "/spec/template/spec/nodeSelector/kubernetes.io~1os", "value": CASE.healthy},
        {"op": "replace", "path": "/spec/template/spec/nodeSelector/kubernetes.io~1os", "value": CASE.broken},
    ]
    drift = deepcopy(deployment)
    drift["spec"]["template"]["spec"]["nodeSelector"]["extra"] = "unrelated"
    with pytest.raises(trial.live.Refused, match="designated field"):
        trial.patch_path(drift, baseline, CASE.healthy, CASE.broken, CASE)
    write.assert_called_once()


def test_scheduling_receipt_requires_original_error_and_model_attribution():
    case = {"id": CASE.case_id, "environment": "isolated-workload"}
    arm = "single_sol"

    def healthy(generation):
        return {"generation": generation, "replicas": 1, "contract_ok": True, "pod_uid": "pod", "requests": [200, 200]}

    receipt = {
        "case": CASE.case_id, "arm": arm, "target": CASE.target, "protocol": trial.PROTOCOL,
        "case_sha256": trial.hash_text(json.dumps(case, sort_keys=True, ensure_ascii=False)),
        "input_sha256": trial.hash_text(trial.TASK + ERROR), "fixture_sha256": "a" * 64,
        "node_os_labels": ["linux"], "status": "verified_dev_recovered", "lease_released": True,
        "healthy_before": healthy(1), "restored": healthy(3), "verified_before_cleanup": healthy(3),
        "operator_restored": False, "standalone_action": {"accepted": True, "action": "node_selector_os_cas",
                                                         "from": CASE.broken, "to": CASE.healthy},
        "fault_before": {"error": ERROR, "reason": CASE.signal, "sha256": trial.hash_text(ERROR),
                         "replicaset": CASE.workload + "-revision", "pod_uid": "failed-pod", "generation": 2,
                         "available_old": 1, "ready_new": 0, "waiting_reason": "Unschedulable", "pod_scheduled": False,
                         "node_selector": {"kubernetes.io/os": CASE.broken}},
    }
    assert dev_rollout_record(receipt, arm, case)["status"] == "verified_recovered"
    with pytest.raises(ValueError, match="rejection or node inventory"):
        dev_rollout_record({**receipt, "node_os_labels": ["linux", CASE.broken]}, arm, case)
    with pytest.raises(ValueError, match="repair, ownership or independent result"):
        dev_rollout_record({**receipt, "operator_restored": True}, arm, case)


def test_liveness_warning_belongs_to_the_current_container(monkeypatch):
    pod = {"metadata": {"uid": "current", "creationTimestamp": "2026-11-23T10:00:00Z"},
           "status": {"containerStatuses": [{"name": "verification", "restartCount": 1}]}}
    event = {"metadata": {"uid": "warning"}, "type": "Warning", "count": 3, "reason": "Unhealthy",
             "involvedObject": {"uid": "current", "fieldPath": "spec.containers{verification}"},
             "source": {"component": "kubelet"}, "firstTimestamp": "2026-11-23T10:00:00.250Z",
             "message": trial.LIVENESS_ERROR}
    stale = {**event, "metadata": {"uid": "stale"}, "firstTimestamp": "2026-11-23T09:59:59Z"}
    readiness = {**event, "message": "Readiness probe failed: HTTP probe failed with statuscode: 404"}
    older_pod = {**event, "involvedObject": {**event["involvedObject"], "uid": "older"}}
    read = Mock(return_value=json.dumps({"items": [stale, readiness, older_pod, event]}))
    monkeypatch.setattr(trial.live, "command", read)
    proof = trial.liveness_evidence(pod, {"min_ready_seconds": 24})
    assert proof["event_uid"] == "warning" and proof["warnings"] == 3 and proof["restarts"] == 1
    assert "involvedObject.uid=current,reason=Unhealthy" in read.call_args.args
    read.return_value = json.dumps({"items": [stale, readiness, older_pod]})
    assert trial.liveness_evidence(pod, {"min_ready_seconds": 24}) is None


def test_liveness_miss_requires_stable_watchers_and_end_of_window_failure():
    fixture = trial.LIVENESS
    case = {"id": fixture.case_id, "environment": "isolated-workload"}
    err = trial.LIVENESS_ERROR
    fault = {"reason": fixture.signal, "error": err, "sha256": trial.hash_text(err),
             "replicaset": fixture.workload + "-revision", "pod_uid": "fault", "generation": 2,
             "available_old": 1, "ready_new": 1, "liveness_path": fixture.broken, "min_ready_seconds": 24,
             "event_uid": "warning", "first_warning_at": "2026-11-23T10:00:01Z", "warnings": 1, "restarts": 0}
    repeated = {**fault, "warnings": 3, "restarts": 1}
    healthy = {"pod_uid": "normal", "replicas": 1, "contract_ok": True, "requests": [200, 200],
               "restarts": 0, "min_ready_seconds": 24}
    epochs = {"pod_watch": "2026-11-23T09:00:01Z", "event_watch": "2026-11-23T09:00:00Z"}
    receipt = {"case": fixture.case_id, "arm": "liveprobe", "target": fixture.target, "protocol": trial.PROTOCOL,
               "status": "product_observation_only", "fixture_sha256": "b" * 64,
               "case_sha256": trial.hash_text(json.dumps(case, sort_keys=True, ensure_ascii=False)),
               "input_sha256": trial.hash_text(trial.TASK + err), "healthy_before": {**healthy, "generation": 1},
               "fault_before": fault, "fault_repeated": repeated, "still_failed_before_cleanup": repeated,
               "restored": {**healthy, "generation": 3}, "operator_restored": True, "watchers_at_start": epochs,
               "watchers_at_end": epochs, "observation_seconds": 180, "matched_incident_count_after": 0}
    row = dev_rollout_record(receipt, "liveprobe", case)
    assert row["status"] == "product_observation_only" and row["calls"] == [] and row["usage"] == {}
    with pytest.raises(ValueError, match="bounded undetected failure"):
        dev_rollout_record({**receipt, "observation_seconds": 179}, "liveprobe", case)
    with pytest.raises(ValueError, match="bounded undetected failure"):
        changed = {**epochs, "event_watch": "2026-11-23T10:01:00Z"}
        dev_rollout_record({**receipt, "watchers_at_end": changed}, "liveprobe", case)
    with pytest.raises(ValueError, match="same-Pod warning"):
        dev_rollout_record({**receipt, "fault_repeated": {**repeated, "pod_uid": "different"}}, "liveprobe", case)


def test_volume_fixture_uses_only_scoped_claims_and_a_read_only_mount():
    volume, healthy, broken, workload = volume_fixture.manifest("test-node")["items"]
    assert volume["kind"] == "PersistentVolume"
    assert volume["spec"]["local"] == {"path": volume_fixture.VOLUME_MISSING_PATH}
    assert volume["spec"]["claimRef"] == {"namespace": "dev", "name": trial.VOLUME.broken}
    assert volume["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert broken["spec"]["volumeName"] == volume["metadata"]["name"]
    assert healthy["spec"]["storageClassName"] == "local-path"
    assert all(obj["metadata"]["namespace"] == "dev" for obj in (healthy, broken, workload))
    pod = workload["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"] == [{"name": "check", "persistentVolumeClaim": {
        "claimName": trial.VOLUME.healthy, "readOnly": True,
    }}]
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True


def test_volume_evidence_requires_the_exact_warning_on_the_current_scheduled_pod(monkeypatch):
    pod = {"metadata": {"uid": "current", "name": "pending", "creationTimestamp": "2026-11-23T10:00:00Z"},
           "spec": {"nodeName": "test-node", "volumes": [{"name": "check", "persistentVolumeClaim": {
               "claimName": trial.VOLUME.broken, "readOnly": True,
           }}]}, "status": {"phase": "Pending"}}
    event = {"metadata": {"uid": "mount-warning"}, "type": "Warning", "count": 3, "reason": "FailedMount",
             "involvedObject": {"uid": "current", "name": "pending", "namespace": "dev", "kind": "Pod"},
             "source": {"component": "kubelet"}, "firstTimestamp": "2026-11-23T10:00:01Z",
             "message": trial.VOLUME_ERROR}
    binding = {"claim_uid": "fixture-claim", "pv_uid": "fixture-volume", "pv": volume_fixture.VOLUME_NAME}
    baseline = {"volume_bindings": {trial.VOLUME.broken: binding}}
    foreign = {**event, "involvedObject": {**event["involvedObject"], "uid": "retired"}}
    stale = {**event, "firstTimestamp": "2026-11-23T09:59:59Z"}
    other_component = {**event, "reportingComponent": "other"}
    read = Mock(return_value=json.dumps({"items": [foreign, stale, other_component]}))
    monkeypatch.setattr(trial.live, "command", read)
    assert trial.volume_evidence(pod, baseline) is None
    read.return_value = json.dumps({"items": [foreign, stale, event]})
    assert trial.volume_evidence(pod, baseline) == {
        "event_uid": "mount-warning", "first_warning_at": "2026-11-23T10:00:01Z", "warnings": 3,
        "claim": trial.VOLUME.broken, "claim_binding": binding, "container_started": False, "pod_scheduled": True,
    }
    assert "involvedObject.uid=current,reason=FailedMount" in read.call_args.args
    read.return_value = json.dumps({"items": [{**event, "message": "unrelated projection error"}]})
    with pytest.raises(trial.live.Refused, match="first current-Pod mount warning differs"):
        trial.volume_evidence(pod, baseline)


def test_volume_receipt_requires_repeated_failure_and_the_original_claim_bindings():
    fixture = trial.VOLUME
    case = {"id": fixture.case_id, "environment": "isolated-workload"}
    arm = "single_sol"
    binding = {"claim_uid": "broken-claim", "pv_uid": "broken-volume", "pv": volume_fixture.VOLUME_NAME}
    bindings = {fixture.broken: binding, fixture.healthy: {
        "claim_uid": "initial-claim", "pv_uid": "initial-volume", "pv": "pvc-original",
    }}
    error = trial.VOLUME_ERROR
    fault = {"reason": fixture.signal, "error": error, "sha256": trial.hash_text(error),
             "replicaset": fixture.workload + "-revision", "pod_uid": "fault", "generation": 2,
             "available_old": 1, "ready_new": 0, "claim": fixture.broken, "claim_binding": binding,
             "pod_scheduled": True, "container_started": False, "event_uid": "warning",
             "first_warning_at": "2026-11-23T10:00:01Z", "warnings": 1}
    health = {"pod_uid": "original", "replicas": 1, "contract_ok": True, "requests": [200, 200]}
    recovered = {**health, "generation": 3}
    repeated = {**fault, "warnings": 4}
    receipt = {"case": fixture.case_id, "arm": arm, "target": fixture.target, "protocol": trial.PROTOCOL,
               "status": "verified_dev_recovered", "fixture_sha256": "e" * 64,
               "case_sha256": trial.hash_text(json.dumps(case, sort_keys=True, ensure_ascii=False)),
               "input_sha256": trial.hash_text(trial.TASK + error), "healthy_before": {**health, "generation": 1},
               "fault_before": fault, "fault_repeated": repeated, "verified_before_cleanup": recovered,
               "restored": recovered, "operator_restored": False, "lease_released": True,
               "volume_bindings": bindings, "restored_volume_bindings": bindings,
               "standalone_action": {"accepted": True, "action": "volume_claim_name_cas",
                                     "from": fixture.broken, "to": fixture.healthy}}
    assert dev_rollout_record(receipt, arm, case)["status"] == "verified_recovered"
    with pytest.raises(ValueError, match="mount event and original volume bindings"):
        dev_rollout_record({**receipt, "fault_repeated": {**repeated, "event_uid": "different"}}, arm, case)
    with pytest.raises(ValueError, match="mount event and original volume bindings"):
        dev_rollout_record({**receipt, "restored_volume_bindings": {}}, arm, case)
