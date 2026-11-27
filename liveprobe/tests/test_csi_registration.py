import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app import db, evidence
from app.config import AppConfig, Settings
from app.engine import Engine
from app.incidents import Signal
from app.models import Action, AgentRun, Evidence, Incident, RoutingDecision
from app.router import decide
from app.workload_signals import CSI_REGISTRATION_AUTHORITY, VOLUME_MOUNT
from conftest import FakeKube, mk_dep, mk_pod, mk_rs, needs_db


@pytest.fixture
def mount_state():
    observed = datetime.now(timezone.utc)
    kube = FakeKube()
    app, ns, claim, volume = "inventory", "dev", "existing-claim", "existing-pv"
    dep = mk_dep(app, 2, "inventory:1", status={"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1})
    dep["metadata"].update(namespace=ns, uid="dep-uid")
    replica = mk_rs(f"{app}-abc", app, 2, "inventory:1", available=0)
    replica["metadata"].update(namespace=ns, uid="rs-uid")
    replica["metadata"]["ownerReferences"][0].update(uid="dep-uid", controller=True)
    pod = mk_pod(f"{app}-abc-1", app, f"{app}-abc", waiting="ContainerCreating", ready=False)
    pod["metadata"].update(namespace=ns, uid="current-pod-uid", creationTimestamp=(observed - timedelta(minutes=4)).isoformat())
    pod["metadata"]["ownerReferences"][0].update(uid="rs-uid", controller=True)
    pod["spec"] = {"nodeName": "worker-a", "containers": [{"name": app, "volumeMounts": [{"name": "data"}]}],
                   "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}
    warn = {"metadata": {"uid": "event-uid"}, "type": "Warning", "reason": VOLUME_MOUNT, "count": 8,
            "source": {"component": "kubelet", "host": "worker-a"},
            "involvedObject": {"kind": "Pod", "namespace": ns, "name": pod["metadata"]["name"],
                               "uid": "current-pod-uid"},
            "firstTimestamp": (observed - timedelta(seconds=160)).isoformat(),
            "lastTimestamp": (observed - timedelta(seconds=30)).isoformat(),
            "message": f'MountVolume.MountDevice failed for volume "{volume}" : kubernetes.io/csi: '
                       'attacher.MountDevice failed to create newCsiDriverClient: driver name '
                       'ebs.csi.aws.com not found in the list of registered CSI drivers'}
    pvc = {"metadata": {"namespace": ns, "name": claim, "uid": "pvc-uid"},
           "spec": {"volumeName": volume}, "status": {"phase": "Bound"}}
    kube.deployments[app] = dep
    kube.replicasets = [replica]
    kube.pods = [pod]
    kube.events = [warn]
    kube.services[app] = {"metadata": {"name": app, "namespace": ns, "uid": "svc-uid"},
                          "spec": {"selector": {"app": app}}}
    kube.endpoints[app] = {"subsets": []}
    kube.get_pvc = Mock(side_effect=lambda *_: deepcopy(pvc))
    return kube, AppConfig(target_namespaces=[ns]), pvc


@pytest.mark.parametrize("variant", ["exact", "wrong_reporter", "wrong_node", "old_pod", "expired_warning", "other_pv",
                                     "unbound_claim", "pvc_denied", "started", "several_replicas", "different_rollout",
                                     "other_error", "healthy_service", "parallel_signal", "past_proof", "fixed_baseline"])
def test_csi_authority_uses_current_attributed_proof(mount_state, variant):
    kube, cfg, pvc = mount_state
    warn, pod = kube.events[0], kube.pods[0]
    reasons = [VOLUME_MOUNT]
    match variant:
        case "wrong_reporter":
            warn["source"]["component"] = "scheduler"
        case "wrong_node":
            warn["source"]["host"] = "different-node"
        case "old_pod":
            warn["involvedObject"]["uid"] = "old-pod-uid"
        case "expired_warning":
            pod["metadata"]["creationTimestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            warn["firstTimestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
            warn["lastTimestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        case "other_pv":
            warn["message"] = warn["message"].replace("existing-pv", "data")
        case "unbound_claim":
            pvc["status"]["phase"] = "Pending"
        case "pvc_denied":
            kube.get_pvc.side_effect = PermissionError("denied")
        case "started":
            pod["status"]["containerStatuses"][0]["restartCount"] = 1
        case "several_replicas":
            kube.deployments["inventory"]["spec"]["replicas"] = 2
        case "different_rollout":
            kube.replicasets[0]["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "1"
        case "other_error":
            other = deepcopy(warn)
            other.update(reason="FailedCreatePodSandBox", message="cni network not ready")
            kube.events.append(other)
        case "healthy_service":
            kube.endpoints["inventory"]["subsets"] = [{"addresses": [{"ip": "10.0.0.12", "targetRef": {
                "kind": "Pod", "uid": "current-pod-uid", "name": pod["metadata"]["name"]}}]}]
        case "parallel_signal":
            reasons.append("TargetProxyListenerUpdateRejected")
    bundle = evidence._collect_kube(kube, cfg, "dev", "inventory", time.monotonic() + 10)
    if variant == "past_proof":
        bundle.deployment["csi_registration_failure"]["observed_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    classifier = Mock(return_value={"level": "COMPLEX", "confidence": 0.9, "rationale": "continue analysis"})
    result = decide(bundle, 15, classifier, cfg, reasons, allow_authority_shortcut=variant != "fixed_baseline")
    if variant == "exact":
        assert result.decided_by == "rules_authority_boundary"
        assert result.features["reason_code"] == CSI_REGISTRATION_AUTHORITY
        proof = result.features["csi_registration_failure"]
        assert (proof["pod_uid"], proof["pvc_uid"], proof["event_uid"]) == ("current-pod-uid", "pvc-uid", "event-uid")
        assert proof["driver"] == "ebs.csi.aws.com" and result.policy["handoff_only"]
        assert next(i.payload for i in bundle.items if i.source == "deployment")["csi_registration_failure"] == proof
        assert any(proof["driver"] in event["message"] for event in bundle.events)
        classifier.assert_not_called()
        kube.get_pvc.assert_called_once_with("dev", "existing-claim")
    else:
        assert result.features.get("reason_code") != CSI_REGISTRATION_AUTHORITY


def test_csi_keeps_core_events_when_optional_pvc_read_is_unavailable(mount_state):
    kube, cfg, _ = mount_state
    published = []
    before_pvc = []

    def unavailable(*_):
        before_pvc.append(published[-1])
        raise TimeoutError

    kube.get_pvc.side_effect = unavailable
    bundle = evidence._collect_kube(kube, cfg, "dev", "inventory", time.monotonic() + 10,
                                   publish=lambda snapshot: published.append(deepcopy(snapshot)))
    checkpoint, = before_pvc
    assert checkpoint.events[0]["reason"] == VOLUME_MOUNT and not checkpoint.deployment.get("csi_registration_failure")
    assert bundle.events[0]["reason"] == VOLUME_MOUNT and not bundle.deployment.get("csi_registration_failure")


@needs_db
def test_csi_authority_persists_evidence_and_skips_diagnostic_calls(db_url, mount_state, monkeypatch):
    kube, cfg, _ = mount_state
    bundle = evidence._collect_kube(kube, cfg, "dev", "inventory", time.monotonic() + 10)
    runner = Engine(cfg, Settings(), kube, SimpleNamespace(name="test"), None)
    monkeypatch.setattr("app.engine.collect", lambda *args, **kwargs: bundle)
    monkeypatch.setattr(Engine, "_classify", Mock(side_effect=AssertionError("unexpected classifier")))
    monkeypatch.setattr(Engine, "_run_decision", Mock(side_effect=AssertionError("unexpected agent group")))
    runner.on_signal(Signal("dev", "Deployment", "inventory", VOLUME_MOUNT, "workload_poller"))
    with db.session() as session:
        iid = session.scalars(select(Incident.id)).one()
    runner.process(iid)
    with db.session() as session:
        incident = session.get(Incident, iid)
        decision = session.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).one()
        assert incident.status == "handed_off" and decision.features["reason_code"] == CSI_REGISTRATION_AUTHORITY
        assert "ebs.csi.aws.com" in incident.summary
        entries = {row.source: row.payload for row in session.scalars(select(Evidence).where(Evidence.incident_id == iid))}
        assert entries["deployment"]["csi_registration_failure"]["pvc_uid"] == "pvc-uid"
        assert entries["k8s_event"][0]["reason"] == VOLUME_MOUNT
        assert session.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid)) is None
        assert session.scalar(select(Action.id).where(Action.incident_id == iid)) is None
