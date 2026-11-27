from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from evaluation import k8s_dev_crash_trial as trial
from evaluation.comparison_summary import dev_crash_record, repair_rows
from evaluation.dev_restart_case import CASES, PROTOCOL, crash_sample, first_error, load_case, operation_window, restart_baseline
from evaluation.k8s_dev_replica_trial import hash_text
from evaluation.live_repair import TASK


START = datetime(2026, 11, 24, 1, 0, 0, 125_000, tzinfo=timezone.utc)
OP = str(UUID(int=1))


def status(container, count, **fields):
    return {"containerID": container, "restartCount": count, **fields}


def termination(container, seconds, reason="Error", code=70):
    return {"containerID": container, "reason": reason, "exitCode": code,
            "finishedAt": (START.replace(microsecond=0) + timedelta(seconds=seconds)).isoformat()}


@pytest.mark.parametrize("case_id", CASES)
def test_catalog_windows(case_id):
    case = load_case(case_id)
    end = START + timedelta(seconds=case.duration)
    receipt = {"accepted_at": START.isoformat(), "expires_at": end.isoformat(), "operation_id": OP, "mode": "crash", "exit_code": 70}
    assert operation_window(case, receipt) == (START, end)
    with pytest.raises(ValueError, match="approved window"):
        operation_window(case, {**receipt, "expires_at": (end + timedelta(seconds=1)).isoformat()})


def test_termination_records_bind_to_first_container_and_fresh_window():
    before = status("original", 2)
    expiry = START + timedelta(seconds=9)
    assert not crash_sample(status("original", 2, lastState={"terminated": termination("old", 0)}),
                            before, START, expiry)["terminations"]
    assert not crash_sample(status("new", 3, lastState={"terminated": termination("original", -1)}),
                            before, START, expiry)["terminations"]
    assert not crash_sample(status("new", 3, lastState={"terminated": termination("original", 1, "OOMKilled", 137)}),
                            before, START, expiry)["terminations"]
    current = crash_sample(status("original", 2, state={"terminated": termination("original", 0)}), before, START, expiry)
    previous = crash_sample(status("new", 3, lastState={"terminated": termination("original", 0)}), before, START, expiry)
    assert current["terminations"] == previous["terminations"]
    assert first_error(current["terminations"][0]) == '{"reason":"Error","exitCode":70}'


def test_restarted_container_needs_ten_healthy_minutes_before_next_injection():
    running = {"ready": True, "state": {"running": {"startedAt": START.isoformat()}}}
    assert restart_baseline(status("first", 0, **running)) == (START, START)
    assert restart_baseline(status("restarted", 1, **running)) == (START, START + timedelta(minutes=10))


def test_preflight_rejects_persistent_storage_before_entering_the_container(monkeypatch):
    case = load_case("F1-06")
    spec = {"restartPolicy": "Always", "automountServiceAccountToken": False,
            "volumes": [{"name": "chaos-state", "emptyDir": {"sizeLimit": "2Mi"}}],
            "containers": [{"name": case.application,
                            "volumeMounts": [{"name": "chaos-state", "mountPath": "/var/run/liveprobe-chaos"}]}]}
    monkeypatch.setattr(trial.service, "checked_deployment", lambda *_: {"spec": {"template": {"spec": spec}}})
    reads = []
    monkeypatch.setattr(trial.live, "command", lambda *args: reads.append(args))
    trial.verify_boundary(case, {}, "pinned")
    assert reads[0][:4] == ("exec", "pod/pinned", "-c", case.application)
    spec["volumes"].append({"name": "existing-data", "persistentVolumeClaim": {"claimName": "data"}})
    with pytest.raises(trial.live.Refused, match="ephemeral"):
        trial.verify_boundary(case, {}, "pinned")
    assert len(reads) == 1


def receipt():
    case = load_case("F1-04")
    end = START + timedelta(seconds=case.duration)
    a = crash_sample(status("one", 2, state={"terminated": termination("one", 0)}), status("one", 2), START, end)
    b = crash_sample(status("two", 3, state={"terminated": termination("two", 3)},
                            lastState={"terminated": termination("one", 0)}), status("one", 2), START, end)
    c = {"waiting": "CrashLoopBackOff", "restarts": 3, "terminations": b["terminations"]}
    probes = [{"endpoint_uid": "pod", "ready": 200, "business": 200, "order_dependency": 200, "contract_ok": True} for _ in range(2)]
    first = a["terminations"][0]
    return {"case": case.case_id, "arm": "liveprobe", "protocol": PROTOCOL, "target": case.target,
            "status": "crash_observed", "case_sha256": case.case_sha256, "fixture_sha256": "0" * 64,
            "operation": {"accepted_at": START.isoformat(), "expires_at": end.isoformat(), "operation_id": OP,
                          "mode": "crash", "exit_code": 70},
            "healthy_before": {"probes": probes, "container_id": "one", "restarts": 2,
                               "running_since": (START - timedelta(minutes=12)).isoformat(),
                               "restart_backoff_eligible_at": (START - timedelta(minutes=2)).isoformat()},
            "restored": {"probes": probes, "container_id": "three", "restarts": 4},
            "fault_before": {"endpoint_uid": "pod", "contract_ok": False, "error": first_error(first),
                             "first_error_at": first["finished_at"], "first_termination": first,
                             "ready_endpoints": 0, "http_status": 503, "service_error": case.errors[0],
                             "unavailable_at": (START + timedelta(seconds=5)).isoformat()},
            "input_sha256": hash_text(TASK + first_error(first)),
            "recovered_at": (end + timedelta(seconds=3)).isoformat(), "observation_seconds": 75,
            "observations": [a, b, c], "termination_events": [{"event": "expired", "timestamp": end.isoformat()}],
            "incidents": []}


def test_crash_receipt_needs_repeated_exits_and_independent_recovery():
    record = receipt()
    result = dev_crash_record(record, "liveprobe")
    assert result["status"] == "verified_natural_recovery" and result["reason_code"] == "crash_loop_without_product_incident"
    broken = deepcopy(record)
    broken["observations"] = [broken["observations"][0], broken["observations"][0]]
    with pytest.raises(ValueError, match="repeated crash"):
        dev_crash_record(broken, "liveprobe")
    broken = deepcopy(record)
    broken["restored"]["probes"][0]["endpoint_uid"] = "replacement"
    with pytest.raises(ValueError, match="in-place"):
        dev_crash_record(broken, "liveprobe")
    broken = deepcopy(record)
    broken["termination_events"][0]["timestamp"] = START.isoformat()
    with pytest.raises(ValueError, match="attributable reset or expiry"):
        dev_crash_record(broken, "liveprobe")


def test_crash_receipt_reaches_the_paired_ledger(tmp_path):
    raw = receipt()
    folder = tmp_path / PROTOCOL.replace("/", "-") / raw["case"]
    folder.mkdir(parents=True)
    (folder / "liveprobe.json").write_text(json.dumps(raw))
    rows = list(repair_rows(Path("evaluation/catalogs/comparative-2026-11-23.json"), tmp_path))
    assert len(rows) == 375
    result, = (r for r in rows if r["case"] == raw["case"] and r["arm"] == "liveprobe")
    assert result["status"] == "verified_natural_recovery"
    assert result["total_tokens"] == 0 and result["model_calls"] == 0
