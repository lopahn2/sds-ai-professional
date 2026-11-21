"""Selected source IDs persist in the existing settings table. Only deployment config supplies origins."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from threading import Lock
from time import monotonic, time
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert

from . import db
from .auth import require_user
from .config import get_app_config, get_settings
from .models import Setting

Adapter = Literal["loki", "prometheus", "kubernetes"]
NAMES = {"kubernetes": "Kubernetes 이벤트", "loki": "Loki 로그", "prometheus": "Prometheus 메트릭"}
_lock = Lock()
_runtime_settings = None
_cache = {}  # bounded by adapter count; other workers refresh from the same DB


def bind_source_settings(settings):
    global _runtime_settings
    with _lock:
        _runtime_settings = settings
        _cache.clear()


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def locations(settings, adapter: str) -> dict[str, str]:
    if adapter == "kubernetes":
        return {"cluster": "Kubernetes API"}
    registered = dict(getattr(settings, "observation_locations", {}).get(adapter, {}))
    registered["default"] = getattr(settings, adapter + "_url")
    result = {}
    for name, addr in registered.items():
        u = urlsplit(addr)
        if (re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) and u.scheme in ("http", "https") and u.hostname
                and u.username is None and u.password is None and not u.query and not u.fragment):
            result[name] = addr.rstrip("/")
    return result


def selected(session, adapter):
    row = session.get(Setting, "observation_location:" + adapter)
    return (row.value or {}).get("location") if row else ("cluster" if adapter == "kubernetes" else "default")


def adapter_url(settings, adapter: str) -> str:
    choices = locations(settings, adapter)
    if settings is not _runtime_settings:
        # Standalone/offline collection may not start the API database.
        if not getattr(settings, "observation_locations", {}).get(adapter):
            return choices["default"]
        with db.session() as session:
            active = selected(session, adapter)
    else:
        with _lock:
            cached = _cache.get(adapter)
            if not cached or monotonic() >= cached[0]:
                with db.session() as session:
                    active = selected(session, adapter)
                _cache[adapter] = (monotonic() + 3, active)
            else:
                active = cached[1]
    if active not in choices:
        raise ValueError("selected observation location unavailable")
    return choices[active]


def _save(s, key: str, value: dict):
    stmt = insert(Setting).values(key=key, value=value)
    s.execute(stmt.on_conflict_do_update(index_elements=[Setting.key],
              set_={"value": stmt.excluded.value, "updated_at": datetime.now(timezone.utc)}))


def tag_summary(rows: list[dict], environments: list[str]) -> dict:
    # Missing tags stay unassigned. Never derive a system by guessing from the resource name.
    missing = {key: 0 for key in ("namespace", "app", "system")}
    identities = {}
    unassigned = 0
    for row in rows:
        absent = [key for key in missing if not row.get(key)]
        if row.get("namespace") and row["namespace"] not in environments:
            absent.append("namespace")
        for key in absent:
            missing[key] += 1
        if absent:
            unassigned += 1
        else:
            identities.setdefault((row["namespace"], row["app"]), set()).add(row["system"])
    return {"examined": len(rows), "unassigned": unassigned, "missing": missing,
            "ambiguous_services": sum(len(systems) > 1 for systems in identities.values())}


def _get_json(url: str, params: dict):
    with httpx.stream("GET", url, params=params, timeout=5, follow_redirects=False, trust_env=False) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 1_000_000:
                raise ValueError("response too large")
        return json.loads(data)


def check_location(adapter: str, url: str, kube, namespaces: list[str]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    try:
        failed_scrapes = 0
        if adapter == "kubernetes":
            if kube is None:
                raise RuntimeError("observer unavailable")
            rows = []
            for ns in namespaces:
                # Same read-only event API and workload identity used by the live watcher.
                kube.list_events(ns)
                for d in kube.list_deployments(ns):
                    md = d.get("metadata") or {}
                    labels = md.get("labels") or {}
                    rows.append({"namespace": md.get("namespace"), "app": labels.get("app"),
                                 "system": labels.get("app.kubernetes.io/part-of")})
        else:
            matcher = "namespace=~" + json.dumps("|".join(map(re.escape, namespaces)))
            if adapter == "prometheus":
                query = 'up{job="kubernetes-pods",' + matcher + '}'
                data = _get_json(url + "/api/v1/query", {"query": query})
                if data.get("status") != "success" or data["data"].get("resultType") != "vector":
                    raise ValueError("invalid query result")
                result = data["data"]["result"]
                rows = [r["metric"] for r in result]
                failed_scrapes = sum(float(r["value"][1]) == 0 for r in result)
            else:
                end = int(time() * 1e9)
                data = _get_json(url + "/loki/api/v1/series", {
                    "match[]": "{" + matcher + "}", "start": end - 300_000_000_000, "end": end})
                if data.get("status") != "success" or not isinstance(data.get("data"), list):
                    raise ValueError("invalid series result")
                rows = data["data"]
        state = "observed" if rows else "no_samples"
        if failed_scrapes:
            state = "scrape_failed" if failed_scrapes == len(rows) else "partial"
        return {"state": state, "checked_at": now, "tags": tag_summary(rows, namespaces),
                **({"failed_scrapes": failed_scrapes} if adapter == "prometheus" else {})}
    except Exception:
        # Kubernetes ApiException may include complete response bodies. Return only the state.
        return {"state": "unavailable", "checked_at": now}


def source_router(state: dict) -> APIRouter:
    router = APIRouter(prefix="/api/settings/observation-sources", dependencies=[Depends(require_user)])

    @router.get("")
    def sources():
        items = []
        with db.session() as session:
            for adapter, label in NAMES.items():
                choices = locations(get_settings(), adapter)
                active = selected(session, adapter)
                row = session.get(Setting, "observation_check:" + adapter)
                check = row.value if row and row.value.get("location") == active else None
                items.append({"adapter": adapter, "label": label, "active": active,
                              "locations": [{"id": k, "label": addr if adapter == "kubernetes" else urlsplit(addr).netloc}
                                            for k, addr in choices.items()],
                              "check": check})
        return {"sources": items}

    @router.put("/{adapter}")
    def save(adapter: Adapter, body: LocationIn, user: str = Depends(require_user)):
        if body.location not in locations(get_settings(), adapter):
            raise HTTPException(422, "등록된 수집 위치를 선택하세요.")
        with db.session() as session:
            _save(session, "observation_location:" + adapter, {"location": body.location})
            db.audit(session, user, "observation_location_changed", adapter=adapter, location=body.location)
        with _lock:
            _cache.pop(adapter, None)
        return {"active": body.location}

    @router.post("/{adapter}/check")
    def check(adapter: Adapter, body: LocationIn):
        choices = locations(get_settings(), adapter)
        if body.location not in choices:
            raise HTTPException(422, "등록된 수집 위치를 선택하세요.")
        result = check_location(adapter, choices[body.location], state.get("kube"), get_app_config().target_namespaces)
        with db.session() as session:
            _save(session, "observation_check:" + adapter, {**result, "location": body.location})
        return result

    return router
