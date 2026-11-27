"""Independently verify faults in the frozen member service running on loopback."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from time import monotonic, sleep
from urllib.error import URLError

from evaluation.live_repair import CatalogTrial, NODE_PROGRAM, RuntimeTrial
from evaluation.payment_cpu_fixture import process_cpu_seconds


CRASH_MESSAGE = "장애 실험으로 프로세스를 종료합니다."
PROFILE_DEADLINE = 1.0


class MemberTrial(RuntimeTrial):
    fixture_files = ("server.js", "chaos.js", "business.js", "dependency.js", "metrics.js",
                     "static/index.html", "static/app.css", "static/app.js")

    def __init__(self, case):
        super().__init__(case)
        self.node = shutil.which("node")
        if not self.node:
            self.close()
            raise RuntimeError("Node.js is required for the frozen member fixture")
        self.runtime = subprocess.check_output([self.node, "--version"], text=True, timeout=3).strip()

    def process_command(self):
        return [self.node, "-e", NODE_PROGRAM]

    def member_workflow(self, request):
        ready = request("/healthz/ready")[0]
        live = request("/healthz/live")[0]
        business, members = request(self.path)
        login, session = request("/api/login", {"name": "김하늘"})
        profile, user = request("/api/members/1/profile")
        intact = (members.get("total") == 3 and [row["id"] for row in members.get("members", [])] == ["1", "2", "3"]
                  and session.get("member", {}).get("id") == user.get("id") == "1"
                  and session.get("displayName") == user.get("name") == "김하늘")
        missing = request("/api/members/missing/profile")[0]
        return {"ready": ready, "business": business, "liveness": live, "login": login, "profile": profile,
                "unknown_profile": missing, "data_intact": intact, "contract_ok": ready == business == live == login
                == profile == 200 and missing == 404 and intact}

    def active_configuration(self):
        status, state = self.request("/api/chaos")
        if status != 200 or not isinstance(state.get("active"), list):
            raise RuntimeError("the member runtime configuration was not available")
        return {name: state["faults"][name]["params"] for name in state["active"]}


class MemberCrashTrial(MemberTrial):
    protocol = "live-repair/v28"

    def __init__(self):
        super().__init__("member-03")
        self.exit_observations = []

    def restart(self, *, fault):
        if not fault:
            super().restart(fault=False)
            return
        status, state = self.request("/api/chaos", {"fault": self.fault, "on": True, "ttl_seconds": 300})
        if status != 200 or state.get("active") != [self.fault]:
            raise RuntimeError("the intended member crash timer did not start")
        self.fault_enabled = True
        self.exit_observations.append(self.observe_exit())
        CatalogTrial.restart(self, fault=False)
        self.fault_enabled = True
        self.exit_observations.append(self.observe_exit())

    def observe_exit(self):
        exit_code = self.process.wait(timeout=8)
        stdout = self.process.stdout.read().decode(errors="replace")
        stack = any(line == "Error: " + CRASH_MESSAGE for line in stdout.splitlines())
        if exit_code != 1 or not stack:
            raise RuntimeError("the member process did not exit with its runtime exception")
        return {"exit_code": exit_code, "runtime_exception_observed": stack}

    def probe(self):
        if self.process.poll() is not None:
            try:
                self.request(self.path, timeout=0.5)
            except URLError as failure:
                refused = isinstance(failure.reason, ConnectionRefusedError)
            else:
                refused = False
            return {"ready": None, "business": None, "contract_ok": False, "connection_refused": refused,
                    "process_exit": self.process.returncode}
        return self.member_workflow(self.request)

    def initial(self, failed):
        if (len(self.exit_observations) != 2 or failed["process_exit"] != 1 or not failed["connection_refused"]):
            raise RuntimeError("the service did not reproduce two exits and the expected refused HTTP request")
        return json.dumps({"level": "ERROR", "service": "test-application", "event": "process_exit",
                           "exception": "Error", "message": CRASH_MESSAGE}, ensure_ascii=False)

    def operate(self, request):
        tool = request["tool"]
        if tool == "source":
            if request.get("key") not in (None, "", "syncResources", "crash"):
                return {"available_symbols": ["syncResources"]}
            code = (self.fixture / "chaos.js").read_text(encoding="utf-8")
            start, end = code.index("      if(name==='crash')"), code.index("      if(name==='cpu_spin')")
            return {"path": "chaos.js", "source": code[start:end].strip()}
        if tool == "logs":
            return {"process_exits": self.exit_observations, "probe": self.probe()}
        if tool == "workload" and self.process.poll() is not None:
            state = json.loads((Path(self.runtime_dir.name) / "faults.json").read_text())
            return {"process": "exited", "configuration": {name: row["params"] for name, row in state.items()},
                    "restart_count": len(self.exit_observations) - 1}
        if tool == "workload":
            return {"process": "running", "configuration": self.active_configuration()}
        if tool == "configure":
            if request.get("key") != self.fault or str(request.get("value")).lower() != "false":
                return {"accepted": False, "reason": "only the designated runtime setting can be disabled"}
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
        sleep(3.25)
        checks.append(self.probe())
        return checks if all(check["contract_ok"] for check in checks) and not self.active_configuration() else None


class MemberTailTrial(MemberTrial):
    protocol = "live-repair/v29"

    def __init__(self, case="member-17"):
        super().__init__(case)
        self.original_process = None

    def restart(self, *, fault):
        if not fault:
            super().restart(fault=False)
            self.original_process = self.process
            return
        status, state = self.request("/api/chaos", {"fault": self.fault, "on": True,
                                                   "params": self.options, "ttl_seconds": 300})
        if status != 200 or state.get("active") != [self.fault]:
            raise RuntimeError("the intended member latency setting did not take effect")
        self.fault_enabled = True

    def profile_samples(self):
        samples = []
        for _ in range(2):
            start = monotonic()
            try:
                status, profile = self.request("/api/members/1/profile", timeout=PROFILE_DEADLINE)
            except (TimeoutError, URLError) as error:
                cause = getattr(error, "reason", error)
                if not isinstance(cause, TimeoutError):
                    raise
                status, profile = None, {}
            samples.append({"http_status": status, "duration_ms": round((monotonic() - start) * 1000),
                            "profile_intact": status == 200 and profile.get("id") == "1"
                            and profile.get("name") == "김하늘"})
        return samples

    def probe(self):
        samples = self.profile_samples()
        scope = {"ready": self.request("/healthz/ready")[0], "liveness": self.request("/healthz/live")[0],
                 "same_instance": self.process is self.original_process and self.process.poll() is None,
                 "profile_samples": samples, "deadline_ms": int(PROFILE_DEADLINE * 1000)}
        if any(not row["profile_intact"] or row["duration_ms"] >= scope["deadline_ms"] for row in samples):
            return {**scope, "business": None, "contract_ok": False}
        workflow = self.member_workflow(self.request)
        return {**workflow, **scope, "contract_ok": scope["same_instance"] and workflow["contract_ok"]}

    def initial(self, failed):
        first, second = failed["profile_samples"]
        if (failed["ready"] != 200 or failed["liveness"] != 200 or first["http_status"] is not None
                or first["duration_ms"] < 900 or not second["profile_intact"]
                or second["duration_ms"] >= failed["deadline_ms"]):
            raise RuntimeError("the member fixture did not show both a real deadline and a successful adjacent request")
        return json.dumps({"level": "ERROR", "service": "test-application", "event": "http_client_timeout",
                           "method": "GET", "uri": "/api/members/{id}/profile", "deadline_ms": failed["deadline_ms"]})

    def operate(self, request):
        tool = request["tool"]
        if tool == "source":
            if request.get("key") not in (None, "", "beforeBusiness"):
                return {"available_symbols": ["beforeBusiness"]}
            code = (self.fixture / "chaos.js").read_text(encoding="utf-8")
            start, end = code.index("  async function beforeBusiness("), code.index("  function loadRuntime(")
            return {"path": "chaos.js", "source": code[start:end].strip()}
        if tool == "logs":
            return self.probe()
        if tool == "workload":
            return {"configuration": self.active_configuration(), "probe": self.probe()}
        if tool == "configure":
            if request.get("key") != self.fault or str(request.get("value")).lower() != "false":
                return {"accepted": False, "reason": "only the designated runtime setting can be disabled"}
            if set(self.active_configuration()) != {self.fault}:
                return {"accepted": False, "reason": "the designated runtime setting is no longer active"}
            result = super().operate(request)
            return {**result, "setting": self.fault} if result["accepted"] else result
        return super().operate(request)


class MemberCpuTailTrial(MemberTailTrial):
    protocol = "live-repair/v34"
    settings = {"cpu_spin", "latency_spike"}

    def __init__(self):
        super().__init__("member-28")

    def restart(self, *, fault):
        super().restart(fault=fault)
        if fault:
            status, state = self.request("/api/chaos", {"fault": "cpu_spin", "on": True,
                                                       "params": {"threads": 2}, "ttl_seconds": 300})
            if status != 200 or set(state["active"]) != self.settings:
                raise RuntimeError("both intended member CPU and latency settings must take effect")

    def probe(self):
        before = process_cpu_seconds(self.process)
        started = monotonic()
        workflow = super().probe()
        sleep(max(0, 2 - (monotonic() - started)))
        elapsed = monotonic() - started
        consumed = process_cpu_seconds(self.process) - before
        if consumed < 0:
            raise RuntimeError("the member CPU counter moved backwards")
        state = self.request("/api/chaos")[1]
        fraction = round(consumed / elapsed, 4)
        workers = state["cpuWorkers"]
        return {**workflow, "cpu_core_fraction": fraction, "cpu_seconds": round(consumed, 4),
                "sample_seconds": round(elapsed, 4), "cpu_workers": workers,
                "contract_ok": workflow["contract_ok"] and fraction < 0.20 and workers == 0}

    def initial(self, failed):
        if failed["cpu_workers"] != 2 or failed["cpu_core_fraction"] < 1.1:
            raise RuntimeError(
                f"the recorded member CPU rate did not exceed one core: {failed['cpu_core_fraction']}")
        return super().initial(failed)

    def operate(self, request):
        tool = request["tool"]
        if tool == "source" and request.get("key") in ("cpu_spin", "syncResources"):
            code = (self.fixture / "chaos.js").read_text(encoding="utf-8")
            start, end = code.index("      if(name==='cpu_spin')"), code.index("      if(name==='worker_exhaustion')")
            return {"path": "chaos.js", "source": code[start:end].strip()}
        if tool != "configure":
            return super().operate(request)
        key = request.get("key")
        if key not in self.settings or str(request.get("value")).lower() != "false":
            return {"accepted": False, "reason": "only a designated member runtime setting can be disabled"}
        if key not in self.active_configuration():
            return {"accepted": False, "reason": "the supplied setting is no longer active"}
        status, state = self.request("/api/chaos", {"fault": key, "on": False})
        if status != 200:
            return {"accepted": False, "http": status}
        self.applied += 1
        return {"accepted": True, "setting": key, "still_active": state["active"]}

    def recovery_checks(self):
        return None if self.active_configuration() else super().recovery_checks()
