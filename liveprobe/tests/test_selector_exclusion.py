import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app import db, evidence, safety, selector_exclusion
from app.config import AppConfig, Settings
from app.engine import Engine
from app.incidents import Signal
from app.models import Action, AgentRun, Evidence, Incident, ReviewLedger, RoutingDecision, WorkflowRun
from conftest import FakeKube, mk_dep, mk_pod, mk_rs, needs_db


def selector_fixture(name="inventory", *, namespace="dev", selector=None, service_uid="service-1", pod_uid="pod-1"):
    kube = FakeKube()
    labels = {"app": name, "liveprobe.io/managed": "true", "app.kubernetes.io/part-of": "samsung-trade"}
    selector = selector or {**labels, "app": f"{name}-moved"}
    # The proof always needs the Deployment and Pod identities, independent of the Service name alone.
    pod_labels = {**selector, **labels}
    deployment = mk_dep(name, 2, "app:v1", replicas=1)
    deployment["metadata"].update(namespace=namespace, uid="dep-1", labels=labels)
    deployment["spec"]["template"]["metadata"]["labels"] = pod_labels
    replica = mk_rs(f"{name}-abc", name, 2, "app:v1")
    replica["metadata"].update(namespace=namespace, uid="rs-1")
    replica["metadata"]["ownerReferences"][0].update(uid="dep-1", controller=True)
    pod = mk_pod(f"{name}-abc-1", name, f"{name}-abc")
    pod["metadata"].update(namespace=namespace, uid=pod_uid, labels=pod_labels)
    pod["metadata"]["ownerReferences"][0].update(uid="rs-1", controller=True)
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    kube.deployments[name], kube.replicasets, kube.pods = deployment, [replica], [pod]
    kube.services[name] = {"metadata": {"name": name, "namespace": namespace, "uid": service_uid,
                                        "resourceVersion": "321", "labels": labels},
                           "spec": {"type": "ClusterIP", "clusterIP": "10.0.0.1", "selector": selector}}
    kube.endpoints[name] = {"subsets": []}
    return kube, AppConfig(target_namespaces=[namespace])


@pytest.mark.parametrize("variant", ["exact", "unready", "unmanaged", "old_pod_owner", "extra_matching_pod",
                                     "rolling", "other_system", "active_backend", "endpoint_unknown", "headless",
                                     "other_selector_diff", "no_resource_version"])
def test_evidence_proves_only_ready_managed_selector_exclusion(variant):
    kube, cfg = selector_fixture()
    dep, svc, pod = kube.deployments["inventory"], kube.services["inventory"], kube.pods[0]
    match variant:
        case "unready":
            pod["status"]["conditions"][0]["status"] = "False"
        case "unmanaged":
            svc["metadata"]["labels"] = {"app.kubernetes.io/part-of": "samsung-trade"}
        case "old_pod_owner":
            pod["metadata"]["ownerReferences"][0]["uid"] = "old-rs"
        case "extra_matching_pod":
            other = deepcopy(pod)
            other["metadata"].update(ownerReferences=[], name="other-pod", uid="other-1")
            kube.pods.append(other)
        case "rolling":
            dep["status"]["updatedReplicas"] = 0
        case "other_system":
            svc["metadata"]["labels"] = {**svc["metadata"]["labels"], "app.kubernetes.io/part-of": "other-system"}
        case "active_backend":
            kube.endpoints["inventory"]["subsets"] = [{"addresses": [{"ip": "10.20.30.40"}]}]
        case "endpoint_unknown":
            kube.endpoints.clear()
        case "headless":
            svc["spec"]["clusterIP"] = "None"
        case "other_selector_diff":
            svc["spec"]["selector"]["pool"] = "only-a-subset"
        case "no_resource_version":
            del svc["metadata"]["resourceVersion"]
    bundle = evidence._collect_kube(kube, cfg, "dev", "inventory", time.monotonic() + 10)
    proof = bundle.service.get(selector_exclusion.PROOF_KEY)
    assert bool(proof) == (variant == "exact")
    if proof:
        assert proof["selector_app"] == "inventory-moved" and proof["pod_app"] == "inventory"
        assert proof["ready_pods"] == [{"name": "inventory-abc-1", "uid": "pod-1"}]
        assert next(i.payload for i in bundle.items if i.source == "service")[selector_exclusion.PROOF_KEY] == proof


