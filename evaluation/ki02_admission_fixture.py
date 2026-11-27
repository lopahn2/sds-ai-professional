"""KI-02 admission fixture and its bounded broker in a disposable K3s server."""
import base64
import json
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from select import select
import socket
from socketserver import BaseRequestHandler, ThreadingUnixStreamServer
import ssl
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlsplit


WORK = Path("/tmp/ki02")
K3S = "/opt/isolated/k3s-arm64"
CERT = Path("/opt/isolated/webhook-cert.pem")
KEY = "/opt/isolated/webhook-key.pem"
NAMESPACE = "dev-ki02-verify"
TARGET = "versioned-admission"
WEBHOOK = "review.ki02.verify.invalid"
LABELS = {"liveprobe.example/fixture": "ki02"}
API_VERSION = "admission.k8s.io/v1"
OLD_VERSION = "admission.k8s.io/v1beta1"
CASE = "KI-02"
PROTOCOL = "live-repair/v37"
API_SERVER_VERSION = "v1.34.13+k3s1"
REGISTRATION_NAME = "ki02-fixture-admission"
OBSERVER_SOCKET = Path("/tmp/lp-ki02-observer.sock")
AUDIT_POLICY = WORK / "audit-policy.json"
AUDIT_LOG = WORK / "audit.jsonl"
requests = []
kubeconfig = WORK / "kubeconfig"


class Admission(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        review = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        req = review["request"]
        version = review.get("apiVersion")
        object_name = req.get("name") or (req.get("object") or {}).get("metadata", {}).get("name")
        if (urlsplit(self.path).path != "/review" or req.get("namespace") != NAMESPACE
                or object_name != TARGET or req.get("operation") != "CREATE"
                or req.get("resource", {}).get("resource") != "configmaps"):
            self.send_error(400, "request is outside the KI-02 fixture")
            return
        requests.append(version)
        if version != API_VERSION:
            self.send_error(400, "fixture accepts admission.k8s.io/v1 only")
            return
        response = {"apiVersion": API_VERSION, "kind": "AdmissionReview",
                    "response": {"uid": req["uid"], "allowed": True}}
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def kubectl(*args, obj=None):
    return subprocess.run(
        [K3S, "kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=5s", *args],
        input=json.dumps(obj) if obj is not None else None, text=True, capture_output=True, timeout=12,
    )


def require(result, stage):
    if result.returncode:
        raise RuntimeError(stage)
    return result.stdout


def fixture_configmap(*, control=False):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"namespace": NAMESPACE, "name": "unmatched-control" if control else TARGET,
                         "labels": {} if control else LABELS},
            "data": {"scenario": "ki02"}}


def registration(version):
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingWebhookConfiguration",
            "metadata": {"name": REGISTRATION_NAME},
            "webhooks": [{
                "name": WEBHOOK, "admissionReviewVersions": [version], "failurePolicy": "Fail",
                "sideEffects": "None", "timeoutSeconds": 3, "matchPolicy": "Exact",
                "clientConfig": {"url": "https://127.0.0.1:9443/review",
                                 "caBundle": base64.b64encode(CERT.read_bytes()).decode()},
                "rules": [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE"],
                           "resources": ["configmaps"], "scope": "Namespaced"}],
                "namespaceSelector": {"matchLabels": LABELS}, "objectSelector": {"matchLabels": LABELS},
            }]}


def dry_run_until(version):
    deadline = monotonic() + 28
    wanted_ok = version == API_VERSION
    while monotonic() < deadline:
        before = len(requests)
        check = kubectl("create", "--dry-run=server", "-f", "-", "-o", "json", obj=fixture_configmap())
        if requests[before:] == [version] and (check.returncode == 0) == wanted_ok:
            return
        sleep(.5)
    raise RuntimeError(f"webhook change did not reach the API dispatcher for {version.rsplit('/', 1)[-1]}")


def successful_creation(*, control=False):
    obj = fixture_configmap(control=control)
    name = obj["metadata"]["name"]
    before = len(requests)
    created = require(kubectl("create", "-f", "-", "-o", "json", obj=obj), "fixture creation")
    read = require(kubectl("-n", NAMESPACE, "get", "configmap", name, "-o", "json"), "fixture readback")
    uid = json.loads(created)["metadata"]["uid"]
    item = json.loads(read)
    assert item["metadata"]["uid"] == uid and item["data"] == obj["data"]
    assert requests[before:] == ([] if control else [API_VERSION])
    require(kubectl("-n", NAMESPACE, "delete", "configmap", name, "--wait=true"), "fixture cleanup")
    return {"stored": True, "uid": uid, "webhook_requests": requests[before:]}


