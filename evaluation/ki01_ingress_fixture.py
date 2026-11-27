"""Real offline Kubernetes requests for the retired Ingress API."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
from select import select
import socket
from socketserver import BaseRequestHandler, ThreadingUnixStreamServer
import ssl
import subprocess
import sys
from threading import Thread
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request, urlopen


CASE = "KI-01"
PROTOCOL = "live-repair/v39"
NAMESPACE = "dev-ki01-verify"
NAME = "lp-eval-ki01-ingress"
BACKEND = "lp-eval-ki01-backend"
NEW = "networking.k8s.io/v1"
OLD = "networking.k8s.io/v1beta1"
K3S_VERSION = "v1.34.13+k3s1"
ROOT = Path("/tmp/lp-ki01-k3s")
TLS = ROOT / "state/server/tls"
SOCKET = Path("/tmp/lp-ki01-api.sock")


def manifest(version):
    backend = {"service": {"name": BACKEND, "port": {"number": 8080}}} if version == NEW else {
        "serviceName": BACKEND, "servicePort": 8080}
    return {"apiVersion": version, "kind": "Ingress", "metadata": {"namespace": NAMESPACE, "name": NAME},
            "spec": {"rules": [{"host": "ki01.verify.invalid", "http": {"paths": [
                {"path": "/check", "pathType": "Prefix", "backend": backend},
            ]}}]}}


def contract_sha():
    return sha256(json.dumps([K3S_VERSION, manifest(OLD), manifest(NEW)], sort_keys=True).encode()).hexdigest()


def kubectl(*args, obj=None, required=True):
    result = subprocess.run(
        ["/opt/isolated/k3s-arm64", "kubectl", "--kubeconfig", str(ROOT / "kubeconfig"),
         "--request-timeout=8s", *args],
        input=json.dumps(obj) if obj is not None else None, text=True, capture_output=True, timeout=12)
    if not required:
        return result
    if result.returncode:
        raise RuntimeError(f"fixture {args[0]} failed: {result.stderr[:260]}")
    return result.stdout


class PrivateAPI:
    def __init__(self):
        self.context = ssl.create_default_context(cafile=str(TLS / "server-ca.crt"))
        self.context.load_cert_chain(TLS / "client-admin.crt", TLS / "client-admin.key")

    def request(self, path, payload=None, *, method=None):
        request = Request("https://127.0.0.1:6443" + path, method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, context=self.context, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)


def collection(version):
    return f"/apis/{version}/namespaces/{NAMESPACE}/ingresses"


def missing(api):
    status, result = api.request(collection(NEW) + "/" + NAME)
    return status == 404 and result.get("reason") == "NotFound"


def stored_cycle(api, document):
    assert document == manifest(NEW) and missing(api)
    status, result = api.request(collection(NEW) + "?fieldValidation=Strict", document)
    assert status == 201
    uid = result["metadata"]["uid"]
    read_status, stored = api.request(collection(NEW) + "/" + NAME)
    assert read_status == 200 and stored["metadata"]["uid"] == uid
    assert {key: stored[key] for key in ("apiVersion", "kind", "spec")} == {
        key: document[key] for key in ("apiVersion", "kind", "spec")}
    assert stored["metadata"]["name"] == NAME and stored["metadata"]["namespace"] == NAMESPACE
    code, _ = api.request(collection(NEW) + "/" + NAME, {"preconditions": {"uid": uid}}, method="DELETE")
    assert code == 200 and missing(api)
    return {"http_status": status, "readback_status": read_status, "uid": uid,
            "manifest": document, "deleted": True}


def rejection(api):
    status, response = api.request(collection(OLD) + "?fieldValidation=Strict", manifest(OLD))
    assert status == 404 and response.get("reason") == "NotFound"
    assert missing(api)
    return {"http_status": status, "uri": collection(OLD), "response": response, "object_absent": True}


def discovery(api):
    code, data = api.request("/apis/networking.k8s.io")
    assert code == 200
    versions = [row["groupVersion"] for row in data["versions"]]
    assert NEW in versions and OLD not in versions
    return versions


def create_namespace(api):
    status, data = api.request("/api/v1/namespaces", {"apiVersion":"v1", "kind":"Namespace",
                                                      "metadata":{"name":NAMESPACE}})
    assert status == 201
    return data["metadata"]["uid"]


def prepare(api, uid):
    code, version = api.request("/version")
    assert code == 200 and version["gitVersion"] == K3S_VERSION
    code, backend = api.request(f"/api/v1/namespaces/{NAMESPACE}/services", {
        "apiVersion":"v1", "kind":"Service", "metadata":{"namespace":NAMESPACE,"name":BACKEND},
        "spec":{"ports":[{"port":8080,"targetPort":8080}]}})
    assert code == 201
    control = stored_cycle(api, manifest(NEW))
    result = kubectl("create", "-f", "-", obj=manifest(OLD), required=False)
    assert result.returncode and f'no matches for kind "Ingress" in version "{OLD}"' in result.stderr
    raw = rejection(api)
    start = monotonic()
    return {"case": CASE, "kubernetes": K3S_VERSION, "namespace_uid": uid, "fixture_sha256": contract_sha(),
            "initial_error_log": result.stderr.strip(), "fault_before": raw, "control": control,
            "api_discovery": discovery(api), "backend_uid": backend["metadata"]["uid"]}, start


def observe_credentials():
    vars = {"namespace": NAMESPACE, "name": "liveprobe-ki01-reader"}
    rbac = "rbac.authorization.k8s.io/v1"
    reads = ["get", "list", "watch"]
    for value in (
        {"apiVersion":"v1","kind":"ServiceAccount","metadata":vars},
        {"apiVersion":rbac,"kind":"Role","metadata":vars,"rules":[
            {"apiGroups":[""],"resources":["events","pods","services","endpoints"],"verbs":reads},
            {"apiGroups":["apps"],"resources":["deployments","replicasets"],"verbs":reads},
            {"apiGroups":["discovery.k8s.io"],"resources":["endpointslices"],"verbs":reads}]},
        {"apiVersion":rbac,"kind":"RoleBinding","metadata":vars,
         "roleRef":{"apiGroup":"rbac.authorization.k8s.io","kind":"Role","name":vars["name"]},
         "subjects":[{"kind":"ServiceAccount",**vars}]},
    ):
        kubectl("create", "-f", "-", obj=value)
    return {"token":kubectl("-n",NAMESPACE,"create","token",vars["name"],"--duration=10m").strip(),
            "authority":base64.b64encode((TLS/"server-ca.crt").read_bytes()).decode()}


class SocketPipe(BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("127.0.0.1", 6443), timeout=5) as upstream:
            upstream.settimeout(None)
            peers = {self.request:upstream, upstream:self.request}
            while True:
                active, _, _ = select(tuple(peers), (), (), 15)
                for endpoint in active:
                    data = endpoint.recv(65536)
                    if not data:
                        return
                    peers[endpoint].sendall(data)


@contextmanager
def api_bridge():
    with ThreadingUnixStreamServer(str(SOCKET), SocketPipe) as server:
        server.daemon_threads = True
        os.chown(SOCKET, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        SOCKET.chmod(0o600)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join(timeout=2)
            SOCKET.unlink(missing_ok=True)


def sentinel(api, phase):
    assert phase in ("before", "after")
    now = datetime.now(timezone.utc).isoformat()
    code, event = api.request(f"/api/v1/namespaces/{NAMESPACE}/events", {
        "apiVersion":"v1","kind":"Event","metadata":{"namespace":NAMESPACE,"name":f"lp-watch-proof-{phase}"},
        "type":"Normal","reason":"LiveProbeWatchProof","count":1,"message":"verify the live event watch",
        "source":{"component":"liveprobe-isolated-evaluation"},"firstTimestamp":now,"lastTimestamp":now,
        "involvedObject":{"apiVersion":NEW,"kind":"Ingress","namespace":NAMESPACE,"name":NAME}})
    assert code == 201
    return {"phase":phase,"uid":event["metadata"]["uid"]}


def send(data):
    print(json.dumps(data), flush=True)


def run(api, uid, *, observe=False):
    data, started = prepare(api, uid)
    send(data)
    proposal = None
    for line in sys.stdin:
        packet = json.loads(line)
        op = packet["tool"]
        if op == "shutdown":
            return
        if op == "workload":
            output = {"served_versions":discovery(api),"resource_absent":missing(api),"backend":BACKEND}
        elif op == "logs":
            output = {"client_error":data["initial_error_log"],"server_rejection":data["fault_before"]}
        elif op == "source":
            output = {"manifest":manifest(OLD)}
        elif op == "probe":
            output = rejection(api)
        elif op == "configure" and not observe:
            try:
                value = json.loads(packet.get("value") or "null")
            except (TypeError, ValueError):
                value = None
            if packet.get("key") == "manifest" and value == manifest(NEW):
                output = {"applied":True,"write":stored_cycle(api,value)}
                proposal = value
            else:
                output = {"applied":False,"reason":"Submit a served Ingress manifest that preserves the original route and backend."}
        elif op == "verify":
            proofs = [stored_cycle(api,proposal) for _ in range(2)] if proposal else [rejection(api) for _ in range(2)]
            output = {"independent_probes":proofs,"resource_absent":missing(api),
                      "wall_duration_ms":round((monotonic()-started)*1000)}
        elif op == "sentinel" and observe:
            output = sentinel(api, packet["phase"])
        else:
            output = {"accepted":False}
        send(output)


def main():
    assert os.geteuid() == 0 and os.environ["K3S_DATA_DIR"] == str(ROOT / "state")
    packet = json.loads(sys.stdin.readline())
    assert packet in ({"tool":"prepare"}, {"tool":"observer"})
    api = PrivateAPI()
    uid = create_namespace(api)
    try:
        if packet["tool"] == "observer":
            with api_bridge():
                send(observe_credentials())
                assert json.loads(sys.stdin.readline()) == {"tool":"sentinel","phase":"before"}
                send(sentinel(api,"before"))
                assert json.loads(sys.stdin.readline()) == {"tool":"inject"}
                run(api,uid,observe=True)
        else:
            run(api,uid)
    finally:
        kubectl("delete","namespace",NAMESPACE,"--ignore-not-found","--wait=true","--timeout=40s")
    assert not kubectl("get","namespace",NAMESPACE,"--ignore-not-found","-o","name").strip()
    send({"removed":True})


if __name__ == "__main__":
    main()
