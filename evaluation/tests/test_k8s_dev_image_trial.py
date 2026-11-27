from copy import deepcopy
from unittest.mock import Mock

import pytest

from evaluation import k8s_dev_image_trial as trial
from evaluation.dev_image_case import load_image_case


CASE = load_image_case("F1-01")
ORIGINAL = "liveprobe/inventory@sha256:" + "a" * 64
FAULT = "liveprobe/inventory:does-not-exist-01"


def test_order_case_is_pinned_and_checks_the_order_dependency(monkeypatch):
    import json

    case = load_image_case("F1-07")
    assert case.target == "dev/Deployment/order"
    assert case.bad_image == "liveprobe/order:does-not-exist-07"
    with pytest.raises(ValueError, match="outside the bounded dev"):
        load_image_case("F1-04")

    responses = {
        "actuator/health/readiness": {"status": "UP"},
        "api/orders/1": {"id": 1, "item": "a", "quantity": 1, "amount": 100},
        "api/orders/check?item=a": {"item": "a", "orderable": True,
                                    "inventory": {"item": "a", "quantity": 1, "available": True}},
    }
    command = Mock(side_effect=lambda *args: json.dumps(responses[args[-1].split("/proxy/")[1]]))
    monkeypatch.setattr(trial, "command", command)
    monkeypatch.setattr(trial, "ready_endpoints", lambda selected: selected.workload + "-ready")
    assert trial.probe(case)["order_dependency"] == 200
    assert all("/namespaces/dev/services/order:8080/" in call.args[-1] for call in command.call_args_list)
    responses["api/orders/check?item=a"]["inventory"]["item"] = "wrong-item"
    with pytest.raises(trial.Refused, match="order-to-inventory"):
        trial.probe(case)


def test_matched_order_recognizes_the_digest_returned_by_docker_cri():
    from evaluation.matched_order_image import runtime_digest

    image_id = "sha256:" + "a" * 64
    assert runtime_digest(f"docker://{image_id}") == image_id
    assert runtime_digest(f"docker-pullable://liveprobe/order@{image_id}") == image_id


def test_order_attributes_an_aggregate_alert_only_to_the_observed_502_route(monkeypatch):
    from evaluation import matched_order_image as order

    alert = {"namespace": "dev", "app": "order", "method": None, "uri": None}
    incident = {"alerts": [alert]}
    sample = {"metric": {"method": "GET", "uri": order.ROUTE, "status": "502"}, "value": [1, "0.5"]}
    monkeypatch.setattr(order, "failing_routes", lambda: [sample])
    assert order.detector_receipt(incident, None) is None
    assert order.detector_receipt(incident, {"error": "502"}) == {"alert": alert, "failing_routes": [sample]}
    monkeypatch.setattr(order, "failing_routes", lambda: [sample, {"metric": {"uri": "/api/orders"}}])
    assert order.detector_receipt(incident, {"error": "502"}) is None


def test_order_retries_observations_without_repeating_writes(monkeypatch):
    from evaluation import matched_order_drained as drained

    monkeypatch.setattr(drained, "sleep", lambda _: None)
    refused = trial.Refused("request failed")
    command = Mock(side_effect=[refused, "200"])
    assert drained.observation_command(command, "exec", "deploy/inventory", "-c", "inventory", "--", "curl") == "200"
    assert command.call_count == 2
    command = Mock(side_effect=refused)
    with pytest.raises(trial.Refused):
        drained.observation_command(command, "patch", "deployment", "order")
    command.assert_called_once()


