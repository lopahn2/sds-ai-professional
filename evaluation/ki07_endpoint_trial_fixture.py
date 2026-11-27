"""Serve bounded read and collector-switch operations for the offline KI-07 case."""
import importlib.util
from hashlib import sha256
import json
import sys
from threading import Thread
from time import monotonic
from urllib.parse import urlencode

from http.server import ThreadingHTTPServer


CASE = "KI-07"
PROTOCOL = "live-repair/v40"
KUBERNETES = "v1.34.13+k3s1"
LEGACY = "core/v1/Endpoints"
DISCOVERY = "discovery.k8s.io/v1/EndpointSlice"
HOST_FIXTURE = "/tmp/ki07-endpoint-controller-fixture-20261124.py"
NS = "dev-ki07-verify"


def load_fixture():
    spec = importlib.util.spec_from_file_location("ki07_nodes", HOST_FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.NS == NS
    return module


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Trial:
    def __init__(self, fixture):
        self.fixture = fixture
        version = fixture.api("/version")["gitVersion"]
        if version != KUBERNETES:
            raise RuntimeError(f"unexpected trial Kubernetes version {version}")
        self.contract = self.service_contract()
        self.contract_sha = fingerprint([CASE, PROTOCOL, KUBERNETES, self.contract])
        self.source = LEGACY
        self.started = None

    def service_contract(self):
        service = self.fixture.api(f"{self.fixture.BASE}/services/probe")
        witness = self.fixture.api(f"{self.fixture.BASE}/services/omitted-backend-witness")
        return {"services": [{"uid": item["metadata"]["uid"], "name": item["metadata"]["name"],
                              "cluster_ip": item["spec"]["clusterIP"], "selector": item["spec"]["selector"],
                              "ports": item["spec"]["ports"]} for item in (service, witness)],
                "namespace_uid": self.fixture.api(self.fixture.BASE)["metadata"]["uid"]}

    def sample(self):
        if self.service_contract() != self.contract:
            raise RuntimeError("the original Service specifications changed")
        target, witness = self.contract["services"]
        result = self.fixture.inspect(target["uid"])
        selector = urlencode({"labelSelector": "app=ki07-observer"})
        pods = self.fixture.api(f"{self.fixture.BASE}/pods?{selector}")["items"]
        expected = {pod["status"]["podIP"] for pod in pods if pod["status"]["phase"] == "Running"
                    and any(c["type"] == "Ready" and c["status"] == "True"
                            for c in pod["status"]["conditions"])}
        assert len(expected) == len(pods) == 1001
        assert result["legacy_ready"] == 1000 and result["slice_ready"] == 1001
        assert result["over_capacity"] == "truncated" and len(result["omitted"]) == 1
        http = self.fixture.get_http(target["cluster_ip"], 8080)
        omitted_http = self.fixture.get_http(witness["cluster_ip"], 8080)
        assert http["backend"] in expected and omitted_http["backend"] == result["omitted"][0] in expected
        ready = result["legacy_ready"] if self.source == LEGACY else result["slice_ready"]
        return {**result, "source": self.source, "collected_ready": ready, "expected_ready": len(expected),
                "contract_ok": ready == len(expected), "service_contract_sha256": self.contract_sha,
                "service_http": http, "omitted_backend_http": omitted_http}

    def prepare(self):
        self.source = LEGACY
        before = self.sample()
        initial = (f"endpoint collector: expected {before['expected_ready']} ready targets, collected "
                   f"{before['collected_ready']} for Service probe; inventory is {before['over_capacity']}")
        self.started = monotonic()
        return {"case": CASE, "protocol": PROTOCOL, "kubernetes": KUBERNETES, "fixture_sha256": self.contract_sha,
                "initial_error_log": initial, "fault_before": before, "service_contract": self.contract}

    def request(self, packet):
        tool = packet["tool"]
        if tool == "prepare":
            return self.prepare()
        if tool == "workload":
            return {"service": self.contract["services"][0], "namespace": NS, "kubernetes": KUBERNETES}
        if tool == "logs":
            sample = self.sample()
            return {k: sample[k] for k in ("source", "expected_ready", "collected_ready", "over_capacity")}
        if tool == "source":
            return {"setting": "endpoint_source", "active_value": self.source,
                    "supported_values": [LEGACY, DISCOVERY],
                    "endpoint_slice_mode": "index Service-owned current slices by ready address"}
        if tool == "probe":
            return self.sample()
        if tool == "configure":
            if packet.get("key") != "endpoint_source" or packet.get("value") not in (LEGACY, DISCOVERY):
                return {"applied": False, "reason": "the collector source setting is the only editable field"}
            previous = self.source
            self.source = packet["value"]
            return {"applied": previous != self.source, "before": previous, "after": self.source}
        if tool == "verify":
            probes = [self.sample(), self.sample()]
            return {"independent_probes": probes, "wall_duration_ms": round((monotonic() - self.started) * 1000)}
        if tool == "shutdown":
            return {"stopped": True}
        raise ValueError(f"unknown tool {tool}")


def main():
    fixture = load_fixture()
    trial = Trial(fixture)
    with ThreadingHTTPServer(("0.0.0.0", 18080), fixture.Responder) as server:
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            for line in sys.stdin:
                packet = json.loads(line)
                result = trial.request(packet)
                print(json.dumps(result), flush=True)
                if packet["tool"] == "shutdown":
                    break
        finally:
            server.shutdown()
            serving.join()


if __name__ == "__main__":
    main()
