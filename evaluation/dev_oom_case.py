"""The fixed kernel OOM cases and their independently inspected Kubernetes evidence."""
from datetime import datetime

from evaluation.dev_restart_case import acknowledged_window, approved_case, termination_error, termination_sample


CASES = {
    "F2-01": ("inventory", 6), "F2-03": ("order", 8), "F2-05": ("inventory", 10),
    "F2-07": ("order", 12), "F2-10": ("inventory", 15), "F2-13": ("inventory", 18),
    "F2-15": ("order", 20),
}
PROTOCOL = "dev-native-oom/v1"
RECOVERY_OBSERVER = 'independent-read-only/v1'
ALERT = "TargetWorkloadOOMKilled"
KSM = "kube_pod_container_status_last_terminated_reason"
KSM_TERMINATED_AT = "kube_pod_container_status_last_terminated_timestamp"
ALERT_FRESHNESS_SECONDS = 120
LIMIT_MIB = 512
EXPECTED_CAP_MIB = 576
MIB = 1024 * 1024


def load_case(case_id):
    service, duration = CASES[case_id]
    return approved_case(case_id, service, duration, "controlled_oom", "oom_killed")


def operation_window(case, operation):
    if ((operation["mode"], operation["allocation_cap_mib"]) != ("oom", EXPECTED_CAP_MIB)
            or operation.get("effect") != "pending"):
        raise ValueError("OOM acknowledgement does not show the approved isolated limit")
    return acknowledged_window(case, operation)


def expiry_abort(case, operation, first_oom_at, event, readback):
    """Recognize a restarted allocator stopped at its deadline after a verified OOM."""
    if not readback or readback.get("control_off") is not True or event.get("event") != "oom_not_killed":
        return False
    start, expiry = operation_window(case, operation)
    try:
        allocations = readback["allocations"]
        alloc, = allocations
        allocated_at = datetime.fromisoformat(alloc)
        ended_at = datetime.fromisoformat(event["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    intent = readback.get("intent")
    return (isinstance(allocations, list) and isinstance(intent, dict)
            and type(intent.get("v")) is int and intent.get("aborted") is True
            and intent == {"v": 1, "operation_id": operation["operation_id"], "mode": "oom",
                           "created_ms": round(start.timestamp() * 1000),
                           "armed_until_ms": round(expiry.timestamp() * 1000), "aborted": True}
            and allocated_at.tzinfo is not None and ended_at.tzinfo is not None
            and max(start, first_oom_at) <= allocated_at < expiry <= ended_at)


def oom_sample(status, before, start, expiry):
    return termination_sample(status, before, start, expiry, reason="OOMKilled", exit_code=137)


def first_error(termination):
    return termination_error(termination, reason="OOMKilled", exit_code=137)
