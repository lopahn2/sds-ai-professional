"""Corroborate kubelet CNI version errors with the Pods they currently block."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import re

from .incidents import CNI_EVENT_SOURCE, CNI_VERSION_MISMATCH, Signal
from .k8s import parse_ts


SANDBOX_FAILURE = "FailedCreatePodSandBox"
WINDOW = timedelta(minutes=2)
VERSION = r'\d{1,3}\.\d{1,3}\.\d{1,3}'
CONFLICT = re.compile(
    rf'incompatible CNI versions; config is "({VERSION})", plugin supports '
    rf'\[("{VERSION}"(?: "{VERSION}"){{0,15}})\]\s*$'
)


class CniSignals:
    def __init__(self, kube, emit):
        self.kube, self.emit = kube, emit
        self.recent = OrderedDict()
        self.reported = OrderedDict()

    @staticmethod
    def _remember(history, key, value):
        history[key] = value
        history.move_to_end(key)
        if len(history) > 128:
            history.popitem(last=False)

    def _current_pod(self, sample) -> bool:
        pod = self.kube.get_pod(sample["namespace"], sample["pod"]) or {}
        metadata, spec, status = (pod.get(key) or {} for key in ("metadata", "spec", "status"))
        created = parse_ts(metadata.get("creationTimestamp"))
        return (
            (metadata.get("namespace"), metadata.get("uid")) == (sample["namespace"], sample["pod_uid"])
            and not metadata.get("deletionTimestamp")
            and created is not None and created.tzinfo is not None and created <= sample["at"]
            and spec.get("nodeName") == sample["node"] and not spec.get("hostNetwork")
            and status.get("phase") == "Pending" and not status.get("podIP") and not status.get("podIPs")
            and not any(c.get("type") == "PodReadyToStartContainers" and c.get("status") == "True"
                        for c in status.get("conditions") or [])
        )

    def observe(self, event: dict, namespace: str, *, now: datetime | None = None) -> None:
        obj, meta = (event.get(k) or {} for k in ("involvedObject", "metadata"))
        source, series = (event.get(k) or {} for k in ("source", "series"))
        node, event_uid = source.get("host") or event.get("reportingInstance"), meta.get("uid")
        if ((event.get("reportingComponent") or source.get("component")) != "kubelet"
                or obj.get("namespace") != namespace or meta.get("namespace") != namespace
                or not all((node, event_uid, obj.get("uid"), obj.get("name")))):
            return
        message = event.get("message") or ""
        match = CONFLICT.search(message) if "failed to setup network for sandbox" in message else None
        at = parse_ts(series.get("lastObservedTime") or event.get("lastTimestamp") or event.get("eventTime"))
        now = now or datetime.now(timezone.utc)
        if not match or not at or not at.tzinfo or not now - WINDOW <= at <= now:
            return
        configured, supported = match[1], tuple(sorted(v.strip('"') for v in match[2].split()))
        if configured in supported:
            return
        key = (namespace, node, configured, supported)
        sample = {"namespace": namespace, "pod": obj["name"], "pod_uid": obj["uid"],
                  "node": node, "at": at, "event_uid": event_uid}
        if not self._current_pod(sample):
            return
        last = self.reported.get(key)
        if last and now - WINDOW <= last:
            self._remember(self.reported, key, now)
            return
        prior = next((old for (seen, pod_uid), old in reversed(self.recent.items())
                      if seen == key and pod_uid != sample["pod_uid"] and old["event_uid"] != event_uid
                      and now - WINDOW <= old["at"] <= now and self._current_pod(old)), None)
        self._remember(self.recent, (key, sample["pod_uid"]), sample)
        count = series.get("count") or event.get("count")
        if prior is None and (type(count) is not int or count < 2):
            return
        last = next((seen_at for seen, seen_at in reversed(self.reported) if seen == key), None)
        if last and now - WINDOW <= last:
            return
        pods = [prior, sample] if prior is not None else [sample]
        self.emit(Signal(namespace, "Node", node, CNI_VERSION_MISMATCH, CNI_EVENT_SOURCE, {
            "node": node, "configured_version": configured, "supported_versions": list(supported),
            "observed_at": at.isoformat(),
            "pods": [{**{k: p[k] for k in ("pod", "pod_uid", "event_uid")}, "observed_at": p["at"].isoformat()}
                     for p in pods],
            "warning_count": len(pods) if prior is not None else count,
        }))
        self._remember(self.reported, key, now)
