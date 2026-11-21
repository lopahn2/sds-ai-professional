from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import io
import json

import pytest

from liveprobe.application_runner.profiles import Registry, digest
from liveprobe.application_runner.recovery_probe import http_error_signature, run_recovery


NOW = 1_800_000_000
IMAGE = "liveprobe/catalog@sha256:" + "a" * 64
REG = Registry(Path(__file__).resolve().parents[2])
PATH = "/api/products"


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def access(t, code, path=PATH):
    return stamp(t) + " " + json.dumps({"event":"http_access","service":"catalog","environment":"prd",
                                        "uri":path,"method":"GET","status":code})


class Stream(io.BytesIO):
    def release_conn(self):
        pass


class Observer:
    def __init__(self):
        self.image = IMAGE
        self.api = self
        self.http = []
        self.lines = []
        self.body = {"products":[{"name":"item","price":20}]}
        self.status = 200
        self.after_read = None
        self.roll_uid = "deployment-1"
        self.started = NOW-3600

    def read(self, env, service):
        assert env == "prd" and service == "catalog"
        return {"metadata":{"namespace":env,"name":service,"uid":self.roll_uid,"resourceVersion":"17","generation":2},
                "spec":{"replicas":1,"template":{"spec":{"containers":[{"name":service,"image":self.image}]}}},
                "status":{"observedGeneration":2,"readyReplicas":1,"updatedReplicas":1,"availableReplicas":1}}

    def pods(self, env, service):
        return [{"owned":True,"ready":True,"image_id":self.image}]

    def _call(self, path, method, **kw):
        assert method == "GET" and kw["query"] == [("labelSelector","app=catalog")]
        if path.endswith("/replicasets"):
            return {"items":[{"metadata":{"uid":"rs1","ownerReferences":[{"kind":"Deployment","controller":True,"uid":self.roll_uid}]}}]}
        assert path == "/api/v1/namespaces/prd/pods"
        return {"items":[{"metadata":{"name":"catalog-1","uid":"pod1","ownerReferences":[{"kind":"ReplicaSet","controller":True,"uid":"rs1"}]},
                          "status":{"containerStatuses":[{"name":"catalog","ready":True,"containerID":"ctr1","imageID":self.image,
                                                          "state":{"running":{"startedAt":stamp(self.started)}}}]}}]}

    def call_api(self, path, method, **kwargs):
        assert method == "GET" and path == "/api/v1/namespaces/prd/pods/catalog-1/log"
        assert dict(kwargs["query_params"])["container"] == "catalog"
        return Stream("\n".join(self.lines).encode())

    def business(self, env, service, path):
        assert env == "prd" and service == "catalog" and path == PATH
        self.http.append(path)
        if self.after_read:
            self.after_read()
        return {"status":self.status,"body":self.body}


def job(*, passive=False, active=False):
    artifact = {"image":IMAGE,"source_tree_sha256":"e"*64}
    contract = {"service":"catalog","deployed_at":stamp(NOW-60),"allow_harmless_check":active,
                "operation":{"method":"GET","path":PATH}}
    if passive:
        contract.update(http_status=500, original_error_sha256=http_error_signature("catalog","GET",PATH,500))
    return {"stage":"prd","artifact":artifact,"release_id":"release-42","artifact_sha256":digest(artifact),
            "plan_sha256":"f"*64,"targets":[{"environment":"prd","service":"catalog","uid":"deployment-1","system":"samsung-trade"}],
            "recovery_contract":contract}


def observe(k, payload, **kw):
    result = run_recovery(REG,k,payload,clock=lambda:NOW,**kw)
    assert result["write_outcome"] == "not_written"
    proof = dict(result["recovery"])
    assert proof.pop("receipt_id") == digest(proof)
    return result["recovery"]


def test_actual_postdeployment_access_then_matching_failure_blocks_active_fallback():
    k=Observer()
    k.lines=[access(NOW-59,200),access(NOW-58,200,"/healthz"),access(NOW-61,500)]
    proof=observe(k,job(passive=True,active=True))
    assert (proof["outcome"],proof["method"],proof["success_count"],proof["error_count"]) == ("passed","original_error_absent",1,0)
    assert not k.http
    k.lines.append(access(NOW-2,500))
    proof=observe(k,job(passive=True,active=True))
    assert proof["outcome"]=="failed" and proof["error_count"]==1 and not k.http
    k.lines = [access(NOW-2,500)]
    assert observe(k,job(passive=True,active=True))["outcome"] == "failed" and not k.http


@pytest.mark.parametrize("evidence", [[],[access(NOW-2,200)],[access(NOW-61,200)],[access(NOW-2,200,"/healthz")],["x"*65537]])
def test_no_traffic_stale_unrelated_or_truncated_log_remains_unobserved(evidence):
    k=Observer(); k.lines=evidence
    assert observe(k,job(passive=True))["outcome"]=="unverifiable" and not k.http


def test_retry_can_observe_real_semantic_read_and_retains_no_response_body():
    k=Observer();p=job(active=True)
    first=observe(k,p)
    assert first["method"]=="harmless_check" and first["outcome"]=="passed" and first["original_error_unmatched"] is True
    assert first["body_sha256"]==digest(k.body) and "products" not in str(first.get("body",""))
    k.body={"products":[{"name":"", "price":20}],"authorization":"never-export-this"}
    second=observe(k,p)
    assert second["outcome"]=="failed" and "never-export-this" not in str(second)
    k.status=503
    assert observe(k,p)["status_code"]==503
    k.status=200;k.body={"products":[{"name":"recovered","price":30}]}
    assert observe(k,p)["outcome"]=="passed" and len(k.http)==4


@pytest.mark.parametrize("key,val", [("path","/healthz/ready"),("path","https://other/path?token=secret"),
                                     ("method","POST"),("headers",{"Authorization":"do-not-leak"})])
def test_off_operation_and_credential_fields_never_reach_transport(key,val):
    k=Observer(); p=job(active=True);p["recovery_contract"]["operation"][key]=val
    proof=observe(k,p)
    assert proof["outcome"]=="unverifiable" and not k.http
    assert "do-not-leak" not in str(proof) and "?token=" not in str(proof)


def test_replaced_deployment_wrong_plan_or_stale_release_cannot_close():
    k=Observer();p=job(active=True)
    k.after_read=lambda:setattr(k,"roll_uid","replacement")
    assert observe(k,p)["outcome"]=="unverifiable"
    k=Observer()
    for patch in ({"environment":"dev"},{"deployed_at":NOW+10},{"release_id":"different"},{"deployment_uid":"other"}):
        bad=deepcopy(p);bad["recovery_contract"].update(patch)
        assert observe(k,bad)["outcome"]=="unverifiable"
    p["artifact_sha256"]="1"*64
    assert observe(k,p)["outcome"]=="unverifiable" and not k.http


def test_wrong_window_or_release_from_adapter_is_rejected():
    k=Observer();p=job(passive=True,active=True)
    def wrong(scope):
        return {**scope,"complete":True,"success_count":1,"error_count":0,"release_id":"obsolete"}
    assert observe(k,p,read_signals=wrong)["outcome"]=="unverifiable" and not k.http


def test_unavailable_contract_never_probes():
    k=Observer();p=job(active=True);p.pop("recovery_contract")
    assert observe(k,p)["outcome"]=="unverifiable" and not k.http
