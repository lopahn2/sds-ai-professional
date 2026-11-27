"""Correlate an unavailable resource metrics provider with a current HPA impact."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .incidents import HPA_METRICS_BLOCKED, HPA_METRICS_SOURCE, Signal
from .k8s import parse_ts

API_SERVICE = "v1beta1.metrics.k8s.io"
CONTROLLER_LAG = timedelta(seconds=30)
RECOVERY_SCOPE = "hpa_resource_metrics"
RECOVERY_LABEL = "자원 지표·HPA 계산 복구 관측 · 운영자 소명 확인 대기"


@dataclass(frozen=True)
class Outage:
    incident_id: int
    evidence_id: int
    detail: dict


def _condition(obj, name):
    return next((item for item in ((obj.get("status") or {}).get("conditions") or [])
                 if item.get("type") == name), {})


def _transition(condition, now):
    stamp = parse_ts(condition.get("lastTransitionTime"))
    return stamp if stamp and stamp.tzinfo and stamp <= now else None


def _metric_key(metric):
    kind = metric.get("type")
    if kind == "Resource":
        value = metric.get("resource") or {}
        return (kind, value["name"], "") if value.get("name") else None
    if kind == "ContainerResource":
        value = metric.get("containerResource") or {}
        return (kind, value["name"], value["container"]) if value.get("name") and value.get("container") else None
    return None


def _resource_metrics(spec):
    return {key for metric in spec.get("metrics") or [] if (key := _metric_key(metric))}


def _metric_value(metric):
    value = (metric.get("resource") or metric.get("containerResource") or {}).get("current") or {}
    utilization = value.get("averageUtilization")
    average = value.get("averageValue")
    valid = {}
    if type(utilization) is int and utilization >= 0:
        valid["average_utilization"] = utilization
    if isinstance(average, str) and average:
        valid["average_value"] = average
    return valid


def _affected(ns, hpa, provider_since, now):
    metadata = hpa.get("metadata") or {}
    spec = hpa.get("spec") or {}
    status = hpa.get("status") or {}
    target = spec.get("scaleTargetRef") or {}
    metrics = _resource_metrics(spec)
    if (metadata.get("namespace") != ns or metadata.get("deletionTimestamp") or not metadata.get("uid")
            or not metadata.get("name") or not target.get("name") or not target.get("kind")
            or not metrics):
        return None
    if status.get("observedGeneration") is not None and status["observedGeneration"] != metadata.get("generation"):
        return None
    condition = _condition(hpa, "ScalingActive")
    observed = _transition(condition, now)
    if (condition.get("status") != "False" or condition.get("reason") not in (
            "FailedGetResourceMetric", "FailedGetContainerResourceMetric") or not observed
            or observed < provider_since - CONTROLLER_LAG
            or "resource metrics api" not in (condition.get("message") or "").lower()):
        return None
    return {"namespace": ns, "name": metadata["name"], "uid": metadata["uid"],
            "generation": metadata.get("generation"), "target_api_version": target.get("apiVersion"),
            "target_kind": target["kind"], "target_name": target["name"], "reason": condition["reason"],
            "since": observed.isoformat(),
            "resource_metrics": [list(key) for key in sorted(metrics)]}


def _recovered(hpa, affected, outage_observed, now):
    metadata, spec, status = (hpa.get(key) or {} for key in ("metadata", "spec", "status"))
    target = spec.get("scaleTargetRef") or {}
    generation = metadata.get("generation")
    if (metadata.get("deletionTimestamp") or generation is None
            or any(metadata.get(key) != affected[key] for key in ("namespace", "name", "uid"))
            or any(target.get(key) != affected[f"target_{key}"] for key in ("kind", "name"))
            or (affected.get("target_api_version") and target.get("apiVersion") != affected["target_api_version"])
            or (affected.get("generation") is not None and generation != affected["generation"])
            or (status.get("observedGeneration") is not None and status["observedGeneration"] != generation)):
        return None
    condition = _condition(hpa, "ScalingActive")
    since = _transition(condition, now)
    failed_at = _transition({"lastTransitionTime": affected.get("since")}, now)
    if (condition.get("status") != "True" or condition.get("reason") != "ValidMetricFound"
            or not since or not failed_at or since <= failed_at or since < outage_observed):
        return None
    required = _resource_metrics(spec)
    original = {tuple(key) for key in affected.get("resource_metrics") or []}
    current = {_metric_key(metric): value for metric in status.get("currentMetrics") or []
               if (value := _metric_value(metric))}
    if not required or (original and required != original) or not required <= current.keys():
        return None
    return {"namespace": metadata["namespace"], "name": metadata["name"], "uid": metadata["uid"],
            "generation": generation, "target": target, "since": since.isoformat(),
            "reason": condition["reason"], "resource_metrics": [list(key) for key in sorted(required)],
            "current_values": [{"metric": list(key), **current[key]} for key in sorted(required)]}


class HpaMetricsSignals:
    def __init__(self, kube, namespaces, emit, *, max_gap, recovery=None):
        self.kube, self.namespaces, self.emit = kube, namespaces, emit
        self.max_gap = timedelta(seconds=max_gap)
        self.pending = None
        self.emitted = None
        self.recovery = recovery
        self.recovery_sample = None

    def _check_recovery(self, uid, available, now):
        previous, self.recovery_sample = self.recovery_sample, None
        since = _transition(available, now)
        if self.recovery is None or since is None:
            return None
        outage = self.recovery.pending(uid)
        if outage is None:
            return None
        observed = _transition({"lastTransitionTime": outage.detail.get("observed_at")}, now)
        failed = _transition({"lastTransitionTime": outage.detail.get("provider_since")}, now)
        affected = outage.detail.get("affected_hpas") or []
        namespaces = {hpa["namespace"] for hpa in affected}
        if not observed or not failed or since <= failed or since < observed.replace(microsecond=0):
            return "unobserved"
        if not namespaces or not namespaces.issubset(self.namespaces):
            return "unobserved"
        autoscalers = {}
        for ns in sorted(namespaces):
            try:
                for hpa in self.kube.list_autoscalers(ns):
                    meta = hpa.get("metadata") or {}
                    autoscalers[ns, meta.get("uid")] = hpa
            except Exception:
                return "unobserved"
        targets = []
        for hpa in sorted(affected, key=lambda hpa: (hpa["namespace"], hpa["name"])):
            current = autoscalers.get((hpa["namespace"], hpa["uid"]))
            ready = current and _recovered(current, hpa, observed.replace(microsecond=0), now)
            if not ready:
                return "awaiting_hpa"
            targets.append(ready)
        sample = {"observed_at": now.isoformat(), "provider_available_since": since.isoformat(), "hpas": targets}
        # Metric quantities can change between polls. Require the same condition and metric identities.
        identities = [{key: value for key, value in target.items() if key != "current_values"} for target in targets]
        self.recovery_sample = (outage.evidence_id, now, sample, identities)
        if (previous and previous[0] == outage.evidence_id and timedelta(0) < now - previous[1] <= self.max_gap
                and previous[2]["provider_available_since"] == sample["provider_available_since"]
                and previous[3] == identities
                and self.recovery.record(outage, [previous[2], sample])):
            self.recovery_sample = None
            return "observed"
        return "confirming"

    def poll_once(self, now=None):
        now = now or datetime.now(timezone.utc)
        status = {"observed_at": now.isoformat()}
        try:
            provider = self.kube.get_resource_metrics_api()
        except Exception:
            self.pending = None
            self.recovery_sample = None
            return {**status, "state": "degraded", "error": "resource_metrics_api_read_failed"}
        if provider is None:
            self.pending = None
            self.recovery_sample = None
            return {**status, "state": "not_installed"}

        available = _condition(provider, "Available")
        metadata = provider.get("metadata") or {}
        if metadata.get("name") != API_SERVICE or not metadata.get("uid") or metadata.get("deletionTimestamp"):
            self.pending = None
            self.recovery_sample = None
            return {**status, "state": "degraded", "impact": "unobserved"}
        if available.get("status") == "True":
            self.pending = None
            recovery = self._check_recovery(metadata["uid"], available, now)
            return {**status, "state": "running", **({"hpa_recovery": recovery} if recovery else {})}
        self.recovery_sample = None
        since = _transition(available, now)
        if available.get("status") != "False" or not since:
            self.pending = None
            return {**status, "state": "degraded", "impact": "unobserved"}

        affected, unobserved = [], []
        for ns in self.namespaces:
            try:
                autoscalers = self.kube.list_autoscalers(ns)
            except Exception:
                unobserved.append(ns)
                continue
            for hpa in autoscalers:
                if (impact := _affected(ns, hpa, since, now)) is not None:
                    affected.append(impact)
        if not affected:
            self.pending = None
            return {**status, "state": "degraded", "affected": [], "unobserved_namespaces": unobserved}

        key = (metadata["uid"], since)
        impacted_ids = {(hpa["namespace"], hpa["uid"]) for hpa in affected}
        previous = self.pending
        self.pending = (key, now, impacted_ids)
        if (self.emitted != key and previous and previous[0] == key
                and timedelta(0) < now - previous[1] <= self.max_gap and previous[2] & impacted_ids):
            detail = {"api_service": API_SERVICE, "provider_uid": metadata["uid"],
                      "provider_since": since.isoformat(), "provider_reason": available.get("reason"),
                      "observed_at": now.isoformat(), "affected_hpas": affected,
                      "unobserved_namespaces": unobserved}
            self.emit(Signal(affected[0]["namespace"], "APIService", API_SERVICE,
                             HPA_METRICS_BLOCKED, HPA_METRICS_SOURCE, detail))
            self.emitted = key
        return {**status, "state": "degraded", "affected": affected, "unobserved_namespaces": unobserved}
