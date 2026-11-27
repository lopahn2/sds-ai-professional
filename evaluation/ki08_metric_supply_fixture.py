"""Offline KI-08 fixture with Kubernetes-native API aggregation, HPA and running pause Pods."""
from datetime import datetime, timezone
from contextlib import nullcontext
from hashlib import sha256
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import base64
import json
from pathlib import Path
import ssl
import sys
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlencode, urlsplit


CASE = "KI-08"
VERSION = "v1.34.13+k3s1"
NS = "dev-ki08-verify"
WORK = Path("/tmp/lp-ki08-k3s")
TLS = WORK / "state/server/tls"
NAMESPACED = f"/api/v1/namespaces/{NS}"
NS_API = "/api/v1/namespaces"
HPA_API = f"/apis/autoscaling/v2/namespaces/{NS}/horizontalpodautoscalers"
DEPLOYMENT_API = f"/apis/apps/v1/namespaces/{NS}/deployments"
SLICE_API = f"/apis/discovery.k8s.io/v1/namespaces/{NS}/endpointslices"
API_SERVICE = "/apis/apiregistration.k8s.io/v1/apiservices"
METRIC_PREFIX = "/apis/metrics.k8s.io/v1beta1"
METRIC_PODS = f"{METRIC_PREFIX}/namespaces/{NS}/pods"
API_SERVER = "v1beta1.metrics.k8s.io"
PROVIDER = "ki08-metrics"
WORKLOAD = "ki08-target"
SELECTOR = {"app": WORKLOAD}
PORT = 9443
ADDRESS = "192.0.2.10"
ENDPOINT = {"addresses": [ADDRESS], "conditions": {"ready": True}}
LOAD_NORMAL = "100m"
LOAD_HIGH = "600m"
OBSERVER_SOCKET = Path("/tmp/lp-ki08-observer.sock")


def kube_client():
    context = ssl.create_default_context(cafile=TLS / "server-ca.crt")
    context.load_cert_chain(TLS / "client-admin.crt", TLS / "client-admin.key")
    return context


class Kube:
    def __init__(self):
        self.context = kube_client()

    def request(self, path, method="GET", payload=None):
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/merge-patch+json" if method == "PATCH" else "application/json"
        connection = HTTPSConnection("127.0.0.1", 6443, context=self.context, timeout=8)
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload), headers)
            result = connection.getresponse()
            body = result.read()
            try:
                document = json.loads(body)
            except ValueError:
                if result.status < 400:
                    raise
                document = {"message": body.decode(errors="replace").strip()}
            return result.status, document
        finally:
            connection.close()

    def get(self, path, method="GET", payload=None, *, missing_ok=False):
        status, result = self.request(path, method, payload)
        if missing_ok and status == 404:
            return None
        if not 200 <= status < 300:
            raise RuntimeError(f"{method} {path}: {status} {result.get('message')}")
        return result

    def pods(self):
        query = urlencode({"labelSelector": f"app={WORKLOAD}"})
        return self.get(f"{NAMESPACED}/pods?{query}")["items"]


