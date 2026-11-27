"""Frozen Java order release and independent loopback business checks."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree


ORDER_SOURCE = "src/main/java/io/liveprobe/target/order/AppFaultBusiness.java"
ORDER_TEST = "src/test/java/io/liveprobe/target/order/OrderControllerTest.java"
ORDER_MAIN = "io.liveprobe.target.order.OrderApplication"
ORDER_ASSIGNMENT = "calculateOrderAmount.divisor"
ORDER_VALIDATION_ASSIGNMENT = "normalizeOrderItem.found"
ORIGINAL = 'int divisor=state.enabled("code_bug")?0:1;'
FAULTY = 'int divisor=state.enabled("code_bug")?0:(quantity==100?0:1);'
VALIDATION_ORIGINAL = 'String found=state.enabled("code_bug_validation")?null:item;'
VALIDATION_FAULTY = 'String found=state.enabled("code_bug_validation")?null:("a".equals(item)?null:item);'


class InventoryFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        item = self.path.removeprefix("/api/stock/")
        quantity = {"a": 7, "b": 0}.get(item)
        if quantity is None or self.path != f"/api/stock/{item}":
            self.send_response(404)
            payload = {"error": "unknown item"}
        else:
            self.send_response(200)
            payload = {"item": item, "quantity": quantity, "available": quantity > 0}
        body = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def order_probe(request):
    ready, _ = request("/actuator/health/readiness")
    business, data = request("/api/orders/check?item=a")
    unavailable, other = request("/api/orders/check?item=b")
    missing, _ = request("/api/orders/check?item=missing")
    stock = data.get("inventory") == {"item": "a", "quantity": 7, "available": True} and data.get("orderable") is True
    out_of_stock = (other.get("inventory") == {"item": "b", "quantity": 0, "available": False}
                    and other.get("orderable") is False)
    return {"ready": ready, "business": business, "error": data.get("error"),
            "stock_intact": stock, "unavailable_status": unavailable, "out_of_stock_intact": out_of_stock,
            "missing_status": missing, "contract_ok": ready == business == unavailable == 200 and missing == 404
            and stock and out_of_stock}


def order_creation_probe(request):
    placed, order = request("/api/orders", {"item": "a"})
    order_id = order.get("id")
    saved, existing = request(f"/api/orders/{order_id}") if type(order_id) is int and order_id > 1 else (None, {})
    seed_status, seed = request("/api/orders/1")
    invalid_status, _ = request("/api/orders", {"item": ""})
    seed_intact = seed_status == 200 and seed == {"id": 1, "item": "a", "quantity": 1, "amount": 100}
    saved_intact = saved == 200 and existing == order == {"id": order_id, "item": "a", "quantity": 1, "amount": 100}
    return {"create_status": placed, "create_error": order.get("error"), "order_id": order_id,
            "stored_status": saved, "stored_intact": saved_intact, "seed_intact": seed_intact,
            "invalid_item_status": invalid_status}


def dual_order_probe(request):
    result = {**order_probe(request), **order_creation_probe(request)}
    result["contract_ok"] = (result["contract_ok"] and result["create_status"] == 201 and result["stored_intact"]
                             and result["seed_intact"] and result["invalid_item_status"] == 400)
    return result


class OrderReleaseTrial:
    case = "order-11"
    protocol = "live-repair/v22"

    def __init__(self):
        self.scratch = TemporaryDirectory(prefix="liveprobe-order-repair-")
        self.root = Path(self.scratch.name)
        self.fixture = self.root / "order"
        self.fixture.mkdir()
        sources = Path(__file__).resolve().parents[1] / "services/order"
        shutil.copytree(sources / "src", self.fixture / "src")
        shutil.copy2(sources / "pom.xml", self.fixture / "pom.xml")
        self.fixture_files = ("pom.xml", *sorted(path.relative_to(self.fixture).as_posix()
                                                 for path in (self.fixture / "src").rglob("*") if path.is_file()))
        self.source_path = self.fixture / ORDER_SOURCE
        self.healthy_source = self.source_path.read_text()
        try:
            self.original_source = self.freeze(self.healthy_source)
        except ValueError:
            self.scratch.cleanup()
            raise
        self.java_home = Path(subprocess.check_output(["/usr/libexec/java_home", "-v", "21"], text=True).strip())
        self.java = self.java_home / "bin/java"
        self.maven_settings = self.root / "settings.xml"
        self.maven_settings.write_text("<settings/>")
        self.log = self.root / "order.log"
        state_dir = self.root / "state"
        state_dir.mkdir()
        self.opener = build_opener(ProxyHandler({}))
        self.token = secrets.token_urlsafe(32)
        self.inventory = ThreadingHTTPServer(("127.0.0.1", 0), InventoryFixture)
        self.inventory_thread = Thread(target=self.inventory.serve_forever, daemon=True)
        self.inventory_thread.start()
        self.environment = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), "JAVA_HOME": str(self.java_home),
                            "ORDER_DB_ENABLED": "false", "SELF_TRAFFIC_INTERVAL_SECONDS": "0"}
        self.service_environment = {**self.environment, "SPRING_PROFILES_ACTIVE": "chaos", "CHAOS_STATE_DIR": str(state_dir),
                                    "SERVER_PORT": "0", "SERVER_ADDRESS": "127.0.0.1", "RELEASE_FAULTS": '{"faults":{}}',
                                    "CHAOS_OPERATOR_TOKEN": self.token,
                                    "INVENTORY_URL": f"http://127.0.0.1:{self.inventory.server_port}"}
        self.process = self.port = self.classpath = None
        self.runtime = subprocess.check_output([str(self.java), "-version"], stderr=subprocess.STDOUT,
                                               text=True, timeout=4).splitlines()[0]
        self.applied = self.applied_source = 0
        self.regression_before = self.regression_after = None
        self.traces = []

    def maven(self, goal, *, test_class=None):
        test_selection = [f"-Dtest={test_class}"] if test_class else []
        return subprocess.run(["mvn", "-B", "-o", "-s", str(self.maven_settings), "-gs", str(self.maven_settings),
                               "-Dstyle.color=never", *test_selection, goal], cwd=self.fixture, env=self.environment,
                              capture_output=True, timeout=100, text=True)

    def regressions(self, *, test_class=None):
        reports = self.fixture / "target/surefire-reports"
        if reports.exists():
            shutil.rmtree(reports)
        result = self.maven("test", test_class=test_class)
        reports = [ElementTree.parse(path) for path in sorted(reports.glob("TEST-*.xml"))]
        if reports:
            classpaths = {prop.get("name"): prop.get("value") for prop in reports[0].findall("properties/property")
                          if prop.get("name") in ("java.class.path", "surefire.test.class.path")}
            self.classpath = classpaths.get("surefire.test.class.path") or classpaths.get("java.class.path")
        return {"suite": "order/maven-test", "exit_code": result.returncode,
                "tests": sum(int(report.getroot().get("tests", "0")) for report in reports)}

    def freeze(self, source):
        if source.count(ORIGINAL) != 1:
            raise ValueError("the frozen order calculation source changed")
        return source.replace(ORIGINAL, FAULTY)

    def request(self, path, payload=None, *, timeout=3):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if path == "/internal/chaos/v1/app-faults":
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def restart(self):
        self.close_process()
        result = self.maven("compile")
        if not self.classpath or result.returncode:
            raise RuntimeError("the fixture Java build did not succeed")
        with self.log.open("wb") as output:
            self.process = subprocess.Popen([str(self.java), "-Xmx256m", "-XX:ActiveProcessorCount=2",
                                             "-cp", self.classpath, ORDER_MAIN], cwd=self.fixture,
                                            env=self.service_environment, stdout=output, stderr=subprocess.STDOUT)
        deadline = monotonic() + 30
        while monotonic() < deadline:
            lines = self.log.read_text(errors="replace")
            if self.process.poll() is not None:
                causes = re.findall(r"Caused by: ([\w.]+(?:Exception|Error)):", lines)
                raise RuntimeError(f"the Java fixture exited during startup ({causes[-2:]})")
            port = re.search(r"Tomcat started on port (\d+)", lines)
            if port:
                self.port = int(port[1])
                try:
                    if self.request("/actuator/health/readiness")[0] == 200:
                        return
                except URLError:
                    pass
            sleep(0.1)
        raise RuntimeError("the Java fixture failed startup readiness")

    def close_process(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.process = self.port = None

    def close(self):
        try:
            self.close_process()
        finally:
            self.inventory.shutdown()
            self.inventory_thread.join()
            self.inventory.server_close()
            self.scratch.cleanup()

    def probe(self):
        return order_probe(self.request)

    def active_faults(self):
        status, state = self.request("/internal/chaos/v1/app-faults")
        if status != 200 or not isinstance(state.get("active"), list):
            raise RuntimeError("the runtime state was not independently observed")
        return state["active"]

    def observe_baseline(self):
        self.regression_before = self.regressions()
        if self.regression_before["exit_code"] or not self.regression_before["tests"]:
            raise RuntimeError("the healthy order regression did not pass")
        self.restart()
        healthy = self.probe()
        self.source_path.write_text(self.original_source)
        self.restart()
        failure = self.probe()
        raw = self.log.read_text()
        event = next((json.loads(line) for line in raw.splitlines()
                      if line.startswith('{"event":"app_exception"') and '"ArithmeticException"' in line), None)
        if not healthy["contract_ok"] or not self.observed_failure(failure) or not event or self.active_faults():
            raise RuntimeError("the frozen Java fixture did not reproduce the arithmetic failure")
        self.traces = self.exception_frames(raw)
        self.trace_observed_at = datetime.now(timezone.utc).isoformat()
        return healthy, failure, json.dumps({**event, "service": "test-application"})

    def observed_failure(self, probe):
        return probe["ready"] == 200 and probe["business"] == 500 and probe["error"] == "ArithmeticException"

    def exception_frames(self, logs):
        return [self.source_frame(logs, "calculateOrderAmount", "ArithmeticException")]

    @staticmethod
    def source_frame(logs, function, exception):
        frame = re.search(rf"^java\.lang\.{exception}: ([^\r\n]+)\n"
                          rf"\s+at [\w.]+\.AppFaultBusiness\.{function}\(AppFaultBusiness\.java:(\d+)\)",
                          logs, re.M)
        if not frame:
            raise RuntimeError(f"the order service did not report {exception} at {function}")
        return {"path": ORDER_SOURCE, "line": int(frame[2]), "function": function, "exception": exception, "message": frame[1]}

    def operate(self, request):
        tool, key = request["tool"], request.get("key")
        if tool == "source":
            if key not in ("AppFaultBusiness", "AppFaultBusiness.java", "calculateOrderAmount", ORDER_SOURCE):
                return {"available_symbols": ["calculateOrderAmount"]}
            body = self.source_path.read_text()
            return {"path": ORDER_SOURCE, "source": body[body.index(" public int calculateOrderAmount("):
                                                        body.index(" public String normalizeOrderItem(")].strip()}
        if tool == "logs":
            return {"trace": self.traces[0], "probe": self.probe()}
        if tool == "workload":
            return {"state": "running", "ready": self.request("/actuator/health/readiness")[0],
                    "runtime_faults": self.active_faults(), "source_version": hashlib.sha256(self.source_path.read_bytes()).hexdigest()}
        if tool == "patch":
            expression = re.sub(r"\s+", "", request.get("value") or "")
            if (key != ORDER_ASSIGNMENT or expression not in ('1', '(1)', 'state.enabled("code_bug")?0:1',
                                                                    'state.enabled("code_bug")?0:(1)')):
                return {"accepted": False, "reason": "the tool changes only the divisor and preserves the existing named test switch"}
            return self.apply_source_change(ORDER_ASSIGNMENT, FAULTY, ORIGINAL)
        return self.probe() if tool == "probe" else {"finished": tool == "finish"}

    def apply_source_change(self, assignment, before, after):
        body = self.source_path.read_text()
        if body.count(before) != 1:
            return {"accepted": False, "reason": "the source is already changed"}
        patched = body.replace(before, after)
        self.source_path.write_text(patched)
        self.restart()
        self.applied_source += 1
        return {"accepted": True, "assignment": assignment, "from_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "to_sha256": hashlib.sha256(patched.encode()).hexdigest(), "probe": self.probe()}

    def recovery_checks(self):
        samples = [self.probe(), self.probe()]
        if self.active_faults() or not all(row["contract_ok"] for row in samples):
            return None
        self.regression_after = self.regressions()
        return samples if self.regression_after == self.regression_before else None


class OrderDualReleaseTrial(OrderReleaseTrial):
    case = "order-21"
    protocol = "live-repair/v23"

    def freeze(self, source):
        source = super().freeze(source)
        if source.count(VALIDATION_ORIGINAL) != 1:
            raise ValueError("the frozen order input source changed")
        return source.replace(VALIDATION_ORIGINAL, VALIDATION_FAULTY)

    def probe(self):
        return dual_order_probe(self.request)

    def observed_failure(self, probe):
        return (super().observed_failure(probe) and probe["create_status"] == 500
                and probe["create_error"] == "NullPointerException" and probe["stored_status"] is None
                and probe["seed_intact"] and probe["invalid_item_status"] == 400)

    def exception_frames(self, logs):
        return [*super().exception_frames(logs), self.source_frame(logs, "normalizeOrderItem", "NullPointerException")]

    def operate(self, request):
        tool, key = request["tool"], request.get("key")
        if tool == "logs":
            return {"traces": self.traces, "probe": self.probe()}
        if tool == "source":
            if key == "normalizeOrderItem":
                body = self.source_path.read_text()
                return {"path": ORDER_SOURCE, "source": body[body.index(" public String normalizeOrderItem("):
                                                            body.index(" public int serializeOrderAmount(")].strip()}
            if key in (None, ""):
                return {"available_symbols": ["calculateOrderAmount", "normalizeOrderItem"]}
        if tool == "patch" and key == ORDER_VALIDATION_ASSIGNMENT:
            expression = re.sub(r"\s+", "", request.get("value") or "")
            if expression not in ('item', '(item)', 'state.enabled("code_bug_validation")?null:item',
                                  'state.enabled("code_bug_validation")?null:(item)'):
                return {"accepted": False, "reason": "the tool changes only the normal input expression"}
            return self.apply_source_change(ORDER_VALIDATION_ASSIGNMENT, VALIDATION_FAULTY, VALIDATION_ORIGINAL)
        return super().operate(request)


ORDER_TRIALS = {trial.case: trial for trial in (OrderReleaseTrial, OrderDualReleaseTrial)}
