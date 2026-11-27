from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from evaluation import isolated_oom_target as binding, k8s_dev_image_trial as live
from evaluation import k8s_dev_oom_trial as oom, k8s_dev_restart_campaign as campaign
from evaluation import k8s_dev_selector_trial as service, oom_isolated_preflight as preflight


def uid(number):
    return str(UUID(int=number))


@pytest.fixture
def dedicated(tmp_path, monkeypatch):
    monkeypatch.setattr(binding, "_active", None)
    config = tmp_path / "kubeconfig"
    config.write_text("private test config")
    config.chmod(0o600)
    proof = {"node": preflight.NODE, "node_uid": uid(1), "kubernetes": preflight.KUBERNETES,
             "namespace_uid": uid(2), "fault_injections": 0, "gate": {"mem_mi": 1200},
             "business": {name: 200 for name in ("inventory_lookup", "order_lookup", "order_stock_check",
                                                  "dependent_order_check")}, "workloads": {}, "receiver": {"pods": {}}}
    for i, (name, roles) in enumerate(preflight.FIXTURES.items(), 10):
        proof["workloads"][name] = {"service_uid": uid(i * 3), "deployment_uid": uid(i * 3 + 1),
                                   "pod_uid": uid(i * 3 + 2), "pod_name": name + "-pod", "service_spec": {"ports": [8080]},
                                   "deployment_sha256": "a" * 64, "containers": {roles[0]: {"image_id": "sha256:" + "b" * 64}}}
    for i, name in enumerate(("liveprobe-api", "postgres"), 4):
        proof["receiver"]["pods"][name] = {"name": name + "-pinned", "uid": uid(i), "image_id": "sha256:" + "c" * 64}
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(proof))
    return SimpleNamespace(isolated_kubeconfig=config, isolated_proof=proof_path), proof


def mock_transport(dedicated, monkeypatch, *, problem=None):
    args, proof = dedicated
    requests = []

    def run(command, **kwargs):
        requests.append((command, kwargs))
        assert command[:5] == ["kubectl", "--kubeconfig", str(args.isolated_kubeconfig), "--context", binding.CONTEXT]
        command = command[8:]
        if command[:2] == ["config", "view"]:
            value = {"contexts": [{"name": binding.CONTEXT}],
                     "clusters": [{"cluster": {"server": "https://127.0.0.1:16443"}}], "users": [{"user": {}}]}
            if problem == "exec_plugin":
                value["users"][0]["user"]["exec"] = {"command": "unexpected"}
        elif command == ["get", "--raw=/version"]:
            value = {"gitVersion": preflight.KUBERNETES, "platform": "linux/arm64"}
        elif command[:2] == ["get", "nodes"]:
            value = {"items": [{"metadata": {"name": preflight.NODE, "uid": proof["node_uid"]}}]}
        elif command[:3] == ["get", "namespace", "dev"]:
            value = {"metadata": {"uid": uid(3) if problem == "wrong_namespace" else proof["namespace_uid"],
                                  "labels": {"liveprobe.io/evaluation": preflight.MARKER}}}
            fixture_scope = proof.get("fixture_scope")
            if fixture_scope and problem != "scope_label_missing":
                value["metadata"]["labels"][preflight.FIXTURE_SCOPE_LABEL] = fixture_scope
        elif command[:2] == ["get", "deployments"]:
            workloads = preflight.FIXTURES if problem == "unrelated_deployment" else proof["workloads"]
            value = {"items": [{"metadata": {"name": name}} for name in workloads]}
        elif command[:2] == ["get", "pod"]:
            pod, = [item for item in proof["receiver"]["pods"].values() if item["name"] == command[2]]
            value = {"metadata": {"uid": uid(6) if problem == "replaced_pod" else pod["uid"]},
                     "status": {"containerStatuses": [{"ready": True, "imageID": pod["image_id"]}]}}
            retention = pod.get("rag_retention_sha256")
            if retention and problem != "missing_retention_annotation":
                value["metadata"]["annotations"] = {preflight.RETENTION_ANNOTATION: retention}
        elif command[0] == "exec" and command[-1] == "/api/status":
            assert command == list(preflight.product_read_args("liveprobe-api-pinned", "/api/status"))
            value = {"ready": True}
        else:
            value = {"path": command[1], "query": True}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    monkeypatch.setattr(binding.subprocess, "run", run)
    monkeypatch.setattr(live, "run", run)
    return requests


