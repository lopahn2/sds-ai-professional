"""Shared fixtures: a disposable pgvector DB and fake Kubernetes / LLM objects."""
from __future__ import annotations

import copy
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB_ADMIN = os.environ.get("LIVEPROBE_TEST_DB_ADMIN")


def _db_available() -> bool:
    if not TEST_DB_ADMIN:
        return False
    try:
        with create_engine(TEST_DB_ADMIN).connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()
needs_db = pytest.mark.skipif(not DB_AVAILABLE, reason="test postgres (pgvector) not reachable")


@pytest.fixture(scope="session")
def disposable_db():
    """Allocate a new database for this pytest session, including when old test records already exist."""
    if not DB_AVAILABLE:
        pytest.skip("dedicated PostgreSQL test server not configured or unreachable")
    name = "liveprobe_pytest_" + uuid.uuid4().hex
    admin = create_engine(TEST_DB_ADMIN, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
        try:
            yield make_url(TEST_DB_ADMIN).set(database=name).render_as_string(hide_password=False)
        finally:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture()
def db_url(disposable_db, request):
    """Reset only the schema owned by this suite."""
    eng = create_engine(disposable_db, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))
    finally:
        eng.dispose()
    from app import db
    revision = getattr(request, "param", None)
    if revision is None:
        db.run_migrations(disposable_db)
    else:
        from alembic import command
        from alembic.config import Config
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        cfg.attributes["sqlalchemy_url"] = disposable_db
        command.upgrade(cfg, revision)
    db.init_db(disposable_db)
    try:
        yield disposable_db
    finally:
        db.engine().dispose()


# ---------------------------------------------------------------- fake kube
def mk_template(image: str, app: str = "inventory") -> dict:
    return {"metadata": {"labels": {"app": app}},
            "spec": {"containers": [{"name": app, "image": image, "env": [{"name": "X", "value": "secret-ish"}]}]}}