def current_registration():
    value = require(kubectl("get", "validatingwebhookconfiguration", REGISTRATION_NAME, "-o", "json"),
                    "read fixture webhook")
    resource = json.loads(value)
    webhook, = resource["webhooks"]
    webhook["clientConfig"].pop("caBundle")
    return resource["metadata"], webhook


def configuration_receipt(metadata, webhook):
    scope = {key: value for key, value in webhook.items() if key != "admissionReviewVersions"}
    return {"uid": metadata["uid"], "resource_version": metadata["resourceVersion"],
            "versions": webhook["admissionReviewVersions"],
            "scope_sha256": sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()}


def rejected_creation():
    before = len(requests)
    fault = kubectl("create", "-f", "-", "-o", "json", obj=fixture_configmap())
    observed_at = monotonic()
    assert fault.returncode and WEBHOOK in fault.stderr and requests[before:] == [OLD_VERSION]
    assert not require(kubectl("-n", NAMESPACE, "get", "configmap", TARGET, "--ignore-not-found", "-o", "name"),
                       "rejected object check").strip()
    return {"stored": False, "sent_review_version": OLD_VERSION, "message": fault.stderr.strip()}, observed_at


def prepare(server, before_fault=None):
    deadline = monotonic() + 70
    while monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"isolated K3s exited during startup with status {server.returncode}")
        if kubeconfig.is_file():
            response = kubectl("get", "--raw=/readyz")
            if response.returncode == 0 and response.stdout.strip() == "ok":
                break
        sleep(1)
    else:
        raise RuntimeError("isolated API readiness timed out")

    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": LABELS}}
    require(kubectl("create", "-f", "-", obj=ns), "private namespace creation")
    require(kubectl("apply", "-f", "-", obj=registration("v1")), "baseline webhook")
    dry_run_until(API_VERSION)
    baseline = successful_creation()
    if before_fault:
        before_fault()
    require(kubectl("apply", "-f", "-", obj=registration("v1beta1")), "version mismatch fixture")
    dry_run_until(OLD_VERSION)
    fault, started = rejected_creation()
    control = successful_creation(control=True)
    version = json.loads(require(kubectl("get", "--raw=/version"), "server version"))
    assert version["gitVersion"] == API_SERVER_VERSION
    metadata, webhook = current_registration()
    return {"case": CASE, "api_version": version["gitVersion"], "baseline_request": baseline,
            "injected_request": fault, "unmatched_request_during_fault": control,
            "initial_registration": configuration_receipt(metadata, webhook)}, started


def update_versions(key, value):
    if key != "admissionReviewVersions":
        return {"applied": False, "reason": "Only the fixture's admissionReviewVersions array can change."}
    try:
        versions = json.loads(value)
    except (TypeError, ValueError):
        versions = None
    if (not isinstance(versions, list) or not versions or len(versions) > 2
            or any(version not in ("v1", "v1beta1") for version in versions)
            or len(set(versions)) != len(versions)):
        return {"applied": False, "reason": "Provide a JSON array of unique served Review version strings."}
    metadata, webhook = current_registration()
    before = configuration_receipt(metadata, webhook)
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
             {"op": "replace", "path": "/webhooks/0/admissionReviewVersions", "value": versions}]
    require(kubectl("patch", "validatingwebhookconfiguration", REGISTRATION_NAME, "--type=json",
                    "-p", json.dumps(patch)), "patch fixture Review versions")
    metadata, webhook = current_registration()
    after = configuration_receipt(metadata, webhook)
    assert (after["uid"], after["scope_sha256"], after["versions"]) == (before["uid"], before["scope_sha256"], versions)
    dry_run_until("admission.k8s.io/" + versions[0])
    return {"applied": True, "before": before, "after": after}


def probe():
    metadata, webhook = current_registration()
    registered = configuration_receipt(metadata, webhook)
    result = successful_creation() if registered["versions"][0] == "v1" else rejected_creation()[0]
    return {"registration": registered, "request": result}