@pytest.mark.parametrize("problem", ["exec_plugin", "wrong_namespace", "replaced_pod"])
def test_transport_refuses_to_bind_when_identity_does_not_match(dedicated, monkeypatch, problem):
    requests = mock_transport(dedicated, monkeypatch, problem=problem)
    with pytest.raises(binding.TargetError):
        binding.bind(dedicated[0])
    assert binding.active() is None and requests
    assert all(argv[8] in ("config", "get") for argv, _ in requests)


def test_half_config_and_replaced_config_have_no_fallback(dedicated, monkeypatch):
    args, _ = dedicated
    requests = mock_transport(dedicated, monkeypatch)
    with pytest.raises(binding.TargetError, match="both"):
        binding.bind(SimpleNamespace(isolated_kubeconfig=args.isolated_kubeconfig, isolated_proof=None))
    assert requests == []
    target = binding.bind(args)
    args.isolated_kubeconfig.write_text("replacement means file identity changed")
    with pytest.raises(binding.TargetError, match="changed"):
        target.prefix()


def test_rag_retention_receipts_are_bound_to_the_extended_receiver_proof(dedicated, monkeypatch):
    args, proof = dedicated
    fingerprint = "e" * 64
    proof["receiver"]["pods"]["liveprobe-api"]["rag_retention_sha256"] = fingerprint
    args.isolated_proof.write_text(json.dumps(proof))
    mock_transport(dedicated, monkeypatch, problem="missing_retention_annotation")
    with pytest.raises(binding.TargetError, match="liveprobe-api Pod changed"):
        binding.bind(args)
    mock_transport(dedicated, monkeypatch)
    target = binding.bind(args)
    assert target.scope()["rag_retention_sha256"] == fingerprint
    changed = {**target.scope(), "rag_retention_sha256": "b" * 64}
    with pytest.raises(binding.TargetError, match="different isolated runtime"):
        target.verify_receipt({"isolated_target": changed})


def test_scope_routes_product_database_leases_gate_and_model_through_same_config(dedicated, monkeypatch):
    requests = mock_transport(dedicated, monkeypatch)
    target = binding.bind(dedicated[0])
    assert live.product_status("/api/status") == {"ready": True}
    assert live.query("SELECT json_build_object('ok',true)")["path"] == "pod/postgres-pinned"
    live.lease_command(oom.load_case("F2-07"), "unlock", "owner", token="only-on-stdin")
    lease_argv, lease_kwargs = requests[-1]
    assert "pod/liveprobe-api-pinned" in lease_argv and "only-on-stdin" not in lease_argv
    assert lease_kwargs["input"] == "only-on-stdin\n"
    live.CHAOS.kubectl("get", "nodes", "-o", "json")
    assert requests[-1][0][8:10] == ["get", "nodes"]
    model = target.model_command("gpt-6.2-sol", "high", oom.MODEL_PROTOCOL)
    assert model[:5] == target.prefix() and model[8:11] == ["exec", "-i", "pod/liveprobe-api-pinned"]
    assert model[-3:] == ["gpt-6.2-sol", "high", "live-repair/v36"]
    argv = campaign.command(campaign.Attempt("F2-07", "liveprobe"), live.EVIDENCE, "output.json")
    assert argv[-4:] == target.cli_flags()


def test_fixture_and_receipt_cannot_cross_namespace_or_dependency(dedicated, monkeypatch):
    mock_transport(dedicated, monkeypatch)
    target = binding.bind(dedicated[0])
    proof = dedicated[1]["workloads"]
    case = oom.load_case("F2-10")
    fixture = proof[case.workload]
    baseline = {**fixture, "endpoint_uid": fixture["pod_uid"]}
    caller = proof[case.dependency_caller]
    baseline["dependency_caller"] = {**caller, "endpoint_uid": caller["pod_uid"]}
    approval = target.approval(case, baseline, fixture["pod_name"])
    assert approval["namespace"] == "dev" and len(approval["fixture_sha256"]) == 64
    drift = deepcopy(baseline)
    drift["dependency_caller"]["endpoint_uid"] = uid(99)
    with pytest.raises(binding.TargetError, match="fixture identity"):
        target.approval(case, drift, fixture["pod_name"])
    with pytest.raises(binding.TargetError, match="different isolated runtime"):
        target.verify_receipt({"isolated_target": {**target.scope(), "namespace_uid": uid(99)}})


