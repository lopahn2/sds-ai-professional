"""Read-only proof of an isolated HTTP readiness-path regression."""

from datetime import timedelta

from .k8s import parse_ts, revision_of, strip_template


READINESS_PATH_AUTHORITY = "readiness_path_authority"
READINESS_404_WARNING = "Readiness probe failed: HTTP probe failed with statuscode: 404"


def _controlled_by(resource, kind, name, uid):
    return bool(uid) and any(
        (ref.get("kind"), ref.get("name"), ref.get("uid"), ref.get("controller")) == (kind, name, uid, True)
        for ref in (resource.get("metadata") or {}).get("ownerReferences") or ()
    )


def _readiness_http(container):
    return (container.get("readinessProbe") or {}).get("httpGet") or {}


def _container_probe(spec, name):
    return next((_readiness_http(row) for row in spec.get("containers") or () if row.get("name") == name), {})


def _changed_path(previous_template, current_template):
    before = strip_template(previous_template)
    after = strip_template(current_template)
    old_containers = (before.get("spec") or {}).get("containers") or []
    new_containers = (after.get("spec") or {}).get("containers") or []
    if not old_containers or len(old_containers) != len(new_containers):
        return None
    changed = []
    for old, new in zip(old_containers, new_containers):
        old_http, new_http = _readiness_http(old), _readiness_http(new)
        old_path, new_path = old_http.get("path"), new_http.get("path")
        if (old.get("name") and old["name"] == new.get("name") and old_path != new_path
                and all(isinstance(path, str) and path.startswith("/") for path in (old_path, new_path))):
            changed.append(old["name"])
            old_http["path"] = new_path
    return changed[0] if len(changed) == 1 and before == after else None


def _probe_warning(event, pod, container, earliest, now):
    ref, md = event.get("involvedObject") or {}, pod["metadata"]
    reporter = event.get("reportingComponent") or (event.get("source") or {}).get("component")
    series = event.get("series") or {}
    count = series.get("count") or event.get("count")
    last = parse_ts(series.get("lastObservedTime") or event.get("lastTimestamp") or event.get("eventTime"))
    return (event.get("type") == "Warning" and event.get("reason") == "Unhealthy" and reporter == "kubelet"
            and (ref.get("kind"), ref.get("namespace"), ref.get("name"), ref.get("uid"), ref.get("fieldPath"))
            == ("Pod", md["namespace"], md["name"], md["uid"], f"spec.containers{{{container}}}")
            and (event.get("message") or "").strip() == READINESS_404_WARNING
            and type(count) is int and count >= 2 and last and last.tzinfo and earliest <= last <= now
            and bool((event.get("metadata") or {}).get("uid")))


def confirm_readiness_path(deployment, replicasets, pods, events, namespace, now):
    from .workload_signals import rollout_overdue

    dep = deployment
    if not dep or not rollout_overdue(dep):
        return None
    md, status, spec = dep["metadata"], dep["status"], dep["spec"]
    desired, revision = spec["replicas"], revision_of(dep)
    if (type(desired) is not int or not revision or md.get("namespace") != namespace
            or (status.get("availableReplicas") or 0) < desired):
        return None
    owned = [rs for rs in replicasets if _controlled_by(rs, "Deployment", md["name"], md.get("uid"))
             and rs["metadata"].get("namespace") == namespace and not rs["metadata"].get("deletionTimestamp")]
    current = [rs for rs in owned if revision_of(rs) == revision]
    older_revision = max((revision_of(rs) for rs in owned if 0 < revision_of(rs) < revision), default=None)
    previous = [rs for rs in owned if revision_of(rs) == older_revision]
    if len(current) != 1 or len(previous) != 1:
        return None
    new_rs, old_rs = current[0], previous[0]
    new_template, old_template = new_rs["spec"]["template"], old_rs["spec"]["template"]
    if strip_template(new_template) != strip_template(spec["template"]):
        return None
    container = _changed_path(old_template, new_template)
    if container is None:
        return None
    replicasets = (new_rs["metadata"], old_rs["metadata"])
    running = [pod for pod in pods if (pod.get("metadata") or {}).get("namespace") == namespace
               and (pod["metadata"].get("uid") and pod["metadata"].get("name")
                    and not pod["metadata"].get("deletionTimestamp"))]
    new_pods, old_pods = [
        [pod for pod in running if _controlled_by(pod, "ReplicaSet", rs["name"], rs.get("uid"))]
        for rs in replicasets
    ]
    old_probe = _container_probe(old_template["spec"], container)
    old_ready = sum(
        _container_probe(pod.get("spec") or {}, container) == old_probe
        and any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in (pod.get("status") or {}).get("conditions") or ())
        for pod in old_pods
    )
    if old_ready < desired or not new_pods:
        return None
    failures = []
    expected_http = _container_probe(new_template["spec"], container)
    names = {row["name"] for row in new_template["spec"]["containers"]}
    for pod in new_pods:
        state = pod.get("status") or {}
        states = {row["name"]: row for row in state.get("containerStatuses") or ()}
        probe = _container_probe(pod.get("spec") or {}, container)
        pod_state = states.get(container) or {}
        created = parse_ts(pod["metadata"].get("creationTimestamp"))
        if (state.get("phase") != "Running" or set(states) != names
                or probe != expected_http or pod_state.get("ready") is not False
                or "running" not in (pod_state.get("state") or {})
                or any(row.get("ready") is not True for name, row in states.items() if name != container)
                or not created or not created.tzinfo):
            return None
        earliest = max(created, now - timedelta(minutes=2))
        warning = next((event for event in events if _probe_warning(event, pod, container, earliest, now)), None)
        if warning is None:
            return None
        failures.append({"pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
                         "event_uid": warning["metadata"]["uid"]})
    return {"reason": READINESS_PATH_AUTHORITY, "generation": md["generation"], "deployment_uid": md["uid"],
            "current_revision": revision, "previous_revision": older_revision, "old_ready": old_ready,
            "current_replicaset": new_rs["metadata"]["name"], "previous_replicaset": old_rs["metadata"]["name"],
            "container": container, "http_status": 404, "pods": failures, "observed_at": now.isoformat()}