class ApiTlsBridge(BaseRequestHandler):
    def handle(self):
        # Forward the wire bytes. Host Kubernetes clients still verify the test TLS certificate.
        with socket.create_connection(("127.0.0.1", 6443), timeout=5) as upstream:
            upstream.settimeout(None)
            peers = {self.request: upstream, upstream: self.request}
            while True:
                readable, _, _ = select(peers, [], [], 10)
                if not readable:
                    continue
                for connection in readable:
                    data = connection.recv(65536)
                    if not data:
                        return
                    peers[connection].sendall(data)


@contextmanager
def observer_transport():
    server = ThreadingUnixStreamServer(str(OBSERVER_SOCKET), ApiTlsBridge)
    server.daemon_threads = True
    try:
        os.chown(OBSERVER_SOCKET, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        OBSERVER_SOCKET.chmod(0o600)
        Thread(target=server.serve_forever, daemon=True).start()
    except Exception:
        server.server_close()
        OBSERVER_SOCKET.unlink(missing_ok=True)
        raise
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        OBSERVER_SOCKET.unlink(missing_ok=True)


def read_only_observer():
    account = "liveprobe-ki02-reader"
    rbac = "rbac.authorization.k8s.io/v1"
    metadata = {"name": account, "namespace": NAMESPACE}
    role = {"apiVersion": rbac, "kind": "Role", "metadata": metadata, "rules": [
        {"apiGroups": [""], "resources": ["pods", "events", "services", "endpoints"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["get", "list", "watch"]},
    ]}
    binding = {"apiVersion": rbac, "kind": "RoleBinding", "metadata": metadata,
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": account},
               "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": NAMESPACE}]}
    resources = [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata}, role, binding]
    for resource in resources:
        require(kubectl("create", "-f", "-", obj=resource), "read-only observer setup")
    token = require(kubectl("-n", NAMESPACE, "create", "token", account, "--duration=10m"), "reader credentials").strip()
    access = json.loads(require(kubectl("config", "view", "--raw", "-o", "json"), "fixture CA"))
    ca = access["clusters"][0]["cluster"]["certificate-authority-data"]
    return {"token": token, "authority": ca}


def sentinel(phase):
    if phase not in ("before", "after"):
        raise ValueError("unknown watcher sentinel phase")
    now = datetime.now(timezone.utc).isoformat()
    event = {"apiVersion": "v1", "kind": "Event", "metadata": {"name": f"lp-watch-proof-{phase}", "namespace": NAMESPACE},
             "involvedObject": {"apiVersion": "v1", "kind": "ConfigMap", "name": TARGET, "namespace": NAMESPACE},
             "source": {"component": "liveprobe-isolated-evaluation"}, "message": "testing the active watch stream",
             "type": "Normal", "reason": "LiveProbeWatchProof", "count": 1, "firstTimestamp": now, "lastTimestamp": now}
    item = json.loads(require(kubectl("create", "-f", "-", "-o", "json", obj=event), "watcher continuity marker"))
    return {"uid": item["metadata"]["uid"], "phase": phase}


def metric_counts():
    result = require(kubectl("get", "--raw=/metrics"), "read admission API counters")
    return [line for line in result.splitlines() if line.startswith("apiserver_admission_webhook_rejection_count{")
            and f'name="{WEBHOOK}"' in line]


def audit_entries():
    """Project the real API response without returning request bodies or caller metadata."""
    rows = []
    if not AUDIT_LOG.exists():
        return rows
    for line in AUDIT_LOG.read_text().splitlines():
        record = json.loads(line)
        obj = record.get("objectRef", {})
        if obj.get("namespace") == NAMESPACE and obj.get("resource") == "configmaps":
            rows.append({key: record[key] for key in (
                "apiVersion", "kind", "level", "stage", "verb", "objectRef", "auditID",
                "requestURI", "stageTimestamp", "responseStatus") if key in record})
    return rows


