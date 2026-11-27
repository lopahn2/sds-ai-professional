"""Observe a real Istiod listener update and version-specific Envoy rejection."""
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import URLError, HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4


CASE, KUBERNETES = "KI-09", "v1.35.1+k3s1"
NAMESPACE, WORKLOAD = "dev-ki09-verify", "ki09-gateway"
PREVIOUS, CURRENT = "1.29.10", "1.30.5"
HOST = "ki09.test"
CONTROL_PORT, FAULT_PORT, ADMIN_PORT = 18080, 18081, 15000
HCM = "envoy.filters.network.http_connection_manager"
TYPE = "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager"
LISTENER_TYPE = "type.googleapis.com/envoy.config.listener.v3.Listener"
CONTROL_BODY, NEW_BODY = "ki09-control-ok", "ki09-revision-ok"
ROOT = Path("/tmp/lp-ki09-k3s")
OBSERVER_SOCKET = Path("/tmp/lp-ki09-observer.sock")
K3S = "/opt/isolated/k3s-arm64"
BINARIES = {
    "pilot-discovery": "6e690df840dfe7df7a0f78c263d6b0f440e969f263b87134805f382cbe071f64",
    "envoy-prev": "1dfc7075b3de31280bba6007363818d1517e5872d4d612fbcaa5190887aa0f92",
    "envoy-current": "30f60e0cff0cc043633c7f35efa70d9a9c7812bbfd333b78ce122d726ee0cfcb",
}
ENV = {**os.environ, "K3S_DATA_DIR": str(ROOT / "state")}
OPEN = build_opener(ProxyHandler({}))
SHUTDOWN = {"namespace_removed": True, "gateway_process_stopped": True}


def digest(payload):
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def kube(*args, data=None, timeout=14):
    result = subprocess.run([K3S, "kubectl", "--kubeconfig", str(ROOT / "kubeconfig"), "--request-timeout=9s", *args],
                            env=ENV, capture_output=True, text=True, timeout=timeout,
                            input=json.dumps(data) if data is not None else None)
    if result.returncode:
        raise RuntimeError(f"kubernetes {args[0]}: {result.stderr[-800:]}")
    return result.stdout


def mesh(kind, name, spec):
    return {"apiVersion": "networking.istio.io/v1alpha3", "kind": kind,
            "metadata": {"namespace": NAMESPACE, "name": name}, "spec": spec}


def gateway(ports):
    return mesh("Gateway", WORKLOAD, {"selector": {"istio": WORKLOAD},
                 "servers": [{"port": {"number": port, "name": f"http-{port}", "protocol": "HTTP"},
                              "hosts": [HOST]} for port in ports]})


def routes():
    return mesh("VirtualService", WORKLOAD, {"hosts": [HOST], "gateways": [WORKLOAD],
                "http": [{"name": f"ki09-{port}", "match": [{"port": port}],
                          "directResponse": {"status": 200, "body": {"string": body}}}
                         for port, body in ((CONTROL_PORT, CONTROL_BODY), (FAULT_PORT, NEW_BODY))]})


def patch():
    return mesh("EnvoyFilter", "ki09-compatibility", {"workloadSelector": {"labels": {"istio": WORKLOAD}},
                "configPatches": [{"applyTo": "NETWORK_FILTER", "match": {"context": "GATEWAY",
                    "listener": {"portNumber": FAULT_PORT, "filterChain": {"filter": {"name": HCM}}}},
                    "patch": {"operation": "MERGE", "value": {"name": HCM,
                        "typed_config": {"@type": TYPE, "forward_proto_config": {"https_destination_ports": [FAULT_PORT]}}}}}]})


