"""The dev KW-03 Deployment-only repair boundary."""

from datetime import datetime, timezone

from .workload_signals import WorkloadSignals, _controller, _pending_claim_mounts, _persistent_mount_events


NAMESPACE = "dev"
DEPLOYMENT = "lp-eval-kw03"
SYSTEM = "liveprobe-verification"
VOLUME = "check"
READY = "lp-eval-kw03-ready"
BROKEN = "lp-eval-kw03-unprovisioned"
BROKEN_PV = "lp-eval-kw03-unprovisioned-pv"
POINTER = "/spec/template/spec/volumes/0/persistentVolumeClaim/claimName"


def reference(deployment: dict) -> str | None:
    volumes = (deployment.get("spec") or {}).get("template", {}).get("spec", {}).get("volumes") or []
    if len(volumes) == 1 and set(volumes[0]) == {"name", "persistentVolumeClaim"} and volumes[0]["name"] == VOLUME:
        claim = volumes[0]["persistentVolumeClaim"] or {}
        if set(claim) == {"claimName", "readOnly"} and claim["readOnly"] is True:
            return claim["claimName"]
    return None


def bindings(kube, ready_pv_name: str) -> dict | None:
    proof = {}
    for claim, volume in ((READY, ready_pv_name), (BROKEN, BROKEN_PV)):
        pvc = kube.get_pvc(NAMESPACE, claim) or {}
        pv = kube.get_pv(volume) or {}
        cm, vm = pvc.get("metadata") or {}, pv.get("metadata") or {}
        labels = cm.get("labels") or {}
        ref = (pv.get("spec") or {}).get("claimRef") or {}
        if (labels.get("app"), labels.get("app.kubernetes.io/part-of")) != (DEPLOYMENT, SYSTEM):
            return None
        if (cm.get("namespace"), cm.get("name"), vm.get("name"), cm.get("uid")) != (
                NAMESPACE, claim, volume, ref.get("uid")) or not cm.get("uid") or not vm.get("uid"):
            return None
        if ((ref.get("namespace"), ref.get("name")) != (NAMESPACE, claim)
                or cm.get("deletionTimestamp") or vm.get("deletionTimestamp")
                or (pvc.get("spec") or {}).get("volumeName") != volume
                or (pvc.get("status") or {}).get("phase") != "Bound" or (pv.get("status") or {}).get("phase") != "Bound"):
            return None
        proof[claim] = {"claim_uid": cm["uid"], "pv_uid": vm["uid"], "pv_name": volume}
    return proof if proof[READY]["pv_uid"] != proof[BROKEN]["pv_uid"] else None


def failing_pod(kube, deployment: dict) -> str | None:
    finding = mount_failure(deployment, kube.list_replicasets(NAMESPACE),
                            kube.list_pods(NAMESPACE, {"app": DEPLOYMENT}), kube.list_events(NAMESPACE),
                            datetime.now(timezone.utc))
    return finding["pod_uid"] if finding else None


def mount_failure(deployment: dict, replicasets: list[dict], pods: list[dict], events: list[dict], observed_at) -> dict | None:
    """Link the retained current kubelet warning and the ready previous revision to specific claims."""
    md, status = deployment["metadata"], deployment.get("status") or {}
    if ((md.get("namespace"), md.get("name")) != (NAMESPACE, DEPLOYMENT) or md.get("deletionTimestamp")
            or reference(deployment) != BROKEN or (deployment["spec"].get("replicas"), status.get("availableReplicas")) != (1, 1)):
        return None
    owned = [rs for rs in replicasets if (ref := _controller(rs, "Deployment"))
             and (ref["name"], ref["uid"]) == (md["name"], md["uid"]) and not rs["metadata"].get("deletionTimestamp")]
    current = [rs for rs in owned if WorkloadSignals._matches_rollout(deployment, rs) and reference(rs) == BROKEN]
    previous = [rs for rs in owned if reference(rs) == READY and (rs.get("status") or {}).get("availableReplicas") == 1]
    if len(current) != 1 or len(previous) != 1:
        return None
    current_rs = current[0]["metadata"]
    candidates = {}
    for pod in pods:
        pm, ref = pod["metadata"], _controller(pod, "ReplicaSet")
        if (ref and (ref["name"], ref["uid"]) == (current_rs["name"], current_rs["uid"])
                and pm.get("namespace") == NAMESPACE and not pm.get("deletionTimestamp")
                and _pending_claim_mounts(pod) == {VOLUME: BROKEN}):
            candidates[pm["uid"]] = (pod, DEPLOYMENT, {VOLUME: BROKEN})
    if len(candidates) != 1:
        return None
    local_path_failures = [event for event in events
                           if (event.get("message") or "").startswith("MountVolume.NewMounter initialization failed")
                           and (event.get("message") or "").endswith(" does not exist")]
    for pod, _, _, reported, count, duration, *_ in _persistent_mount_events(local_path_failures, candidates, observed_at):
        if reported == BROKEN_PV:
            older = previous[0]["metadata"]
            return {"pod_uid": pod["metadata"]["uid"], "read_only": True, "warning_count": count,
                    "failure_observed_seconds": duration,
                    "failing_revision": {"replicaset": current_rs["name"], "claim": BROKEN, "persistent_volume": reported},
                    "previous_ready_revision": {"replicaset": older["name"], "claim": READY, "available_replicas": 1}}
    return None