def audit_args():
    if os.environ.get("KI02_AUDIT_CAPTURE") != "1":
        return []
    AUDIT_POLICY.write_text(json.dumps({
        "apiVersion": "audit.k8s.io/v1", "kind": "Policy",
        "omitStages": ["RequestReceived", "ResponseStarted", "Panic"],
        "rules": [{"level": "Metadata", "verbs": ["create"], "namespaces": [NAMESPACE],
                   "resources": [{"group": "", "resources": ["configmaps"]}]}],
    }))
    return [f"--kube-apiserver-arg=audit-policy-file={AUDIT_POLICY}",
            f"--kube-apiserver-arg=audit-log-path={AUDIT_LOG}",
            "--kube-apiserver-arg=audit-log-maxbackup=1", "--kube-apiserver-arg=audit-log-maxsize=10"]


def observe_before_fault():
    print(json.dumps(read_only_observer()), flush=True)
    for line in sys.stdin:
        operation = json.loads(line)
        if operation == {"tool": "inject"}:
            return
        if operation == {"tool": "sentinel", "phase": "before"}:
            print(json.dumps(sentinel("before")), flush=True)
        else:
            raise ValueError("unknown pre-fault observer operation")
    raise EOFError("observer closed before the fault")


def serve(server):
    first = json.loads(sys.stdin.readline())
    if first == {"tool": "prepare_observer"}:
        with observer_transport():
            return serve_trial(server, observing=True)
    if first != {"tool": "prepare"}:
        raise ValueError("expected fixture preparation")
    return serve_trial(server, observing=False)


def serve_trial(server, *, observing):
    baseline, started = prepare(server, observe_before_fault if observing else None)
    print(json.dumps(baseline), flush=True)
    for line in sys.stdin:
        operation = json.loads(line)
        name = operation.get("tool")
        if name == "workload":
            metadata, webhook = current_registration()
            out = {"server_version": baseline["api_version"],
                   "registration": {"name": metadata["name"], **configuration_receipt(metadata, webhook)}}
        elif name == "logs":
            out = {"api_rejection": baseline["injected_request"]["message"],
                   "webhook_received_version": baseline["injected_request"]["sent_review_version"],
                   "webhook_diagnostic": "fixture accepts admission.k8s.io/v1 only"}
        elif name == "source":
            metadata, webhook = current_registration()
            out = {"name": metadata["name"], "webhook": webhook}
        elif name == "configure" and not observing:
            out = update_versions(operation.get("key"), operation.get("value"))
        elif name == "probe":
            out = probe()
        elif name == "audit" and os.environ.get("KI02_AUDIT_CAPTURE") == "1":
            out = {"events": audit_entries()}
        elif name == "verify":
            checked = [probe(), probe()]
            out = {"independent_probes": checked, "unmatched_request": successful_creation(control=True),
                   "wall_duration_ms": round((monotonic() - started) * 1000)}
            if observing:
                out["rejection_counter"] = metric_counts()
        elif name == "sentinel" and observing and operation.get("phase") == "after":
            out = sentinel("after")
        elif name == "shutdown":
            return True
        else:
            out = {"accepted": False, "reason": "unknown fixture operation"}
        print(json.dumps(out), flush=True)
    return False


def main():
    WORK.mkdir(mode=0o700)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(CERT), KEY)
    listener = ThreadingHTTPServer(("127.0.0.1", 9443), Admission)
    listener.socket = tls.wrap_socket(listener.socket, server_side=True)
    Thread(target=listener.serve_forever, daemon=True).start()
    with (WORK / "server.log").open("w") as log:
        server = subprocess.Popen([
            K3S, "server", "--disable-agent", "--data-dir", str(WORK / "state"),
            "--write-kubeconfig", str(kubeconfig), "--write-kubeconfig-mode", "0600",
            "--bind-address=127.0.0.1", "--advertise-address=127.0.0.1", "--node-ip=127.0.0.1",
            "--egress-selector-mode=disabled", "--flannel-backend=none", "--disable-scheduler",
            "--disable-network-policy", "--disable-cloud-controller", "--disable-helm-controller",
            "--disable=traefik,servicelb,local-storage,metrics-server,coredns",
            *audit_args(),
        ], stdout=log, stderr=subprocess.STDOUT)
        try:
            stopped_on_request = serve(server)
        finally:
            server.terminate()
            try:
                server.wait(timeout=6)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            listener.shutdown()
    if stopped_on_request:
        print(json.dumps({"stopped": True}), flush=True)


if __name__ == "__main__":
    main()
