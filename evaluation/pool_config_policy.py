"""Build the dev-only, UID-bound permissions for the ConfigMap recovery trial."""

from copy import deepcopy
import json
from uuid import UUID

from evaluation.jdbc_dev_manifest import POOL_CONFIG, POOL_KEY, POOL_NAME, POOL_VERSION


ROLE = "liveprobe-eval-pool"
EXECUTOR = "system:serviceaccount:liveprobe-system:safe-action-executor"
READER = "system:serviceaccount:liveprobe-system:liveprobe"


def permissions():
    config_reads = {"apiGroups": [""], "resources": ["configmaps"],
                    "resourceNames": [POOL_CONFIG, POOL_VERSION], "verbs": ["get"]}
    config_write = {"apiGroups": [""], "resources": ["configmaps"],
                    "resourceNames": [POOL_CONFIG], "verbs": ["patch"]}
    restart = {"apiGroups": ["apps"], "resources": ["deployments"],
               "resourceNames": [POOL_NAME], "verbs": ["get", "patch"]}
    items = []
    for name, identity, rules in ((ROLE, "safe-action-executor", [config_reads, config_write, restart]),
                                  (ROLE + "-reader", "liveprobe", [config_reads])):
        meta = {"name": name, "namespace": "dev"}
        items.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta, "rules": rules})
        items.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta,
                      "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                      "subjects": [{"kind": "ServiceAccount", "name": identity, "namespace": "liveprobe-system"}]})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def guard_patch(policy: dict, deployment_uid: str, config_uid: str) -> list[dict]:
    if str(UUID(deployment_uid)) != deployment_uid or str(UUID(config_uid)) != config_uid:
        raise ValueError("pool recovery requires the exact live Kubernetes UIDs")
    meta = policy["metadata"]
    spec = policy["spec"]
    if meta["name"] != "safe-action-executor-bounds" or spec["matchConditions"] != [
        {"name": "executor-only", "expression": f"request.userInfo.username == '{EXECUTOR}'"}
    ]:
        raise ValueError("unexpected shared admission policy identity")
    rules = deepcopy(spec["validations"])
    config, = [row for row in rules if row.get("message") ==
               "only the UID-bound temporary ConfigMap may restore its exact approved data"]
    deployment, = [row for row in rules if row.get("message") == "only managed P18 system deployments may be tuned"]
    old_config = config["expression"]
    old_deployment = deployment["expression"]
    if POOL_CONFIG in old_config or POOL_NAME in old_deployment or "lp-config-settings" not in old_config:
        raise ValueError("the active shared policy is already extended or its shape changed")

    # Keep the pre-existing metadata and all non-data invariants verbatim for the new ConfigMap.
    _, invariants = old_config.split(" && dyn(object).all(", 1)
    before = json.dumps({POOL_KEY: "1"})
    after = json.dumps({POOL_KEY: "4"})
    config["expression"] = (
        f"({old_config}) || (request.resource.resource == 'configmaps' && request.operation == 'UPDATE' "
        f"&& request.namespace == 'dev' && request.name == '{POOL_CONFIG}' "
        f"&& oldObject.metadata.uid == '{config_uid}' && object.metadata.uid == oldObject.metadata.uid "
        f"&& dyn(oldObject).data == {before} && dyn(object).data == {after} && dyn(object).all({invariants}"
    )
    # The existing template-metadata rule limits changes to the precise restart annotation.
    # This branch also freezes the complete Pod spec (including the init sidecar) and replica count.
    deployment["expression"] = (
        f"({old_deployment}) || (request.resource.resource == 'deployments' && request.operation == 'UPDATE' "
        f"&& request.namespace == 'dev' && request.name == '{POOL_NAME}' && oldObject.metadata.uid == '{deployment_uid}' "
        "&& object.metadata.labels == oldObject.metadata.labels "
        "&& oldObject.metadata.labels[?'liveprobe.io/managed'].orValue('') == 'true' "
        "&& oldObject.metadata.labels[?'app.kubernetes.io/part-of'].orValue('') == 'liveprobe-verification' "
        "&& dyn(object).spec.replicas == dyn(oldObject).spec.replicas "
        "&& dyn(object).spec.template.spec == dyn(oldObject).spec.template.spec)"
    )
    return [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "replace", "path": "/spec/validations", "value": rules},
    ]
