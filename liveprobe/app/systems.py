"""삼성 업무 소속·표시와 논리 연계 계약. 실자원 소속은 언제나 수집 라벨을 따른다."""
from __future__ import annotations

# 배포 시 카탈로그 파일이 이미지 밖에 있으므로 격리 시험으로 원본과 일치시킨다.
SYSTEM_NAMES = {
    "samsung-trade": "삼성 트레이드 시스템",
    "samsung-delivery": "삼성 딜리버리 시스템",
    "samsung-business": "삼성 비즈니스 시스템",
}
SERVICE_SYSTEMS = {
    "order": "samsung-trade", "inventory": "samsung-trade", "catalog": "samsung-trade",
    "payment": "samsung-trade", "trade-member": "samsung-trade",
    "delivery": "samsung-delivery", "notification": "samsung-delivery", "delivery-member": "samsung-delivery",
    "business-api": "samsung-business",
}
KAFKA_NAMESPACE, KAFKA_SERVICE = "liveprobe-messaging", "kafka"
LIFECYCLE_LABEL = "liveprobe.io/lifecycle"


def system_ko(system: str | None) -> str:
    return SYSTEM_NAMES.get(system or "", system or "")


def current_inventory(inventory: dict) -> tuple[dict, set[str]]:
    """명시적 은퇴와 그 Pod만 제외한다. 0 replica·Pod 부재로 은퇴를 추정하지 않는다."""
    retired: set[str] = set()
    deployments = {(d.get("metadata", {}).get("namespace"), d.get("metadata", {}).get("name"))
                   for d in inventory.get("deployments", [])
                   if (d.get("metadata", {}).get("labels") or {}).get(LIFECYCLE_LABEL) == "retired"}
    result = dict(inventory)
    kinds = {"deployments": "Deployment", "pods": "pod", "services": "Service", "endpoints": "Endpoints", "ingresses": "Ingress"}
    for key, kind in kinds.items():
        kept = []
        for obj in inventory.get(key, []):
            md = obj.get("metadata") or {}
            ns, name = md.get("namespace"), md.get("name")
            excluded = (md.get("labels") or {}).get(LIFECYCLE_LABEL) == "retired"
            if key == "pods":
                for owner in md.get("ownerReferences") or []:
                    if owner.get("kind") == "ReplicaSet":
                        excluded |= (ns, owner.get("name", "").rsplit("-", 1)[0]) in deployments
            if excluded:
                retired.add(f"{ns}/{kind}/{name}")
                ip = (obj.get("status") or {}).get("podIP") or (obj.get("spec") or {}).get("clusterIP")
                if ip and ip != "None":
                    retired.add(f"ip/{ip}")
            else:
                kept.append(obj)
        result[key] = kept
    return result, retired


def contract_edges(nodes: dict[str, dict], environments: list[str]) -> list[dict]:
    """존재 여부가 붙은 참조만 반환한다. 미배포 계약 때문에 실제 노드를 만들지 않는다."""
    def endpoint(ns, service, system, env):
        sid = f"{ns}/Service/{service}"
        node = nodes.get(sid)
        installed = bool(node and (node.get("system") == system if system is not None else node.get("system") not in SYSTEM_NAMES))
        actual_system = node.get("system") if installed and node else system
        return {"id": sid, "name": service, "namespace": ns, "system": actual_system,
                "environment": env, "installed": installed}

    edges = []
    for env in environments:
        if env not in ("dev", "stg", "prd"):
            continue
        business = endpoint(env, "business-api", "samsung-business", env)
        kafka = endpoint(KAFKA_NAMESPACE, KAFKA_SERVICE, None, None)
        def add(source, target, kind, operation, target_system=None, topic=None):
            edges.append({"id": f"{env}:{kind}:{operation}:{source['id']}>{target['id']}",
                "from": source["id"], "to": target["id"], "from_ref": source, "to_ref": target,
                "type": kind, "operation": operation, "environment": env, "observed": False,
                "target_system": target_system, "topic": topic,
                "connection_direction": "consumer_to_broker" if operation == "consume" else "producer_to_broker" if operation == "publish" else "consumer_to_business",
                "deployment_state": "deployed" if source["installed"] and target["installed"] else "not_deployed"})
        add(business, kafka, "declared_event", "publish")
        for target in ("trade", "delivery"):
            system = f"samsung-{target}"
            consumer = endpoint(env, f"{target}-member", system, env)
            add(kafka, consumer, "declared_event", "consume", system, f"{env}.business.{target}.master.v1")
            add(consumer, business, "declared_snapshot_api", "snapshot", system)
    return edges
