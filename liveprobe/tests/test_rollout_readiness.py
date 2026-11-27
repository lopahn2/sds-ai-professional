"""Correlate a readiness-only rollout regression with current Pod and event provenance."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.config import AppConfig, Settings
from app.engine import Engine
from app.evidence import _collect_kube
from app.incidents import Signal, ingest
from app.models import Action, AgentRun, Incident, RoutingDecision
from app.rollout_readiness import READINESS_404_WARNING, READINESS_PATH_AUTHORITY
from app.router import decide
from conftest import needs_db


def rollout_fixture(old_spec, failed_path, *, namespace="dev", name="app", deployment_uid="dep-uid",
                    old_pod_uid="old-pod-uid", new_pod_uid="new-pod-uid", new_rs_name=None):
    now = datetime.now(timezone.utc)
    annotations = "deployment.kubernetes.io/revision"
    new_spec = deepcopy(old_spec)
    new_spec["containers"][0]["readinessProbe"]["httpGet"]["path"] = failed_path
    container = new_spec["containers"][0]["name"]
    labels = {"app": name}
    old_rs_name, new_rs_name = name + "-previous", new_rs_name or name + "-current"
    node = "worker-a"

    def replica_set(rs, uid, revision, spec):
        return {
            "metadata": {"namespace": namespace, "name": rs, "uid": uid,
                         "annotations": {annotations: str(revision)}, "ownerReferences": [
                             {"kind": "Deployment", "controller": True, "name": name, "uid": deployment_uid}]},
            "spec": {"template": {"metadata": {"labels": {**labels, "pod-template-hash": uid}}, "spec": spec}},
        }

    def pod(rs, uid, spec, ready):
        return {
            "metadata": {"name": rs["metadata"]["name"] + "-pod", "uid": uid, "namespace": namespace,
                         "labels": deepcopy(labels), "creationTimestamp": (now - timedelta(seconds=60)).isoformat(),
                         "ownerReferences": [{"kind": "ReplicaSet", "controller": True,
                                              "name": rs["metadata"]["name"], "uid": rs["metadata"]["uid"]}]},
            "spec": {**deepcopy(spec), "nodeName": node},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [{"name": container, "image": spec["containers"][0]["image"],
                                              "ready": ready, "state": {"running": {}}}]},
        }

    dep = {
        "metadata": {"name": name, "namespace": namespace, "uid": deployment_uid,
                     "generation": 2, "annotations": {annotations: "2"}},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": new_spec}},
        "status": {"observedGeneration": 2, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 2,
                   "conditions": [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}]},
    }
    old, new = replica_set(old_rs_name, "rs-old", 1, old_spec), replica_set(new_rs_name, "rs-new", 2, new_spec)
    old_pod, new_pod = pod(old, old_pod_uid, old_spec, True), pod(new, new_pod_uid, new_spec, False)
    event = {
        "metadata": {"uid": "warning-uid", "namespace": namespace}, "reason": "Unhealthy", "type": "Warning",
        "source": {"component": "kubelet"}, "count": 2, "message": READINESS_404_WARNING,
        "lastTimestamp": (now - timedelta(seconds=4)).isoformat(),
        "involvedObject": {"kind": "Pod", "name": new_pod["metadata"]["name"], "namespace": namespace,
                           "uid": new_pod_uid, "fieldPath": f"spec.containers{{{container}}}"},
    }
    kube = SimpleNamespace(
        list_pods=Mock(return_value=[old_pod, new_pod]), list_replicasets=Mock(return_value=[old, new]),
        get_deployment=Mock(return_value=dep), get_service=Mock(return_value=None),
        list_events=Mock(return_value=[event]), get_node=Mock(return_value={"metadata": {"name": node}}),
    )
    return SimpleNamespace(kube=kube, deployment=dep, old_pod=old_pod, new_pod=new_pod, old=old,
                           new=new, event=event, namespace=namespace, name=name, now=now)


@pytest.fixture
def rollout():
    return rollout_fixture({"containers": [{"name": "app", "image": "local/app:v1",
                                           "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}},
                                           "env": [{"name": "SYNTHETIC_FLAG", "value": "private-test-value"}]}]},
                           "/wrong")


@pytest.mark.parametrize("variant", ["matching", "other_field", "other_image", "wrong_container", "stale", "forged",
                                    "wrong_pod", "replaced_owner", "old_unready", "generation", "new_pod_changed",
                                    "single_404", "network_risk", "mixed_error", "fixed_baseline"])
def test_readiness_handoff_requires_scoped_observations(rollout, variant):
    event, kube = rollout.event, rollout.kube
    if variant == "other_field":
        rollout.old["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "different-private-value"
    elif variant == "other_image":
        rollout.old["spec"]["template"]["spec"]["containers"][0]["image"] = "local/app:v0"
    elif variant == "wrong_container":
        event["involvedObject"]["fieldPath"] = "spec.containers{other}"
    elif variant == "stale":
        event["lastTimestamp"] = (rollout.now - timedelta(minutes=3)).isoformat()
    elif variant == "forged":
        event["source"]["component"] = "other"
    elif variant == "wrong_pod":
        event["involvedObject"]["uid"] = "other-pod-uid"
    elif variant == "replaced_owner":
        rollout.old_pod["metadata"]["ownerReferences"][0]["uid"] = "stale-replicaset"
    elif variant == "old_unready":
        rollout.old_pod["status"]["conditions"][0]["status"] = "False"
    elif variant == "generation":
        rollout.deployment["status"]["observedGeneration"] = 1
    elif variant == "new_pod_changed":
        rollout.new_pod["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = "/actual"
    elif variant == "single_404":
        event["count"] = 1
    cfg = AppConfig(target_namespaces=["dev"])
    bundle = _collect_kube(kube, cfg, "dev", "app", monotonic() + 10)
    signals = ["ProgressDeadlineExceeded"]
    if variant == "network_risk":
        signals.append("NetworkDrop")
    if variant == "mixed_error":
        bundle.logs = ["ID lookup failed"]
    classify = Mock(return_value={"level": "COMPLEX", "rationale": "additional analysis", "confidence": 1.0})
    result = decide(bundle, 15, classify, cfg, signals, allow_authority_shortcut=variant != "fixed_baseline")
    if variant == "matching":
        classify.assert_not_called()
        assert result.decided_by == "rules_authority_boundary" and result.policy["handoff_only"]
        assert result.features["reason_code"] == READINESS_PATH_AUTHORITY
        proof = bundle.deployment["rollout_readiness_failure"]
        assert proof["pods"] == [{"pod": rollout.new_pod["metadata"]["name"],
                                  "pod_uid": "new-pod-uid", "event_uid": "warning-uid"}]
        assert proof["http_status"] == 404 and proof["old_ready"] == 1
        assert "private-test-value" not in repr(proof) and "/wrong" not in repr(proof)
        kube.list_events.assert_called_once_with("dev")
        kube.get_deployment.assert_called_once_with("dev", "app")
    else:
        assert result.decided_by != "rules_authority_boundary"


@needs_db
def test_readiness_handoff_records_observed_cause_without_model_or_write(db_url, rollout, monkeypatch):
    from app import db, engine as engine_module
    cfg = AppConfig(target_namespaces=["dev"])
    bundle = _collect_kube(rollout.kube, cfg, "dev", "app", monotonic() + 10)
    engine = Engine(cfg, Settings(), rollout.kube, SimpleNamespace(name="unexpected-call"), None)
    monkeypatch.setattr(engine_module, "collect", lambda *_args, **_kwargs: bundle)
    with db.session() as session:
        incident, _ = ingest(session, cfg, Signal("dev", "Deployment", "app", "ProgressDeadlineExceeded", "workload_poller"))
        iid = incident.id
    engine.process(iid)
    with db.session() as session:
        inc = session.get(Incident, iid)
        assert inc.status == "handed_off" and inc.handoff["handoff_classification"]["reason_code"] == READINESS_PATH_AUTHORITY
        assert "HTTP 404" in inc.summary and "app-previous" in inc.handoff["recommended_steps"][0]
        route = session.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).one()
        assert route.policy["handoff_only"] is True
        assert session.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid)) is None
        assert session.scalar(select(Action.id).where(Action.incident_id == iid)) is None
