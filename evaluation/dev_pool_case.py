"""Frozen pool cases and checks shared by the dev trial and receipt reader."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

from evaluation.dev_endpoint_pulse_case import INVENTORY_FIXTURE, ORDER_FIXTURE
from evaluation.dev_image_case import ROOT


POOL_CASES = {"F3-02": ("inventory", 2), "F3-08": ("order", 8), "F3-14": ("inventory", 6)}
POOL_FIXTURES = {"F3-02": INVENTORY_FIXTURE, "F3-08": ORDER_FIXTURE, "F3-14": INVENTORY_FIXTURE}
POOL_MODE = "pool_exhaust"
POOL_DURATION_SECONDS = 30
POOL_PROTOCOL = "dev-pool-hold/v1"
POOL_ERRORS = {
    "inventory": re.compile(r"stock lookup db_error item=a error=[A-Za-z]{3,100}"),
    "order": re.compile(r"order db lookup failed item=a error=[A-Za-z]{3,100}"),
}


@dataclass(frozen=True)
class PoolCase:
    case_id: str
    application: str
    connections: int
    case_sha256: str


def load_pool_case(case_id):
    application, connections = POOL_CASES[case_id]
    relative = f"chaos/cases/{case_id}.yaml"
    source = (ROOT / relative).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    catalog = json.loads((ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    pinned, = [row for row in catalog["basic"]["cases"] if row["id"] == case_id]
    case = json.loads(source)
    if ((pinned["source"], pinned["case_sha256"]) != (relative, digest)
            or (case["id"], case["target"], case["expected_signal"]) != (case_id, application, "pool_exhaustion")
            or case["injection"] != {"mode": "controlled_pool_hold", "parameters": {"connections": connections}}):
        raise ValueError("approved pool case changed")
    return PoolCase(case_id, application, connections, digest)


def operation_window(case, operation):
    accepted = datetime.fromisoformat(operation["accepted_at"])
    expires = datetime.fromisoformat(operation["expires_at"])
    ident = operation["operation_id"]
    if ((operation["mode"], operation["held_connections"], operation["pool_max"])
            != (POOL_MODE, case.connections, case.connections)
            or str(UUID(ident)) != ident or not accepted.tzinfo or not expires.tzinfo
            or (expires - accepted).total_seconds() != POOL_DURATION_SECONDS):
        raise ValueError("pool operation differs from the approved fixture")
    return accepted, expires


def pool_log_evidence(case, operation, logs):
    accepted, expires = operation_window(case, operation)
    errors, ends = [], []
    for line in logs.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            stamp = datetime.fromisoformat(event.get("@timestamp"))
        except (ValueError, TypeError):
            continue
        if not stamp.tzinfo or stamp < accepted:
            continue
        message = event.get("message")
        if isinstance(message, str) and stamp < expires and POOL_ERRORS[case.application].fullmatch(message):
            errors.append({"message": message, "timestamp": stamp.isoformat()})
        kind = event.get("chaos_event")
        if (event.get("operation_id"), event.get("mode")) != (operation["operation_id"], POOL_MODE):
            continue
        if (kind == "reset" and stamp < expires) or (kind == "expired" and stamp >= expires):
            ends.append({"event": kind, "timestamp": stamp.isoformat()})
    first = min(errors, key=lambda e: datetime.fromisoformat(e["timestamp"]), default=None)
    return first, ends
