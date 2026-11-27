"""Run the matched dev rollout whose new image breaks the order-to-inventory contract."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Lock
from time import monotonic, sleep
from urllib.parse import urlencode

from app.redact import redact
from evaluation import k8s_dev_image_trial as cluster
from evaluation.dev_image_case import DevImageCase
from evaluation.isolated_recovery_agent import Unobserved, run_agent
from evaluation.matched_recovery_campaign import (
    ARMS, CONTEXT, NS, RECOVERY_BUDGET_S, PreflightBlocked, canonical_digest, dev_preflight, matched_start, write_new,
)


CASE = DevImageCase("K8S-IMAGE-CONTRACT", "order", "liveprobe/order:matched-contract-20261126")
SIGNAL = "TargetHigh5xxRate"
ROUTE = "/api/orders/check"
CALLER = "inventory"
ERROR_PREFIX = "inventory call failed item=a error="
TOOLS = [
    {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": list(properties), "additionalProperties": False,
    }}
    for name, description, properties in (
        ("read_rollout", "Read the order Deployment image and its revisions.", {}),
        ("read_error", "Read the order service's current error.", {}),
        ("check_routes", "Check order readiness, order's stock check and inventory's stock route.", {}),
        ("update_image", "Change the order Deployment image using a compare-and-set.", {
            "current_image": {"type": "string"}, "image": {"type": "string"},
        }),
    )
]


def http_status(service, route):
    result = cluster.command("exec", f"deploy/{CALLER}", "-c", CALLER, "--", "curl", "-sS",
                             "--max-time", "4", "-o", "/dev/null", "-w", "%{http_code}",
                             f"http://{service}.dev.svc.cluster.local:8080{route}").strip()
    if len(result) != 3 or not result.isascii() or not result.isdecimal():
        raise cluster.Refused("independent HTTP status was not returned")
    return int(result)


def routes():
    return {"readiness": http_status("order", "/actuator/health/readiness"),
            "order": http_status("order", ROUTE + "?item=a"),
            "inventory": http_status("inventory", "/api/stock/a")}


def active_order(baseline):
    current = cluster.get("deployment", CASE.workload)
    unchanged, image = cluster.same_workload(CASE, current, baseline)
    if not unchanged or image not in (cluster.image(CASE, baseline), CASE.bad_image):
        raise cluster.Refused("order identity or unrelated Deployment configuration changed")
    return current, image


def runtime_digest(image_id):
    return image_id.removeprefix("docker-pullable://").removeprefix("docker://").rsplit("@", 1)[-1]


def error_message(line):
    try:
        fields = json.loads(line)
    except ValueError:
        fields = None
    message = fields.get("message") if isinstance(fields, dict) else line
    if isinstance(message, str) and ERROR_PREFIX in message and "InventoryStock" in message:
        return redact(message[message.index(ERROR_PREFIX):])
    return None


def first_error(image_id, stock_status, after, baseline):
    endpoint = cluster.ready_endpoints(CASE)
    pods = cluster.get("pods", selector="app=order")["items"]
    pod, = [row for row in pods if row["metadata"]["uid"] == endpoint]
    current, image = active_order(baseline)
    if image != CASE.bad_image or not cluster.is_settled(current):
        return None
    state, = pod["status"]["containerStatuses"]
    expected = runtime_digest(state.get("imageID", ""))
    if pod["spec"]["containers"][0]["image"] != CASE.bad_image or expected != image_id:
        raise cluster.Refused("the ready endpoint does not serve the exact sealed candidate")
    logs = cluster.command("logs", pod["metadata"]["name"], "-c", CASE.workload,
                           f"--since-time={after}", "--tail=100")
    for line in logs.splitlines():
        message = error_message(line)
        if message:
            return {"pod": pod["metadata"]["name"], "pod_uid": endpoint, "image_id": expected,
                    "http": stock_status, "error": redact(line), "message_sha256": sha256(message.encode()).hexdigest(),
                    "source": "order application log"}
    return None


def product_incident(cursor):
    rows = cluster.query(f"""
        SELECT coalesce(json_agg(row_to_json(result)), '[]'::json) FROM (
          SELECT i.id,i.status,i.severity_level,i.signal_reasons,
            (SELECT json_agg(json_build_object('namespace',l.detail#>>'{{signal,detail,labels,namespace}}',
                       'app',l.detail#>>'{{signal,detail,labels,app}}','method',l.detail#>>'{{signal,detail,labels,method}}',
                       'uri',l.detail#>>'{{signal,detail,labels,uri}}','received_at',l.detail#>>'{{signal,received_at}}'))
               FROM audit_log l WHERE l.incident_id=i.id AND l.event='signal_observed'
                 AND l.detail->>'reason'='{SIGNAL}' AND l.detail#>>'{{signal,source}}'='alertmanager') AS alerts,
            (SELECT json_agg(json_build_object('id',a.id,'type',a.type,'target',a.target,'executed',a.executed,
                       'status',a.status,'from_images',a.before_snapshot->'images','to_images',a.params->'to_images'))
               FROM actions a WHERE a.incident_id=i.id) AS actions,
            (SELECT json_agg(json_build_object('id',r.id,'role',r.role,'model',r.model,'status',r.status,
                       'input_tokens',r.input_tokens,'output_tokens',r.output_tokens,'estimated_usd',r.estimated_cost_usd))
               FROM agent_runs r WHERE r.incident_id=i.id) AS calls
          FROM incidents i WHERE i.id>{int(cursor)} AND i.namespace='dev' AND i.workload='order'
          ORDER BY i.id LIMIT 2
        ) result
    """)
    if len(rows) > 1:
        raise cluster.Refused("multiple incidents matched the order trial")
    return rows[0] if rows else None


def failing_routes():
    query = ('sum by (method,uri,status) (rate(http_server_requests_seconds_count'
             '{namespace="dev",app="order",status=~"5..",uri=~"/api/.*"}[1m])) > 0')
    path = "/api/v1/namespaces/liveprobe-observability/services/prometheus:9090/proxy/api/v1/query?"
    data = json.loads(cluster.command("get", "--raw", path + urlencode({"query": query})))
    return data["data"]["result"]


def detector_receipt(incident, fault):
    for alert in incident.get("alerts") or []:
        if (alert["namespace"], alert["app"]) != (NS, CASE.workload):
            continue
        route = alert["method"], alert["uri"]
        if route == ("GET", ROUTE):
            return {"alert": alert}
        if route == (None, None) and fault:
            signals = failing_routes()
            if len(signals) == 1 and signals[0]["metric"] == {"method": "GET", "uri": ROUTE, "status": "502"}:
                return {"alert": alert, "failing_routes": signals}
    return None


class OrderBroker:
    def __init__(self, baseline, fault, lease, deadline):
        self.baseline, self.fault, self.lease, self.deadline = baseline, fault, lease, deadline
        self.lock = Lock()
        self.closed = False
        self.changed = False

    def close(self):
        with self.lock:
            self.closed = True

    def __call__(self, method, args):
        with self.lock:
            if self.closed or monotonic() >= self.deadline:
                raise Unobserved("the trial's broker is closed")
            return self.request(method, args)

    def request(self, method, args):
        deployment, image = active_order(self.baseline)
        if method == "update_image":
            requested = args.get("image")
            if (set(args) != {"current_image", "image"} or self.changed or args["current_image"] != image
                    or (image, requested) != (CASE.bad_image, cluster.image(CASE, self.baseline))):
                return {"updated": False, "reason": "requested image transition is outside the authorized deployment"}
            self.lease.require_active()
            cluster.patch_image(CASE, deployment, requested)
            self.changed = True
            return {"updated": True, "target": CASE.target, "image": requested}
        if args:
            raise Unobserved("the observation tool takes no arguments")
        if method == "read_error":
            return {"error": self.fault["error"], "pod": self.fault["pod"]}
        if method == "check_routes":
            return routes()
        if method != "read_rollout":
            raise Unobserved("unknown observation")
        revisions = []
        for rs in cluster.get("replicasets", selector="app=order")["items"]:
            meta = rs["metadata"]
            if any(row.get("kind") == "Deployment" and row.get("controller") is True
                   and row["uid"] == deployment["metadata"]["uid"] for row in meta.get("ownerReferences", [])):
                revisions.append({"name": meta["name"], "revision": meta.get("annotations", {}).get("deployment.kubernetes.io/revision"),
                                  "image": cluster.image(CASE, rs), "ready": rs.get("status", {}).get("readyReplicas", 0)})
        return {"deployment": CASE.target, "image": image, "revisions": revisions}


def preflight(artifact):
    gate = dev_preflight()
    baseline, healthy, cursor, _ = cluster.preflight(CASE)
    if (artifact.get("original_image"), artifact.get("candidate_image"), artifact.get("changed_layers")) != (
            cluster.image(CASE, baseline), CASE.bad_image, 1):
        raise cluster.Refused("candidate does not correspond to the current pinned order release")
    image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", CASE.bad_image],
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if image_id != artifact.get("candidate_image_id") or not image_id.startswith("sha256:"):
        raise cluster.Refused("sealed local image is unavailable")
    node, = cluster.get("nodes")["items"]
    if not node["status"]["nodeInfo"]["containerRuntimeVersion"].startswith("docker://"):
        raise cluster.Refused("the node does not use the inspected Docker image store")
    inventory = cluster.get("deployment", "inventory")
    if routes() != {"readiness": 200, "order": 200, "inventory": 200}:
        raise cluster.Refused("business traffic cannot reach the original Services")
    if failing_routes():
        raise cluster.Refused("previous order 5xx samples have not left the detection window")
    start = {"context": CONTEXT, "namespace": NS, "node_uid": node["metadata"]["uid"],
             "deployment_uid": baseline["metadata"]["uid"], "fixture_digest": canonical_digest({
                 "order": baseline["spec"], "inventory": inventory["spec"], "inventory_uid": inventory["metadata"]["uid"]}),
             "fault_digest": canonical_digest({"path": cluster.IMAGE_PTR, "image": CASE.bad_image, "image_id": image_id}),
             "verifier_digest": sha256(Path(__file__).read_bytes()).hexdigest()}
    return baseline, healthy, cursor, gate, start


def trial(args, artifact, reference, journal):
    baseline, healthy, cursor, gate, start = preflight(artifact)
    original = cluster.image(CASE, baseline)
    if reference and any(reference["start_evidence"].get(key) != value for key, value in start.items()):
        raise cluster.Refused("the order starting state differs from the reference arm")
    write_new(journal, {"target": CASE.target, "uid": start["deployment_uid"], "field": cluster.IMAGE_PTR,
                        "original_value": original, "failed_value": CASE.bad_image, "receipt": str(args.output)})
    result = {"case": CASE.case_id, "arm": args.arm, "target": CASE.target, "healthy_before": healthy,
              "headroom": gate, "status": "unobserved"}
    lease = broker = future = fault = incident = None
    mutated = clean = False
    with ThreadPoolExecutor(max_workers=1) as agents:
        try:
            if args.arm != "liveprobe":
                lease = cluster.WorkloadLease.claim(CASE, args.arm, ttl=900)
            dev_preflight()
            current, image = active_order(baseline)
            if image != original or not cluster.is_settled(current):
                raise cluster.Refused("order changed after the preflight")
            logs_after = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            mutated = True
            cluster.patch_image(CASE, current, CASE.bad_image)
            began = monotonic()
            result["injected_at"] = datetime.now(timezone.utc).isoformat()
            deadline, renew_at = began + RECOVERY_BUDGET_S, began + 240
            while monotonic() < deadline:
                if lease and monotonic() >= renew_at:
                    lease.renew(900)
                    renew_at = monotonic() + 240
                status = http_status("order", ROUTE + "?item=a")
                if fault is None and status == 502:
                    current, image = active_order(baseline)
                    if image == CASE.bad_image and cluster.is_settled(current):
                        checks = routes()
                        if checks == {"readiness": 200, "order": 502, "inventory": 200}:
                            fault = first_error(artifact["candidate_image_id"], checks, logs_after, baseline)
                    if fault:
                        start["initial_error_digest"] = fault["message_sha256"]
                        result.update(fault=fault, first_error_ms=round((monotonic() - began) * 1000),
                                      start_evidence=start, matched_start=matched_start(start))
                        if reference and reference["matched_start"] != result["matched_start"]:
                            raise cluster.Refused("the first order error differs from the reference")
                        if lease:
                            broker = OrderBroker(baseline, fault, lease, deadline)
                            future = agents.submit(run_agent, binary=args.binary, auth_file=args.auth, arm=args.arm,
                                error_line=fault["error"], tools=TOOLS, broker=broker, deadline=deadline)
                incident = product_incident(cursor)
                if incident:
                    if lease:
                        raise cluster.Refused("LiveProbe observed a standalone workload under lease")
                    result["incident"] = incident
                    if "detector" not in result:
                        detector = detector_receipt(incident, fault)
                        if detector:
                            result["detector"] = detector
                            result["detected_ms"] = round((monotonic() - began) * 1000)
                if fault and status == 200 and "recovery_ms" not in result:
                    current, image = active_order(baseline)
                    if image == original and cluster.is_settled(current):
                        result["verified_before_cleanup"] = [cluster.probe(CASE), cluster.probe(CASE)]
                        result["recovery_ms"] = round((monotonic() - began) * 1000)
                if future and future.done():
                    result["standalone"] = future.result()
                    if result["standalone"]["status"] != "completed" or not broker.changed or "recovery_ms" in result:
                        break
                if incident and incident["status"] not in cluster.NONTERMINAL and fault:
                    if "recovery_ms" in result or not cluster.attributed_recovery(CASE, incident, original):
                        break
                sleep(1)
            if future and "standalone" not in result:
                result["standalone"] = future.result(timeout=3)
            result["trial_elapsed_ms"] = round((monotonic() - began) * 1000)
            completed = result.get("standalone", {}).get("status") == "completed"
            attributable = (completed and broker.changed) if broker else (
                incident and result.get("detector") and cluster.attributed_recovery(CASE, incident, original))
            if attributable and "recovery_ms" in result:
                result["status"] = "recovered"
            elif fault and "recovery_ms" not in result:
                checks = routes()
                if checks == fault["http"] and (completed or result.get("detector")):
                    result.update(status="unrecovered", still_failed_before_cleanup=checks)
        except (cluster.Refused, PreflightBlocked, Unobserved, OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
            result.update(status="unobserved", trial_error=str(error))
        finally:
            if broker:
                broker.close()
            try:
                if mutated:
                    result["restored"] = cluster.restore(CASE, baseline)
                if lease:
                    lease.release()
                clean = True
            finally:
                if not clean:
                    result.update(status="unobserved", cleanup_incomplete=True)
                write_new(args.output, result)
                if clean:
                    journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--auth", type=Path)
    args = parser.parse_args()
    artifact = json.loads(args.artifact.read_text())
    if args.arm is None and args.output is None:
        _, healthy, cursor, gate, start = preflight(artifact)
        print(json.dumps({"healthy": healthy, "cursor": cursor, "headroom": gate, "start": start}))
        return 0
    if not args.arm or not args.output or not args.output.parent.is_dir():
        parser.error("an arm and a new output path are required")
    if args.arm != "liveprobe" and not all((args.reference, args.binary, args.auth)):
        parser.error("standalone trials require a LiveProbe receipt, pinned CLI and independent account")
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if reference and (reference.get("case"), reference.get("arm"), bool(reference.get("restored"))) != (
            CASE.case_id, "liveprobe", True):
        parser.error("the comparison needs a LiveProbe reference and verified cleanup")
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
        if args.output.exists() or journal.exists():
            parser.error("an output or an unfinished fault journal already exists")
        result = trial(args, artifact, reference, journal)
    print(json.dumps({"arm": args.arm, "status": result["status"]}))
    return 2 if result["status"] == "unobserved" else 0


if __name__ == "__main__":
    raise SystemExit(main())
