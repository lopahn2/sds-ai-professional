"""Signals -> incidents: fingerprinting, merge window, cooldown suppression."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import audit
from .evalguard import lock_incident_fingerprint, workload_lease_active
from .models import INCIDENT_OPEN_STATES, Incident
from .redact import redact_obj
from .runtime_resume import fingerprint as detail_fingerprint


def _observe_signal(s, incident_id, sig, now):
    # Hash only the sanitized, detached sample; append rather than replacing old observations.
    detail = redact_obj(sig.detail)
    audit(s, "watcher", "signal_observed", incident_id, reason=sig.reason,
          object=f"{sig.kind}/{sig.name}", signal={
              "source": sig.source, "received_at": now.isoformat(),
              "detail": detail, "sha256": detail_fingerprint(detail)})

log = logging.getLogger(__name__)

_ingest_lock = threading.Lock()
ADMISSION_WEBHOOK_FAILURE = "AdmissionWebhookCallFailed"
RETIRED_INGRESS_API = "RetiredIngressApiRejected"
CNI_VERSION_MISMATCH = "CniVersionMismatch"
CNI_EVENT_SOURCE = "k8s_cni_event"
NODE_STARTUP_BLOCKED = "NodeStartupBlocked"
NODE_STARTUP_SOURCE = "k8s_node_startup"
NODE_STARTUP_FINGERPRINT_PREFIX = "NodeStartup/"
DISK_PRESSURE_EVICTION = "NodeDiskPressureEviction"
DISK_PRESSURE_SOURCE = "k8s_disk_pressure"
HPA_METRICS_BLOCKED = "ResourceMetricsHpaBlocked"
HPA_METRICS_SOURCE = "k8s_hpa_metrics"
PROXY_LDS_REJECTED = "TargetProxyListenerUpdateRejected"

# reason classes: signals of the same class after an incident closed are treated as residue
REASON_CLASS = {
    "ImagePullBackOff": "image", "ErrImagePull": "image", "InvalidImageName": "image",
    "TargetWorkloadImagePullFailing": "image",
    "CrashLoopBackOff": "crash", "LivenessProbeFailed": "crash",
    "NoEndpoints": "endpoints", "TargetServiceNoEndpoints": "endpoints",
    "High5xx": "http5xx", "OrderHigh5xxRate": "http5xx",
    "InventoryHigh5xxRate": "http5xx", "TargetHigh5xxRate": "http5xx", "ApplicationException": "http5xx",
}
FAMILY_HINT = {"image": "image_pull", "crash": "crashloop", "endpoints": "endpoint_loss", "http5xx": "http_5xx",
               # reasons without a residue class (`ingest` uses the reason itself as the class)
               "TargetWorkloadOOMKilled": "oom", "OOMKilled": "oom", "TargetWorkloadCrashLooping": "crashloop"}


@dataclass
class Signal:
    namespace: str
    kind: str          # Deployment | Service | App | AdmissionWebhook | Node
    name: str
    reason: str
    source: str        # pod_watch | event_watch | endpoint_poller | alertmanager
    detail: dict = field(default_factory=dict)


def fingerprint(sig: Signal, cfg: AppConfig) -> tuple[str, str]:
    """Returns (fingerprint, workload). Caller-side 5xx maps to its (single) callee via the dependency map."""
    workload = sig.name
    # Preserve pre-P18 caller/callee correlation only for the archived single-target installation.
    # In multi-environment deployments a 5xx and an exception identify the SAME emitting workload.
    if cfg.target_namespaces == ["liveprobe-target"] and REASON_CLASS.get(sig.reason) == "http5xx" and sig.reason != "ApplicationException":
        callees = cfg.dependencies.get(sig.name) or []
        if len(callees) == 1:
            workload = callees[0]
    if sig.reason == CNI_VERSION_MISMATCH:
        return f"{sig.namespace}/Node/{workload}", workload
    if sig.reason == NODE_STARTUP_BLOCKED:
        return f"{NODE_STARTUP_FINGERPRINT_PREFIX}{detail_fingerprint([sig.namespace, workload])}", workload
    if sig.reason == DISK_PRESSURE_EVICTION:
        outage = detail_fingerprint([sig.namespace, workload, sig.detail["deployment_uid"],
                                      sig.detail["node_uid"], sig.detail["disk_pressure_since"]])
        return f"NodeDiskPressure/{outage}", workload
    if sig.reason == HPA_METRICS_BLOCKED:
        outage = detail_fingerprint([sig.detail["provider_uid"], sig.detail["provider_since"]])
        return f"APIService/{workload}/{outage}", workload
    return f"{sig.namespace}/{workload}", workload


def ingest(s: Session, cfg: AppConfig, sig: Signal, now: datetime | None = None, *, kube=None) -> tuple[Incident | None, str]:
    """Create or merge. Returns (incident, outcome) with outcome in created|merged|suppressed."""
    if sig.reason == NODE_STARTUP_BLOCKED or sig.source == NODE_STARTUP_SOURCE:
        return None, "suppressed"
    if not cfg.is_active_environment(sig.namespace):
        return None, "suppressed"
    now = now or datetime.now(timezone.utc)
    fp, workload = fingerprint(sig, cfg)
    rclass = REASON_CLASS.get(sig.reason, sig.reason)
    # The thread lock alone was released before the caller's commit, so a webhook
    # thread and a watch thread (or two replicas) could both miss each other's
    # uncommitted insert. This transaction-scoped advisory lock is held until
    # commit/rollback, serialising same-fingerprint ingest across sessions. It is
    # taken before the thread lock so a waiter never blocks unrelated fingerprints.
    lock_incident_fingerprint(s, fp)
    if workload_lease_active(s, sig.namespace, workload):
        return None, "suppressed"
    with _ingest_lock:
        if sig.reason in {HPA_METRICS_BLOCKED, DISK_PRESSURE_EVICTION}:
            existing = s.scalar(select(Incident).where(Incident.fingerprint == fp).limit(1))
            if existing:
                return existing, "suppressed"
        open_inc = s.scalars(
            select(Incident).where(Incident.fingerprint == fp, Incident.status.in_(INCIDENT_OPEN_STATES),
                                   Incident.created_at >= now - timedelta(seconds=cfg.merge_window_seconds))
            .order_by(Incident.id.desc()).limit(1)
        ).first()
        if not open_inc:
            # Past the merge window, an incident that is STILL open for the same reason class absorbs the repeat
            # (a stalled analysis must not fan out into one incident per alert repeat).
            reasons = [r for r, cls in REASON_CLASS.items() if cls == rclass] + [rclass]
            open_inc = s.scalars(
                select(Incident).where(Incident.fingerprint == fp, Incident.status.in_(INCIDENT_OPEN_STATES),
                                       or_(*(Incident.signal_reasons.contains([r]) for r in reasons)))
                .order_by(Incident.id.desc()).limit(1)
            ).first()
        if open_inc:
            open_inc.signal_count += 1
            if sig.reason not in open_inc.signal_reasons:
                open_inc.signal_reasons = [*open_inc.signal_reasons, sig.reason]
                audit(s, "watcher", "signal_merged", open_inc.id, source=sig.source, reason=sig.reason,
                      object=f"{sig.kind}/{sig.name}")
            open_inc.updated_at = now
            _observe_signal(s, open_inc.id, sig, now)
            return open_inc, "merged"

        # Caller 5xx is supporting evidence for a dependency incident, not
        # sufficient by itself to open an inventory incident. Alertmanager can
        # repeat a stale firing alert after the selector has been repaired.
        if cfg.target_namespaces == ["liveprobe-target"] and rclass == "http5xx" and sig.reason != "ApplicationException":
            return None, "suppressed"

        # Interrupted analysis is deliberately never replayed. Suppress the
        # same still-firing signal for a full merge window after restart,
        # rather than creating a second incident when the short normal
        # post-resolution cooldown expires.
        interrupted = s.scalars(
            select(Incident).where(Incident.fingerprint == fp, Incident.status == "failed",
                                   Incident.status_reason.like("LiveProbe restarted while incident%"),
                                   Incident.closed_at >= now - timedelta(seconds=cfg.merge_window_seconds))
            .order_by(Incident.id.desc()).limit(1)
        ).first()
        if interrupted and any(REASON_CLASS.get(r, r) == rclass for r in interrupted.signal_reasons):
            return interrupted, "suppressed"

        recent_closed = s.scalars(
            select(Incident).where(Incident.fingerprint == fp, Incident.closed_at.is_not(None),
                                   Incident.closed_at >= now - timedelta(seconds=cfg.close_cooldown_seconds))
            .order_by(Incident.id.desc()).limit(1)
        ).first()
        if recent_closed and any(REASON_CLASS.get(r, r) == rclass for r in recent_closed.signal_reasons):
            return recent_closed, "suppressed"

        system = None
        if kube is not None and sig.reason not in {
            ADMISSION_WEBHOOK_FAILURE, RETIRED_INGRESS_API, CNI_VERSION_MISMATCH, HPA_METRICS_BLOCKED, NODE_STARTUP_BLOCKED,
        }:
            try:
                dep = kube.get_deployment(sig.namespace, workload)
                system = (((dep or {}).get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/part-of") or None
            except Exception:
                log.warning("system label unavailable ns=%s workload=%s", sig.namespace, workload)
        incident_name = sig.detail["webhook"] if sig.reason == ADMISSION_WEBHOOK_FAILURE else sig.name
        inc = Incident(
            system=system, fingerprint=fp, family_hint=FAMILY_HINT.get(rclass), namespace=sig.namespace, workload=workload,
            status="open", title=f"{sig.reason} on {sig.kind}/{incident_name}", signal_count=1,
            signal_reasons=[sig.reason], created_at=now, updated_at=now,
        )
        s.add(inc)
        s.flush()
        audit(s, "watcher", "incident_created", inc.id, source=sig.source, reason=sig.reason,
              object=f"{sig.kind}/{sig.name}", fingerprint=fp)
        _observe_signal(s, inc.id, sig, now)
        return inc, "created"
