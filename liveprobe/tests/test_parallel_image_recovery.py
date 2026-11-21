"""Drive the production intake and rollback path while a different investigation stays busy."""
from datetime import datetime, timezone
import os
import threading
import time

from sqlalchemy import select

from app import app_errors, db, evidence, rollback_grant
from app.config import AppConfig, Settings
from app.engine import Engine
from app.incidents import Signal
from app.llm.base import LLMResult, Usage
from app.models import Action, AgentRun, Incident
from conftest import family1_kube, mk_dep, mk_pod, mk_rs
from test_core import _answer


def test_busy_service_does_not_delay_verified_image_rollback(db_url, monkeypatch):
    busy, release = threading.Event(), threading.Event()
    kube = family1_kube(created_bad=datetime.now(timezone.utc).isoformat())
    kube.deployments["catalog"] = mk_dep("catalog", 1, "catalog:dev")
    kube.replicasets.append(mk_rs("catalog-last", "catalog", 1, "catalog:dev"))
    kube.pods.append(mk_pod("catalog-last-1", "catalog", "catalog-last", ready=False))
    kube.services["catalog"] = {"metadata": {"name": "catalog"}, "spec": {"selector": {"app": "catalog"}}}
    kube.endpoints["catalog"] = {"subsets": []}

    def observe_rollback(target):
        target.pods = [pod for pod in target.pods if pod["metadata"]["name"] != "inventory-bbb-1"]
        target.pods[0]["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
        target.deployments["inventory"]["status"].update(replicas=1, unavailableReplicas=0)

    kube.after_patch = observe_rollback
    key = os.urandom(48)
    grants = []
    send_rollback = kube.request_rollback

    def receive_rollback(ns, name, *, grant):
        rollback_grant.verify(key, grant)
        grants.append((grant["incident_id"], grant["action_id"], grant["namespace"], grant["name"],
                       grant["expected_revision"], grant["expected_source"]))
        return send_rollback(ns, name, grant=grant)

    monkeypatch.setattr(kube, "request_rollback", receive_rollback)
    monkeypatch.setattr(rollback_grant, "load_key", lambda: key)
    monkeypatch.setattr(evidence, "_prom", lambda *args: [])
    monkeypatch.setattr(evidence, "_loki", lambda *args: [])
    monkeypatch.setattr(app_errors, "fetch_exceptions", lambda *args: [])

    class Provider:
        name = "offline"

        def complete(self, role, prompt, schema, model, effort, timeout):
            if role == "k8s_log_analyst":
                busy.set()
                assert release.wait(30)
            if role == "router":
                level = "COMPLEX" if "NoEndpoints" in prompt else "SIMPLE"
                data = {"level": level, "rationale": "observed deployment state", "confidence": 0.95}
            else:
                data = _answer()
            return LLMResult(text="", data=data, usage=Usage(input_tokens=10, output_tokens=10),
                             provider=self.name, model=model)

    cfg = AppConfig(settle_seconds=0, rollout_verify_timeout_seconds=1)
    engine = Engine(cfg, Settings(), kube, Provider(), None)
    engine.start()
    try:
        engine.on_signal(Signal("dev", "Service", "catalog", "NoEndpoints", "endpoint_poller"))
        engine.on_signal(Signal("dev", "App", "catalog", "ApplicationException", "exception_watch"))
        assert busy.wait(10)
        engine.on_signal(Signal("dev", "Deployment", "inventory", "ImagePullBackOff", "pod_watch"))
        end = time.monotonic() + 20
        while time.monotonic() < end:
            with db.session() as session:
                image = session.scalar(select(Incident).where(Incident.workload == "inventory"))
                status, reason = image.status, image.status_reason
                if status in {"resolved", "recovery_review", "handed_off", "failed", "action_pending"}:
                    break
            time.sleep(0.05)
        with db.session() as session:
            image = session.scalar(select(Incident).where(Incident.workload == "inventory"))
            assert image.status in {"resolved", "recovery_review"}, (status, reason)
            assert image.severity_level == "SIMPLE"
            actions = list(session.scalars(select(Action).where(Action.incident_id == image.id)))
            assert [(a.type, a.status, a.executed) for a in actions] == [("rollout_undo", "succeeded", True)]
            assert grants == [(image.id, actions[0].id, "dev", "inventory", 2, "inventory-aaa")]
            roles = set(session.scalars(select(AgentRun.role).where(AgentRun.incident_id == image.id)))
            assert {"signal_analyst", "result_verifier"} <= roles
            catalog = session.scalar(select(Incident).where(Incident.workload == "catalog"))
            assert (catalog.status, catalog.severity_level) == ("analyzing", "COMPLEX")
        assert len(kube.patches) == 1 and kube.patches[0][:2] == ("dev", "inventory")
    finally:
        release.set()
        engine.stop()
        for thread in (engine._worker_thread, engine._image_worker):
            thread.join(timeout=15)
            assert not thread.is_alive()
