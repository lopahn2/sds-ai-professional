"""Approved delay cases and canonical first error from an actual client deadline."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

from evaluation.dev_endpoint_pulse_case import INVENTORY_CALLER, INVENTORY_FIXTURE, ORDER_FIXTURE
from evaluation.dev_image_case import ROOT


CASES = {"F3-05": ("inventory", 500), "F3-07": ("order", 700),
         "F3-09": ("inventory", 900), "F3-11": ("order", 1100)}
PROTOCOL = "dev-latency-hold/v1"
DURATION = 30
DEADLINE_MS = 400
ALERT = "TargetHighLatency"
REQUEST = {"inventory": "/api/stock/a", "order": "/api/orders/check?item=a"}
TIMEOUT = re.compile(r"curl: \(28\) Operation timed out after [0-9]+ milliseconds with 0 bytes received")


@dataclass(frozen=True)
class LatencyCase:
    case_id: str
    application: str
    delay_ms: int
    case_sha256: str

    @property
    def workload(self):
        return INVENTORY_FIXTURE if self.application == "inventory" else ORDER_FIXTURE

    @property
    def dependency_caller(self):
        return INVENTORY_CALLER if self.application == "inventory" else self.workload

    @property
    def target(self):
        return f"dev/Service/{self.workload}"


def load_case(case_id):
    application, delay = CASES[case_id]
    relative = f"chaos/cases/{case_id}.yaml"
    raw = (ROOT / relative).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    catalog = json.loads((ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    pinned, = [r for r in catalog["basic"]["cases"] if r["id"] == case_id]
    case = json.loads(raw)
    if ((pinned["source"], pinned["case_sha256"]) != (relative, digest)
            or (case["id"], case["target"], case["expected_signal"]) != (case_id, application, "slow_response")
            or case["injection"] != {"mode": "controlled_latency", "parameters": {"delay_ms": delay}}):
        raise ValueError("approved latency case changed")
    return LatencyCase(case_id, application, delay, digest)


def operation_window(case, operation):
    start = datetime.fromisoformat(operation["accepted_at"])
    end = datetime.fromisoformat(operation["expires_at"])
    operation_id = operation["operation_id"]
    if ((operation.get("mode"), operation.get("delay_ms")) != ("latency", case.delay_ms)
            or not start.tzinfo or not end.tzinfo or (end - start).total_seconds() != DURATION
            or str(UUID(operation_id)) != operation_id):
        raise ValueError("latency operation differs from the approved case")
    return start, end


def initial_error(case, sample):
    if ((sample.get("curl_exit"), sample.get("http_status"), sample.get("deadline_ms")) != (28, 0, DEADLINE_MS)
            or not TIMEOUT.fullmatch(sample.get("stderr") or "")):
        raise ValueError("the client did not log an actual deadline without a response")
    # Remove the measured elapsed digits only; the source stderr remains in the receipt.
    return f"GET {REQUEST[case.application]}: curl: (28) Operation timed out with 0 bytes received"