@pytest.mark.parametrize("case_id", ["F1-01", "F1-07", "F1-13"])
def test_comparison_requires_action_provenance_and_two_pre_cleanup_checks(case_id):
    import hashlib

    from evaluation.comparison_summary import dev_image_record
    from evaluation.live_repair import TASK

    selected = load_image_case(case_id)
    original = f"liveprobe/{selected.workload}@sha256:" + "a" * 64
    message = f"ErrImagePull: {selected.bad_image}"
    probe = {"ready": 200, "business": 200, "contract_ok": True, "endpoint_uid": "fixture"}
    if case_id != "F1-01":
        probe["order_dependency"] = 200
    result = {"case": case_id, "arm": "single_terra", "namespace": "dev", "target": selected.workload,
              "protocol": "live-repair/v36", "status": "verified_dev_recovered",
              "baseline_image": original, "injected_image": selected.bad_image, "fixture_sha256": "a" * 64,
              "fault_before": {"reason": "ErrImagePull", "initial_error_log": message}, "initial_error_log": message,
              "input_sha256": hashlib.sha256((TASK + message).encode()).hexdigest(),
              "healthy_before": [probe, probe], "fault_service_probe": probe,
              "standalone_action": {"accepted": True, "action": "image_cas", "from_images": [selected.bad_image],
                                    "to_images": [original]},
              "pre_cleanup_verified": [probe, probe], "restored": [probe, probe], "lease_released": True,
              "liveprobe_incidents": [], "first_error_to_verified_ms": 31_000}
    assert dev_image_record(result, "single_terra")["status"] == "verified_recovered"
    for override in ({"lease_released": False}, {"liveprobe_incidents": [42]}, {"pre_cleanup_verified": [probe]}):
        with pytest.raises(ValueError, match="not attributable"):
            dev_image_record({**result, **override}, "single_terra")
    if case_id != "F1-01":
        failed_probe = {**probe, "order_dependency": 503}
        with pytest.raises(ValueError, match="not attributable"):
            dev_image_record({**result, "pre_cleanup_verified": [probe, failed_probe]}, "single_terra")


def deployment(image=ORIGINAL, *, uid="owned-deployment", rv="81"):
    return {"metadata": {"uid": uid, "resourceVersion": rv, "generation": 1, "labels": {"managed": "yes"}},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "inventory", "image": image}]}}}}


def test_image_patch_checks_scoped_identity_and_only_replaces_image(monkeypatch):
    import json

    command = Mock()
    monkeypatch.setattr(trial, "command", command)
    trial.patch_image(CASE, deployment(), FAULT)
    args = command.call_args.args
    assert args[:-1] == ("patch", "deployment", "inventory", "--type=json", "-p")
    operations = json.loads(args[-1])
    assert operations == [
        {"op": "test", "path": "/metadata/uid", "value": "owned-deployment"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "81"},
        {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "inventory"},
        {"op": "test", "path": trial.IMAGE_PTR, "value": ORIGINAL},
        {"op": "replace", "path": trial.IMAGE_PTR, "value": FAULT},
    ]


@pytest.mark.parametrize("conflict", ["uid", "replicas", "foreign-image"])
def test_cleanup_refuses_to_clobber_concurrent_changes(monkeypatch, conflict):
    before = deployment()
    current = deployment(FAULT)
    if conflict == "uid":
        current["metadata"]["uid"] = "another-deployment"
    elif conflict == "replicas":
        current["spec"]["replicas"] = 2
    else:
        current["spec"]["template"]["spec"]["containers"][0]["image"] = "external-change"
    writes = Mock()
    monkeypatch.setattr(trial, "get", lambda *_: current)
    monkeypatch.setattr(trial, "patch_image", writes)
    with pytest.raises(trial.Refused, match="concurrent change"):
        trial.restore(CASE, before)
    writes.assert_not_called()


def test_cleanup_retries_only_after_reading_fresh_resource_version(monkeypatch):
    before = deployment()
    restored = deployment()
    restored["status"] = dict(observedGeneration=1, replicas=1, updatedReplicas=1, readyReplicas=1, availableReplicas=1)
    reads = iter([deployment(FAULT, rv="81"), deployment(FAULT, rv="82"), restored])
    writes = Mock(side_effect=[trial.Refused("CAS changed"), None])
    monkeypatch.setattr(trial, "get", lambda *_: next(reads))
    monkeypatch.setattr(trial, "patch_image", writes)
    monkeypatch.setattr(trial, "probe", lambda *_: {"ready": 200, "business": 200})
    monkeypatch.setattr(trial, "sleep", lambda _: None)
    assert len(trial.restore(CASE, before)) == 2
    assert [call.args[1]["metadata"]["resourceVersion"] for call in writes.call_args_list] == ["81", "82"]


