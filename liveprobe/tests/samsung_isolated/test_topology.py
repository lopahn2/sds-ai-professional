"""DB fixture를 가져오지 않는 삼성 관제 경계 회귀."""
from __future__ import annotations
from collections import Counter
import copy
import json
from pathlib import Path
import unittest

from app.config import AppConfig
from app.flows import FlowRec, FlowStore, build_topology
from app.overview import assemble

PART = "app.kubernetes.io/part-of"
LIFECYCLE = "liveprobe.io/lifecycle"

def deployment(name, system, namespace="prd", **labels):
    return {"metadata": {"name": name, "namespace": namespace, "labels": {PART: system, **labels}}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}}

def service(name, system, namespace="prd"):
    return {"metadata": {"name": name, "namespace": namespace, "labels": {PART: system}}, "spec": {"clusterIP": "None", "selector": {"app": name}, "ports": [{"port": 9092 if name == "kafka" else 8080}]}}

def inventory():
    catalog = json.loads((Path(__file__).resolve().parents[3] / "deploy/samsung/system-catalog.json").read_text())
    return {"deployments": [deployment(name, system, env) for env in catalog["environments"] for name, system in catalog["service_to_system"].items()], "pods": [], "services": [service(name, system, env) for env in catalog["environments"] for name, system in catalog["service_to_system"].items()], "namespaces": [{"metadata": {"name": env}} for env in catalog["environments"]]}

