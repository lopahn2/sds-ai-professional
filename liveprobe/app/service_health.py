"""What Kubernetes routing evidence proves, independently of observed request failures."""
from __future__ import annotations

from .k8s import labels_match, ready_addresses


def backend_observation(service: dict, endpoint: dict | int | None, deployments: list[dict]) -> dict:
    spec = service.get("spec") or {}
    selector = spec.get("selector") or {}
    count = ready_addresses(endpoint) if endpoint is not None else None
    if spec.get("type") == "ExternalName":
        state = "external_name"
    elif not selector:
        state = "manual"
    elif count:
        state = "ready"
    else:
        def pod_labels(deployment):
            if "pod_labels" in deployment:  # compact cluster inventory
                return deployment["pod_labels"]
            template = (deployment.get("spec") or {}).get("template") or {}
            return (template.get("metadata") or {}).get("labels") or {}
        selected = [d.get("spec") or {} for d in deployments if labels_match(selector, pod_labels(d))]
        state = "scaled_down" if selected and all(d.get("replicas", 1) == 0 for d in selected) else (
            "unknown" if count is None else "missing")
    return {"backend_state": state, "ready_endpoints": count}


def service_state(summary: dict | None) -> str:
    if not summary:
        return "unknown"
    if summary.get("backend_state"):
        return summary["backend_state"]
    # Older evidence did not retain Service type or controller intent.
    count = summary.get("ready_endpoints")
    if count is None or summary.get("exists") is False:
        return "unknown"
    if "selector" in summary and not summary["selector"]:
        return "manual"
    return "ready" if count > 0 else "missing"


def read_backend(kube, ns: str, service: dict, deployments=None, *, endpoint_counts=None) -> dict:
    name = service["metadata"]["name"]
    spec = service.get("spec") or {}
    if spec.get("type") == "ExternalName":
        return backend_observation(service, None, [])
    if endpoint_counts is None:
        read_snapshot = getattr(kube, "service_endpoint_counts", None)
        endpoint = (read_snapshot([service], ns).get((ns, name)) if read_snapshot is not None
                    else kube.get_endpoints(ns, name))
    else:
        endpoint = endpoint_counts.get((ns, name))
    if (deployments is None and spec.get("selector") and spec.get("type") != "ExternalName"
            and (endpoint is None or ready_addresses(endpoint) == 0)):
        deployments = kube.list_deployments(ns)
    return backend_observation(service, endpoint, deployments or [])
