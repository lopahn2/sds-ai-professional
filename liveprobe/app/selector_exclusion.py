"""Read-only proof for a managed Service whose app selector excludes its ready Deployment."""
from datetime import datetime, timezone

from .k8s import labels_match
from .service_health import read_backend

AUTHORITY = "service_selector_authority"
SIGNALS = frozenset({"NoEndpoints", "TargetServiceNoEndpoints"})
PROOF_KEY = "selector_exclusion"


def _controlled_by(resource, kind, md):
    return bool(md.get("uid")) and any(
        (ref.get("kind"), ref.get("name"), ref.get("uid"), ref.get("controller"))
        == (kind, md.get("name"), md["uid"], True)
        for ref in (resource.get("metadata") or {}).get("ownerReferences") or ()
    )


def prove(namespace, name, svc, deployment, replicasets, pods, backend, *, managed):
    """Prove the observed exclusion, while leaving the intended selector to the operator."""
    if (not svc or not deployment or not managed or backend.get("backend_state") != "missing"
            or backend.get("ready_endpoints") != 0):
        return None
    svc_md, dep_md = svc.get("metadata") or {}, deployment.get("metadata") or {}
    for meta in (svc_md, dep_md):
        if ((meta.get("namespace"), meta.get("name")) != (namespace, name)
                or not meta.get("uid") or meta.get("deletionTimestamp")
                or not labels_match(managed, meta.get("labels") or {})):
            return None
    system = (svc_md.get("labels") or {}).get("app.kubernetes.io/part-of")
    rv = svc_md.get("resourceVersion")
    if not rv or not system or system != (dep_md.get("labels") or {}).get("app.kubernetes.io/part-of"):
        return None
    spec, state = deployment.get("spec") or {}, deployment.get("status") or {}
    replicas, generation = spec.get("replicas"), dep_md.get("generation")
    if (type(replicas) is not int or replicas < 1 or not generation or state.get("observedGeneration") != generation
            or spec.get("paused") or any(state.get(key) != replicas for key in
                ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
        return None
    svc_spec = svc.get("spec") or {}
    selector = svc_spec.get("selector") or {}
    app = selector.get("app")
    if (not app or app == name or svc_spec.get("type", "ClusterIP") != "ClusterIP"
            or svc_spec.get("clusterIP") in (None, "", "None") or svc_spec.get("publishNotReadyAddresses")):
        return None
    labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    comparison = {**selector, "app": name}
    if not labels_match(comparison, labels):
        return None
    owners = [rs["metadata"] for rs in replicasets if _controlled_by(rs, "Deployment", dep_md)
              and (rs.get("metadata") or {}).get("namespace") == namespace]
    current = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        if meta.get("namespace") != namespace:
            continue
        if labels_match(selector, meta.get("labels") or {}):
            return None
        owned = any(_controlled_by(pod, "ReplicaSet", rs) for rs in owners)
        matches = labels_match(comparison, meta.get("labels") or {})
        if owned != matches:
            return None
        if owned:
            status = pod.get("status") or {}
            if (meta.get("deletionTimestamp") or not meta.get("uid") or not meta.get("name")
                    or status.get("phase") != "Running" or not any(
                        (condition.get("type"), condition.get("status")) == ("Ready", "True")
                        for condition in status.get("conditions") or ())):
                return None
            current.append({"name": meta["name"], "uid": meta["uid"]})
    if len(current) != replicas:
        return None
    return {"service_uid": svc_md["uid"], "service_resource_version": rv, "deployment_uid": dep_md["uid"],
            "deployment_generation": generation, "system": system, "selector_app": app, "pod_app": name,
            "ready_pods": sorted(current, key=lambda pod: pod["uid"]), "ready_endpoints": 0,
            "observed_at": datetime.now(timezone.utc).isoformat()}


def recheck(kube, namespace, name, first, *, managed):
    svc = kube.get_service(namespace, name)
    if not svc or (svc.get("metadata") or {}).get("resourceVersion") != first["service_resource_version"]:
        return None
    deployment = kube.get_deployment(namespace, name)
    if not deployment:
        return None
    backend = read_backend(kube, namespace, svc)
    verified = prove(namespace, name, svc, deployment, kube.list_replicasets(namespace), kube.list_pods(namespace),
                     backend, managed=managed)
    if verified and all(verified.get(key) == value for key, value in first.items() if key != "observed_at"):
        latest = kube.get_service(namespace, name)
        metadata = (latest or {}).get("metadata") or {}
        if (metadata.get("uid"), metadata.get("resourceVersion")) == (
                first["service_uid"], first["service_resource_version"]):
            return verified
    return None