def test_product_recovery_requires_action_attribution(monkeypatch):
    match = {"actions": [{"id": 1, "type": "rollout_undo", "target": CASE.target, "executed": True,
                          "status": "succeeded", "from_images": [FAULT], "to_images": [ORIGINAL]}]}
    assert trial.attributed_recovery(CASE, match, ORIGINAL)
    for key, other in (("executed", False), ("type", "rollout_restart"), ("status", "failed"),
                       ("from_images", [ORIGINAL]), ("to_images", [FAULT]), ("target", "prd/Deployment/inventory")):
        mismatch = deepcopy(match)
        mismatch["actions"][0][key] = other
        assert not trial.attributed_recovery(CASE, mismatch, ORIGINAL)


def test_fault_requires_owned_new_pod_and_sanitizes_initial_log(monkeypatch):
    owner = {"kind": "ReplicaSet", "controller": True, "uid": "owned-rs", "name": "inventory-test"}
    old = {"metadata": {"uid": "old"}}
    new = {"metadata": {"uid": "new", "name": "inventory-test-abc", "ownerReferences": [owner]},
           "spec": {"containers": [{"name": "inventory", "image": FAULT}]},
           "status": {"containerStatuses": [{"name": "inventory", "state": {"waiting": {
               "reason": "ErrImagePull", "message": f'failed to pull and unpack image "docker.io/{FAULT}": detail external to fixture'}}}]}}
    history = {"metadata": {"uid": "owned-rs", "ownerReferences": [{"controller": True, "uid": "owned-deployment"}]}}
    monkeypatch.setattr(trial, "get", lambda kind, *_, **__: {"items": [old, new]} if kind == "pod" else history)
    assert trial.fault_observation(CASE, "different-owner", "old") is None
    result = trial.fault_observation(CASE, "owned-deployment", "old")
    assert result["initial_error_log"] == f'failed to pull and unpack image "docker.io/{FAULT}"'
    assert "external to fixture" not in str(result)


def probes():
    return [{"ready": 200, "business": 200, "contract_ok": True, "endpoint_uid": "old"} for _ in range(2)]


def test_lease_material_is_only_provided_on_stdin(monkeypatch):
    token = "test-transport-token-" + "a" * 30
    command = Mock(return_value='{"active": true}')
    monkeypatch.setattr(trial, "command", command)
    lease = trial.WorkloadLease(CASE, "f1-01-single-terra", token)
    lease.require_active()
    args, kwargs = command.call_args
    assert "--token-stdin" in args and token not in " ".join(args[1:])
    assert kwargs == {"namespace": "liveprobe-system", "stdin": token + "\n"}
    assert token not in repr(lease)


def test_standalone_uses_same_normalization_for_a_real_initial_error():
    reference = {"fault_before": {"reason": "ErrImagePull", "initial_error_log": f"ErrImagePull: {FAULT}",
                                  "initial_error_source": "waiting reason and declared image"}}
    observed = {"reason": "ErrImagePull", "initial_error_log": f'failed to pull image "{FAULT}"'}
    assert trial.comparable_first_error(observed, reference, FAULT) == f"ErrImagePull: {FAULT}"
    observed["reason"] = "ImagePullBackOff"
    with pytest.raises(trial.Refused, match="differs"):
        trial.comparable_first_error(observed, reference, FAULT)


