"""Identify an affected business route and retain its actual Prometheus sample time."""
from __future__ import annotations

import json
import math
import re

from sqlalchemy import select


ALERT = "TargetRouteSlowRequests"
RECORDING = "liveprobe:http_business_route_slow_requests:increase1m"
METRIC = "business_routes_slow_requests_1m"
MAX_ROUTES = 32
RECORDING_INTERVAL_SECONDS = 15
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
URI_PATTERN = r"/api/[A-Za-z0-9_./{}-]{1,145}"
URI = re.compile(URI_PATTERN)


def route_key(method, uri) -> str | None:
    if isinstance(method, str) and method in METHODS and isinstance(uri, str) and URI.fullmatch(uri):
        return f"{method} {uri}"
    return None


def observed_routes(session, incident_id: int, alert: str = ALERT) -> frozenset[str]:
    from .models import AuditLog

    labels = AuditLog.detail["signal"]["detail"]["labels"]
    observed = session.execute(select(labels["method"].as_string(), labels["uri"].as_string())
        .where(AuditLog.incident_id == incident_id, AuditLog.event == "signal_observed",
               AuditLog.detail["reason"].as_string() == alert)
        .distinct().limit(MAX_ROUTES + 1)).all()
    routes = frozenset(route_key(method, uri) for method, uri in observed)
    return routes if len(observed) <= MAX_ROUTES and None not in routes else frozenset()


def sample_query(namespace: str, workload: str) -> str:
    selector = f'namespace={json.dumps(namespace)},app={json.dumps(workload)}'
    return f'{RECORDING}{{{selector}}}[45s]'


def observed_samples(rows: list[dict], namespace: str, workload: str, now: float) -> dict:
    if len(rows) > MAX_ROUTES:
        return {}
    result = {}
    duplicates = set()
    for row in rows:
        try:
            labels = row["metric"]
            if (labels["namespace"], labels["app"]) != (namespace, workload):
                continue
            key = route_key(labels.get("method"), labels.get("uri"))
            stamp, count = map(float, row["values"][-1])
            if key and now - 45 <= stamp <= now and math.isfinite(count) and count >= 0:
                if key in result:
                    duplicates.add(key)
                result[key] = {"at": stamp, "slow_requests": count}
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue
    return {key: sample for key, sample in result.items() if key not in duplicates}
