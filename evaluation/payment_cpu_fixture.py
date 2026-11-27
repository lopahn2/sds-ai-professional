"""Verify an actual runaway payment process and its recovery without changing the cluster."""
from __future__ import annotations

import json
import subprocess
from time import monotonic, sleep

from evaluation.live_repair import RuntimeTrial


CASE = "payment-08"
PROTOCOL = "live-repair/v27"
CPU_WINDOW_SECONDS = 2
HIGH_CPU_CORE_FRACTION = 0.65
NORMAL_CPU_CORE_FRACTION = 0.20
DIAGNOSTIC = {"level": "ERROR", "service": "test-application", "event": "process_cpu_high",
              "sample_seconds": CPU_WINDOW_SECONDS, "threshold_percent_of_core": 65}


def process_cpu_seconds(process):
    if process.poll() is not None:
        raise RuntimeError("the fixture process exited before CPU could be measured")
    sample = subprocess.check_output(["ps", "-o", "time=", "-p", str(process.pid)], text=True, timeout=2)
    parts = sample.strip().split(":")
    if len(parts) not in (2, 3):
        raise RuntimeError("the process CPU counter was unavailable")
    return sum(float(value) * 60 ** index for index, value in enumerate(reversed(parts)))


class PaymentCpuTrial(RuntimeTrial):
    protocol = PROTOCOL

    def __init__(self):
        super().__init__(CASE)
        self.original_process = None
        self.first_observation = None

    def restart(self, *, fault):
        if not fault:
            super().restart(fault=False)
            self.original_process = self.process
            return
        status, state = self.request("/api/chaos", {"fault": self.fault, "on": True,
                                                   "params": self.options, "ttl_seconds": 300})
        if status != 200 or state.get("active") != [self.fault]:
            raise RuntimeError("the payment process did not start its designated background worker")
        self.fault_enabled = True

    def probe(self):
        before = process_cpu_seconds(self.process)
        started = monotonic()
        business = super().probe()
        sleep(max(0, CPU_WINDOW_SECONDS - (monotonic() - started)))
        elapsed = monotonic() - started
        consumed = process_cpu_seconds(self.process) - before
        if consumed < 0:
            raise RuntimeError("the process CPU counter moved backwards")
        liveness = self.request("/healthz/live")[0]
        same_process = self.process is self.original_process and self.process.poll() is None
        cpu_fraction = round(consumed / elapsed, 4)
        return {**business, "business_intact": business["contract_ok"], "cpu_core_fraction": cpu_fraction,
                "cpu_seconds": round(consumed, 4), "sample_seconds": round(elapsed, 4), "liveness": liveness,
                "same_instance": same_process, "contract_ok": business["contract_ok"] and liveness == 200
                and same_process and cpu_fraction < NORMAL_CPU_CORE_FRACTION}

    def initial(self, failed):
        if (failed["cpu_core_fraction"] < HIGH_CPU_CORE_FRACTION or not failed["business_intact"]
                or failed["liveness"] != 200 or not failed["same_instance"]):
            raise RuntimeError("the worker did not reproduce the intended CPU alert with the payment ledger intact")
        self.first_observation = DIAGNOSTIC
        return json.dumps(DIAGNOSTIC)

    def operate(self, request):
        tool = request["tool"]
        if tool == "source":
            if request.get("key") not in ("", None, "_sync_workers", "_spin"):
                return {"available_symbols": ["_sync_workers", "_spin"]}
            code = (self.fixture / "runtime.py").read_text(encoding="utf-8")
            start = code.index("    def _sync_workers(")
            end = code.index("    def acquire_slot(")
            return {"path": "runtime.py", "source": code[start:end].strip()}
        if tool == "logs":
            return {"diagnostic": self.first_observation, "probe": self.probe()}
        if tool == "configure":
            key = request.get("key")
            if key != self.fault or str(request.get("value")).lower() != "false":
                return {"accepted": False, "reason": "only the designated worker may be disabled"}
            status, state = self.request("/api/chaos")
            if status != 200 or state.get("active") != [self.fault]:
                return {"accepted": False, "reason": "the worker is no longer active"}
            result = super().operate(request)
            return {**result, "setting": self.fault} if result["accepted"] else result
        return super().operate(request)