def bootstrap(version):
    return {"node": {"id": f"router~192.0.2.10~{WORKLOAD}.{NAMESPACE}~{NAMESPACE}.svc.cluster.local",
                     "cluster": f"{WORKLOAD}.{NAMESPACE}",
                     "metadata": {"ISTIO_VERSION": version, "NAMESPACE": NAMESPACE, "CLUSTER_ID": "Kubernetes",
                                  "LABELS": {"istio": WORKLOAD}}},
            "admin": {"address": {"socket_address": {"address": "127.0.0.1", "port_value": ADMIN_PORT}}},
            "dynamic_resources": {"ads_config": {"api_type": "GRPC", "transport_api_version": "V3",
                                               "grpc_services": [{"envoy_grpc": {"cluster_name": "xds-grpc"}}]},
                                  "lds_config": {"ads": {}, "resource_api_version": "V3"}},
            "static_resources": {"clusters": [{"name": "xds-grpc", "type": "STATIC", "connect_timeout": "1s",
                                               "http2_protocol_options": {},
                                               "load_assignment": {"cluster_name": "xds-grpc", "endpoints": [{
                                                   "lb_endpoints": [{"endpoint": {"address": {"socket_address": {
                                                       "address": "127.0.0.1", "port_value": 15010}}}}]}]}}]}}


def probe(port):
    nonce = uuid4().hex
    try:
        req = Request(f"http://127.0.0.1:{port}/check?nonce={nonce}", headers={"Host": HOST})
        with OPEN.open(req, timeout=2) as response:
            return {"status": response.status, "body": response.read(120).decode(), "nonce": nonce}
    except HTTPError as exc:
        return {"status": exc.code, "body": None, "nonce": nonce}
    except (URLError, OSError):
        return {"status": None, "body": None, "nonce": nonce}


def admin(endpoint):
    try:
        with OPEN.open(f"http://127.0.0.1:{ADMIN_PORT}/{endpoint}", timeout=2) as response:
            return json.load(response)
    except (URLError, OSError, ValueError):
        return {}


def sync_listener():
    try:
        with OPEN.open("http://127.0.0.1:8080/debug/syncz", timeout=2) as response:
            records = json.load(response)
    except (URLError, OSError, ValueError):
        return None
    if not isinstance(records, list):
        return None
    return next((row for row in records if WORKLOAD in json.dumps(row)), None)


def lds_reject_count():
    with OPEN.open("http://127.0.0.1:15014/metrics", timeout=4) as response:
        lines = response.read(1048576).decode().splitlines()
    prefix = 'pilot_total_xds_rejects{type="lds"} '
    hits = [int(line.removeprefix(prefix)) for line in lines if line.startswith(prefix)]
    if len(hits) > 1:
        raise RuntimeError("ambiguous Istiod LDS rejection metric")
    return hits[0] if hits else 0


def listener_dump():
    doc = admin("config_dump")
    active, errors = {}, []
    for records in doc.get("configs", []):
        for listener in records.get("dynamic_listeners", []):
            if error := listener.get("error_state"):
                errors.append({"resource": listener.get("name"), "details": error.get("details"),
                               "version": error.get("version_info")})
            if state := listener.get("active_state"):
                socket = state["listener"]["address"].get("socket_address", {})
                if port := socket.get("port_value"):
                    active[str(port)] = {"name": listener.get("name"), "version": state.get("version_info"),
                                         "typed_forward_proto": "forward_proto_config" in json.dumps(state)}
    return active, errors


def wait_for(observe, predicate, label, timeout):
    end = monotonic() + timeout
    while True:
        item = observe()
        if predicate(item):
            return item
        if monotonic() >= end:
            raise RuntimeError(f"{label}: {json.dumps(item, default=str)[:3000]}")
        sleep(0.65)


def healthy_control(row):
    return (row["control"]["status"], row["control"]["body"]) == (200, CONTROL_BODY)


def listener_sync(row):
    return (row["istiod_sync"] or {}).get("resources", {}).get(LISTENER_TYPE, {})


def listener_acked(row):
    sync = listener_sync(row)
    return bool(sync.get("sent") and sync["sent"] == sync.get("acked"))


