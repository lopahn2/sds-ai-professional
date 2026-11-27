"""Normalize current Service-owned EndpointSlices for routing observations."""
from __future__ import annotations

from collections.abc import Iterable


SERVICE_LABEL = "kubernetes.io/service-name"


def is_selector_service(service: dict) -> bool:
    spec = service.get("spec") or {}
    return bool(spec.get("selector")) and spec.get("type") != "ExternalName"


def endpoint_key(resource: dict) -> tuple[str, str]:
    metadata = resource["metadata"]
    return metadata["namespace"], metadata["name"]


def current_slice_addresses(services: list[dict], slices: Iterable[dict]) -> dict[tuple[str, str], set[str]]:
    """Exclude stale owners and deduplicate addresses across all slices for each Service."""
    services_by_key = {
        endpoint_key(service): service["metadata"].get("uid")
        for service in services if is_selector_service(service)
    }
    ready_by_service: dict[tuple[str, str], set[str]] = {}
    for item in slices:
        metadata = item.get("metadata") or {}
        name = (metadata.get("labels") or {}).get(SERVICE_LABEL)
        key = (metadata.get("namespace"), name)
        uid = services_by_key.get(key)
        if not uid or item.get("addressType") not in {"IPv4", "IPv6"} or metadata.get("deletionTimestamp"):
            continue
        owners = metadata.get("ownerReferences") or []
        if not any(ref.get("kind") == "Service" and ref.get("name") == name and ref.get("uid") == uid
                   for ref in owners):
            continue
        addresses = ready_by_service.setdefault(key, set())
        for endpoint in item.get("endpoints") or []:
            conditions = endpoint.get("conditions") or {}
            if conditions.get("ready") is not False and conditions.get("terminating") is not True:
                addresses.update(ip for ip in endpoint.get("addresses") or [] if ip)
    return ready_by_service
