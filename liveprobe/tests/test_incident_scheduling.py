"""Two incident workers share the production intake, settle and recovery paths."""
import threading
from collections import Counter

from sqlalchemy import select

from app import db
from app.config import AppConfig, Settings
from app.engine import Engine
from app.incidents import Signal
from app.models import Incident


IMAGE_SIGNALS = ["ErrImagePull", "ImagePullBackOff", "TargetWorkloadImagePullFailing"]


def stop_and_join(engine):
    engine.stop()
    for worker in (engine._worker_thread, engine._image_worker):
        worker.join(3)
        assert not worker.is_alive()


def test_new_image_incident_runs_while_complex_incident_is_blocked(db_url, monkeypatch):
    engine = Engine(AppConfig(settle_seconds=0), Settings(background=False), None, None, None)
    complex_running, release, image_seen = (threading.Event() for _ in range(3))
    names = {}

    def process(iid):
        with db.session() as s:
            workload = s.get(Incident, iid).workload
        names[workload] = threading.current_thread().name
        if workload == "catalog":
            complex_running.set()
            release.wait(8)
        elif workload == "notification":
            image_seen.set()

    monkeypatch.setattr(engine, "process", process)
    engine.start()
    try:
        engine.on_signal(Signal("dev", "Deployment", "catalog", "ApplicationException", "pod_watch"))
        assert complex_running.wait(3)
        for reason in IMAGE_SIGNALS:
            engine.on_signal(Signal("dev", "Deployment", "notification", reason, "pod_watch"))
        assert image_seen.wait(3)
        assert names == {"catalog": "incident-worker", "notification": "incident-image-pull"}
        with db.session() as s:
            row = s.scalars(select(Incident).where(Incident.workload == "notification")).one()
            assert row.signal_reasons == IMAGE_SIGNALS
            assert row.signal_count == 3
    finally:
        release.set()
        stop_and_join(engine)


def test_restart_reserves_capacity_deduplicates_and_serializes_same_workload(db_url, monkeypatch):
    engine = Engine(AppConfig(settle_seconds=0), Settings(background=False), None, None, None)
    # The second incident shares the exact workload with the first. It must not
    # occupy the image worker while waiting for that workload to become free.
    rows = [
        ("dev", "catalog", ["ApplicationException", "NoEndpoints"]),
        ("dev", "catalog", IMAGE_SIGNALS),
        ("dev", "notification", IMAGE_SIGNALS),
        ("prd", "catalog", IMAGE_SIGNALS),
        ("dev", "payment", IMAGE_SIGNALS),
    ]
    with db.session() as s:
        incidents = [Incident(namespace=ns, workload=name, fingerprint=f"{ns}/{name}", signal_reasons=reasons)
                     for ns, name, reasons in rows]
        s.add_all(incidents)
        s.flush()
        slow, same, simple, other_namespace, mixed = [inc.id for inc in incidents]

    engine.recover_on_startup()
    engine.recover_on_startup()
    # A non-image reason arrived after the stored placement, before dequeue.
    engine.on_signal(Signal("dev", "Deployment", "payment", "NoEndpoints", "endpoint_poller"))
    entered = {iid: threading.Event() for iid in (slow, same, simple, other_namespace, mixed)}
    release = threading.Event()
    calls = Counter()
    workers = {}
    trace = []
    lock = threading.Lock()

    def process(iid):
        with lock:
            calls[iid] += 1
            workers[iid] = threading.current_thread().name
            trace.append(("start", iid))
        entered[iid].set()
        if iid == slow:
            release.wait(8)
        with lock:
            trace.append(("finish", iid))

    monkeypatch.setattr(engine, "process", process)
    engine.start()
    try:
        assert entered[slow].wait(3)
        engine.schedule(slow)
        assert entered[simple].wait(3) and entered[other_namespace].wait(3)
        assert not entered[same].is_set() and not entered[mixed].is_set()
        release.set()
        assert entered[same].wait(3) and entered[mixed].wait(3)
    finally:
        release.set()
        stop_and_join(engine)

    assert calls == {iid: 1 for iid in entered}
    assert trace.index(("finish", slow)) < trace.index(("start", same))
    assert workers == {slow: "incident-worker", same: "incident-image-pull", simple: "incident-image-pull",
                       other_namespace: "incident-image-pull", mixed: "incident-worker"}
    assert not engine._scheduled


def test_merged_mixed_incident_leaves_busy_image_lane_before_first_image_finishes(db_url, monkeypatch):
    engine = Engine(AppConfig(settle_seconds=0), Settings(background=False), None, None, None)
    first_running, mixed_running, release = (threading.Event() for _ in range(3))
    starts = []

    def process(iid):
        with db.session() as s:
            name = s.get(Incident, iid).workload
        starts.append((name, threading.current_thread().name))
        if name == "notification":
            first_running.set()
            release.wait(8)
        else:
            mixed_running.set()

    monkeypatch.setattr(engine, "process", process)
    engine.start()
    try:
        engine.on_signal(Signal("dev", "Deployment", "notification", "ErrImagePull", "pod_watch"))
        assert first_running.wait(3)
        engine.on_signal(Signal("dev", "Deployment", "catalog", "ImagePullBackOff", "pod_watch"))
        engine.on_signal(Signal("dev", "Deployment", "catalog", "ApplicationException", "application"))
        assert mixed_running.wait(3)
        # A subsequent merge into an active attempt cannot start it a second time.
        engine.on_signal(Signal("dev", "Deployment", "notification", "NoEndpoints", "endpoint_poller"))
    finally:
        release.set()
        stop_and_join(engine)
    assert starts == [("notification", "incident-image-pull"), ("catalog", "incident-worker")]


def test_two_pending_cleanups_release_only_their_own_target(db_url, monkeypatch):
    from app.runlog import CleanupResult

    engine = Engine(AppConfig(settle_seconds=0), Settings(background=False), None, None, None)
    with db.session() as s:
        rows = [Incident(namespace="dev", workload=name, fingerprint=f"dev/{name}", signal_reasons=reasons)
                for name, reasons in [("catalog", ["NoEndpoints"]), ("notification", IMAGE_SIGNALS)] * 2]
        s.add_all(rows)
        s.flush()
        normal, image, next_normal, next_image = [row.id for row in rows]
    cleaning = {iid: threading.Event() for iid in (normal, image)}
    release = {iid: threading.Event() for iid in cleaning}
    successors = {iid: threading.Event() for iid in (next_normal, next_image)}

    def process(iid):
        if iid in cleaning:
            raise ValueError("isolated failure")
        successors[iid].set()

    def finalize(iid, error, *, worker_run_id):
        cleaning[iid].set()
        release[iid].wait(8)
        return CleanupResult.COMPLETED

    monkeypatch.setattr(engine, "process", process)
    monkeypatch.setattr(engine, "_record_worker_failure", finalize)
    engine.recover_on_startup()
    engine.start()
    try:
        assert all(event.wait(3) for event in cleaning.values())
        release[image].set()
        assert successors[next_image].wait(3)
        assert not successors[next_normal].is_set()
        assert engine.worker_health() == {"alive": True, "cleanup_pending": True, "ready": False}
        release[normal].set()
        assert successors[next_normal].wait(3)
        assert engine.worker_health() == {"alive": True, "cleanup_pending": False, "ready": True}
    finally:
        for event in release.values():
            event.set()
        stop_and_join(engine)