def ready_pods(pods):
    return [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
            and pod.get("status", {}).get("phase") == "Running"
            and any(c["type"] == "Ready" and c["status"] == "True"
                    for c in (pod["status"].get("conditions") or []))]


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == METRIC_PREFIX:
            self.send_document(200, {"apiVersion": "v1", "kind": "APIResourceList",
                                     "groupVersion": "metrics.k8s.io/v1beta1", "resources": [
                {"name": "pods", "singularName": "", "namespaced": True, "kind": "PodMetrics",
                 "verbs": ["get", "list"]},
            ]})
        elif path == METRIC_PODS:
            cpu = self.server.cpu
            when = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            entries = [{"apiVersion": "metrics.k8s.io/v1beta1", "kind": "PodMetrics",
                        "metadata": {"name": p["metadata"]["name"], "namespace": NS},
                        "timestamp": when, "window": "1s",
                        "containers": [{"name": "pause", "usage": {"cpu": cpu, "memory": "1Mi"}}]}
                       for p in ready_pods(self.server.kube.pods())]
            self.send_document(200, {"apiVersion": "metrics.k8s.io/v1beta1", "kind": "PodMetricsList", "items": entries})
        else:
            self.send_document(404, {"apiVersion": "v1", "kind": "Status", "status": "Failure",
                                     "reason": "NotFound", "code": 404})

    def send_document(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def until(read, predicate, *, timeout, description):
    deadline = monotonic() + timeout
    while True:
        state = read()
        if predicate(state):
            return state
        if monotonic() >= deadline:
            raise RuntimeError(f"{description}: {json.dumps(state, default=str)[:2400]}")
        sleep(0.75)


def named(kind, group, name, spec=None, *, namespace=None, **fields):
    metadata = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    result = {"apiVersion": group, "kind": kind, "metadata": metadata, **fields}
    if spec is not None:
        result["spec"] = spec
    return result


def condition(body, key):
    return next((c for c in (body.get("status", {}).get("conditions") or []) if c["type"] == key), {})


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def comparable_contract(contract):
    specs = [{"name": item["name"], "spec": item["spec"].copy()} for item in contract]
    for field in ("clusterIP", "clusterIPs"):
        specs[0]["spec"].pop(field, None)
    return fingerprint([CASE, VERSION, specs])


class WorkloadTrial:
    def __init__(self, kube, server):
        self.kube = kube
        self.server = server
        self.contract = None
        self.test_fingerprint = None
        self.started = None
        self.created = []

    def setup(self):
        assert self.kube.get("/version")["gitVersion"] == VERSION
        self.kube.get(NS_API, "POST", named("Namespace", "v1", NS))
        self.created.append(NAMESPACED)
        self.kube.get(f"{NAMESPACED}/services", "POST", named("Service", "v1", PROVIDER, {
            "ports": [{"name": "https", "port": 443, "targetPort": PORT}],
        }, namespace=NS))
        service = self.kube.get(f"{NAMESPACED}/services/{PROVIDER}")
        endpoint = named("EndpointSlice", "discovery.k8s.io/v1", PROVIDER, namespace=NS,
                         addressType="IPv4", ports=[{"name": "https", "port": PORT, "protocol": "TCP"}],
                         endpoints=[ENDPOINT])
        endpoint["metadata"].update(labels={"kubernetes.io/service-name": PROVIDER,
                                             "endpointslice.kubernetes.io/managed-by": "liveprobe-isolated-evaluation"},
                                    ownerReferences=[{"apiVersion": "v1", "kind": "Service", "name": PROVIDER,
                                                      "uid": service["metadata"]["uid"]}])
        self.kube.get(SLICE_API, "POST", endpoint)
        self.kube.get(DEPLOYMENT_API, "POST", named("Deployment", "apps/v1", WORKLOAD, {
            "replicas": 1, "selector": {"matchLabels": SELECTOR},
            "template": {"metadata": {"labels": SELECTOR}, "spec": {
                "nodeName": "ki08-fixture", "hostNetwork": True, "dnsPolicy": "Default",
                "automountServiceAccountToken": False, "tolerations": [{"operator": "Exists"}],
                "terminationGracePeriodSeconds": 0,
                "containers": [{"name": "pause", "image": "docker.io/rancher/mirrored-pause:3.10.2",
                                "imagePullPolicy": "Never", "resources": {"requests": {"cpu": "200m"}}}],
            }},
        }, namespace=NS))
        ca = base64.b64encode((WORK / "metrics-ca.crt").read_bytes()).decode()
        self.kube.get(API_SERVICE, "POST", named("APIService", "apiregistration.k8s.io/v1", API_SERVER, {
            "group": "metrics.k8s.io", "version": "v1beta1", "groupPriorityMinimum": 100,
            "versionPriority": 100, "caBundle": ca,
            "service": {"name": PROVIDER, "namespace": NS, "port": 443},
        }))
        self.created.append(f"{API_SERVICE}/{API_SERVER}")
        self.kube.get(HPA_API, "POST", named("HorizontalPodAutoscaler", "autoscaling/v2", WORKLOAD, {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": WORKLOAD},
            "minReplicas": 1, "maxReplicas": 2, "behavior": {"scaleDown": {"stabilizationWindowSeconds": 0}},
            "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {
                "type": "Utilization", "averageUtilization": 60}}}],
        }, namespace=NS))
        self.contract = self.read_contract()
        self.test_fingerprint = comparable_contract(self.contract)

    def cleanup(self):
        while self.created:
            path = self.created[-1]
            self.kube.get(path, "DELETE", missing_ok=True)
            until(lambda: self.kube.get(path, missing_ok=True), lambda item: item is None,
                  timeout=50, description="disposable trial resources not removed")
            self.created.pop()
        return {"stopped": True, "resources_removed": True}

    def read_contract(self):
        resources = [self.kube.get(path) for path in (f"{NAMESPACED}/services/{PROVIDER}",
                     f"{API_SERVICE}/{API_SERVER}", f"{DEPLOYMENT_API}/{WORKLOAD}", f"{HPA_API}/{WORKLOAD}")]
        summary = [{"uid": r["metadata"]["uid"], "name": r["metadata"]["name"], "spec": r["spec"].copy()} for r in resources]
        summary[1]["spec"]["caBundle"] = fingerprint(summary[1]["spec"]["caBundle"])
        summary[2]["spec"].pop("replicas")
        return summary

    def observe(self):
        provider = self.kube.get(f"{API_SERVICE}/{API_SERVER}")
        hpa = self.kube.get(f"{HPA_API}/{WORKLOAD}")
        deployment = self.kube.get(f"{DEPLOYMENT_API}/{WORKLOAD}")
        endpoint = self.kube.get(f"{SLICE_API}/{PROVIDER}")
        status, metrics = self.kube.request(METRIC_PODS)
        samples = [{"pod": item["metadata"]["name"], "at": item["timestamp"],
                    "cpu": item["containers"][0]["usage"]["cpu"]} for item in (metrics.get("items") or [])]
        return {"provider_condition": condition(provider, "Available"), "hpa_condition": condition(hpa, "ScalingActive"),
                "desired": hpa.get("status", {}).get("desiredReplicas"), "metrics_status": status,
                "metric_error": metrics.get("message"), "metrics": samples, "ready": deployment["status"].get("readyReplicas", 0),
                "replicas": deployment["spec"]["replicas"], "deployment_uid": deployment["metadata"]["uid"],
                "pods": [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"]}
                         for p in ready_pods(self.kube.pods())],
                "endpoint_ready": endpoint["endpoints"][0]["conditions"]["ready"],
                "endpoint_uid": endpoint["metadata"]["uid"],
                "observed_at": datetime.now(timezone.utc).isoformat()}

    def endpoint(self, ready):
        path = f"{SLICE_API}/{PROVIDER}"
        current = self.kube.get(path)
        backend = current["endpoints"]
        assert len(backend) == 1 and backend[0]["addresses"] == [ADDRESS]
        new = {**backend[0], "conditions": {**backend[0]["conditions"], "ready": ready}}
        version = current["metadata"]["resourceVersion"]
        updated = self.kube.get(path, "PATCH", {"metadata": {"resourceVersion": version}, "endpoints": [new]})
        assert updated["metadata"]["uid"] == current["metadata"]["uid"]
        assert updated["endpoints"] == [new]
        return {"name": PROVIDER, "namespace": NS, "uid": updated["metadata"]["uid"],
                "before": backend[0]["conditions"]["ready"], "after": ready}

    @staticmethod
    def normal(item):
        return (item["provider_condition"].get("status") == "True" and item["hpa_condition"].get("status") == "True"
                and item["metrics_status"] == 200 and item["ready"] == item["replicas"] == 1
                and item["desired"] == 1 and len(item["pods"]) == len(item["metrics"]) == 1
                and item["metrics"][0]["cpu"] == LOAD_NORMAL)

    @staticmethod
    def broken(item):
        return (item["provider_condition"].get("status") == "False" and item["metrics_status"] == 503
                and item["hpa_condition"].get("status") == "False"
                and item["hpa_condition"].get("reason") == "FailedGetResourceMetric"
                and item["ready"] == item["replicas"] == 1 and not item["endpoint_ready"])

    @staticmethod
    def restored(item):
        return (item["provider_condition"].get("status") == "True" and item["metrics_status"] == 200
                and item["hpa_condition"].get("status") == "True" and item["endpoint_ready"]
                and item["desired"] == item["ready"] == item["replicas"] == len(item["pods"]) == 2
                and len(item["metrics"]) == 2 and all(metric["cpu"] == LOAD_HIGH for metric in item["metrics"]))

    def hpa_warning(self):
        hpa_uid = self.contract[3]["uid"]
        query = urlencode({"fieldSelector": f"involvedObject.uid={hpa_uid},type=Warning"})
        events = self.kube.get(f"{NAMESPACED}/events?{query}")["items"]
        return next(({"uid": e["metadata"]["uid"], "reason": e["reason"], "message": e["message"]}
                     for e in events if e["reason"] == "FailedGetResourceMetric"
                     and "currently unable to handle the request" in e["message"]), None)

    def prepare(self):
        self.server.cpu = LOAD_NORMAL
        self.endpoint(True)
        healthy = until(self.observe, self.normal, timeout=80, description="healthy preflight")
        self.endpoint(False)
        self.server.cpu = LOAD_HIGH
        fault = until(self.observe, self.broken, timeout=45, description="metrics and HPA failure")
        reason = fault["hpa_condition"]
        assert "currently unable to handle the request" in reason["message"]
        initial = f"{reason['reason']}: {reason['message']}"
        self.started = monotonic()
        assert self.read_contract() == self.contract
        return {"case": CASE, "kubernetes": VERSION, "healthy_before": healthy, "fault_before": fault,
                "initial_error_log": initial, "initial_event": self.hpa_warning(), "fixture_sha256": self.test_fingerprint,
                "service_contract": self.contract}

    def verify(self):
        healthy_route = self.observe()["endpoint_ready"]
        expectation = self.restored if healthy_route else self.broken
        first = until(self.observe, expectation, timeout=54, description="independent HPA verification")
        sleep(2)
        second = until(self.observe, expectation, timeout=12, description="second independent HPA verification")
        assert self.read_contract() == self.contract
        return {"independent_probes": [first, second],
                "wall_duration_ms": round((monotonic() - self.started) * 1000)}

    def handle(self, command):
        tool = command["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool == "probe":
            return self.observe()
        if tool == "workload":
            return {"workload": WORKLOAD, "metrics_service": self.contract[0],
                    "apiservice": {"name": API_SERVER, "condition": self.observe()["provider_condition"]},
                    "hpa": self.contract[3]}
        if tool == "logs":
            return {"condition": self.observe()["hpa_condition"], "event": self.hpa_warning()}
        if tool == "source":
            return {"endpoint_slice": self.kube.get(f"{SLICE_API}/{PROVIDER}"),
                    "configurable_field": "metrics_endpoint_ready"}
        if tool == "configure":
            if command.get("key") != "metrics_endpoint_ready" or command.get("value") != "true":
                return {"applied": False, "reason": "only the known metrics provider readiness can be restored"}
            return {"applied": True, "write": self.endpoint(True)}
        if tool == "verify":
            return self.verify()
        if tool == "shutdown":
            return self.cleanup()
        raise ValueError("operation not allowed by the trial broker")


def main():
    kube = Kube()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(WORK / "metrics.crt", WORK / "metrics.key")
    tls.load_verify_locations(TLS / "request-header-ca.crt")
    tls.verify_mode = ssl.CERT_REQUIRED
    observer = None
    if len(sys.argv) == 2 and sys.argv[1] == "--observe":
        spec = importlib.util.spec_from_file_location("observer", WORK / "observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        observer.NAMESPACE = NS
        observer.TARGET = WORKLOAD
        observer.OBSERVER_SOCKET = OBSERVER_SOCKET
        observer.kubeconfig = WORK / "kubeconfig"
    with ThreadingHTTPServer((ADDRESS, PORT), MetricsHandler) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        server.kube = kube
        server.cpu = LOAD_NORMAL
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        trial = WorkloadTrial(kube, server)
        try:
            trial.setup()
            with (observer.observer_transport() if observer else nullcontext()):
                for raw in sys.stdin:
                    command = json.loads(raw)
                    tool = command["tool"]
                    if observer and tool == "access":
                        result = observer.read_only_observer()
                    elif observer and tool == "sentinel":
                        result = observer.sentinel(command["phase"])
                    else:
                        result = trial.handle(command)
                    print(json.dumps(result), flush=True)
                    if tool == "shutdown":
                        break
        finally:
            try:
                trial.cleanup()
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
