"""Background watchers: Pod watch, Event watch, Endpoint poller. Each runs in its own daemon thread."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import k8s as K
from .cni_signals import CniSignals, SANDBOX_FAILURE
from .config import AppConfig, MIN_ENDPOINT_MISSING_SECONDS
from .disk_pressure import DiskPressureSignals, EVICTED
from .incidents import Signal
from .flows import FlowStore
from .hpa_metrics import HpaMetricsSignals
from .service_health import read_backend
from .network_signals import NetworkSignalAdapter
from .workload_signals import WorkloadSignals

log = logging.getLogger(__name__)

SIGNAL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CrashLoopBackOff"}
EVENT_REASON_MAP = {"Failed": None, "BackOff": None}  # message decides the signal reason

Emit = Callable[[Signal], None]


class Watchers:
    def __init__(self, kube: K.Kube, cfg: AppConfig, emit: Emit, *, flow_store: FlowStore | None = None,
                 hpa_recovery=None):
        self.kube, self.cfg, self.emit = kube, cfg, emit
        self.ns = cfg.target_namespaces[0]
        self._stop = threading.Event()
        self._empty_since: dict[str, tuple[str | None, float, float]] = {}
        self.status: dict[str, dict] = {}
        self.cni = CniSignals(kube, emit)
        self.disk_pressure = DiskPressureSignals(kube, emit)
        self.hpa_metrics = HpaMetricsSignals(kube, cfg.target_namespaces, emit,
                                            max_gap=max(60, 3 * cfg.endpoint_poll_interval_seconds), recovery=hpa_recovery)
        self.network = NetworkSignalAdapter(flow_store, kube, cfg, emit) if flow_store is not None else None
        self.workload_signals = (WorkloadSignals(self.ns, kube, emit,
                                                max_gap=max(30, 3 * cfg.endpoint_poll_interval_seconds),
                                                workloads=cfg.workload_state_signal_workloads)
                                 if cfg.workload_state_signals_enabled else None)

    def start(self) -> None:
        threading.Thread(target=self._guard, args=("hpa_metrics_poller", self._hpa_metrics_loop),
                         name="hpa_metrics_poller", daemon=True).start()
        if len(self.cfg.target_namespaces) > 1:
            for ns in self.cfg.target_namespaces:
                child = Watchers(self.kube, self.cfg.model_copy(update={"target_namespaces": [ns]}), self.emit)
                child._stop, child.status = self._stop, self.status
                child._start_loops(suffix="/" + ns)
            if self.network is not None:
                threading.Thread(target=self._guard, args=("network_poller", self._network_loop), name="network_poller", daemon=True).start()
        else:
            self._start_loops()

    def _start_loops(self, suffix: str = "") -> None:
        loops = [("pod_watch", self._pod_loop), ("event_watch", self._event_loop),
                 ("endpoint_poller", self._endpoint_loop)]
        if self.network is not None:
            loops.append(("network_poller", self._network_loop))
        for name, fn in loops:
            name += suffix
            threading.Thread(target=self._guard, args=(name, fn), name=name, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _guard(self, name: str, fn: Callable[[], None]) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.status[name] = {"state": "running", "since": datetime.now(timezone.utc).isoformat()}
                fn()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                self.status[name] = {"state": "reconnecting", "error": f"{type(e).__name__}: {str(e)[:200]}"}
                log.warning("%s error: %s; reconnect in %.0fs", name, type(e).__name__, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    # --- pods ----------------------------------------------------------------
    def handle_pod(self, pod: dict) -> None:
        ns = (pod.get("metadata") or {}).get("namespace", self.ns)
        if ns not in self.cfg.target_namespaces:
            return
        if (pod.get("metadata") or {}).get("deletionTimestamp"):
            return
        reasons = [r for r in K.pod_waiting_reasons(pod) if r in SIGNAL_REASONS]
        if not reasons:
            return
        dep = K.deployment_for_pod(self.kube, ns, pod)
        if not dep:
            return
        for r in sorted(set(reasons)):
            self.emit(Signal(ns, "Deployment", dep, r, "pod_watch", {"pod": pod["metadata"]["name"]}))

    def _pod_loop(self) -> None:
        from kubernetes import watch
        from kubernetes.client import ApiException
        lst = self.kube.core.list_namespaced_pod(self.ns)
        rv = lst.metadata.resource_version
        for p in lst.items:
            self.handle_pod(self.kube.to_dict(p))
        w = watch.Watch()
        while not self._stop.is_set():
            try:
                for ev in w.stream(self.kube.core.list_namespaced_pod, self.ns, resource_version=rv,
                                   timeout_seconds=300, allow_watch_bookmarks=True):
                    obj = ev["object"]
                    if ev["type"] == "BOOKMARK":  # raw dict, not a V1Pod: only advance the resume point
                        rv = _bookmark_rv(obj) or rv
                        continue
                    rv = obj.metadata.resource_version
                    if ev["type"] in ("ADDED", "MODIFIED"):
                        self.handle_pod(self.kube.to_dict(obj))
            except ApiException as e:
                if e.status == 410:  # resourceVersion too old -> relist
                    return
                raise

    # --- events ----------------------------------------------------------------
    def handle_event(self, e: dict) -> None:
        io = e.get("involvedObject") or {}
        ns = io.get("namespace") or (e.get("metadata") or {}).get("namespace") or self.ns
        if ns not in self.cfg.target_namespaces:
            return
        if io.get("kind") != "Pod" or e.get("type") != "Warning":
            return
        msg = (e.get("message") or "")
        reason = e.get("reason")
        if reason == EVICTED:
            self.disk_pressure.observe(e, ns)
            return
        if reason == SANDBOX_FAILURE:
            self.cni.observe(e, ns)
            return
        sig = None
        if reason in ("Failed", "BackOff") and ("pull" in msg.lower() or "image" in msg.lower()):
            sig = "ImagePullBackOff" if reason == "BackOff" else "ErrImagePull"
        elif reason == "BackOff" and "restarting failed container" in msg.lower():
            sig = "CrashLoopBackOff"
        if not sig:
            return
        pod = self.kube.get_pod(ns, io.get("name"))
        if not pod or (pod.get("metadata") or {}).get("deletionTimestamp"):
            return
        # a same-name Pod recreated since the Event must not inherit it (owner-aware, UID-exact)
        if io.get("uid") and (pod.get("metadata") or {}).get("uid") not in (None, io["uid"]):
            return
        # only signal if the pod is *currently* in a failing state (ignores stale events)
        if not set(K.pod_waiting_reasons(pod)) & SIGNAL_REASONS:
            return
        dep = K.deployment_for_pod(self.kube, ns, pod)
        if dep:
            self.emit(Signal(ns, "Deployment", dep, sig, "event_watch", {"pod": io.get("name"), "event": reason}))

    def _event_loop(self) -> None:
        from kubernetes import watch
        from kubernetes.client import ApiException
        lst = self.kube.core.list_namespaced_event(self.ns)
        rv = lst.metadata.resource_version
        for event in lst.items:
            if event.reason in {SANDBOX_FAILURE, EVICTED}:
                # Both detectors revalidate recent warnings against live objects after relisting.
                self.handle_event(self.kube.to_dict(event))
        w = watch.Watch()
        while not self._stop.is_set():
            try:
                for ev in w.stream(self.kube.core.list_namespaced_event, self.ns, resource_version=rv,
                                   timeout_seconds=300):
                    obj = ev["object"]
                    if ev["type"] == "BOOKMARK":
                        rv = _bookmark_rv(obj) or rv
                        continue
                    rv = obj.metadata.resource_version
                    if ev["type"] in ("ADDED", "MODIFIED"):
                        self.handle_event(self.kube.to_dict(obj))
            except ApiException as e:
                if e.status == 410:
                    return
                raise

    # --- optional Hubble/Retina flows -----------------------------------------
    def poll_network_once(self) -> int:
        if self.network is None:
            return 0
        emitted = self.network.poll_once()
        self.status["network_poller"] = {
            "state": "degraded" if self.network.last_errors else
                     "stale" if self.network.store.freshness()["stale"] else "running",
            "errors": list(self.network.last_errors), "emitted": emitted,
        }
        return emitted

    def _network_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_network_once()
            self._stop.wait(self.cfg.endpoint_poll_interval_seconds)

    def _hpa_metrics_loop(self) -> None:
        while not self._stop.is_set():
            self.status["hpa_metrics_poller"] = self.hpa_metrics.poll_once()
            self._stop.wait(self.cfg.endpoint_poll_interval_seconds)

    # --- endpoints -----------------------------------------------------------
    def poll_endpoints_once(self, now: float | None = None) -> None:
        try:
            pods = self.kube.list_pods(self.ns)
        except Exception:
            self._empty_since.clear()
            if self.workload_signals is not None:
                self.workload_signals.reset()
                self.status[f"workload_poller/{self.ns}"] = {"state": "degraded", "error": "pod_list_failed"}
            raise
        pressure_status = f"disk_pressure_poller/{self.ns}"
        try:
            if self.disk_pressure.poll_once(pods):
                self.status[pressure_status] = {"state": "running"}
        except Exception as exc:  # unrelated endpoint detection can continue with the same Pod inventory
            self.status[pressure_status] = {"state": "degraded", "error": type(exc).__name__}
        deployments = None
        if self.workload_signals is not None:
            name = f"workload_poller/{self.ns}"
            try:
                deployments = self.kube.list_deployments(self.ns)
                self.workload_signals.observe(pods, deployments, now=now if now is not None else time.monotonic())
                self.status[name] = {"state": "running", "since": datetime.now(timezone.utc).isoformat()}
            except Exception as exc:
                self.workload_signals.reset()
                self.status[name] = {"state": "degraded", "error": type(exc).__name__}
                log.warning("%s error: %s", name, type(exc).__name__)
        try:
            services = self.kube.list_services(self.ns)
            read_snapshot = getattr(self.kube, "service_endpoint_counts", None)
            monitored = [service for service in services
                         if K.is_selector_service(service) and not service["metadata"].get("deletionTimestamp")]
            endpoint_counts = read_snapshot(monitored, self.ns) if read_snapshot is not None else None
            if endpoint_counts is not None and deployments is None and any(
                endpoint_counts.get((self.ns, s["metadata"]["name"]), 0) == 0 for s in monitored
            ):
                deployments = self.kube.list_deployments(self.ns)
        except Exception:
            self._empty_since.clear()
            raise
        live_services = {s["metadata"]["name"] for s in monitored}
        self._empty_since = {name: interval for name, interval in self._empty_since.items() if name in live_services}
        grace = max(MIN_ENDPOINT_MISSING_SECONDS, self.cfg.endpoint_grace_seconds)
        # A few missed polls are enough to lose continuity. Allow time for a slow API read.
        max_observation_gap = max(60, 3 * self.cfg.endpoint_poll_interval_seconds)
        read_error = None
        for svc in monitored:
            metadata = svc["metadata"]
            name, uid = metadata["name"], metadata.get("uid")
            spec = svc.get("spec") or {}
            selector = spec.get("selector") or {}
            try:
                backend = read_backend(self.kube, self.ns, svc, deployments, endpoint_counts=endpoint_counts)
            except Exception as exc:
                self._empty_since.pop(name, None)
                if read_error is None:
                    read_error = exc
                continue
            if backend["backend_state"] != "missing":
                self._empty_since.pop(name, None)
                continue
            observed_at = now if now is not None else time.monotonic()
            last_uid, first, last = self._empty_since.get(name, (uid, observed_at, observed_at))
            if last_uid != uid or observed_at - last > max_observation_gap:
                first = observed_at
            self._empty_since[name] = (uid, first, observed_at)
            empty_for = observed_at - first
            if empty_for < grace:
                continue
            matching = [p for p in pods if K.labels_match(selector, p["metadata"].get("labels") or {})]
            self.emit(Signal(self.ns, "Service", name, "NoEndpoints", "endpoint_poller",
                             {"selector": selector, "matching_pods": len(matching),
                              "empty_for_s": int(empty_for)}))
        if read_error is not None:
            raise read_error

    def _endpoint_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_endpoints_once()
            self._stop.wait(self.cfg.endpoint_poll_interval_seconds)


def _bookmark_rv(obj) -> str | None:
    md = (obj.get("metadata") if isinstance(obj, dict) else None) or {}
    return md.get("resourceVersion")
