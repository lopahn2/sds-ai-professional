"""Compare a real connection-pool hold on the isolated dev stock service."""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode

from evaluation import k8s_dev_endpoint_pulse as pulse
from evaluation import k8s_dev_image_trial as live
from evaluation import k8s_dev_selector_trial as service
from evaluation.dev_endpoint_pulse_case import INVENTORY_CALLER
from evaluation.dev_pool_case import (
    POOL_DURATION_SECONDS, POOL_FIXTURES, POOL_MODE, POOL_PROTOCOL, PoolCase, load_pool_case, operation_window,
    pool_log_evidence,
)
from evaluation.k8s_dev_replica_trial import READABLE_ERRORS, hash_text, infer
from evaluation.live_repair import TASK
from evaluation.single_baseline import MODELS


ALERT_REASON = "TargetConnectionPoolExhausted"
OBSERVE_SECONDS = 75
PRODUCT_WAIT_SECONDS = 180
PROMETHEUS_NS = "liveprobe-observability"


@dataclass(frozen=True)
class PoolFixture:
    case: PoolCase

    @property
    def case_id(self):
        return self.case.case_id

    @property
    def workload(self):
        return POOL_FIXTURES[self.case_id]

    @property
    def application(self):
        return self.case.application

    @property
    def dependency_caller(self):
        return self.workload if self.application == "order" else INVENTORY_CALLER

    @property
    def target(self):
        return f"dev/Service/{self.workload}"


SCOPE = PoolFixture(load_pool_case("F3-02"))


def state(pod, scope=SCOPE):
    response = pulse.control_response(scope, pod, "GET", "", "200")
    fields = ("mode", "pool_max", "held_connections", "operation_id", "expires_at", "readiness")
    return {key: response.get(key) for key in fields}


def idle(control, scope=SCOPE):
    return (pulse.state_is_off(control) and control.get("pool_max") == scope.case.connections
            and control.get("held_connections") == 0)


def query_pool(metric, *, at=None, scope=SCOPE):
    path = f"/api/v1/namespaces/{PROMETHEUS_NS}/services/prometheus:9090/proxy/api/v1/query?"
    query = metric + f'{{namespace="dev",app="{scope.workload}",pool="{scope.application}"}}'
    params = {"query": query}
    if at is not None:
        params = {"query": f"{query}[{POOL_DURATION_SECONDS}s]", "time": at.isoformat()}
    response = json.loads(live.command("get", "--raw", path + urlencode(params), namespace=PROMETHEUS_NS))
    if response.get("status") != "success":
        raise live.Refused("Prometheus pool query did not complete")
    results = response["data"]["result"]
    return [{"timestamp": timestamp, "value": float(value)}
            for row in results for timestamp, value in (row["values"] if at is not None else [row["value"]])]


def logs_for(pod, operation, scope):
    raw = live.command("logs", f"pod/{pod}", "-c", scope.application,
                       "--since-time=" + operation["accepted_at"], "--limit-bytes=262144")
    return pool_log_evidence(scope.case, operation, raw)


def loopback_status(pod, scope, path):
    output = live.command("exec", f"pod/{pod}", "-c", scope.application, "--", "curl", "-sS",
                          "--max-time", "6", "-o", "/dev/null", "-w", "%{http_code}",
                          "http://127.0.0.1:8080" + path)
    return int(output)


def probe_inventory_fault(pod, scope):
    with ThreadPoolExecutor(max_workers=2) as probes:
        stock = probes.submit(loopback_status, pod, scope, "/api/stock/a")
        order = probes.submit(pulse.upstream_error, scope)
        http, dependency = stock.result(), order.result()
    if http != 503 or dependency not in (500, 502, 503, 504):
        raise live.Refused("stock and dependent order failures were not observed during the hold")
    return {"http_status": http, "order_dependency": dependency}


