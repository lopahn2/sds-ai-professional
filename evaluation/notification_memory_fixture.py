"""Measure the notification allocator and delayed list request under controlled memory use."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from time import monotonic, sleep

from evaluation.live_repair import CatalogTrial, NotificationMixedTrial, PROGRAM, RuntimeTrial


SETTINGS = {"memory_leak": {"mib_per_second": 4}, "latency": {"seconds": 1}}
SLOW_REQUEST_MS = 850
RECOVERED_REQUEST_MS = 500


class NotificationMemoryTrial(RuntimeTrial):
    protocol = "live-repair/v31"

    def __init__(self):
        super().__init__("notification-20")
        self.failure = None

    def process_command(self):
        return [sys.executable, "-u", "-c", PROGRAM.replace("background=False", "background=True", 1)]

    def workload(self):
        status, state = self.request("/api/chaos")
        if status != 200:
            raise RuntimeError("the notification allocator could not be observed")
        return state

    def restart(self, *, fault):
        if not fault:
            super().restart(fault=False)
            return
        try:
            for name, parameters in SETTINGS.items():
                status, state = self.request("/api/chaos", {"fault": name, "on": True,
                                                           "params": parameters, "ttl_seconds": 300})
                if status != 200 or name not in state.get("active", []):
                    raise RuntimeError("a compound notification setting did not take effect")
            growth = [self.workload()["allocated_mib"]]
            current = self.probe()
            deadline = monotonic() + 4
            while len(growth) < 3 and monotonic() < deadline:
                allocated = self.workload()["allocated_mib"]
                if allocated > growth[-1]:
                    growth.append(allocated)
                sleep(0.1)
            if (len(growth) != 3 or growth[-1] - growth[0] < 8 or current["ready"] != 200
                    or current["liveness"] != 200 or current["business"] != 200 or not current["notifications_intact"]
                    or current["duration_ms"] < SLOW_REQUEST_MS):
                raise RuntimeError(f"notification delay or retained memory was not reproduced: {current}, {growth}")
            self.failure = {**current, "allocated_mib_samples": growth, "contract_ok": False,
                            "stop_reason": "fixture_memory_containment"}
        finally:
            self.close_process()
        self.fault_enabled = True

    def probe(self):
        if self.process is None:
            return self.failure
        ready = self.request("/healthz/ready")[0]
        live = self.request("/healthz/live")[0]
        started = monotonic()
        status, body = self.request(self.path, timeout=3)
        elapsed = round((monotonic() - started) * 1000)
        intact = NotificationMixedTrial.service_contract(self, body)
        allocated = self.workload()["allocated_mib"]
        return {"ready": ready, "business": status, "liveness": live, "duration_ms": elapsed,
                "notifications_intact": intact, "allocated_mib": allocated,
                "contract_ok": ready == status == live == 200 and intact and allocated == 0
                and elapsed < RECOVERED_REQUEST_MS}

    def initial(self, failed):
        return json.dumps({"level": "ERROR", "service": "test-application", "event": "http_response_deadline_exceeded",
                           "method": "GET", "uri": self.path, "status": 200, "deadline_ms": SLOW_REQUEST_MS})

    def operate(self, request):
        tool = request["tool"]
        if tool == "source":
            symbol = request.get("key") or "tick"
            if symbol not in ("tick", "before_business"):
                return {"available_symbols": ["tick", "before_business"]}
            return self.source("runtime.py", symbol)
        if tool == "logs":
            return {"initial_observation": self.failure, "current": self.probe()}
        if tool == "workload":
            if self.process is None:
                config = json.loads((Path(self.runtime_dir.name) / "faults.json").read_text(encoding="utf-8"))
                return {"process": "stopped_by_fixture_memory_containment",
                        "configuration": {name: row["params"] for name, row in config.items()}}
            state = self.workload()
            return {"process": "running", "configuration": {entry["name"]: entry["params"]
                    for entry in state["faults"] if entry["on"]}}
        if tool == "configure":
            key = request.get("key")
            if key not in SETTINGS or str(request.get("value")).lower() != "false":
                return {"accepted": False, "reason": "only an active designated setting can be disabled"}
            if self.process is None:
                CatalogTrial.restart(self, fault=False)
            remaining = ["memory_leak"]
            try:
                remaining = self.workload()["active"]
                if key not in remaining:
                    return {"accepted": False, "reason": "the requested setting is no longer active"}
                status, state = self.request("/api/chaos", {"fault": key, "on": False})
                if status != 200:
                    return {"accepted": False, "http": status}
                remaining = state["active"]
                self.applied += 1
                return {"accepted": True, "setting": key, "still_active": remaining}
            finally:
                if "memory_leak" in remaining:
                    self.close_process()
        return CatalogTrial.operate(self, request)

    def recovery_checks(self):
        if self.process is None or self.workload()["active"]:
            return None
        samples = [self.probe()]
        sleep(1.25)
        samples.append(self.probe())
        return samples if all(sample["contract_ok"] for sample in samples) else None
