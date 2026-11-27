"""Original cases and the timing rules for a bounded Service outage."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from uuid import UUID

from evaluation.dev_image_case import ROOT
from evaluation.single_baseline import K8S_MAX_TOOL_TURNS, MODELS


PULSES = {"F4-02": ("inventory", 7), "F4-08": ("order", 13), "F4-12": ("order", 17)}
ORDER_FIXTURE = "lp-eval-order-pulse"
INVENTORY_FIXTURE = "lp-eval-inventory-pulse"
INVENTORY_CALLER = "lp-eval-order-inventory"
FIXTURES = {"order": ORDER_FIXTURE, "inventory": INVENTORY_FIXTURE}
PROTOCOL = "dev-endpoint-pulse/v1"
OBSERVE_AFTER_RECOVERY = 75
DEPENDENCY_PROBE_LIMIT_SECONDS = 6


@dataclass(frozen=True)
class Pulse:
    case_id: str
    workload: str
    duration: int
    case_sha256: str

    @property
    def application(self):
        return PULSES[self.case_id][0]

    @property
    def target(self):
        return f"dev/Service/{self.workload}"

    @property
    def dependency_caller(self):
        if self.application == "order":
            return self.workload
        return INVENTORY_CALLER if self.workload == INVENTORY_FIXTURE else "order"

    @property
    def errors(self):
        return tuple(f'no endpoints available for service "{name}"'
                     for name in (self.workload, f"{self.workload}:8080"))


def load_case(case_id, *, isolated_order=False, isolated_inventory=False):
    workload, duration = PULSES[case_id]
    path = f"chaos/cases/{case_id}.yaml"
    raw = (ROOT / path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    catalog = json.loads((ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    record, = [row for row in catalog["basic"]["cases"] if row["id"] == case_id]
    definition = json.loads(raw)
    expected = {"mode": "controlled_endpoint_off", "parameters": {"duration_seconds": duration}}
    if ((record["source"], record["case_sha256"]) != (path, digest)
            or (definition["id"], definition["target"], definition["injection"], definition["expected_signal"])
            != (case_id, workload, expected, "upstream_5xx")):
        raise ValueError("approved short endpoint case changed")
    if isolated_order and workload != "order":
        raise ValueError("the isolated order fixture is only for the pinned order cases")
    if isolated_inventory and workload != "inventory":
        raise ValueError("the isolated inventory fixture is only for the pinned inventory case")
    return Pulse(case_id, FIXTURES[workload] if isolated_order or isolated_inventory else workload, duration, digest)


def receipt_times(case, receipt):
    start = datetime.fromisoformat(receipt["accepted_at"])
    end = datetime.fromisoformat(receipt["expires_at"])
    if (receipt["mode"] != "endpoint_off" or str(UUID(receipt["operation_id"])) != receipt["operation_id"]
            or not start.tzinfo or not end.tzinfo or (end - start).total_seconds() != case.duration):
        raise ValueError("workload accepted a different fault or duration")
    return start, end


def bounded_termination(case, operation, events):
    if len(events) != 1 or not events[0].get("timestamp"):
        return None
    start, expires = receipt_times(case, operation)
    try:
        at = datetime.fromisoformat(events[0]["timestamp"])
    except (ValueError, TypeError):
        return None
    if not at.tzinfo:
        return None
    actor = events[0]["event"]
    if (actor == "expired" and at >= expires) or (actor == "reset" and start <= at < expires):
        return actor, at
    return None


def standalone_completion(record):
    calls = record.get("calls") or []
    requests = record.get("investigation") or []
    model, effort, _ = MODELS[record["arm"]]
    duration = record.get("model_evaluation_duration_ms")
    if (not 1 <= len(calls) == len(requests) <= K8S_MAX_TOOL_TURNS
            or any((c.get("status"), c.get("model"), c.get("effort")) != ("ok", model, effort) for c in calls)
            or type(duration) is not int or duration <= 0):
        return None
    if requests[-1]["request"]["tool"] == "finish":
        return "standalone_reported_completion"
    if len(calls) == K8S_MAX_TOOL_TURNS and all(row["request"]["tool"] != "finish"
                                               and row["result"].get("accepted") is not True for row in requests):
        return "standalone_tool_limit_without_conclusion"
    return None