def probe_order_fault(pod, scope):
    if loopback_status(pod, scope, "/api/orders/check?item=a") != 503:
        raise live.Refused("the original order check did not return 503 during the hold")
    route = f"/api/v1/namespaces/dev/services/{scope.workload}:8080/proxy"
    primary = json.loads(live.command("get", "--raw", route + "/api/orders/1"))
    stock_route = f"/api/v1/namespaces/dev/services/{POOL_FIXTURES['F3-02']}:8080/proxy/api/stock/a"
    stock = json.loads(live.command("get", "--raw", stock_route))
    if (primary.get("id") != 1 or stock.get("item") != "a" or type(stock.get("quantity")) is not int
            or stock["quantity"] < 0 or type(stock.get("available")) is not bool):
        raise live.Refused("independent order record and inventory reads did not remain healthy")
    return {"http_status": 503, "order_lookup": 200, "inventory_lookup": 200}


def observe_fault(pod, baseline, operation, accepted, expires, scope):
    observe_requests = probe_order_fault if scope.application == "order" else probe_inventory_fault
    request_observations = observe_requests(pod, scope)
    current = state(pod, scope)
    uid = baseline["endpoint_uid"]
    if ((current["mode"], current["operation_id"], current["held_connections"], current["readiness"])
            != (POOL_MODE, operation["operation_id"], scope.case.connections, "ACCEPTING_TRAFFIC")
            or service.endpoint_uids(scope, baseline) != [uid]):
        raise live.Refused("the same ready Pod and exact pool hold were not observed")
    error, _ = logs_for(pod, operation, scope)
    observed = datetime.now(timezone.utc)
    if not error or not accepted <= datetime.fromisoformat(error["timestamp"]) <= observed < expires:
        raise live.Refused("no original workload error from the live pool hold was observed")
    return {"error": error["message"], "first_error_at": error["timestamp"], "observed_at": observed.isoformat(),
            "endpoint_uid": uid, "held_connections": scope.case.connections, "contract_ok": False,
            **request_observations}


def model_request(scope, pod, operation, data, baseline, fault, lease):
    if data["tool"] in ("logs", "workload", "probe"):
        response = {"first_failure": fault, "current_pool": state(pod, scope),
                    "current_ready_endpoints": len(service.endpoint_uids(scope, baseline))}
        if data["tool"] == "logs":
            _, response["termination_events"] = logs_for(pod, operation, scope)
        return response
    if data["tool"] == "source":
        return {"configured_connections": state(pod, scope)["pool_max"],
                "test_control": {"configure_key": "fault_mode", "configure_value": "off",
                                 "scope": "the same active operation only"}}
    return pulse.model_request(scope, pod, operation, data, baseline, fault, lease)


def observe_product(cursor, epoch, scope, *, alert_reason=ALERT_REASON):
    start = monotonic()
    terminal_at = None
    while monotonic() - start < PRODUCT_WAIT_SECONDS:
        rows = service.incident_rows(scope, cursor)
        if terminal_at is None and any(alert_reason in (row.get("signal_reasons") or [])
                                      and row["status"] not in live.NONTERMINAL for row in rows):
            terminal_at = datetime.now(timezone.utc).isoformat()
        if (monotonic() - start >= OBSERVE_SECONDS and
                not any(row["status"] in live.NONTERMINAL for row in rows)):
            break
        sleep(5)
    if service.endpoint_poller_epoch() != epoch:
        raise live.Refused("the dev observation did not remain continuous")
    return rows, round(monotonic() - start), terminal_at


