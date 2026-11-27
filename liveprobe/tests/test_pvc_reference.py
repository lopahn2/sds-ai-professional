from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from app import actions, pvc_reference as pvc
from app.config import AppConfig


def test_kw03_uses_a_single_guarded_deployment_change(monkeypatch):
    target = {"metadata": {"name": pvc.DEPLOYMENT, "namespace": pvc.NAMESPACE, "uid": "owned", "resourceVersion": "7",
                          "generation": 4, "labels": {"liveprobe.io/managed": "true", "app.kubernetes.io/part-of": pvc.SYSTEM}},
              "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app.kubernetes.io/part-of": pvc.SYSTEM}},
                       "spec": {"volumes": [{"name": pvc.VOLUME,
                                            "persistentVolumeClaim": {"claimName": pvc.BROKEN, "readOnly": True}}]}}}}
    changes = []
    kube = SimpleNamespace(get_deployment=lambda *_: target, patch_deployment_json=lambda *args: changes.append(args))
    config = AppConfig(target_namespaces=["dev", "prd"], allowlist=["pvc_reference_restore"],
                       safe_actions={"kw03_ready_pv": "pvc-candidate"})
    bind = {pvc.READY: {"pv_name": "pvc-candidate"}, pvc.BROKEN: {"pv_name": pvc.BROKEN_PV}}
    monkeypatch.setattr(pvc, "bindings", lambda *_: bind)
    monkeypatch.setattr(pvc, "failing_pod", lambda *_: "pending-pod")
    valid = actions.validate(config, kube, "pvc_reference_restore", "dev", "Deployment", pvc.DEPLOYMENT, {})
    assert valid.fingerprint["bindings"] == bind
    assert actions.apply(kube, valid) == {"claim": pvc.READY}
    namespace, deployment, ops = changes.pop()
    assert (namespace, deployment) == ("dev", pvc.DEPLOYMENT)
    assert [op for op in ops if op["op"] != "test"] == [{"op": "replace", "path": pvc.POINTER, "value": pvc.READY}]
    with pytest.raises(actions.Denied):
        actions.validate(config, kube, "pvc_reference_restore", "prd", "Deployment", pvc.DEPLOYMENT, {})
    target["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"] = False
    with pytest.raises(actions.Denied):
        actions.validate(config, kube, "pvc_reference_restore", "dev", "Deployment", pvc.DEPLOYMENT, {})


def test_binding_proof_rejects_a_changed_claim_uid():
    ready_pv = "pvc-candidate"
    claims = {}
    volumes = {}
    for claim, volume in ((pvc.READY, ready_pv), (pvc.BROKEN, pvc.BROKEN_PV)):
        claims[claim] = {"metadata": {"namespace": "dev", "name": claim, "uid": claim,
                                      "labels": {"app": pvc.DEPLOYMENT, "app.kubernetes.io/part-of": pvc.SYSTEM}},
                         "spec": {"volumeName": volume}, "status": {"phase": "Bound"}}
        volumes[volume] = {"metadata": {"name": volume, "uid": volume}, "status": {"phase": "Bound"},
                           "spec": {"claimRef": {"namespace": "dev", "name": claim, "uid": claim}}}
    kube = SimpleNamespace(get_pvc=lambda ns, name: claims[name], get_pv=volumes.get)
    assert pvc.bindings(kube, ready_pv)
    claims[pvc.READY] = deepcopy(claims[pvc.READY])
    claims[pvc.READY]["metadata"]["uid"] = "recreated"
    assert pvc.bindings(kube, ready_pv) is None


def test_mount_finding_names_both_claims_and_requires_current_kubelet_warning():
    now = datetime.now(timezone.utc)

    def template(claim):
        volume = {"name": pvc.VOLUME, "persistentVolumeClaim": {"claimName": claim, "readOnly": True}}
        return {"volumes": [volume], "containers": [{"name": "verification", "volumeMounts": [{"name": pvc.VOLUME}]}]}

    def owner(kind, name, uid):
        return [{"kind": kind, "name": name, "uid": uid, "controller": True}]

    md = {"namespace": "dev", "name": pvc.DEPLOYMENT, "uid": "target", "generation": 4,
          "annotations": {"deployment.kubernetes.io/revision": "2"}}
    deployment = {"metadata": md, "spec": {"replicas": 1, "template": {"spec": template(pvc.BROKEN)}},
                  "status": {"availableReplicas": 1, "observedGeneration": 4}}
    replicasets = []
    for name, revision, claim in (("ready-rs", "1", pvc.READY), ("failing-rs", "2", pvc.BROKEN)):
        replicasets.append({"metadata": {"name": name, "uid": name, "ownerReferences": owner("Deployment", pvc.DEPLOYMENT, "target"),
                                      "annotations": {"deployment.kubernetes.io/revision": revision}},
                            "spec": {"template": {"spec": template(claim)}}, "status": {"availableReplicas": int(revision == "1")}})
    pod = {"metadata": {"name": "pending", "namespace": "dev", "uid": "current-pod",
                         "creationTimestamp": (now - timedelta(seconds=140)).isoformat(),
                         "ownerReferences": owner("ReplicaSet", "failing-rs", "failing-rs")},
           "spec": {**template(pvc.BROKEN), "nodeName": "existing-node"}, "status": {"phase": "Pending"}}
    event = {"type": "Warning", "reason": "FailedMount", "source": {"component": "kubelet"},
             "involvedObject": {"kind": "Pod", "namespace": "dev", "name": "pending", "uid": "current-pod"},
             "firstTimestamp": (now - timedelta(seconds=132)).isoformat(), "lastTimestamp": now.isoformat(), "count": 3,
             "message": f'MountVolume.NewMounter initialization failed for volume "{pvc.BROKEN_PV}" : path "/fixture" does not exist'}
    proof = pvc.mount_failure(deployment, replicasets, [pod], [event], now)
    assert proof["failing_revision"]["claim"] == pvc.BROKEN
    assert proof["previous_ready_revision"]["claim"] == pvc.READY
    event["involvedObject"]["uid"] = "historical-pod"
    assert pvc.mount_failure(deployment, replicasets, [pod], [event], now) is None


def test_kw03_admission_and_storage_permissions_are_explicit_and_read_only():
    script = Path(__file__).resolve().parents[2] / "scripts/render-p18-actions.py"
    render = runpy.run_path(str(script))["render"]
    assert not any("kw03" in obj["metadata"]["name"] for obj in render())
    pv_name = "pvc-abcdef12-1234-1234-1234-1234567890ab"
    docs = render(pv_name)
    scope = next(doc for doc in docs if doc["kind"] == "ValidatingAdmissionPolicy" and doc["metadata"]["name"] == "safe-action-executor-bounds")
    assert "request.namespace == 'dev'" in scope["spec"]["variables"][0]["expression"]
    assert "lp-eval-kw03-ready" in scope["spec"]["validations"][-1]["expression"]
    roles = {doc["metadata"]["name"]: doc["rules"][0] for doc in docs
             if doc["kind"] in ("Role", "ClusterRole") and doc["metadata"]["name"].startswith("liveprobe-kw03-")}
    assert roles["liveprobe-kw03-deployment"]["resourceNames"] == [pvc.DEPLOYMENT]
    assert roles["liveprobe-kw03-pv"]["resourceNames"] == [pv_name, pvc.BROKEN_PV]
    assert roles["liveprobe-kw03-pvc"]["verbs"] == roles["liveprobe-kw03-pv"]["verbs"] == ["get"]
