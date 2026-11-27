"""Approved restart windows and the Kubernetes records that prove a termination."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from uuid import UUID

from evaluation.dev_endpoint_pulse_case import INVENTORY_CALLER, INVENTORY_FIXTURE, ORDER_FIXTURE
from evaluation.dev_image_case import ROOT


CASES = {"F1-04": ("order", 9), "F1-06": ("inventory", 11), "F1-12": ("order", 17)}
PROTOCOL = "dev-crash-loop/v1"
SIGNALS = frozenset({"CrashLoopBackOff", "TargetWorkloadCrashLooping"})
RESTART_BACKOFF_RESET = timedelta(minutes=10)


@dataclass(frozen=True)
class RestartCase:
    case_id: str
    application: str
    duration: int
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

    @property
    def errors(self):
        return tuple(f'no endpoints available for service "{name}"' for name in
                     (self.workload, f"{self.workload}:8080"))


def load_case(case_id):
    service, duration = CASES[case_id]
    return approved_case(case_id, service, duration, "controlled_crash", "crash_loop")


def approved_case(case_id, service, duration, injection, signal):
    relative = f"chaos/cases/{case_id}.yaml"
    raw = (ROOT / relative).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    catalog = json.loads((ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    pinned, = (row for row in catalog["basic"]["cases"] if row["id"] == case_id)
    case = json.loads(raw)
    if ((pinned["source"], pinned["case_sha256"]) != (relative, digest)
            or (case["id"], case["target"], case["expected_signal"]) != (case_id, service, signal)
            or case["injection"] != {"mode": injection, "parameters": {"duration_seconds": duration}}):
        raise ValueError("approved restart case changed")
    return RestartCase(case_id, service, duration, digest)


def operation_window(case, operation):
    if (operation["mode"], operation["exit_code"]) != ("crash", 70):
        raise ValueError("crash acknowledgement differs from the approved window")
    return acknowledged_window(case, operation)


def acknowledged_window(case, operation):
    start = datetime.fromisoformat(operation["accepted_at"])
    end = datetime.fromisoformat(operation["expires_at"])
    op = operation["operation_id"]
    if (not start.tzinfo or not end.tzinfo or end - start != timedelta(seconds=case.duration)
            or str(UUID(op)) != op):
        raise ValueError("restart acknowledgement differs from the approved window")
    return start, end


def container_identity(status):
    key = status.get("containerID")
    count = status.get("restartCount")
    if not key or type(count) is not int or count < 0:
        raise ValueError("container identity or restart count is missing")
    return key, count


def restart_baseline(status):
    _, count = container_identity(status)
    running = (status.get("state") or {}).get("running") or {}
    started = datetime.fromisoformat(running["startedAt"].replace("Z", "+00:00"))
    if status.get("ready") is not True or not started.tzinfo:
        raise ValueError("the pre-injection application container is not healthy")
    return started, started + RESTART_BACKOFF_RESET if count else started


def crash_sample(status, before, start, expiry):
    return termination_sample(status, before, start, expiry, reason="Error", exit_code=70)


def termination_sample(status, before, start, expiry, *, reason, exit_code):
    container, restarts = container_identity(status)
    original_container, original_restarts = container_identity(before)
    if restarts < original_restarts:
        raise ValueError("restart count regressed")
    records = []
    for key in ("lastState", "state"):
        ended = (status.get(key) or {}).get("terminated") or {}
        ended_container = ended.get("containerID") or (container if key == "state" else None)
        if ((ended.get("reason"), ended.get("exitCode")) != (reason, exit_code) or not ended_container
                or (key == "lastState" and restarts == original_restarts)
                or (restarts == original_restarts and ended_container != original_container)):
            continue
        finish = datetime.fromisoformat(ended["finishedAt"].replace("Z", "+00:00"))
        # Kubelet rounds its container timestamps to seconds, unlike the millisecond acknowledgement.
        if start.replace(microsecond=0) <= finish <= expiry:
            records.append({"container_id": ended_container, "finished_at": finish.isoformat(),
                            "reason": ended["reason"], "exit_code": ended["exitCode"]})
    return {"restarts": restarts, "waiting": ((status.get("state") or {}).get("waiting") or {}).get("reason"),
            "terminations": records}


def first_error(termination):
    return termination_error(termination, reason="Error", exit_code=70)


def termination_error(termination, *, reason, exit_code):
    if (termination.get("reason"), termination.get("exit_code")) != (reason, exit_code):
        raise ValueError("the first native Kubernetes termination record is missing")
    return json.dumps({"reason": termination["reason"], "exitCode": termination["exit_code"]}, separators=(",", ":"))