def execute(arm, baseline, pod, before, cursor, gate, output, journal, reference, *, scope=SCOPE):
    result = {"case": scope.case_id, "arm": arm, "protocol": POOL_PROTOCOL, "target": scope.target,
              "case_sha256": scope.case.case_sha256, "fixture_sha256": hash_text(json.dumps(baseline, sort_keys=True)),
              "healthy_before": {"probes": before, "contract_ok": True}, "node_gate": gate,
              "duration_seconds": POOL_DURATION_SECONDS, "status": "trial_unverified"}
    if arm != "liveprobe" and (not reference or reference.get("arm") != "liveprobe"
            or reference.get("status") != "pool_observed"
            or any(reference.get(key) != result[key] for key in ("case", "case_sha256", "fixture_sha256", "protocol"))):
        raise live.Refused("the saved product reference is for a different pool trial")
    limits = [row["value"] for row in query_pool("hikaricp_connections_max", scope=scope)]
    if not idle(state(pod, scope), scope) or limits != [scope.case.connections]:
        raise live.Refused("the idle fixture does not have the approved scraped pool capacity")
    began = datetime.now(timezone.utc)
    expires = began + timedelta(seconds=POOL_DURATION_SECONDS + 8)
    epoch = service.endpoint_poller_epoch()
    lease = None
    live.write_once(journal, {"target": scope.target, "started_at": began.isoformat(),
                              "only_fault": POOL_MODE, "maximum_duration_s": POOL_DURATION_SECONDS,
                              "receipt": str(output), "pod_uid": baseline["endpoint_uid"]})
    try:
        if arm != "liveprobe":
            lease = live.WorkloadLease.claim(scope, arm)
            lease.require_active()
        operation = pulse.control_response(scope, pod, "POST",
                                           json.dumps({"mode": POOL_MODE, "duration_seconds": POOL_DURATION_SECONDS}), "202")
        accepted, expires = operation_window(scope.case, operation)
        result["operation"] = operation
        fault = observe_fault(pod, baseline, operation, accepted, expires, scope)
        result["fault_before"] = fault
        result["input_sha256"] = hash_text(TASK + fault["error"])
        if lease:
            if result["input_sha256"] != reference.get("input_sha256"):
                raise live.Refused("the initial workload error differs from the saved product reference")
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(fault["first_error_at"])).total_seconds()
            started = monotonic() - max(0.0, elapsed)
            infer(result, arm, baseline, fault, lease, started + pulse.MODEL_BUDGET_SECONDS,
                  apply_request=partial(model_request, scope, pod, operation),
                  verify=partial(service.wait_for_healthy, scope))
            result["model_evaluation_duration_ms"] = round((monotonic() - started) * 1000)
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        try:
            until = datetime.now(timezone.utc) if result.get("standalone_action") else expires
            result["restored"] = pulse.restore_by_expiry(scope, baseline, pod, until)
            if not idle(state(pod, scope), scope):
                raise live.Refused("pool connections remained held after recovery")
            result["recovered_at"] = datetime.now(timezone.utc).isoformat()
        except READABLE_ERRORS as error:
            result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    try:
        if "fault_before" in result and "recovered_at" in result:
            _, result["termination_events"] = logs_for(pod, operation, scope)
            result["pool_active_samples"] = query_pool("hikaricp_connections_active",
                                                        at=min(expires, datetime.now(timezone.utc)), scope=scope)
            if lease:
                result["incidents"] = service.incident_rows(scope, cursor)
                if result["incidents"]:
                    raise live.Refused("LiveProbe interfered with the standalone lease")
            else:
                (result["incidents"], result["observation_seconds_after_recovery"],
                 result["product_terminal_observed_at"]) = observe_product(cursor, epoch, scope)
            if not result.get("trial_error"):
                result["status"] = "pool_observed"
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        if lease:
            try:
                lease.require_active()
                lease.release()
                result["lease_released"] = True
            except READABLE_ERRORS as error:
                result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    live.write_once(output, result)
    if "recovered_at" in result and not result.get("cleanup_error"):
        journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="F3-02", choices=POOL_FIXTURES)
    parser.add_argument("--arm", default="liveprobe", choices=("liveprobe", *MODELS))
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.arm == "liveprobe") != (args.reference is None):
        parser.error("standalone arms require the actual product receipt")
    scope = PoolFixture(load_pool_case(args.case))
    root = live.EVIDENCE
    journal = root / "dev-k8s-fault-active.json"
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference:
        from evaluation.comparison_summary import dev_pool_record

        dev_pool_record(reference, "liveprobe")
    with (root / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise live.Refused("an active trial or saved receipt requires review")
        baseline, pod, before, cursor = pulse.preflight(scope)
        pulse.verify_fixture(scope, baseline, pod)
        gate = live.CHAOS.node_gate()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result = execute(args.arm, baseline, pod, before, cursor, gate, args.output, journal, reference, scope=scope)
    print(json.dumps({key: result.get(key) for key in ("case", "status", "trial_error", "cleanup_error")}))
    return 0 if result["status"] == "pool_observed" and not result.get("cleanup_error") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False,
                          "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop)) else type(error).__name__}))
        raise SystemExit(2) from None