@pytest.mark.parametrize("variant", ["same", "service_replaced", "pod_replaced", "freshly_ready", "write_during_read"])
def test_recheck_refuses_changed_resource_identity_or_health(variant):
    kube, cfg = selector_fixture()
    initial = evidence._collect_kube(kube, cfg, "dev", "inventory", time.monotonic() + 10).service[selector_exclusion.PROOF_KEY]
    if variant == "service_replaced":
        kube.services["inventory"]["metadata"]["uid"] = "replaced-service"
    if variant == "pod_replaced":
        kube.pods[0]["metadata"]["uid"] = "replaced-pod"
    if variant == "freshly_ready":
        kube.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "10.20.30.40"}]}]}
    if variant == "write_during_read":
        first = kube.get_service("dev", "inventory")
        changed = deepcopy(first)
        changed["metadata"]["resourceVersion"] = "322"
        kube.get_service = Mock(side_effect=[first, changed])
    result = selector_exclusion.recheck(kube, "dev", "inventory", initial, managed=cfg.safe_actions.managed_label)
    assert bool(result) == (variant == "same")


@needs_db
@pytest.mark.parametrize("variant", ["verified", "concurrent_risk", "poc_autonomy"])
def test_selector_handoff_persists_independent_proof_and_preserves_changed_signal(db_url, monkeypatch, variant):
    kube, cfg = selector_fixture()
    runner = Engine(cfg, Settings(), kube, SimpleNamespace(name="test"), None)
    monkeypatch.setattr(safety, "high_risk_may_execute", lambda: variant == "poc_autonomy")
    monkeypatch.setattr("app.engine.collect", lambda *a, **k: evidence._collect_kube(
        kube, cfg, "dev", "inventory", time.monotonic() + 10))
    monkeypatch.setattr(Engine, "_classify", Mock(side_effect=AssertionError("unexpected classifier")))
    runner.on_signal(Signal("dev", "Service", "inventory", "NoEndpoints", "endpoint_poller", {"empty_for_s": 600}))
    with db.session() as session:
        iid = session.scalars(select(Incident.id)).one()
    if variant == "verified":
        monkeypatch.setattr(Engine, "_run_decision", Mock(side_effect=AssertionError("unexpected model workflow")))
    else:
        check = selector_exclusion.recheck

        def add_risk(*args, **kwargs):
            new = check(*args, **kwargs)
            assert new
            with db.session() as session:
                inc = session.get(Incident, iid)
                inc.signal_reasons = [*inc.signal_reasons, "NetworkDrop"]
            return new

        def continue_normal(self, iid, decision, bundle, incident, strategy, arm):
            assert decision.level == "HIGH_RISK"
            assert ("NetworkDrop" in incident["signals"]) == (variant == "concurrent_risk")
            self._set_status(iid, "handed_off", status_reason="existing workflow")

        monkeypatch.setattr(selector_exclusion, "recheck", add_risk)
        monkeypatch.setattr(Engine, "_run_decision", continue_normal)
    runner.process(iid)
    with db.session() as session:
        inc = session.get(Incident, iid)
        route = session.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).one()
        assert inc.status == "handed_off" and route.level == "HIGH_RISK"
        assert session.scalar(select(AgentRun.id).where(AgentRun.incident_id == iid)) is None
        assert session.scalar(select(Action.id).where(Action.incident_id == iid)) is None
        stored = list(session.scalars(select(Evidence).where(Evidence.incident_id == iid, Evidence.source == "service")
                                      .order_by(Evidence.id)))
        if variant == "verified":
            assert route.features["reason_code"] == selector_exclusion.AUTHORITY
            assert route.policy["verification_method"] == "kubernetes_reread"
            assert len(stored) == 2 and stored[-1].payload["selector_recheck"]["ready_endpoints"] == 0
            assert "inventory-moved" in inc.summary
            run = session.scalars(select(WorkflowRun).where(WorkflowRun.incident_id == iid)).one()
            assert run.status == "succeeded"
            ledger = session.scalars(select(ReviewLedger).where(ReviewLedger.incident_id == iid)).one()
            assert ledger.reason_code == "high_risk_level" and ledger.outcome == "held_for_operator"
        else:
            assert route.features.get("reason_code") != selector_exclusion.AUTHORITY and len(stored) == 1
