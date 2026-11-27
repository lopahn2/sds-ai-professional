"""Read-only incident signals from current Kubernetes Pod and Deployment states."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re

from .incidents import Signal
from .k8s import parse_ts


SCHEDULING = "FailedScheduling"
CONFIGURATION = "RequiredContainerReferenceMissing"
ROLLOUT = "ProgressDeadlineExceeded"
REPLICA_CREATION = "ReplicaCreationFailed"
LIVENESS = "LivenessProbeFailed"
VOLUME_MOUNT = "FailedMount"
CSI_REGISTRATION_AUTHORITY = "csi_node_registration_authority"
REASONS = frozenset({SCHEDULING, CONFIGURATION, ROLLOUT, REPLICA_CREATION, LIVENESS, VOLUME_MOUNT})

_MISSING_OBJECT = re.compile(r'^(configmap|secret) "[^"\n]+" not found[.!]?$', re.IGNORECASE)
_MISSING_KEY = re.compile(r"^couldn.t find key \S+ in (ConfigMap|Secret) \S+[.!]?$", re.IGNORECASE)
_PROBE_WINDOW = timedelta(minutes=2)
_KILL_TO_TERMINATION = timedelta(seconds=15)
_MOUNT_WINDOW = timedelta(minutes=2)
_MOUNT_VOLUME = re.compile(r'^(?:MountVolume\.[^\n]+? |Unable to attach or mount )volume "([^"\n]+)"\s*[: ]')
_CSI_NOT_REGISTERED = re.compile(
    r'MountVolume\.MountDevice failed for volume "[^"\n]+" : kubernetes\.io/csi: '
    r'attacher\.MountDevice failed to create newCsiDriverClient: driver name '
    r'([a-z0-9][a-z0-9.-]{0,251}[a-z0-9]) not found in the list of registered CSI drivers'
)
_LOG = logging.getLogger(__name__)


@dataclass
class Episode:
    first: float
    last: float
    emitted: bool = False


def _condition(status: dict, kind: str) -> dict:
    return next((c for c in status.get("conditions") or [] if c.get("type") == kind), {})


def _controller(resource: dict, kind: str) -> dict | None:
    return next((o for o in (resource.get("metadata") or {}).get("ownerReferences") or []
                 if o.get("controller") is True and o.get("kind") == kind and o.get("name") and o.get("uid")), None)


def _pod_candidates(pod: dict):
    spec, status = pod.get("spec") or {}, pod.get("status") or {}
    scheduled = _condition(status, "PodScheduled")
    transition = scheduled.get("lastTransitionTime")
    if (status.get("phase") == "Pending" and scheduled.get("status") == "False" and scheduled.get("reason") == "Unschedulable"
            and transition and not spec.get("nodeName") and not spec.get("schedulingGates")):
        yield (SCHEDULING, transition), 120, {"condition": "PodScheduled=False", "reason": "Unschedulable"}

    spec_containers = (("init", status.get("initContainerStatuses") or []),
                       ("app", status.get("containerStatuses") or []))
    for kind, containers in spec_containers:
        for container in containers:
            wait = (container.get("state") or {}).get("waiting") or {}
            name, message = container.get("name"), (wait.get("message") or "").strip()
            if not name or wait.get("reason") != "CreateContainerConfigError":
                continue
            match = _MISSING_OBJECT.fullmatch(message) or _MISSING_KEY.fullmatch(message)
            if match:
                yield (CONFIGURATION, kind, name, message), 60, {
                    "container": name, "container_kind": kind, "reference_kind": match[1].lower(),
                    "waiting_reason": "CreateContainerConfigError",
                }


def rollout_overdue(deployment: dict) -> bool:
    md, spec, status = (deployment.get(k) or {} for k in ("metadata", "spec", "status"))
    generation, desired = md.get("generation"), spec.get("replicas")
    if (not generation or status.get("observedGeneration") != generation
            or not desired or desired < 1 or spec.get("paused") or md.get("deletionTimestamp")):
        return False
    progressing = _condition(status, "Progressing")
    return (progressing.get("status") == "False" and progressing.get("reason") == ROLLOUT
            and ((status.get("updatedReplicas") or 0) < desired
                 or (status.get("availableReplicas") or 0) < desired
                 or (status.get("replicas") or 0) > desired))


def _replica_failure(status: dict) -> dict:
    condition = _condition(status, "ReplicaFailure")
    if condition.get("status") == "True" and condition.get("reason") == "FailedCreate":
        return condition
    return {}


def _replica_failure_kind(message: str) -> str:
    lower = message.lower()
    if "exceeded quota:" in lower:
        return "quota_exceeded"
    if "serviceaccount" in lower and "not found" in lower:
        return "service_account_missing"
    if "admission webhook" in lower and "denied" in lower:
        return "admission_denied"
    return "unclassified"


def _liveness_restarts(pod, cutoff, observed_at):
    probes = {c["name"] for c in (pod.get("spec") or {}).get("containers") or []
              if c.get("name") and c.get("livenessProbe")}
    for container in (pod.get("status") or {}).get("containerStatuses") or []:
        name, restarts = container.get("name"), container.get("restartCount")
        termination = (container.get("lastState") or {}).get("terminated") or {}
        finished = parse_ts(termination.get("finishedAt"))
        if (name in probes and type(restarts) is int and restarts > 0 and finished and finished.tzinfo
                and cutoff <= finished <= observed_at):
            yield name, restarts, finished


def _probe_failure_events(events, candidates, cutoff, observed_at):
    failures, kills = {}, {}
    for event in events:
        obj = event.get("involvedObject") or {}
        key = (obj.get("uid"), obj.get("fieldPath"))
        reporter = event.get("reportingComponent") or (event.get("source") or {}).get("component")
        if key not in candidates or obj.get("kind") != "Pod" or reporter != "kubelet":
            continue
        pod, _, container, _, finished = candidates[key]
        if (obj.get("namespace"), obj.get("name")) != (pod["metadata"]["namespace"], pod["metadata"]["name"]):
            continue
        series = event.get("series") or {}
        last = parse_ts(series.get("lastObservedTime") or event.get("lastTimestamp") or event.get("eventTime"))
        if not last or not last.tzinfo or not cutoff <= last <= observed_at:
            continue
        message = event.get("message") or ""
        if (event.get("reason") == "Killing" and event.get("type") == "Normal"
                and message == f"Container {container} failed liveness probe, will be restarted"
                and timedelta(0) <= finished - last <= _KILL_TO_TERMINATION):
            kills[key] = max(last, kills.get(key, last))
        elif (event.get("reason") == "Unhealthy" and event.get("type") == "Warning"
              and message.startswith("Liveness probe failed:")):
            first = parse_ts(event.get("firstTimestamp") or event.get("eventTime"))
            count = series.get("count") or event.get("count")
            if first and first.tzinfo and cutoff <= first <= last and type(count) is int and count >= 3:
                failures.setdefault(key, []).append((last, count))
    matches = {}
    for key, killed_at in kills.items():
        count = max((count for last, count in failures.get(key, ()) if last <= killed_at), default=0)
        if count:
            matches[key] = count
    return matches


def _pending_claim_mounts(pod):
    spec, status = pod.get("spec") or {}, pod.get("status") or {}
    if status.get("phase") != "Pending" or not spec.get("nodeName"):
        return {}
    started = {c.get("name") for key in ("initContainerStatuses", "containerStatuses")
               for c in status.get(key) or [] if c.get("containerID") or c.get("restartCount")}
    mounted = {m.get("name") for key in ("initContainers", "containers") for c in spec.get(key) or []
               if c.get("name") not in started for m in c.get("volumeMounts") or []}
    return {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes") or []
            if v.get("name") in mounted and (v.get("persistentVolumeClaim") or {}).get("claimName")}


def _persistent_mount_events(events, candidates, observed_at):
    for event in events:
        ref = event.get("involvedObject") or {}
        candidate = candidates.get(ref.get("uid"))
        if (candidate is None or event.get("type") != "Warning" or event.get("reason") != VOLUME_MOUNT
                or (event.get("reportingComponent") or (event.get("source") or {}).get("component")) != "kubelet"):
            continue
        pod, workload, mounts = candidate
        md = pod["metadata"]
        if (ref.get("kind"), ref.get("namespace"), ref.get("name")) != ("Pod", md["namespace"], md["name"]):
            continue
        series = event.get("series") or {}
        count = series.get("count") or event.get("count")
        first = parse_ts(event.get("firstTimestamp") or event.get("eventTime"))
        last = parse_ts(series.get("lastObservedTime") or event.get("lastTimestamp"))
        created = parse_ts(md.get("creationTimestamp"))
        if (type(count) is not int or count < 2 or not all(t and t.tzinfo for t in (first, last, created))
                or not created <= first <= last <= observed_at or last - first < _MOUNT_WINDOW
                or observed_at - last > _MOUNT_WINDOW):
            continue
        message = (event.get("message") or "").strip()
        match = _MOUNT_VOLUME.match(message)
        if match and match[1] not in {v.get("name") for v in pod["spec"].get("volumes") or []
                                      if not v.get("persistentVolumeClaim")}:
            yield pod, workload, mounts, match[1], count, int((last - first).total_seconds()), event


def _bound_claim(pvc, namespace, claim, volume, reported):
    md, spec, status = (pvc.get(k) or {} for k in ("metadata", "spec", "status"))
    return (md.get("namespace") == namespace and md.get("name") == claim and not md.get("deletionTimestamp")
            and bool(md.get("uid")) and status.get("phase") == "Bound" and bool(spec.get("volumeName"))
            and reported in (volume, spec["volumeName"]))


def confirm_csi_registration(kube, namespace, deployment, replicasets, pods, events, observed_at):
    """Prove a node-local registration failure for the sole replica of a current deployment."""
    md, spec, status = (deployment.get(k) or {} for k in ("metadata", "spec", "status"))
    if (md.get("namespace") != namespace or not md.get("uid") or md.get("deletionTimestamp")
            or spec.get("replicas") != 1 or (status.get("availableReplicas") or 0) != 0):
        return None
    owned = {}
    for replica in replicasets:
        ref = _controller(replica, "Deployment")
        replica_md = replica.get("metadata") or {}
        if (ref and (ref["name"], ref["uid"]) == (md["name"], md["uid"])
                and replica_md.get("namespace") == namespace and not replica_md.get("deletionTimestamp")):
            owned[replica_md["uid"]] = replica
    current_pods = []
    for pod in pods:
        ref = _controller(pod, "ReplicaSet")
        pod_md = pod.get("metadata") or {}
        replica = owned.get(ref["uid"]) if ref else None
        if (replica and ref["name"] == replica["metadata"]["name"] and pod_md.get("uid")
                and pod_md.get("namespace") == namespace and not pod_md.get("deletionTimestamp")):
            current_pods.append((pod, replica))
    if len(current_pods) != 1:
        return None
    pod, replica = current_pods[0]
    mounts = _pending_claim_mounts(pod)
    pod_status = pod.get("status") or {}
    states = [c for key in ("initContainerStatuses", "containerStatuses") for c in pod_status.get(key) or []]
    if (len(mounts) != 1 or not states or not WorkloadSignals._matches_rollout(deployment, replica)
            or any(((c.get("state") or {}).get("waiting") or {}).get("reason") not in {"ContainerCreating", "PodInitializing"}
                   or c.get("restartCount") or c.get("containerID") for c in states)):
        return None
    pod_md, node = pod["metadata"], pod["spec"]["nodeName"]
    candidate = {pod_md["uid"]: (pod, md["name"], mounts)}
    failures = list(_persistent_mount_events(events, candidate, observed_at))
    if len(failures) != 1:
        return None
    _, _, _, reported, count, duration, event = failures[0]
    match = _CSI_NOT_REGISTERED.fullmatch((event.get("message") or "").strip())
    event_uid = (event.get("metadata") or {}).get("uid")
    reporter_node = event.get("reportingInstance") or (event.get("source") or {}).get("host")
    if not match or not event_uid or reporter_node != node:
        return None
    volume, claim = next(iter(mounts.items()))
    pvc = kube.get_pvc(namespace, claim) or {}
    if not _bound_claim(pvc, namespace, claim, volume, reported) or reported != pvc["spec"]["volumeName"]:
        return None
    return {"reason": CSI_REGISTRATION_AUTHORITY, "deployment_uid": md["uid"], "pod": pod_md["name"],
            "pod_uid": pod_md["uid"], "node": node, "driver": match[1], "event_uid": event_uid,
            "volume": volume, "persistent_volume": reported, "claim": claim, "pvc_uid": pvc["metadata"]["uid"],
            "warnings": count, "warning_duration_s": duration, "observed_at": observed_at.isoformat()}


class WorkloadSignals:
    def __init__(self, namespace, kube, emit, *, max_gap: float, workloads: set[str] | None = None):
        self.namespace, self.kube, self.emit, self.max_gap = namespace, kube, emit, max_gap
        self.workloads = workloads
        self.episodes: dict[tuple, Episode] = {}

    def reset(self) -> None:
        self.episodes.clear()

    def observe(self, pods: list[dict], deployments: list[dict], *, now: float, observed_at: datetime | None = None) -> int:
        active = {d["metadata"]["uid"]: d for d in deployments if self._current(d)
                  and (self.workloads is None or d["metadata"]["name"] in self.workloads)}
        if not active:
            self.reset()
            return 0
        current: dict[tuple, Episode] = {}
        emitted = 0
        replica_sets = {}
        observed_at = observed_at or datetime.now(timezone.utc)
        cutoff = observed_at - _PROBE_WINDOW
        liveness_candidates = {}
        mount_candidates = {}

        def confirmed(key, duration):
            previous = self.episodes.get(key)
            if previous is None or not 0 <= now - previous.last <= self.max_gap:
                previous = Episode(now, now)
            current[key] = previous
            previous.last = now
            return previous if not previous.emitted and now - previous.first >= duration else None

        def replica_set_for(owner):
            key = (owner["name"], owner["uid"])
            if key not in replica_sets:
                replica_sets[key] = self.kube.get_replicaset(self.namespace, owner["name"])
            rs = replica_sets[key]
            return rs if rs and self._current(rs) and rs["metadata"]["uid"] == owner["uid"] else None

        for pod in pods:
            if not self._current(pod):
                continue
            md = pod["metadata"]
            owner = _controller(pod, "ReplicaSet")
            if owner is None:
                continue
            for signature, duration, details in _pod_candidates(pod):
                reason = signature[0]
                key = (md["uid"], owner["uid"], *signature)
                episode = confirmed(key, duration)
                if episode is None:
                    continue
                replica_set = replica_set_for(owner)
                if replica_set is None:
                    current.pop(key, None)
                    continue
                dep_ref = _controller(replica_set, "Deployment")
                deployment = active.get(dep_ref["uid"]) if dep_ref else None
                if not deployment or deployment["metadata"]["name"] != dep_ref["name"]:
                    current.pop(key, None)
                    continue
                self.emit(Signal(self.namespace, "Deployment", dep_ref["name"], reason, "workload_poller",
                                 {"pod": md["name"], "pod_uid": md["uid"], "observed_for_s": int(now - episode.first),
                                  **details}))
                episode.emitted = True
                emitted += 1
            for container, restarts, finished in _liveness_restarts(pod, cutoff, observed_at):
                episode_key = (md["uid"], LIVENESS, container)
                old_episode = self.episodes.get(episode_key)
                if old_episode and old_episode.emitted:
                    old_episode.last = now
                    current[episode_key] = old_episode
                    continue
                rs = replica_set_for(owner)
                dep_ref = _controller(rs, "Deployment") if rs else None
                deployment = active.get(dep_ref["uid"]) if dep_ref else None
                if (deployment and deployment["metadata"]["name"] == dep_ref["name"]
                        and self._matches_rollout(deployment, rs)):
                    key = (md["uid"], f"spec.containers{{{container}}}")
                    liveness_candidates[key] = (pod, dep_ref["name"], container, restarts, finished)
            mounts = _pending_claim_mounts(pod)
            if mounts:
                rs = replica_set_for(owner)
                dep_ref = _controller(rs, "Deployment") if rs else None
                deployment = active.get(dep_ref["uid"]) if dep_ref else None
                if (deployment and deployment["metadata"]["name"] == dep_ref["name"]
                        and self._matches_rollout(deployment, rs)):
                    pending_mounts = {}
                    for volume, claim in mounts.items():
                        key = (md["uid"], VOLUME_MOUNT, volume)
                        previous = self.episodes.get(key)
                        if previous and previous.emitted:
                            previous.last = now
                            current[key] = previous
                        else:
                            pending_mounts[volume] = claim
                    if pending_mounts:
                        mount_candidates[md["uid"]] = (pod, dep_ref["name"], pending_mounts)

        if liveness_candidates or mount_candidates:
            try:
                events = self.kube.list_events(self.namespace)
            except Exception as error:  # optional event evidence must not interrupt other workload observations
                _LOG.warning("workload event read failed in %s: %s", self.namespace, type(error).__name__)
            else:
                for key, count in _probe_failure_events(events, liveness_candidates, cutoff, observed_at).items():
                    pod, workload, container, restarts, _ = liveness_candidates[key]
                    self.emit(Signal(self.namespace, "Deployment", workload, LIVENESS, "workload_poller", {
                        "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"], "container": container,
                        "warnings_last_two_minutes": count, "restart_count": restarts,
                    }))
                    episode_key = (pod["metadata"]["uid"], LIVENESS, container)
                    current[episode_key] = Episode(now, now, emitted=True)
                    emitted += 1
                claims = {}
                for pod, workload, mounts, reported, count, duration, _ in _persistent_mount_events(
                        events, mount_candidates, observed_at):
                    uid = pod["metadata"]["uid"]
                    for volume, claim in mounts.items():
                        key = (uid, VOLUME_MOUNT, volume)
                        if key in current:
                            continue
                        if claim not in claims:
                            try:
                                claims[claim] = self.kube.get_pvc(self.namespace, claim) or {}
                            except Exception as error:
                                _LOG.warning("claim read failed in %s: %s", self.namespace, type(error).__name__)
                                claims[claim] = {}
                        pvc = claims[claim]
                        if not _bound_claim(pvc, self.namespace, claim, volume, reported):
                            continue
                        self.emit(Signal(self.namespace, "Deployment", workload, VOLUME_MOUNT, "workload_poller", {
                            "pod": pod["metadata"]["name"], "pod_uid": uid, "volume": volume, "claim": claim,
                            "claim_uid": pvc["metadata"]["uid"], "warnings": count, "observed_for_s": duration,
                        }))
                        current[key] = Episode(now, now, emitted=True)
                        emitted += 1

        blocked = {uid: deployment for uid, deployment in active.items() if self._missing_replicas(deployment)}
        if blocked:
            for replica_set in self.kube.list_replicasets(self.namespace):
                if not self._current(replica_set):
                    continue
                owner = _controller(replica_set, "Deployment")
                deployment = blocked.get(owner["uid"]) if owner else None
                if deployment is None:
                    continue
                md, rs_md = deployment["metadata"], replica_set["metadata"]
                revision = (md.get("annotations") or {}).get("deployment.kubernetes.io/revision")
                rs_revision = (rs_md.get("annotations") or {}).get("deployment.kubernetes.io/revision")
                rs_status = replica_set.get("status") or {}
                rs_desired = (replica_set.get("spec") or {}).get("replicas") or 0
                condition = _replica_failure(rs_status)
                transition = condition.get("lastTransitionTime")
                if (owner["name"] != md["name"] or not revision or revision != rs_revision
                        or not rs_md.get("generation") or rs_status.get("observedGeneration") != rs_md["generation"]
                        or not transition or (rs_status.get("replicas") or 0) >= rs_desired):
                    continue
                key = (md["uid"], REPLICA_CREATION, md["generation"], rs_md["uid"], transition)
                episode = confirmed(key, 60)
                if episode is None:
                    continue
                self.emit(Signal(self.namespace, "Deployment", md["name"], REPLICA_CREATION, "workload_poller", {
                    "generation": md["generation"], "replicaset": rs_md["name"], "current_revision": revision,
                    "observed_for_s": int(now - episode.first), "reason": "FailedCreate",
                    "failure_class": _replica_failure_kind(condition.get("message") or ""),
                }))
                episode.emitted = True
                emitted += 1

        for uid, deployment in active.items():
            if not rollout_overdue(deployment):
                continue
            md, status = deployment["metadata"], deployment["status"]
            episode = confirmed((uid, ROLLOUT, md["generation"]), 0)
            if episode is None:
                continue
            desired = deployment["spec"]["replicas"]
            available = status.get("availableReplicas") or 0
            self.emit(Signal(self.namespace, "Deployment", md["name"], ROLLOUT, "workload_poller", {
                "generation": md["generation"], "desired_replicas": desired, "available_replicas": available,
                "updated_replicas": status.get("updatedReplicas") or 0,
                "availability_reduced": available < desired,
            }))
            episode.emitted = True
            emitted += 1

        self.episodes = current
        return emitted

    @staticmethod
    def _matches_rollout(deployment: dict, replica_set: dict) -> bool:
        metadata, spec, status = (deployment.get(key) or {} for key in ("metadata", "spec", "status"))
        revision = (metadata.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        observed = (replica_set["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision")
        return bool(revision and revision == observed and metadata.get("generation")
                    and status.get("observedGeneration") == metadata["generation"] and not spec.get("paused"))

    @staticmethod
    def _missing_replicas(deployment: dict) -> bool:
        md, spec, status = (deployment.get(k) or {} for k in ("metadata", "spec", "status"))
        desired = spec.get("replicas") or 0
        return bool(md.get("generation") and md["generation"] == status.get("observedGeneration")
                    and not spec.get("paused") and (status.get("updatedReplicas") or 0) < desired
                    and _replica_failure(status))

    def _current(self, resource: dict) -> bool:
        md = resource.get("metadata") or {}
        return bool(md.get("uid") and md.get("name") and md.get("namespace") == self.namespace
                    and not md.get("deletionTimestamp"))