class TopologyBoundaryTests(unittest.TestCase):
    def test_contract_paths_are_not_measurements_or_installed_kafka(self):
        inv = inventory()
        before = copy.deepcopy(inv)
        topo = build_topology(inv, [], FlowStore().freshness(), cfg=AppConfig())
        contracts = topo.get("contract_edges", [])
        self.assertEqual(Counter(edge["environment"] for edge in contracts), {"dev": 5, "prd": 5},
                         "활성 환경별 발행 1·소비 2·복구 2 계약이 필요하다")
        self.assertFalse(any(n["name"] == "kafka" for n in topo["nodes"]), "미배포 브로커를 실자원으로 만들면 안 된다")
        self.assertEqual(topo["observed_edges"], [])
        self.assertEqual(inv, before)
        for edge in contracts:
            self.assertFalse(edge["observed"])
            self.assertFalse(any(k in edge for k in ("flows", "req_rate", "err_rate")))
            if edge["type"] == "declared_event":
                kafka = edge["to_ref"] if edge["operation"] == "publish" else edge["from_ref"]
                self.assertEqual(kafka["namespace"], "liveprobe-messaging")
                self.assertFalse(kafka["installed"])
                self.assertEqual(kafka["system"], None)

    def test_consumer_network_direction_is_separate_from_logical_event(self):
        inv = inventory()
        inv["services"].append(service("kafka", None, "liveprobe-messaging"))
        flow = FlowRec(100, "prd/Deployment/trade-member", "liveprobe-messaging/Service/kafka", "FORWARDED", "TCP", 9092)
        topo = build_topology(inv, [flow], FlowStore().freshness(), cfg=AppConfig())
        observed = topo["observed_edges"][0]
        self.assertEqual(observed.get("type"), "observed_network")
        self.assertEqual((observed["from"], observed["to"]), (flow.src, flow.dst))
        logical = [e for e in topo["contract_edges"] if e["environment"] == "prd" and e["operation"] == "consume" and e["target_system"] == "samsung-trade"][0]
        self.assertEqual(logical["from"], flow.dst)
        self.assertEqual(logical["to"], "prd/Service/trade-member")
        self.assertFalse(logical["observed"])
        self.assertEqual(logical["connection_direction"], "consumer_to_broker")

    def test_explicit_retirement_excludes_resources_but_keeps_incident_history(self):
        inv = inventory()
        inv["deployments"].extend([deployment("member", "member", **{LIFECYCLE: "retired"}), deployment("quiet", "custom-system")])
        inv["pods"].append({"metadata": {"namespace": "prd", "name": "member-old-pod", "ownerReferences": [{"kind": "ReplicaSet", "name": "member-old"}]}, "status": {"phase": "Running"}})
        inv["services"].append({**service("member", "member"), "metadata": {"name": "member", "namespace": "prd", "labels": {LIFECYCLE: "retired"}}})
        old = {"id": 7, "namespace": "prd", "workload": "member", "system": "member", "status": "handed_off", "updated_ts": 1}
        flow = FlowRec(100, "prd/pod/member-old-pod", "prd/Deployment/order", "FORWARDED", "TCP", 8080)
        result = assemble(inv, {}, incidents=[old], store=FlowStore(), cfg=AppConfig())
        self.assertFalse(any(n["name"] == "member" for n in result["topology"]["nodes"]))
        self.assertFalse(any(w["name"] == "member" for w in result["workloads"]))
        self.assertTrue(any(w["name"] == "quiet" for w in result["workloads"]), "Pod 없음은 은퇴가 아니다")
        self.assertEqual(result["incidents"]["open"][0]["system"], "member")
        self.assertEqual(old["system"], "member")
        topo = build_topology(inv, [flow], FlowStore().freshness())
        self.assertFalse(any(e["from"] == flow.src for e in topo["observed_edges"]))

    def test_ingress_and_deployment_ownership_is_authoritative_not_name_guessing(self):
        inv = inventory()
        inv["deployments"].append(deployment("order-copy", "samsung-delivery"))
        inv["ingresses"] = [{"metadata": {"namespace": "prd", "name": "order-entry", "labels": {PART: "samsung-trade"}}, "spec": {"rules": [{"host": "order.samsung-trade.prd.localhost", "path": "/", "service": "order", "port": 8080}]}}]
        topo = build_topology(inv, [], FlowStore().freshness())
        nodes = {n["id"]: n for n in topo["nodes"]}
        self.assertEqual(nodes["prd/Deployment/order-copy"]["system"], "samsung-delivery")
        self.assertEqual(nodes["prd/Ingress/order-entry"].get("system"), "samsung-trade")
        names = {g["system"]: g["label"] for g in topo["groups"] if g.get("system")}
        self.assertEqual(names["samsung-trade"], "삼성 트레이드 시스템")
        self.assertEqual(names["samsung-delivery"], "삼성 딜리버리 시스템")
        self.assertEqual(names["samsung-business"], "삼성 비즈니스 시스템")

    def test_service_selector_does_not_fold_different_authoritative_systems(self):
        inv = inventory()
        inv["pods"] = [{"metadata": {"namespace": "prd", "name": "order-new-pod", "labels": {"app": "order", PART: "samsung-delivery"}, "ownerReferences": [{"kind": "ReplicaSet", "name": "order-new"}]}, "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}]
        inv["deployments"] = [deployment("order", "samsung-delivery")]
        for svc in inv["services"]:
            if svc["metadata"]["name"] == "business-api":
                svc["metadata"]["labels"][PART] = "samsung-delivery"
        topo = build_topology(inv, [], FlowStore().freshness())
        svc = next(n for n in topo["nodes"] if n["id"] == "prd/Service/order")
        self.assertNotIn("folded_into", svc, "selector가 교차 소속을 가리켜도 시스템을 합치면 안 된다")
        contract = next(e for e in topo["contract_edges"] if e["operation"] == "publish" and e["environment"] == "prd")
        self.assertFalse(contract["from_ref"]["installed"])

    def test_shared_kafka_infrastructure_label_is_preserved_without_business_ownership(self):
        inv = inventory()
        inv["services"].append(service("kafka", "liveprobe-messaging", "liveprobe-messaging"))
        topo = build_topology(inv, [], FlowStore().freshness())
        edge = next(e for e in topo["contract_edges"] if e["operation"] == "publish")
        self.assertTrue(edge["to_ref"]["installed"])
        self.assertEqual(edge["to_ref"]["system"], "liveprobe-messaging")
        self.assertEqual(next(n for n in topo["nodes"] if n["name"] == "kafka")["system"], "liveprobe-messaging")

if __name__ == "__main__":
    unittest.main()
