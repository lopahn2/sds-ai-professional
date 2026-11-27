"""Detect failed mutations in kube-apiserver metadata audit logs."""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from .config import AppConfig
from .incidents import ADMISSION_WEBHOOK_FAILURE, RETIRED_INGRESS_API, Signal
from .observation_log import ObservationLog

SOURCE = "k8s_admission_audit"
_MUTATIONS = {"create", "update", "patch", "delete", "deletecollection"}
_DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_WEBHOOK_FAILURE = re.compile(r'^(?:Internal error occurred: )?failed calling webhook "([^"]+)":')
_RETIRED_INGRESS_GROUPS = {"extensions", "networking.k8s.io"}
_CURRENT_INGRESS_VERSION = "networking.k8s.io/v1"


def failure_signal(entry: object, cfg: AppConfig,
                   served_versions: Callable[[], set[str]] | None = None) -> Signal | None:
    if not isinstance(entry, dict) or (entry.get("apiVersion"), entry.get("kind"), entry.get("stage")) != (
        "audit.k8s.io/v1", "Event", "ResponseComplete"
    ):
        return None
    verb = entry.get("verb")
    if not isinstance(verb, str) or verb not in _MUTATIONS:
        return None
    obj, response = entry.get("objectRef"), entry.get("responseStatus")
    if not isinstance(obj, dict) or not isinstance(response, dict):
        return None
    namespace, resource = obj.get("namespace"), obj.get("resource")
    if (not isinstance(namespace, str) or not cfg.is_active_environment(namespace)
            or not isinstance(resource, str) or not _DNS_NAME.fullmatch(resource)
            or obj.get("subresource") or resource == "secrets"):
        return None
    code = response.get("code")
    if type(code) is not int or (code != 404 and not 500 <= code <= 599):
        return None
    uri = entry.get("requestURI")
    if not isinstance(uri, str) or len(uri) > 4096:
        return None
    try:
        request = urlsplit(uri)
        if any(key == "dryRun" for key, _ in parse_qsl(request.query, keep_blank_values=True, max_num_fields=40)):
            return None
        request_id = str(UUID(entry.get("auditID")))
    except (AttributeError, TypeError, ValueError):
        return None
    detail = {"audit_id": request_id, "resource": resource, "verb": verb, "http_status": code}
    for key, raw in (("object_name", obj.get("name")), ("api_group", obj.get("apiGroup"))):
        if isinstance(raw, str) and _DNS_NAME.fullmatch(raw):
            detail[key] = raw
    stamp = entry.get("stageTimestamp")
    if isinstance(stamp, str) and len(stamp) <= 40:
        try:
            when = datetime.fromisoformat(stamp)
            if when.tzinfo is not None:
                detail["observed_at"] = when.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    if code == 404:
        if served_versions is None or request.scheme or request.netloc or request.fragment:
            return None
        return _retired_ingress_signal(namespace, obj, response, request.path, detail, served_versions)
    message = response.get("message")
    failure = _WEBHOOK_FAILURE.match(message) if isinstance(message, str) else None
    if not failure or not _DNS_NAME.fullmatch(failure[1]):
        return None
    webhook = detail["webhook"] = failure[1]
    if len(webhook) > 140:
        target = f"{webhook[:115]}-{sha256(webhook.encode()).hexdigest()[:24]}"
    else:
        target = webhook
    return Signal(namespace, "AdmissionWebhook", f"admission:{target}", ADMISSION_WEBHOOK_FAILURE, SOURCE, detail)


def _retired_ingress_signal(namespace, obj, response, path, detail, served_versions) -> Signal | None:
    group, version, name = obj.get("apiGroup"), obj.get("apiVersion"), obj.get("name", "")
    if (not isinstance(group, str) or group not in _RETIRED_INGRESS_GROUPS or version != "v1beta1"
            or detail["resource"] != "ingresses" or response.get("reason") not in (None, "NotFound")
            or not isinstance(name, str)):
        return None
    collection = f"/apis/{group}/{version}/namespaces/{namespace}/ingresses"
    verb = detail["verb"]
    if verb == "create":
        if name or path != collection:
            return None
    elif verb not in {"update", "patch"} or not _DNS_NAME.fullmatch(name) or path != f"{collection}/{name}":
        return None
    requested_version = f"{group}/{version}"
    available = served_versions()
    if requested_version in available or _CURRENT_INGRESS_VERSION not in available:
        return None
    detail.update(requested_api_version=requested_version, current_api_version=_CURRENT_INGRESS_VERSION)
    return Signal(namespace, "Ingress", f"ingress-api:{requested_version}", RETIRED_INGRESS_API, SOURCE, detail)


class AdmissionAuditWatcher(ObservationLog):
    def __init__(self, path: Path, cfg: AppConfig, emit,
                 served_versions: Callable[[], set[str]] | None = None):
        super().__init__(path, SOURCE)
        self.cfg, self.emit = cfg, emit
        self.served_versions = served_versions

    def poll_once(self) -> int:
        discovery_error = None
        versions = None

        def discover() -> set[str]:
            nonlocal versions, discovery_error
            if versions is None:
                try:
                    versions = self.served_versions()
                except Exception as exc:
                    # A discovery failure must not prevent later webhook observations.
                    discovery_error = type(exc).__name__
                    versions = set()
            return versions

        def consume(event):
            signal = failure_signal(event, self.cfg, discover if self.served_versions is not None else None)
            if signal is None:
                return False
            self.emit(signal)
            return True

        emitted = self._poll(consume)
        if discovery_error is not None:
            self.status = {"state": "degraded", "error": discovery_error, "source": "api_discovery", "emitted": emitted}
        elif self.status.get("source") != "api_discovery" or versions is not None:
            self.status = {"state": "running", "emitted": emitted}
        return emitted
