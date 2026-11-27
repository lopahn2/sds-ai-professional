"""Correlate a kubelet eviction with the affected Deployment and current Node pressure."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache
from threading import Lock

from .incidents import DISK_PRESSURE_EVICTION, DISK_PRESSURE_SOURCE, Signal
from .k8s import parse_ts


EVICTED = "Evicted"
WINDOW = timedelta(minutes=2)
_STORAGE_EVICTION = "The node was low on resource: ephemeral-storage."
_DISK_TAINT = "node.kubernetes.io/disk-pressure"


@dataclass(frozen=True, slots=True)
class EvictionObservation:
    namespace: str
    pod_uid: str
    pod: str
    node: str
    at: datetime
    message: str


def _owner(metadata, kind):
    return next((ref for ref in metadata.get("ownerReferences") or ()
                 if ref.get("kind") == kind and ref.get("controller") is True
                 and ref.get("name") and ref.get("uid")), None)


class DiskPressureSignals:
    def __init__(self, kube, emit):
        self.kube, self.emit = kube, emit
        self._lock = Lock()
        self._pending = OrderedDict()
        self._seen = OrderedDict()

    def observe(self, event, namespace, *, now=None):
        metadata, pod, source, series = (event.get(k) or {} for k in
                                         ("metadata", "involvedObject", "source", "series"))
        node = source.get("host") or event.get("reportingInstance")
        message = event.get("message") or ""
        at = parse_ts(series.get("lastObservedTime") or event.get("lastTimestamp") or event.get("eventTime"))
        now = now or datetime.now(timezone.utc)
        if (event.get("type") != "Warning" or event.get("reason") != EVICTED or pod.get("kind") != "Pod"
                or pod.get("namespace") != namespace or metadata.get("namespace") != namespace
                or (event.get("reportingComponent") or source.get("component")) != "kubelet"
                or not message.startswith(_STORAGE_EVICTION)
                or not all((metadata.get("uid"), pod.get("uid"), pod.get("name"), node))
                or not at or not at.tzinfo or not now - WINDOW <= at <= now):
            return
        key = metadata["uid"]
        with self._lock:
            if key not in self._seen:
                self._pending[key] = EvictionObservation(namespace, pod["uid"], pod["name"], node, at, message[:320])
                self._pending.move_to_end(key)
                if len(self._pending) > 128:
                    self._pending.popitem(last=False)

    def poll_once(self, pods, *, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            pending = list(self._pending.items())
        if not pending:
            return False
        by_uid = {(pod.get("metadata") or {}).get("uid"): pod for pod in pods}
        reads = (cache(self.kube.get_node), cache(self.kube.get_replicaset), cache(self.kube.get_deployment))
        for event_uid, sample in pending:
            if not now - WINDOW <= sample.at <= now:
                self._finish(event_uid, sample)
                continue
            signal = self._confirm(event_uid, sample, by_uid.get(sample.pod_uid), reads, now)
            if signal is not None:
                self.emit(signal)
                self._finish(event_uid, sample)
        return True

    def _finish(self, event_uid, sample):
        with self._lock:
            if self._pending.get(event_uid) != sample:
                return
            self._pending.pop(event_uid, None)
            self._seen[event_uid] = None
            if len(self._seen) > 128:
                self._seen.popitem(last=False)

    def _confirm(self, event_uid, sample, pod, reads, now):
        namespace, pod_name, node_name = sample.namespace, sample.pod, sample.node
        pod = pod or {}
        metadata, spec, status = (pod.get(k) or {} for k in ("metadata", "spec", "status"))
        created = parse_ts(metadata.get("creationTimestamp"))
        if ((metadata.get("namespace"), metadata.get("name")) != (namespace, pod_name)
                or not created or not created.tzinfo or created > sample.at
                or spec.get("nodeName") != node_name
                or (status.get("phase"), status.get("reason")) != ("Failed", EVICTED)):
            return None
        get_node, get_replicaset, get_deployment = reads
        node = get_node(node_name) or {}
        node_meta = node.get("metadata") or {}
        node_created = parse_ts(node_meta.get("creationTimestamp"))
        condition = next((row for row in (node.get("status") or {}).get("conditions") or ()
                          if row.get("type") == "DiskPressure"), {})
        since = parse_ts(condition.get("lastTransitionTime"))
        if (not node_meta.get("uid") or node_meta.get("name") != node_name or node_meta.get("deletionTimestamp")
                or not node_created or not node_created.tzinfo or node_created > sample.at
                or condition.get("status") != "True" or not since or not since.tzinfo or not node_created <= since <= now):
            return None
        replica_ref = _owner(metadata, "ReplicaSet")
        if replica_ref is None:
            return None
        replica = get_replicaset(namespace, replica_ref["name"]) or {}
        replica_meta = replica.get("metadata") or {}
        deployment_ref = _owner(replica_meta, "Deployment")
        if replica_meta.get("uid") != replica_ref["uid"] or deployment_ref is None:
            return None
        name = deployment_ref["name"]
        deployment = get_deployment(namespace, name) or {}
        dep_meta = deployment.get("metadata") or {}
        if (dep_meta.get("uid") != deployment_ref["uid"] or dep_meta.get("namespace") != namespace
                or dep_meta.get("name") != name or dep_meta.get("deletionTimestamp")):
            return None
        dep_status = deployment.get("status") or {}
        generation = dep_meta.get("generation")
        observed_generation = dep_status.get("observedGeneration")
        ready = (dep_status.get("readyReplicas", 0) if type(generation) is int
                 and type(observed_generation) is int and observed_generation >= generation else None)
        return Signal(namespace, "Deployment", name, DISK_PRESSURE_EVICTION, DISK_PRESSURE_SOURCE, {
            "node": node_name, "node_uid": node_meta["uid"], "disk_pressure_since": since.isoformat(),
            "disk_pressure_taint": any(t.get("key") == _DISK_TAINT and t.get("effect") == "NoSchedule"
                                       for t in (node.get("spec") or {}).get("taints") or ()),
            "deployment": name, "deployment_uid": deployment_ref["uid"], "ready_replicas": ready,
            "desired_replicas": (deployment.get("spec") or {}).get("replicas"),
            "pod": pod_name, "pod_uid": sample.pod_uid, "event_uid": event_uid,
            "eviction_resource": "ephemeral-storage", "eviction_message": sample.message,
            "observed_at": sample.at.isoformat(),
        })