def blocked(row):
    sync = listener_sync(row)
    return (row["version"] == PREVIOUS and healthy_control(row) and row["new"]["status"] is None
            and row["nack"] and str(FAULT_PORT) not in row["active_listeners"]
            and sync.get("sent") and sync["sent"] != sync.get("acked"))


def recovered(row):
    new = row["active_listeners"].get(str(FAULT_PORT), {})
    return (row["version"] == CURRENT and healthy_control(row) and row["new"]["status"] == 200
            and row["new"]["body"] == NEW_BODY and new.get("typed_forward_proto") is True
            and listener_acked(row))


def clean_error(error):
    if not isinstance(error, str):
        return None
    start = f"Error adding/updating listener(s) 0.0.0.0_{FAULT_PORT}:"
    return next((line[line.index(start):].strip() for line in error.splitlines()
                 if start in line and "HttpConnectionManager" in line
                 and "unknown field set {61}" in line and "has unknown fields" in line), None)


class Trial:
    def __init__(self):
        self.created = False
        self.worker = None
        self.version = PREVIOUS
        self.id = uuid4().hex[:12]
        self.injected = False

    def setup(self):
        assert not kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name")
        assert not subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True).strip()
        assert json.loads(kube("get", "--raw=/version"))["gitVersion"] == KUBERNETES
        for name, checksum in BINARIES.items():
            with (Path("/opt/isolated") / name).open("rb") as fh:
                assert file_digest(fh, "sha256").hexdigest() == checksum
        kube("create", "namespace", NAMESPACE)
        self.created = True
        self.contract = {"kubernetes": KUBERNETES, "binaries": BINARIES, "gateway_initial": gateway([CONTROL_PORT]),
                         "gateway_expanded": gateway([CONTROL_PORT, FAULT_PORT]), "routes": routes(), "patch": patch(),
                         "strict_dynamic_config": True, "bootstraps": [bootstrap(v) for v in (PREVIOUS, CURRENT)]}

    def start_proxy(self, version):
        assert version in (PREVIOUS, CURRENT) and self.worker is None
        binary = "envoy-prev" if version == PREVIOUS else "envoy-current"
        config = ROOT / f"{self.id}-{version}.json"
        self.log = ROOT / f"{self.id}-{version}.log"
        config.write_text(json.dumps(bootstrap(version)))
        linker = "/opt/isolated/lib/ld-linux-aarch64.so.1"
        with self.log.open("x") as out:
            self.worker = subprocess.Popen([
                linker, "--library-path", "/opt/isolated/lib", f"/opt/isolated/{binary}", "-c", str(config),
                "--disable-hot-restart", "--concurrency", "1", "--reject-unknown-dynamic-fields", "-l", "info",
            ], env=ENV, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.version = version

    def stop_proxy(self):
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=9)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait(timeout=5)
        self.worker = None

    def observe(self):
        if self.worker and self.worker.poll() is not None:
            raise RuntimeError(f"proxy stopped: {self.log.read_text().splitlines()[-9:]}")
        active, errors = listener_dump()
        nack = next((matched for row in errors if (matched := clean_error(row["details"]))), None)
        if not nack and self.worker:
            nack = clean_error(self.log.read_text())
        info = admin("server_info")
        return {"at": datetime.now(timezone.utc).isoformat(), "version": self.version,
                "active_listeners": active, "nack": nack, "xds_errors": errors,
                "istiod_sync": sync_listener(), "control": probe(CONTROL_PORT), "new": probe(FAULT_PORT),
                "envoy_build": info.get("version")}

    def prepare(self):
        for resource in (gateway([CONTROL_PORT]), routes(), patch()):
            kube("create", "-f", "-", data=resource)
        self.start_proxy(PREVIOUS)
        self.before = wait_for(self.observe, lambda row: healthy_control(row) and listener_acked(row),
                               "control gateway failed to start", 55)
        assert self.before["new"]["status"] is None
        desired = json.loads(kube("-n", NAMESPACE, "get", "gateway.networking.istio.io", WORKLOAD, "-o", "json"))
        desired["spec"] = gateway([CONTROL_PORT, FAULT_PORT])["spec"]
        kube("replace", "-f", "-", data=desired)
        self.injected = True
        self.fault = wait_for(self.observe, blocked, "old Envoy did not reject the new listener", 55)
        self.started = monotonic()
        return {"case": CASE, "kubernetes": KUBERNETES, "fixture_contract": self.contract,
                "fixture_sha256": digest([CASE, self.contract]), "initial_error_log": self.fault["nack"],
                "healthy_before": self.before, "fault_before": self.fault, "mesh_identity": self.mesh_identity()}

    def mesh_identity(self):
        keys = (("gateway.networking.istio.io", WORKLOAD), ("virtualservice", WORKLOAD),
                ("envoyfilter", "ki09-compatibility"))
        result = {}
        for kind, name in keys:
            obj = json.loads(kube("-n", NAMESPACE, "get", kind, name, "-o", "json"))
            result[kind] = {"uid": obj["metadata"]["uid"], "generation": obj["metadata"]["generation"],
                            "spec_sha256": digest(obj["spec"])}
        return result

    def restore(self):
        if not self.injected or self.version == CURRENT:
            return {"applied": False, "reason": "no rejected configuration or the selected proxy is already running"}
        self.stop_proxy()
        self.start_proxy(CURRENT)
        return {"applied": True, "before": PREVIOUS, "after": CURRENT, "target": WORKLOAD}

    def verify(self):
        predicate = recovered if self.version == CURRENT else blocked
        first = wait_for(self.observe, predicate, "first independent route confirmation", 52)
        sleep(1.6)
        second = wait_for(self.observe, predicate, "second independent route confirmation", 12)
        return {"independent_probes": [first, second], "mesh_identity_after": self.mesh_identity(),
                "wall_duration_ms": round((monotonic() - self.started) * 1000)}

    def cleanup(self):
        self.stop_proxy()
        if self.created:
            kube("delete", "namespace", NAMESPACE, "--timeout=45s", timeout=48)
            self.created = False
        assert not kube("get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "name")
        return SHUTDOWN

    def handle(self, request):
        tool = request["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool in {"workload", "probe"}:
            return self.observe()
        if tool == "logs":
            return {"native_xds_error": self.fault["nack"], "affected_gateway": WORKLOAD}
        if tool == "source":
            return {"managed_gateway": WORKLOAD, "control_plane": CURRENT, "available_proxy_versions": [PREVIOUS, CURRENT],
                    "version_setting": "gateway_proxy_version", "listener_patch": patch()["spec"]}
        if tool == "configure":
            if (request.get("key"), request.get("value")) == ("gateway_proxy_version", CURRENT):
                return self.restore()
            return {"applied": False, "reason": "only the selected test gateway proxy version can be updated"}
        if tool == "verify":
            return self.verify()
        if tool == "shutdown":
            return self.cleanup()
        raise ValueError("unknown trial operation")


def main():
    trial = Trial()
    observer = None
    if sys.argv[1:] == ["--observe"]:
        spec = importlib.util.spec_from_file_location("observer", ROOT / "observer.py")
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        observer.NAMESPACE, observer.TARGET = NAMESPACE, WORKLOAD
        observer.kubeconfig = ROOT / "kubeconfig"
        observer.OBSERVER_SOCKET = OBSERVER_SOCKET
    try:
        trial.setup()
        with (observer.observer_transport() if observer else nullcontext()):
            for line in sys.stdin:
                request = json.loads(line)
                tool = request["tool"]
                if observer and tool == "access":
                    result = observer.read_only_observer()
                elif observer and tool == "sentinel":
                    result = observer.sentinel(request["phase"])
                elif observer and tool == "lds_reject_count":
                    result = {"pilot_lds_reject_count": lds_reject_count()}
                else:
                    result = trial.handle(request)
                print(json.dumps(result), flush=True)
                if tool == "shutdown":
                    break
    finally:
        trial.cleanup()


if __name__ == "__main__":
    main()
