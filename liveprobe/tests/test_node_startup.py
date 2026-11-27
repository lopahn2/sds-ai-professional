import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.config import AppConfig, Settings
from app.engine import Engine
from app.flowgraph import _evidence_card
from app.incidents import NODE_STARTUP_BLOCKED, NODE_STARTUP_SOURCE, Signal, fingerprint, ingest
from app.models import Action, AgentRun, Evidence, Incident
from app.node_startup import NodeStartupWatcher
from app.node_startup_feed import FAILURES, observe_line
from conftest import needs_db


NOW = datetime(2026, 11, 24, 13, tzinfo=timezone.utc)
EXAMPLES = (
    ("kubeadm", '[ERROR KubeletVersion]: Kubelet version "1.31.0" is lower than kubeadm can support. Please upgrade kubelet',
     "kubelet_version"),
    ("k3s-agent", "Waiting for CRI startup: rpc error: code = Unimplemented desc = unknown service runtime.v1.RuntimeService",
     "cri_v1"),
    ("kubelet", "failed to validate kubelet configuration, error: kubelet is configured to not run on a host using cgroup v1.",
     "cgroup_v1"),
)


def append(path, entry):
    with path.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


@pytest.mark.parametrize("component,line,failure", EXAMPLES)
def test_normalizer_recognizes_only_startup_facts(component, line, failure):
    record = observe_line("new-worker", component, line, NOW)
    assert record["failure"] == failure and record["observed_at"] == NOW.isoformat()
    assert "message" not in record and line not in json.dumps(record)
    assert observe_line("new-worker", "workload", line, NOW) is None
    assert observe_line("bad..node", component, line, NOW) is None
    assert observe_line("new-worker", component, "retry timed out", NOW) is None


def test_host_normalizer_runs_with_only_standard_library():
    component, line, failure = EXAMPLES[1]
    script = Path(__file__).parents[1] / "app/node_startup_feed.py"
    result = subprocess.run([sys.executable, "-I", str(script), "--component", component, "--node", "new-worker"],
                            input=line + "\nignored line\n", capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["failure"] == failure


def test_watcher_uses_current_node_state_and_scope(tmp_path):
    path = tmp_path / "startup.jsonl"
    cfg = AppConfig(target_namespaces=["dev"], historical_namespaces=["prd"])
    component, line, _ = EXAMPLES[1]
    raw = observe_line("fresh", component, line, NOW)
    nodes = []
    emitted = []

    def lookup(name):
        nodes.append(name)
        return {"metadata": {"name": name}} if name == "ready" else None

    append(path, raw)  # a record before watcher start must not open a new incident
    watcher = NodeStartupWatcher(path, cfg, {"fresh": "dev", "ready": "dev", "prd-node": "prd"},
                                 SimpleNamespace(get_node=lookup), emitted.append, clock=lambda: NOW)
    try:
        assert watcher.poll_once() == 0
        for entry in (
            {**raw, "node": "unknown"}, {**raw, "node": "prd-node"}, {**raw, "component": "workload"},
            {**raw, "observed_at": (NOW - timedelta(seconds=301)).isoformat()},
            {**raw, "observed_at": (NOW + timedelta(seconds=31)).isoformat()},
            {**raw, "node": "ready"}, {**raw, "message": "raw material should not persist"},
        ):
            append(path, entry)
        assert watcher.poll_once() == 1 and nodes == ["ready", "fresh"]
        sig, = emitted
        assert sig.namespace == "dev" and sig.reason == NODE_STARTUP_BLOCKED
        assert sig.detail["node_registered"] is False and "message" not in sig.detail
        assert fingerprint(sig, cfg)[0] != fingerprint(Signal("dev", "Deployment", "fresh", "CrashLoopBackOff", "pod_watch"), cfg)[0]
    finally:
        watcher.stop()


@pytest.mark.parametrize("component,line,failure", EXAMPLES)
def test_retired_startup_intake_stops_before_the_database(component, line, failure):
    detail = observe_line("new-worker", component, line, NOW)
    assert detail["failure"] == failure
    signal = Signal("dev", "Node", "new-worker", NODE_STARTUP_BLOCKED, NODE_STARTUP_SOURCE, detail)
    assert ingest(None, AppConfig(target_namespaces=["dev"]), signal) == (None, "suppressed")


@needs_db
def test_retired_startup_record_is_still_readable_after_worker_recovery(db_url, monkeypatch):
    cfg = AppConfig(target_namespaces=["dev"])
    engine = Engine(cfg, Settings(), SimpleNamespace(), SimpleNamespace(name="test"), None)
    monkeypatch.setattr(engine, "_collect_or_reuse", lambda *_: pytest.fail("unexpected analysis"))
    component, line, failure = EXAMPLES[0]
    detail = {**observe_line("new-worker", component, line, NOW), "node_registered": False}
    sig = Signal("dev", "Node", "new-worker", NODE_STARTUP_BLOCKED, NODE_STARTUP_SOURCE, detail)
    fp, name = fingerprint(sig, cfg)
    with db.session() as session:
        old = Incident(fingerprint=fp, namespace="dev", workload=name, status="open",
                       title=FAILURES[failure], signal_reasons=[NODE_STARTUP_BLOCKED])
        session.add(old)
        session.flush()
        iid = old.id
        session.add(Evidence(incident_id=iid, source=NODE_STARTUP_SOURCE, target="Node/new-worker", payload=detail))
    engine.on_signal(sig)
    engine.recover_on_startup()
    engine.process(iid)
    with db.session() as session:
        incident = session.get(Incident, iid)
        assert incident.status == "open" and incident.signal_count == 1
        evidence = session.scalars(select(Evidence).where(Evidence.incident_id == iid)).one()
        card, = _evidence_card([evidence], [])["items"]
        assert FAILURES[failure].removesuffix(".") in card["summary_ko"] and "미관측" in card["summary_ko"]
        assert session.scalar(select(Action.id)) is None and session.scalar(select(AgentRun.id)) is None
