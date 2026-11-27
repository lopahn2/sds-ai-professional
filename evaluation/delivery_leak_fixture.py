"""Measure real delivery allocations before stopping the disposable leaking process."""
from __future__ import annotations

import json
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.error import URLError

from evaluation.live_repair import CatalogTrial, DeliveryIntermittentTrial, NODE_PROGRAM


MIB = 1024 * 1024
GROWTH_SAMPLE_LIMIT_SECONDS = 5
MEMORY_OBSERVER = r'''
const memoryFs = require('node:fs');
const memoryPath = require('node:path').join(process.env.FAULT_STATE_DIR, 'memory-observation.json');
function sampleMemory() {
    const {arrayBuffers} = process.memoryUsage();
    memoryFs.writeFileSync(memoryPath + '.tmp', JSON.stringify({pid: process.pid, arrayBuffers}), {mode: 0o600});
    memoryFs.renameSync(memoryPath + '.tmp', memoryPath);
}
sampleMemory();
setInterval(sampleMemory, 150).unref();
'''


class DeliveryLeakTrial(DeliveryIntermittentTrial):
    protocol = "live-repair/v30"

    def __init__(self):
        super().__init__("delivery-09")
        self.healthy = None
        self.failure = None

    def process_command(self):
        node, option, _ = super().process_command()
        return [node, option, NODE_PROGRAM + MEMORY_OBSERVER]

    def metrics(self):
        with self.opener.open(f"http://127.0.0.1:{self.port}/metrics", timeout=1) as response:
            values = dict(re.findall(rb"^(chaos_memory_bytes|process_resident_memory_bytes) (\d+)$",
                                     response.read(), re.M))
        if set(values) != {b"chaos_memory_bytes", b"process_resident_memory_bytes"}:
            raise RuntimeError("both delivery memory metrics must be observed")
        runtime = json.loads((Path(self.runtime_dir.name) / "memory-observation.json").read_text(encoding="utf-8"))
        if runtime["pid"] != self.process.pid:
            raise RuntimeError("the cached memory sample is from a different process")
        return {"retained_bytes": int(values[b"chaos_memory_bytes"]),
                "rss_bytes": int(values[b"process_resident_memory_bytes"]),
                "array_buffer_bytes": runtime["arrayBuffers"]}

    def restart(self, *, fault):
        if not fault:
            super().restart(fault=False)
            return
        status, state = self.request("/api/chaos", {"fault": self.fault, "on": True,
                                                   "params": self.options, "ttl_seconds": 300})
        if status != 200 or state.get("active") != [self.fault] or state.get("allocatedBytes") != 0:
            self.close_process()
            raise RuntimeError("the leak did not start from an empty allocation list")
        port = self.port
        try:
            latest = [self.metrics()]
            deadline = monotonic() + GROWTH_SAMPLE_LIMIT_SECONDS
            while len(latest) < 5 and monotonic() < deadline:
                sleep(0.15)
                sample = self.metrics()
                if sample["retained_bytes"] > latest[-1]["retained_bytes"]:
                    latest.append(sample)
                    if (len(latest) >= 3
                            and sample["array_buffer_bytes"] - latest[0]["array_buffer_bytes"] >= 12 * MIB):
                        break
            if (len(latest) < 3 or latest[-1]["retained_bytes"] < 16 * MIB
                    or latest[-1]["array_buffer_bytes"] - latest[0]["array_buffer_bytes"] < 12 * MIB):
                raise RuntimeError(f"delivery did not reproduce bounded allocation and array buffer growth: {latest}")
        finally:
            self.close_process()
        self.port = port
        try:
            self.request(self.path, timeout=0.5)
        except URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError):
                raise
        else:
            raise RuntimeError("the disposable process was still serving after the harness stopped it")
        self.failure = {"ready": None, "business": None, "contract_ok": False, "growth": latest,
                        "stop_reason": "fixture_memory_containment", "connection_refused_after_stop": True}
        self.fault_enabled = True

    def probe(self):
        if not self.process and self.failure:
            return self.failure
        ready = self.request("/healthz/ready")[0]
        live = self.request("/healthz/live")[0]
        status, shipment = self.request(self.path)
        missing = self.request("/api/deliveries/LP-EVAL-MISSING")[0]
        memory = self.metrics()
        intact = (shipment.get("tracking") == "LP1001" and shipment.get("status") == "출고"
                  and shipment.get("delayed") is False and len(shipment.get("events") or []) == 1)
        if self.healthy is None:
            self.healthy = memory
        return {"ready": ready, "business": status, "liveness": live, "shipment_intact": intact,
                "missing_status": missing, "memory": memory, "contract_ok": ready == status == live == 200
                and intact and missing == 404 and memory["retained_bytes"] == 0
                and 0 < memory["rss_bytes"] <= self.healthy["rss_bytes"] + 16 * MIB
                and memory["array_buffer_bytes"] <= self.healthy["array_buffer_bytes"] + 2 * MIB}

    def initial(self, failed):
        return json.dumps({"level": "ERROR", "service": "test-application", "event": "array_buffer_memory_growing",
                           "increase_threshold_mib": 12})

    def operate(self, request):
        tool = request["tool"]
        if tool == "logs":
            return self.failure
        if tool == "source":
            if request.get("key") not in (None, "", "syncResources", "memory_leak"):
                return {"available_symbols": ["syncResources"]}
            code = (self.fixture / "chaos.js").read_text(encoding="utf-8")
            start, end = code.index("      if(name==='memory_leak')"), code.index("      if(name==='crash')")
            return {"path": "chaos.js", "source": code[start:end].strip()}
        if tool == "workload" and not self.process:
            state = json.loads((Path(self.runtime_dir.name) / "faults.json").read_text(encoding="utf-8"))
            return {"process": "stopped_by_fixture_memory_containment",
                    "configuration": {name: row["params"] for name, row in state.items()}, "evidence": self.failure}
        if tool == "workload":
            state = self.request("/api/chaos")[1]
            return {"configuration": {name: state["faults"][name]["params"] for name in state["active"]},
                    "probe": self.probe()}
        if tool == "configure":
            if request.get("key") != self.fault or str(request.get("value")).lower() != "false":
                return {"accepted": False, "reason": "only the designated allocation setting can be disabled"}
            CatalogTrial.restart(self, fault=False)
            status, state = self.request("/api/chaos")
            if status != 200 or state.get("active") != [self.fault]:
                return {"accepted": False, "reason": "the persisted setting was no longer active at restart"}
            status, state = self.request("/api/chaos", {"fault": self.fault, "on": False})
            if status != 200 or state.get("active") != []:
                return {"accepted": False, "http": status}
            self.applied += 1
            return {"accepted": True, "setting": self.fault, "still_active": [], "restarted": True}
        return CatalogTrial.operate(self, request)

    def recovery_checks(self):
        checks = [self.probe()]
        sleep(1.25)
        checks.append(self.probe())
        return checks if self.request("/api/chaos")[1]["active"] == [] and all(
            probe["contract_ok"] for probe in checks) else None