def mk_rs(name: str, dep: str, rev: int, image: str, replicas: int = 1, available: int = 1,
          created: str = "2026-09-25T10:00:00Z") -> dict:
    t = mk_template(image, dep)
    t["metadata"]["labels"]["pod-template-hash"] = name.rsplit("-", 1)[-1]
    return {"metadata": {"name": name, "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                         "ownerReferences": [{"kind": "Deployment", "name": dep}], "creationTimestamp": created},
            "spec": {"template": t}, "status": {"replicas": replicas, "availableReplicas": available}}


def mk_dep(name: str, rev: int, image: str, replicas: int = 1, status: dict | None = None) -> dict:
    return {"metadata": {"name": name, "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                         "generation": 3, "labels": {"app.kubernetes.io/part-of": "liveprobe-target", "liveprobe.io/managed": "true"}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": mk_template(image, name)},
            "status": status or {"observedGeneration": 3, "replicas": replicas, "updatedReplicas": replicas,
                                 "availableReplicas": replicas, "readyReplicas": replicas}}


def mk_pod(name: str, app: str, rs: str | None, waiting: str | None = None, ready: bool = True,
           image: str = "img") -> dict:
    cs = {"name": app, "image": image, "ready": ready and not waiting, "restartCount": 0,
          "state": {"waiting": {"reason": waiting, "message": "Back-off pulling image"}} if waiting else {"running": {}}}
    return {"metadata": {"name": name, "labels": {"app": app},
                         "ownerReferences": [{"kind": "ReplicaSet", "name": rs}] if rs else [],
                         "creationTimestamp": "2026-09-25T09:00:00Z"},
            "status": {"phase": "Pending" if waiting else "Running", "containerStatuses": [cs]}}


class FakeKube:
    def __init__(self):
        self.deployments: dict[str, dict] = {}
        self.replicasets: list[dict] = []
        self.pods: list[dict] = []
        self.services: dict[str, dict] = {}
        self.endpoints: dict[str, dict] = {}
        self.events: list[dict] = []
        self.nodes: dict[str, dict] = {}
        self.configmaps: dict[str, dict] = {}
        self.patches: list[tuple] = []
        self.after_patch = None  # callable(fake) to simulate controller reaction

    def get_deployment(self, ns, name):
        d = self.deployments.get(name)
        return copy.deepcopy(d) if d else None

    def list_deployments(self, ns):
        return [copy.deepcopy(d) for d in self.deployments.values()]

    def list_replicasets(self, ns):
        return copy.deepcopy(self.replicasets)

    def get_replicaset(self, ns, name):
        return next((copy.deepcopy(r) for r in self.replicasets if r["metadata"]["name"] == name), None)

    def list_pods(self, ns, selector=None):
        out = self.pods
        if selector:
            out = [p for p in out if all(p["metadata"]["labels"].get(k) == v for k, v in selector.items())]
        return copy.deepcopy(out)

    def get_pod(self, ns, name):
        return next((copy.deepcopy(p) for p in self.pods if p["metadata"]["name"] == name), None)

    def get_service(self, ns, name):
        return copy.deepcopy(self.services.get(name))

    def list_services(self, ns):
        return copy.deepcopy(list(self.services.values()))

    def get_endpoints(self, ns, name):
        return copy.deepcopy(self.endpoints.get(name))

    def configmap_meta(self, ns, name):
        from app.k8s import configmap_fingerprint
        cm = self.configmaps.get(name)
        return configmap_fingerprint(copy.deepcopy(cm)) if cm is not None else None

    def get_node(self, name):
        return copy.deepcopy(self.nodes.get(name))

    def list_events(self, ns):
        return copy.deepcopy(self.events)

    def request_rollback(self, ns, name, **provenance):
        # Transport double only. Security decisions are exercised against
        # plan_undo and admission with separate adversarial tests.
        old = self.replicasets[0]["spec"]["template"]["spec"]["containers"][0]["image"]
        self.patches.append((ns, name, [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": old}]))
        self.deployments[name]["spec"]["template"]["spec"]["containers"][0]["image"] = old
        if self.after_patch:
            self.after_patch(self)
        return {"to_revision": 1, "to_images": [old], "to_replicaset": self.replicasets[0]["metadata"]["name"],
                "before": {"revision": 2}}


def family1_kube(image_ok="liveprobe/inventory:dev", image_bad="liveprobe/inventory:does-not-exist",
                 created_bad="2026-09-25T10:00:00Z") -> FakeKube:
    k = FakeKube()
    k.deployments["inventory"] = mk_dep("inventory", 2, image_bad, status={
        "observedGeneration": 3, "replicas": 2, "updatedReplicas": 1, "availableReplicas": 1, "readyReplicas": 1,
        "unavailableReplicas": 1})
    k.replicasets = [mk_rs("inventory-aaa", "inventory", 1, image_ok),
                     mk_rs("inventory-bbb", "inventory", 2, image_bad, available=0, created=created_bad)]
    k.pods = [mk_pod("inventory-aaa-1", "inventory", "inventory-aaa", image=image_ok),
              mk_pod("inventory-bbb-1", "inventory", "inventory-bbb", waiting="ImagePullBackOff", image=image_bad),
              mk_pod("order-ccc-1", "order", "order-ccc")]
    k.services["inventory"] = {"metadata": {"name": "inventory"}, "spec": {"selector": {"app": "inventory"}}}
    k.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
    return k


def family4_kube() -> FakeKube:
    k = FakeKube()
    k.deployments["inventory"] = mk_dep("inventory", 1, "liveprobe/inventory:dev")
    k.replicasets = [mk_rs("inventory-aaa", "inventory", 1, "liveprobe/inventory:dev", created="2026-09-24T10:00:00Z")]
    k.pods = [mk_pod("inventory-aaa-1", "inventory", "inventory-aaa"), mk_pod("order-ccc-1", "order", "order-ccc")]
    k.services["inventory"] = {"metadata": {"name": "inventory"}, "spec": {"selector": {"app": "inventory-broken"}}}
    k.endpoints["inventory"] = {"subsets": []}
    return k

@pytest.fixture
def operator_gateway(tmp_path, monkeypatch):
    """Sign requests from the simulated loopback dashboard for core HTTP integration tests."""
    from fastapi.testclient import TestClient
    from app import local_operator

    read_path, write_path = tmp_path / 'gateway-read', tmp_path / 'gateway-write'
    for path in (read_path, write_path):
        path.write_text(uuid.uuid4().hex + uuid.uuid4().hex)
        path.chmod(0o600)
    monkeypatch.setenv('LIVEPROBE_LOCAL_READ_KEY_FILE', str(read_path))
    monkeypatch.setenv('LIVEPROBE_LOCAL_OPERATOR_KEY_FILE', str(write_path))
    key = write_path.read_bytes()
    nonce = local_operator.new_token(key)
    original = TestClient.send

    def forward(client, request, **kwargs):
        host = request.headers.get('host')
        if host == 'liveprobe.localhost':
            request.headers.setdefault('X-LiveProbe-Local-Read', read_path.read_text())
            path = request.url.raw_path.decode('ascii')
            if local_operator.human_path(path) and request.headers.get('X-LiveProbe-Client') == 'dashboard':
                cookie = request.headers.get('Cookie', '')
                request.headers['Cookie'] = (cookie+'; ' if cookie else '') + local_operator.COOKIE+'='+nonce
                request.headers.setdefault('Origin', 'http://'+host)
                request.headers.setdefault(local_operator.CSRF, nonce)
                request.headers[local_operator.HEADER] = local_operator.proof(key, request.method, path, host, nonce)
        return original(client, request, **kwargs)

    monkeypatch.setattr(TestClient, 'send', forward)
