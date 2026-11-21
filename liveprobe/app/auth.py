"""Kubernetes operator requests enter through the loopback gateway; machine webhooks authenticate separately."""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings
from .local_operator import SUBJECT as ACTOR



def _host_only(value: str) -> str:
    return (value or "").strip().lower().rsplit(":", 1)[0] if value.count(":") == 1 else (value or "").strip().lower()


def host_allowed(settings: Settings, host_header: str | None) -> bool:
    allowed = {h.strip().lower() for h in settings.allowed_hosts if h.strip()}
    host = (host_header or "").strip().lower()
    return bool(host) and (host in allowed or _host_only(host) in allowed)


def require_user(request: Request, settings: Settings = Depends(get_settings)) -> str:
    """Local access guard. Returns the fixed actor name used in audit rows."""
    if not host_allowed(settings, request.headers.get("host")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "host not allowed")
    from .human_sessions import local_read_key, http_error, SessionError
    from . import local_operator
    import os
    read_path = os.getenv('LIVEPROBE_LOCAL_READ_KEY_FILE')
    try:
        local_read_key(read_path).require(request.headers)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            key = local_operator.operator_key(os.getenv('LIVEPROBE_LOCAL_OPERATOR_KEY_FILE'), read_path)
            local_operator.verify(request, key)
    except SessionError as exc:
        raise http_error(exc) from None
    return ACTOR


def require_webhook(request: Request, settings: Settings = Depends(get_settings)) -> None:
    auth = request.headers.get("authorization", "")
    expected = settings.webhook_token
    if not expected or not auth.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
    if not hmac.compare_digest(auth[7:].strip().encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