def test_a_preserved_nonrecovery_can_anchor_identical_actual_fault(tmp_path):
    reference = {"case": CASE.case_id, "protocol": trial.PROTOCOL, "namespace": trial.NS, "target": CASE.workload,
                 "arm": "liveprobe", "status": "product_observation_only", "baseline": probes(), "restored": probes(),
                 "baseline_image": ORIGINAL, "injected_image": FAULT, "fault_service_probe": probes()[0],
                 "fault_before": {"reason": "ErrImagePull", "initial_error_log": f"ErrImagePull: {FAULT}"}}
    path = tmp_path / "reference.json"
    trial.write_once(path, reference)
    assert trial.load_reference(CASE, path, deployment()) == reference


def test_only_a_model_requested_exact_image_can_change_the_fixture(monkeypatch):
    current = deployment(FAULT)
    monkeypatch.setattr(trial, "get", lambda *_: current)
    patch = Mock()
    lease = Mock()
    monkeypatch.setattr(trial, "patch_image", patch)
    for request in ({"tool": "configure", "key": "replicas", "value": "2"},
                    {"tool": "configure", "key": "image", "value": "untrusted:v1"}):
        assert trial.standalone_operation(CASE, request, deployment(), {}, lease)["accepted"] is False
    lease.require_active.assert_not_called()
    patch.assert_not_called()
    request = {"tool": "configure", "key": "image", "value": ORIGINAL}
    assert trial.standalone_operation(CASE, request, deployment(), {}, lease)["accepted"] is True
    lease.require_active.assert_called_once()
    patch.assert_called_once_with(CASE, current, ORIGINAL)


@pytest.mark.parametrize("mode,expected", [
    ("success", "verified_dev_recovered"),
    ("lost_lease", "lease_not_verified"),
    ("liveprobe_interference", "interfered_by_liveprobe"),
    ("no_action", "unrecovered"),
])
def test_standalone_proof_and_operator_restore_are_independent(tmp_path, monkeypatch, mode, expected):
    baseline = deployment()
    reference = {"incident": {"id": 1649}, "fault_before": {"reason": "ErrImagePull", "initial_error_log": f"ErrImagePull: {FAULT}"}}
    injected = deployment(FAULT)
    receipt, journal = tmp_path / "receipt.json", tmp_path / "active.json"
    fault = {"pod": "inventory-abc-fault", "reason": "ErrImagePull", "initial_error_log": f"ErrImagePull: {FAULT}"}
    restore = Mock(return_value=probes())
    observation = Mock(return_value=[{"id": 1650}] if mode == "liveprobe_interference" else [])
    lease = Mock()
    if mode == "lost_lease":
        lease.require_active.side_effect = [None, trial.Refused("expired")]
        lease.release.side_effect = trial.Refused("not owner")
    monkeypatch.setattr(trial.WorkloadLease, "claim", Mock(return_value=lease))
    monkeypatch.setattr(trial, "patch_image", Mock())
    monkeypatch.setattr(trial, "get", lambda *_: injected)
    monkeypatch.setattr(trial, "ready_endpoints", lambda *_: "old")
    monkeypatch.setattr(trial, "fault_observation", lambda *_: fault)
    monkeypatch.setattr(trial, "probe", lambda *_: probes()[0])
    monkeypatch.setattr(trial, "restore", restore)
    monkeypatch.setattr(trial, "observation", observation)

    def infer(_, result, *__):
        result["pre_cleanup_verified"] = probes()
        if mode != "no_action":
            result["standalone_action"] = {"accepted": True}

    monkeypatch.setattr(trial, "standalone_inference", infer)
    result = trial.standalone_trial(CASE, baseline, probes(), 1649, {}, 30, receipt, journal,
                                    arm="single_terra", reference=reference)
    assert result["status"] == expected
    assert result["restored"] == probes() and receipt.exists()
    restore.assert_called_once_with(CASE, baseline)
    trial.patch_image.assert_called_once_with(CASE, baseline, FAULT)
    assert journal.exists() is (mode == "lost_lease")
