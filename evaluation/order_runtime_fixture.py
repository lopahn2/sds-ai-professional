"""Time-bound measurements of the real order service with isolated runtime settings."""
from __future__ import annotations

import json
from time import monotonic, sleep

from evaluation.order_release_fixture import OrderReleaseTrial, order_probe


ORDER_FILTER = "src/main/java/io/liveprobe/target/order/AppFaultFilter.java"
FAULT_ENDPOINT = "/internal/chaos/v1/app-faults"
READINESS = "/actuator/health/readiness"
LIVENESS = "/actuator/health/liveness"
ORDER_CHECK = "/api/orders/check?item=a"
CLIENT_DEADLINE = 1.0
RECIPES = {
    "order-01": ("live-repair/v24", "latency", {"seconds": 2}),
    "order-04": ("live-repair/v25", "not_ready", {}),
    "order-16": ("live-repair/v26", "dependency_failure", {}),
}


class OrderRuntimeTrial(OrderReleaseTrial):
    def __init__(self, case):
        self.case = case
        self.protocol, self.fault, self.parameters = RECIPES[case]
        super().__init__()
        self.access_log = self.root / "http"
        self.service_environment.update(SERVER_TOMCAT_ACCESSLOG_ENABLED="true",
                                        SERVER_TOMCAT_ACCESSLOG_PATTERN="%m %U %s",
                                        SERVER_TOMCAT_ACCESSLOG_BUFFERED="false",
                                        SERVER_TOMCAT_ACCESSLOG_DIRECTORY=str(self.access_log))
        self.original_process = None
        self.last_diagnostic = None

    def freeze(self, source):
        return source

    def probe(self):
        if self.case != "order-01":
            result = order_probe(self.request)
        else:
            result = self.latency_probe()
        result["liveness"] = self.request(LIVENESS)[0]
        result["same_instance"] = self.process is self.original_process and self.process.poll() is None
        result["contract_ok"] = result["contract_ok"] and result["liveness"] == 200 and result["same_instance"]
        return result

    def latency_probe(self):
        start = monotonic()
        try:
            status, data = self.request(ORDER_CHECK, timeout=CLIENT_DEADLINE)
        except TimeoutError:
            elapsed = round((monotonic() - start) * 1000)
            self.last_diagnostic = {"event": "client_timeout", "exception": "TimeoutError", "deadline_ms": 1000}
            return {"ready": self.request(READINESS)[0], "business": None, "contract_ok": False,
                    "duration_ms": elapsed, "deadline_ms": 1000, "deadline_timeout": True}

        elapsed = round((monotonic() - start) * 1000)

        def requests(path):
            return (status, data) if path == ORDER_CHECK else self.request(path, timeout=CLIENT_DEADLINE)

        observation = order_probe(requests)
        return {**observation, "duration_ms": elapsed, "deadline_ms": 1000, "deadline_timeout": False,
                "contract_ok": observation["contract_ok"] and elapsed < int(CLIENT_DEADLINE * 1000)}

    def observe_baseline(self):
        self.regression_before = self.regressions(test_class="OrderControllerTest")
        if self.regression_before["exit_code"] or not self.regression_before["tests"]:
            raise RuntimeError("order controller checks did not pass before the runtime trial")
        self.restart()
        self.original_process = self.process
        healthy = self.probe()
        status, state = self.request(FAULT_ENDPOINT, {"fault": self.fault, "on": True,
                                                     "params": self.parameters, "ttl_seconds": 900})
        if not healthy["contract_ok"] or status != 200 or state.get("active") != [self.fault]:
            raise RuntimeError("the runtime fixture did not activate the test setting on a healthy process")
        failed = self.probe()
        diagnostic = self.observe_first_error(failed)
        if failed["contract_ok"] or failed["liveness"] != 200 or not failed["same_instance"]:
            raise RuntimeError("the requested defect affected an unrelated health or process contract")
        return healthy, failed, json.dumps({"service": "test-application", **diagnostic})

    def observe_first_error(self, failed):
        if self.case == "order-01":
            if failed["ready"] != 200 or not failed["deadline_timeout"] or failed["duration_ms"] < 900:
                raise RuntimeError("the client did not observe the intended deadline failure")
            return self.last_diagnostic
        if self.case == "order-04":
            if failed["ready"] != 503 or failed["business"] != 200 or not failed["stock_intact"]:
                raise RuntimeError("the runtime setting did not isolate readiness")
            deadline = monotonic() + 3
            marker = f"GET {READINESS} 503"
            while monotonic() < deadline:
                if any(marker in p.read_text(errors="replace").splitlines() for p in self.access_log.glob("*.log")):
                    self.last_diagnostic = {"event": "http_access", "method": "GET", "uri": READINESS, "status": 503}
                    return self.last_diagnostic
                sleep(0.05)
            raise RuntimeError("no readiness 503 was found in the Tomcat access log")
        raw = self.log.read_text(errors="replace")
        event = next((json.loads(line) for line in raw.splitlines()
                      if line.startswith('{"event":"app_exception"') and '"DependencyError"' in line), None)
        if (failed["ready"] != 200 or failed["business"] != 502 or failed["error"] != "DependencyError"
                or not event or "ConnectException: Connection refused" not in raw):
            raise RuntimeError("the loopback service did not report an actual refused dependency connection")
        self.last_diagnostic = {"event": event["event"], "exception": event["exception"],
                                "cause": "Connection refused", "status": 502}
        return self.last_diagnostic

    def operate(self, request):
        tool = request["tool"]
        if tool == "source":
            if request.get("key") not in (None, "", ORDER_FILTER, "AppFaultFilter", "doFilterInternal"):
                return {"available_symbols": ["doFilterInternal"]}
            return {"path": ORDER_FILTER, "source": (self.fixture / ORDER_FILTER).read_text()}
        if tool == "logs":
            return {"diagnostic": self.last_diagnostic, "probe": self.probe()}
        if tool == "workload":
            status, state = self.request(FAULT_ENDPOINT)
            if status != 200:
                raise RuntimeError("unable to observe the actual order runtime configuration")
            configuration = {row["name"]: row["params"] for row in state["faults"] if row["on"]}
            return {"ready": self.request(READINESS)[0], "live": self.request(LIVENESS)[0],
                    "configuration": configuration, "process": "running" if self.process.poll() is None else "exited"}
        if tool == "configure":
            if request.get("key") != self.fault or str(request.get("value")).lower() != "false":
                return {"accepted": False, "reason": "only the designated runtime setting can be disabled on this fixture"}
            status, state = self.request(FAULT_ENDPOINT, {"fault": self.fault, "on": False})
            if status != 200 or state.get("active") != []:
                return {"accepted": False, "http": status}
            self.applied += 1
            return {"accepted": True, "setting": self.fault, "still_active": state["active"]}
        return self.probe() if tool == "probe" else {"finished": tool == "finish"}

    def recovery_checks(self):
        checks = [self.probe(), self.probe()]
        return checks if all(row["contract_ok"] for row in checks) and not self.active_faults() else None