@pytest.mark.parametrize(("fixture_scope", "allowed"), [
    ("order", {"F2-07", "F2-15"}), ("inventory", {"F2-10", "F2-13"}),
])
def test_narrow_proof_limits_the_campaign_to_its_workloads(dedicated, monkeypatch, fixture_scope, allowed):
    args, proof = dedicated
    proof["fixture_scope"] = fixture_scope
    proof["workloads"] = {name: proof["workloads"][name] for name in preflight.FIXTURE_SCOPES[fixture_scope]}
    proof["business"] = dict.fromkeys(preflight.business_requests(fixture_scope), 200)
    args.isolated_proof.write_text(json.dumps(proof))

    for problem in ("scope_label_missing", "unrelated_deployment"):
        mock_transport(dedicated, monkeypatch, problem=problem)
        with pytest.raises(binding.TargetError, match="live dev fixture scope"):
            binding.bind(args)
    mock_transport(dedicated, monkeypatch)
    target = binding.bind(args)
    assert target.case_ids == allowed
    assert target.scope()["fixture_scope"] == fixture_scope
    for case_id in allowed:
        campaign.command(campaign.Attempt(case_id, "liveprobe"), live.EVIDENCE, "output.json")
    for case_id in binding.CASES - allowed:
        with pytest.raises(binding.TargetError, match="outside the admitted isolated fixture scope"):
            campaign.command(campaign.Attempt(case_id, "liveprobe"), live.EVIDENCE, "output.json")


def test_no_endpoints_probe_pins_isolated_args(dedicated, monkeypatch):
    mock_transport(dedicated, monkeypatch)
    target = binding.bind(dedicated[0])
    case = oom.load_case("F2-07")

    def raw(command, **kwargs):
        assert command[:5] == target.prefix()
        return SimpleNamespace(returncode=1, stderr="Error from server (ServiceUnavailable): " + sorted(case.errors)[0])

    monkeypatch.setattr(service, "run", raw)
    assert service.proxy_error(case) in case.errors


def test_sdk_leases_use_explicit_config_and_reject_fixture_mutations(dedicated, monkeypatch):
    from kubernetes import config

    mock_transport(dedicated, monkeypatch)
    target = binding.bind(dedicated[0])
    connections = []

    def connect(**opts):
        connections.append(opts)
        return SimpleNamespace(sanitize_for_serialization=lambda item: item)

    monkeypatch.setattr(config, "new_client_from_config", connect)
    guard = oom.authority()
    assert connections == [{"context": binding.CONTEXT, "config_file": str(target.kubeconfig),
                            "persist_config": False}] * 2
    assert guard.transport.lease_namespace == "dev"
    with pytest.raises(oom.family2.tx.UnsafeState, match="object patch scope invalid"):
        guard.transport.patch(*oom.family2.tx.OBJECTS[0], [])


@pytest.mark.parametrize("ready", [False, True])
def test_isolated_model_readiness_does_not_inject_or_accept_transport_failure(dedicated, monkeypatch, tmp_path, ready):
    mock_transport(dedicated, monkeypatch)
    target = binding.bind(dedicated[0])
    calls = []

    class Session:
        def __init__(self, model, effort, protocol, *, command, timeout):
            assert command[:5] == target.prefix() and "pod/liveprobe-api-pinned" in command
            assert protocol == oom.MODEL_PROTOCOL

        def turn(self, model, effort, initial, history):
            assert not history and "No incident or action" in initial
            calls.append(model)
            return {"status": "ok" if ready else "transport_error", "model": model, "effort": effort}

        def close(self):
            pass

    monkeypatch.setattr(oom, "RemoteModelSession", Session)
    if ready:
        oom.check_isolated_model(target, "liveprobe", tmp_path)
        oom.check_isolated_model(target, "liveprobe", tmp_path)
        record, = tmp_path.glob("model-ready-*.json")
        target.verify_receipt(json.loads(record.read_text()))
    else:
        with pytest.raises(live.Refused, match="before the fault"):
            oom.check_isolated_model(target, "liveprobe", tmp_path)
        assert not list(tmp_path.glob("model-ready-*.json"))
    assert len(calls) == 1
