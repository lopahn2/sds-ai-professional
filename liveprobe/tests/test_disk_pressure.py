"""Tie disk-pressure coverage to the actual watcher, authority and operator views."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app.config import AppConfig, Settings
from app.engine import Engine
from app.flowgraph import _evidence_card
from app.incidents import DISK_PRESSURE_EVICTION, DISK_PRESSURE_SOURCE, Signal, fingerprint, ingest
from app.models import Action, AgentRun, Evidence, Incident, RoutingDecision
from app.safety import needs_cluster_authority
from app.watcher import Watchers
from conftest import needs_db


@pytest.fixture
def observation():
    now = datetime.now(timezone.utc)
    at = (now - timedelta(seconds=5)).isoformat()
    created = (now - timedelta(minutes=5)).isoformat()
    pod = {"metadata": {"namespace": "dev", "name": "falling", "uid": "pod-uid", "creationTimestamp": created,
                        "ownerReferences": [{"kind": "ReplicaSet", "controller": True, "name": "app-rs", "uid": "rs-uid"}]},
           "spec": {"nodeName": "worker-a"}, "status": {"phase": "Failed", "reason": "Evicted"}}
    node = {"metadata": {"name": "worker-a", "uid": "node-uid", "creationTimestamp": created},
            "spec": {"taints": [{"key": "node.kubernetes.io/disk-pressure", "effect": "NoSchedule"}]},
            "status": {"conditions": [{"type": "DiskPressure", "status": "True", "lastTransitionTime": at}]}}
    owner = {"metadata": {"uid": "rs-uid", "ownerReferences": [
        {"kind": "Deployment", "controller": True, "name": "app", "uid": "dep-uid"}]}}
    dep = {"metadata": {"namespace": "dev", "name": "app", "uid": "dep-uid", "generation": 2},
           "spec": {"replicas": 1}, "status": {"observedGeneration": 2}}
    event = {"metadata": {"namespace": "dev", "uid": "event-uid"}, "type": "Warning", "reason": "Evicted",
             "involvedObject": {"kind": "Pod", "namespace": "dev", "name": "falling", "uid": "pod-uid"},
             "source": {"component": "kubelet", "host": "worker-a"}, "lastTimestamp": at,
             "message": "The node was low on resource: ephemeral-storage. Threshold quantity: 4Gi, available: 2Gi. "}
    kube = SimpleNamespace(list_pods=Mock(return_value=[pod]), list_services=Mock(return_value=[]),
                           get_node=Mock(return_value=node), get_replicaset=Mock(return_value=owner),
                           get_deployment=Mock(return_value=dep))
    return SimpleNamespace(kube=kube, now=now, event=event, pod=pod, node=node, owner=owner, deployment=dep)


def test_watch_waits_for_disk_pressure_then_records_affected_deployment_once(observation):
    cfg = AppConfig(target_namespaces=["dev"], historical_namespaces=["prd"])
    emitted = []
    watcher = Watchers(observation.kube, cfg, emitted.append)
    condition, = observation.node["status"]["conditions"]
    condition["status"] = "False"
    watcher.handle_event(observation.event)
    watcher.poll_endpoints_once()
    assert not emitted
    condition["status"] = "True"
    watcher.poll_endpoints_once()
    watcher.handle_event(observation.event)
    watcher.poll_endpoints_once()
    signal, = emitted
    assert (signal.namespace, signal.kind, signal.name, signal.reason, signal.source) == (
        "dev", "Deployment", "app", DISK_PRESSURE_EVICTION, DISK_PRESSURE_SOURCE)
    assert signal.detail["node_uid"] == "node-uid" and signal.detail["disk_pressure_taint"]
    assert signal.detail["pod_uid"] == "pod-uid" and signal.detail["event_uid"] == "event-uid"
    assert (signal.detail["ready_replicas"], signal.detail["desired_replicas"]) == (0, 1)
    assert observation.kube.get_node.call_count == 2


@pytest.mark.parametrize("case", ["stale", "untrusted", "pod_limit", "other_namespace", "other_node",
                                  "recreated_node", "new_pod_uid", "replicaset_replaced", "deployment_replaced"])
def test_disk_pressure_rejects_unrelated_or_stale_evidence(observation, case):
    event = observation.event
    if case == "stale":
        event["lastTimestamp"] = (observation.now - timedelta(seconds=121)).isoformat()
    elif case == "untrusted":
        event["source"]["component"] = "scheduler"
    elif case == "pod_limit":
        event["message"] = "Pod ephemeral local storage usage exceeds the total limit of containers."
    elif case == "other_namespace":
        event["metadata"]["namespace"] = event["involvedObject"]["namespace"] = "prd"
    elif case == "other_node":
        observation.pod["spec"]["nodeName"] = "worker-b"
    elif case == "recreated_node":
        observation.node["metadata"]["creationTimestamp"] = observation.now.isoformat()
    elif case == "new_pod_uid":
        observation.pod["metadata"]["uid"] = "replacement"
    elif case == "replicaset_replaced":
        observation.owner["metadata"]["uid"] = "different-rs"
    elif case == "deployment_replaced":
        observation.deployment["metadata"]["uid"] = "different-deployment"
    emit = Mock()
    watcher = Watchers(observation.kube, AppConfig(target_namespaces=["dev"]), emit)
    watcher.handle_event(event)
    watcher.poll_endpoints_once()
    emit.assert_not_called()


def test_disk_pressure_read_failure_does_not_stop_service_polling(observation):
    observation.kube.get_node.side_effect = PermissionError("unobserved")
    emit = Mock()
    watcher = Watchers(observation.kube, AppConfig(target_namespaces=["dev"]), emit)
    watcher.handle_event(observation.event)
    watcher.poll_endpoints_once()
    assert watcher.status["disk_pressure_poller/dev"]["state"] == "degraded"
    emit.assert_not_called()
    observation.kube.list_services.assert_called_once_with("dev")


@needs_db
def test_disk_pressure_handoff_preserves_evidence_without_agents_or_cluster_actions(db_url, observation, monkeypatch):
    from app import db

    cfg = AppConfig(target_namespaces=["dev"])
    runner = Engine(cfg, Settings(), observation.kube, SimpleNamespace(name="test"), None)
    monkeypatch.setattr(runner, "_collect_or_reuse", lambda *_: pytest.fail("authority handoff uses the captured evidence"))
    emitted = []
    watcher = Watchers(observation.kube, cfg, emitted.append)
    watcher.handle_event(observation.event)
    watcher.poll_endpoints_once()
    signal, = emitted
    with db.session() as s:
        inc, outcome = ingest(s, cfg, signal, observation.now)
        assert outcome == "created"
        iid = inc.id
    runner.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == "handed_off" and inc.workload == "app" and needs_cluster_authority(inc)
        routing = s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).one()
        assert routing.features["reason_code"] == "node_disk_pressure_cluster_authority"
        evidence = s.scalars(select(Evidence).where(Evidence.incident_id == iid)).one()
        assert evidence.target == "Node/worker-a" and evidence.payload == signal.detail
        card, = _evidence_card([evidence], [])["items"]
        assert "준비 복제본 0/1" in card["summary_ko"] and "업무 요청 영향은 미관측" in card["summary_ko"]
        assert s.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid)) is None
        assert s.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        repeat, outcome = ingest(s, cfg, signal, observation.now + timedelta(seconds=90))
        assert outcome == "suppressed" and repeat.id == iid
    next_pressure = Signal(signal.namespace, signal.kind, signal.name, signal.reason, signal.source,
                           {**signal.detail, "disk_pressure_since": observation.now.isoformat()})
    assert fingerprint(next_pressure, cfg)[0] != fingerprint(signal, cfg)[0]
    assert fingerprint(Signal("dev", "Deployment", "app", "CrashLoopBackOff", "pod_watch"), cfg)[0] != fingerprint(signal, cfg)[0]
