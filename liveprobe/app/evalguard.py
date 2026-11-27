"""Legacy global campaign lock and short-lived, fenced dev workload leases."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from . import db
from .models import Action, CodeActionLink, INCIDENT_OPEN_STATES, Incident, Setting

KEY = "evaluation_lock"
WORKLOAD_LEASE_DEFAULT_TTL = 600
WORKLOAD_LEASE_MAX_TTL = 900
_WORKLOAD = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_cache: tuple[float, bool] = (0.0, False)


class WorkloadLeaseConflict(RuntimeError):
    """The target cannot be reserved without interrupting an existing owner."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _value(row: Setting | None) -> dict:
    return row.value if row and isinstance(row.value, dict) else {}


def _unexpired(value: dict, now: datetime) -> bool:
    try:
        return bool(value.get("owner")) and datetime.fromisoformat(value["until"]) > now
    except (KeyError, TypeError, ValueError):
        return False


def _lease_key(namespace: str, workload: str) -> str | None:
    if namespace != "dev" or not isinstance(workload, str) or not _WORKLOAD.fullmatch(workload):
        return None
    return hashlib.sha256(f"evaluation-workload:{namespace}/{workload}".encode()).hexdigest()


def validate_workload_lease(namespace: str, workload: str, owner: str, ttl_s: int | None = None) -> str:
    key = _lease_key(namespace, workload)
    if not key or not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise ValueError("scoped lease requires namespace dev, a valid workload, and a valid owner")
    if ttl_s is not None and (type(ttl_s) is not int or not 1 <= ttl_s <= WORKLOAD_LEASE_MAX_TTL):
        raise ValueError(f"scoped lease TTL must be 1–{WORKLOAD_LEASE_MAX_TTL} seconds")
    return key


def lock_incident_fingerprint(session, fingerprint: str) -> None:
    """Serialize a lease with signal intake, including when its settings row has not been inserted yet."""
    if session.get_bind().dialect.name == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "incident:" + fingerprint})


def _same_scope(value: dict, namespace: str, workload: str) -> bool:
    return value.get("namespace") == namespace and value.get("workload") == workload


def workload_lease_active(session, namespace: str, workload: str) -> bool:
    """Check inside the intake transaction after acquiring its fingerprint lock. Never cache this result."""
    key = _lease_key(namespace, workload)
    if key is None:
        return False
    value = _value(session.get(Setting, key))
    return _same_scope(value, namespace, workload) and _unexpired(value, _now())


def _target_busy(session, namespace: str, workload: str) -> bool:
    target = (Incident.namespace == namespace, Incident.workload == workload)
    return any(session.scalar(query.limit(1)) is not None for query in (
        select(Incident.id).where(*target, Incident.status.in_(INCIDENT_OPEN_STATES)),
        select(Action.id).join(Incident, Incident.id == Action.incident_id)
            .where(*target, Action.status.in_(("proposed", "pending_approval", "approved", "executing"))),
        select(CodeActionLink.action_id).join(Incident, Incident.id == CodeActionLink.incident_id)
            .where(*target, CodeActionLink.projected_status.in_(INCIDENT_OPEN_STATES)),
    ))


def acquire_workload_lease(namespace: str, workload: str, owner: str,
                           ttl_s: int = WORKLOAD_LEASE_DEFAULT_TTL) -> dict:
    key = validate_workload_lease(namespace, workload, owner, ttl_s)
    token = secrets.token_urlsafe(32)
    with db.session() as session:
        lock_incident_fingerprint(session, f"{namespace}/{workload}")
        now = _now()
        if _unexpired(_value(session.get(Setting, KEY)), now):
            raise WorkloadLeaseConflict("a legacy evaluation campaign is active")
        row = session.get(Setting, key)
        current = _value(row)
        if current and not _same_scope(current, namespace, workload):
            raise WorkloadLeaseConflict("scoped lease identity does not match")
        if _unexpired(current, now) or _target_busy(session, namespace, workload):
            raise WorkloadLeaseConflict("the dev workload already has an active owner")
        expiration = (now + timedelta(seconds=ttl_s)).isoformat()
        scope = {"namespace": namespace, "workload": workload, "owner": owner}
        value = {**scope, "until": expiration, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    return {**scope, "token": token, "expires_at": expiration}


def _lease_owned(row: Setting | None, namespace: str, workload: str, owner: str, token: str) -> bool:
    value = _value(row)
    digest = value.get("token_sha256")
    return (_same_scope(value, namespace, workload) and value.get("owner") == owner
            and _unexpired(value, _now()) and isinstance(digest, str) and bool(_SHA256.fullmatch(digest))
            and hmac.compare_digest(digest, hashlib.sha256(token.encode()).hexdigest()))


def workload_lease_owned(namespace: str, workload: str, owner: str, token: str) -> bool:
    key = validate_workload_lease(namespace, workload, owner)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return False
    with db.session() as session:
        return (not _unexpired(_value(session.get(Setting, KEY)), _now())
                and _lease_owned(session.get(Setting, key), namespace, workload, owner, token))


def renew_workload_lease(namespace: str, workload: str, owner: str, token: str, ttl_s: int) -> str | None:
    key = validate_workload_lease(namespace, workload, owner, ttl_s)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    with db.session() as session:
        lock_incident_fingerprint(session, f"{namespace}/{workload}")
        row = session.get(Setting, key)
        if _unexpired(_value(session.get(Setting, KEY)), _now()) or not _lease_owned(row, namespace, workload, owner, token):
            return None
        current = _value(row)
        expiration = max(datetime.fromisoformat(current["until"]), _now() + timedelta(seconds=ttl_s)).isoformat()
        row.value = {**current, "until": expiration}
        return expiration


def release_workload_lease(namespace: str, workload: str, owner: str, token: str) -> bool:
    key = validate_workload_lease(namespace, workload, owner)
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return False
    with db.session() as session:
        lock_incident_fingerprint(session, f"{namespace}/{workload}")
        row = session.get(Setting, key)
        if not _lease_owned(row, namespace, workload, owner, token):
            return False
        session.delete(row)
    return True


def set_lock(owner: str, ttl_s: int = 900) -> None:
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()
    with db.session() as s:
        row = s.get(Setting, KEY)
        if row is None:
            s.add(Setting(key=KEY, value={"owner": owner, "until": until}))
        else:
            row.value = {"owner": owner, "until": until}


def clear_lock() -> None:
    with db.session() as s:
        row = s.get(Setting, KEY)
        if row is not None:
            row.value = {"owner": None, "until": datetime.now(timezone.utc).isoformat()}


def evaluation_lock_active() -> bool:
    """Cheap (5 s cache) check used on every incoming signal."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < 5.0:
        return _cache[1]
    active = False
    try:
        with db.session() as s:
            row = s.get(Setting, KEY)
            until = (row.value or {}).get("until") if row else None
            if until and (row.value or {}).get("owner"):
                active = datetime.fromisoformat(until) > datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001 - a DB blip must not silence live alerts
        active = False
    _cache = (now, active)
    return active
