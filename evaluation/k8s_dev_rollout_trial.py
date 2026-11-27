"""Compare LiveProbe and standalone models on isolated dev rollout failures."""
from __future__ import annotations

from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
import re
from subprocess import TimeoutExpired
from time import monotonic, sleep

import yaml

from evaluation import k8s_dev_image_trial as live
from evaluation.dev_rollout_case import (
    CASE, CASES, CONFIG, LIVENESS, LIVENESS_ERROR, LIVENESS_OBSERVATION_SECONDS, SCHEDULING,
    VOLUME, VOLUME_ERROR, VOLUME_OBSERVATION_SECONDS,
)
from evaluation.k8s_dev_replica_trial import PROTOCOL, READABLE_ERRORS, confirmed_health, hash_text, infer
from evaluation.live_repair import TASK
from evaluation.single_baseline import MODELS


HEALTHY, BROKEN = CASE.healthy, CASE.broken
ROOT = live.EVIDENCE


def current(case=CASE):
    return live.get("deployment", case.workload)


def revision(resource):
    return (resource["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision")


def fixture_path(deployment, baseline, case=CASE):
    candidate = deepcopy(deployment)
    containers = candidate["spec"]["template"]["spec"]["containers"]
    if (len(containers) != 1 or containers[0]["name"] != "verification"
            or candidate["metadata"]["uid"] != baseline["uid"]):
        raise live.Refused("isolated rollout fixture identity changed")
    location, key = case.field(candidate)
    observed = location[key]
    location[key] = case.healthy
    if observed not in (case.healthy, case.broken) or live.fixture_digest(candidate) != baseline["fixture_sha256"]:
        raise live.Refused("the rollout spec differs outside its designated field")
    return observed


def patch_path(deployment, baseline, expected, replacement, case=CASE):
    if {expected, replacement} != {case.healthy, case.broken} or fixture_path(deployment, baseline, case) != expected:
        raise live.Refused("unexpected isolated fixture change")
    patch = [{"op": "test", "path": "/metadata/uid", "value": baseline["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
             {"op": "test", "path": case.pointer, "value": expected},
             {"op": "replace", "path": case.pointer, "value": replacement}]
    live.command("patch", "deployment", case.workload, "--type=json", "-p", json.dumps(patch))


def owned_by(resource, kind, uid):
    return any(ref.get("kind") == kind and ref.get("uid") == uid and ref.get("controller") is True
               for ref in resource["metadata"].get("ownerReferences") or [])


def deployment_pods(deployment, baseline, case=CASE):
    matches = [rs for rs in live.get("rs", selector=case.selector)["items"]
               if owned_by(rs, "Deployment", baseline["uid"]) and revision(rs) == revision(deployment)]
    if len(matches) != 1:
        return None, []
    rs, = matches
    return rs, [pod for pod in live.get("pods", selector=case.selector)["items"]
                if not pod["metadata"].get("deletionTimestamp") and owned_by(pod, "ReplicaSet", rs["metadata"]["uid"])]


def requests(pod, path):
    code = """
import json, sys
from urllib.error import HTTPError
from urllib.request import urlopen

rows=[]
for _ in range(2):
    try:
        with urlopen('http://127.0.0.1:8080'+sys.argv[1], timeout=3) as response:
            rows.append([response.status, response.read().decode().strip()])
    except HTTPError as error:
        rows.append([error.code, None])
print(json.dumps(rows))
"""
    return json.loads(live.command("exec", f'pod/{pod["metadata"]["name"]}', "-c", "verification",
                                   "--", "python", "-B", "-c", code, path))


def container_status(pod):
    return next((row for row in (pod.get("status", {}).get("containerStatuses") or [])
                 if row.get("name") == "verification"), {})


def verify(baseline, deadline, case=CASE):
    while monotonic() < deadline:
        deployment = current(case)
        if fixture_path(deployment, baseline, case) != case.healthy:
            raise live.Refused("the original fixture value is not present")
        if live.is_settled(deployment):
            _, pods = deployment_pods(deployment, baseline, case)
            if len(pods) == 1:
                status = container_status(pods[0])
                restarts = status.get("restartCount")
                if (status.get("ready") is True and (case != LIVENESS or restarts == 0)
                        and requests(pods[0], HEALTHY) == [[200, case.body]] * 2):
                    proof = {"generation": deployment["metadata"]["generation"], "replicas": 1, "contract_ok": True,
                             "pod_uid": pods[0]["metadata"]["uid"], "requests": [200, 200]}
                    if case == LIVENESS:
                        proof.update(restarts=restarts, min_ready_seconds=deployment["spec"]["minReadySeconds"])
                    return proof
        sleep(2)
    raise live.Refused("current-generation rollout and two original requests did not recover")


def liveness_evidence(pod, baseline):
    metadata = pod["metadata"]
    events = json.loads(live.command("get", "events", "--field-selector",
                                    f'involvedObject.uid={metadata["uid"]},reason=Unhealthy', "-o", "json"))["items"]
    matching = []
    for event in events:
        obj = event.get("involvedObject") or {}
        reporter = event.get("reportingComponent") or (event.get("source") or {}).get("component")
        first = event.get("firstTimestamp") or event.get("eventTime") or event["metadata"].get("creationTimestamp")
        if (obj.get("uid") == metadata["uid"] and obj.get("fieldPath") == "spec.containers{verification}"
                and event.get("type") == "Warning" and event.get("reason") == "Unhealthy"
                and reporter == "kubelet" and first
                and datetime.fromisoformat(first) >= datetime.fromisoformat(metadata["creationTimestamp"])
                and (event.get("message") or "").startswith("Liveness probe failed:")):
            matching.append((first, event))
    if not matching:
        return None
    first, event = min(matching, key=lambda item: item[0])
    if event["message"] != LIVENESS_ERROR:
        raise live.Refused("first current-Pod liveness warning differs from the designated failure")
    return {"event_uid": event["metadata"]["uid"], "first_warning_at": first,
            "warnings": sum(e.get("count") or (e.get("series") or {}).get("count") or 1 for _, e in matching),
            "restarts": container_status(pod).get("restartCount", 0), "liveness_path": LIVENESS.broken,
            "min_ready_seconds": baseline["min_ready_seconds"]}


def volume_evidence(pod, baseline):
    md, spec = pod["metadata"], pod["spec"]
    try:
        volume, = spec["volumes"]
    except ValueError:
        raise live.Refused("unexpected volume list on the current fixture Pod") from None
    if volume != {"name": "check", "persistentVolumeClaim": {"claimName": VOLUME.broken, "readOnly": True}}:
        raise live.Refused("current rollout Pod does not reference the intended read-only claim")
    if (pod["status"].get("phase") != "Pending" or not spec.get("nodeName")
            or container_status(pod).get("containerID")):
        return None
    events = json.loads(live.command(
        "get", "events", "--field-selector", f'involvedObject.uid={md["uid"]},reason=FailedMount', "-o", "json"))
    matching = []
    for event in events["items"]:
        ref = event.get("involvedObject") or {}
        source = event.get("reportingComponent") or (event.get("source") or {}).get("component")
        first = event.get("firstTimestamp") or event.get("eventTime") or event["metadata"].get("creationTimestamp")
        if (ref.get("uid") == md["uid"] and ref.get("name") == md["name"] and ref.get("kind") == "Pod"
                and ref.get("namespace") == "dev" and source == "kubelet" and event.get("type") == "Warning"
                and event.get("reason") == "FailedMount" and first
                and datetime.fromisoformat(first) >= datetime.fromisoformat(md["creationTimestamp"])):
            matching.append((first, event))
    if not matching:
        return None
    first, event = min(matching, key=lambda item: item[0])
    if event.get("message") != VOLUME_ERROR:
        raise live.Refused("first current-Pod mount warning differs from the designated volume failure")
    return {"event_uid": event["metadata"]["uid"], "first_warning_at": first,
            "warnings": event.get("count") or (event.get("series") or {}).get("count") or 1,
            "claim": VOLUME.broken, "claim_binding": baseline["volume_bindings"][VOLUME.broken],
            "container_started": False, "pod_scheduled": True}


def observe(baseline, initial_generation, case=CASE):
    deployment = current(case)
    status = deployment.get("status") or {}
    generation = deployment["metadata"]["generation"]
    if (fixture_path(deployment, baseline, case) != case.broken or generation <= initial_generation
            or status.get("observedGeneration") != generation or status.get("availableReplicas") != 1):
        return None
    rs, pods = deployment_pods(deployment, baseline, case)
    if rs is None or len(pods) != 1:
        return None
    rs_status = rs.get("status") or {}
    ready_new = rs_status.get("readyReplicas", 0)
    if (rs_status.get("availableReplicas", 0) if case == LIVENESS
            else container_status(pods[0]).get("ready") or ready_new):
        return None
    name = rs["metadata"]["name"]
    if not re.fullmatch(re.escape(case.workload) + r"-[a-z0-9]+", name):
        raise live.Refused("unexpected ReplicaSet identity")
    extra = {}
    if case == CONFIG:
        state = container_status(pods[0])
        waiting = (state.get("state") or {}).get("waiting") or {}
        if waiting.get("reason") != "CreateContainerConfigError" or state.get("containerID"):
            return None
        message = waiting.get("message")
        if message != f'configmap "{case.broken}" not found':
            raise live.Refused("unexpected kubelet ConfigMap error for the current Pod")
    elif case == SCHEDULING:
        pod = pods[0]
        scheduling = next((c for c in pod["status"].get("conditions", []) if c.get("type") == "PodScheduled"), {})
        if (pod["status"].get("phase") != "Pending" or pod["spec"].get("nodeName")
                or pod["spec"].get("schedulingGates")
                or (scheduling.get("status"), scheduling.get("reason")) != ("False", "Unschedulable")):
            return None
        message = scheduling.get("message") or ""
        if (pod["spec"].get("nodeSelector") != {"kubernetes.io/os": case.broken}
                or not re.match(r"^0/[1-9][0-9]* nodes are available:", message)
                or "node affinity/selector" not in message.lower()):
            raise live.Refused("current Pod does not show the designated selector mismatch")
    elif case == LIVENESS:
        container, = pods[0]["spec"]["containers"]
        if container["name"] != "verification" or container["livenessProbe"]["httpGet"]["path"] != case.broken:
            raise live.Refused("current Pod does not run the designated liveness probe")
        extra = liveness_evidence(pods[0], baseline)
        if extra is None:
            return None
        message = LIVENESS_ERROR
    elif case == VOLUME:
        extra = volume_evidence(pods[0], baseline)
        if extra is None:
            return None
        message = VOLUME_ERROR
    else:
        progress = next((row for row in (status.get("conditions") or []) if row.get("type") == "Progressing"), {})
        if (progress.get("status"), progress.get("reason")) != ("False", case.signal):
            return None
        message = progress.get("message")
        if message != f'ReplicaSet "{name}" has timed out progressing.':
            raise live.Refused("unexpected controller error message for the current ReplicaSet")
    return {"error": message, "reason": case.signal, "sha256": hash_text(message), "replicaset": name,
            "revision": revision(deployment), "generation": generation, "pod_uid": pods[0]["metadata"]["uid"],
            "available_old": 1, "ready_new": ready_new, "pod": pods[0], **extra}


def evidence(observed, case=CASE):
    proof = {key: value for key, value in observed.items() if key != "pod"}
    if case == CONFIG:
        return {**proof, "waiting_reason": "CreateContainerConfigError", "container_started": False}
    if case == SCHEDULING:
        return {**proof, "waiting_reason": "Unschedulable", "pod_scheduled": False,
                "node_selector": observed["pod"]["spec"]["nodeSelector"]}
    if case in (LIVENESS, VOLUME):
        return proof
    if requests(observed["pod"], case.broken) != [[404, None]] * 2:
        raise live.Refused("the failing current probe path did not reproduce HTTP 404 twice")
    return {**proof, "requests": [404, 404]}


def model_request(request, baseline, fault, lease, case=CASE):
    method = request["tool"]
    if method == "logs":
        return {"error": fault["error"], "reason": fault.get("waiting_reason", case.signal),
                "latest_pod_readiness_http": fault.get("requests")}
    if method in ("workload", "probe"):
        return {"deployment": case.workload, "available_old": fault["available_old"], "ready_new": fault["ready_new"],
                "reason": fault.get("waiting_reason", case.signal),
                **({key: fault[key] for key in ("warnings", "restarts")} if case == LIVENESS else {}),
                **({"claim": fault["claim"], "binding": fault["claim_binding"], "warnings": fault["warnings"]}
                   if case == VOLUME else {})}
    if method == "source":
        dep = current(case)
        previous = None
        for rs in live.get("rs", selector=case.selector)["items"]:
            if owned_by(rs, "Deployment", baseline["uid"]) and (rs.get("status") or {}).get("availableReplicas") == 1:
                location, key = case.field(rs)
                previous = location[key]
                break
        return {case.repair_key: fixture_path(dep, baseline, case), f"previous_owned_{case.repair_key}": previous,
                "configure_key": case.repair_key, **({"node_os_labels": node_os_values()} if case == SCHEDULING else {})}
    if method == "finish":
        return {"finished": True}
    if method != "configure" or (request.get("key"), request.get("value")) != (case.repair_key, case.healthy):
        return {"accepted": False, "reason": "only the previously healthy field value is in the trial's scope"}
    lease.require_active()
    patch_path(current(case), baseline, case.broken, case.healthy, case)
    return {"accepted": True, "action": case.repair_key + "_cas", "from": case.broken, "to": case.healthy}


def incident(cursor, case=CASE):
    rows = live.query(f"""SELECT coalesce(json_agg(row_to_json(q)),'[]'::json) FROM (
        SELECT i.id,i.status,i.severity_level,i.signal_reasons,
          (SELECT json_agg(json_build_object('type',a.type,'status',a.status,'executed',a.executed))
           FROM actions a WHERE a.incident_id=i.id) as actions,
          (SELECT json_agg(json_build_object('role',r.role,'model',r.model,'status',r.status,'duration_ms',r.duration_ms,
                  'input_tokens',r.input_tokens,'output_tokens',r.output_tokens,'estimated_usd',r.estimated_cost_usd))
           FROM agent_runs r WHERE r.incident_id=i.id) as calls
        FROM incidents i WHERE id>{int(cursor)} AND namespace='dev' AND workload='{case.workload}' ORDER BY i.id
    ) q""")
    if len(rows) > 1:
        raise live.Refused("multiple incidents matched the trial")
    return rows[0] if rows else None


def restore(baseline, case=CASE):
    deadline = monotonic() + 70
    operator_restored = False
    while monotonic() < deadline:
        deployment = current(case)
        if fixture_path(deployment, baseline, case) == case.healthy:
            return verify(baseline, deadline, case), operator_restored
        operator_restored = True
        try:
            patch_path(deployment, baseline, case.broken, case.healthy, case)
        except (OSError, TimeoutExpired, live.Refused):
            sleep(2)
    raise live.Refused("the designated fixture field was not restored")


def node_os_values():
    return sorted({node["metadata"].get("labels", {}).get("kubernetes.io/os", "")
                   for node in live.get("nodes")["items"]})


def watcher_epochs(status):
    watchers = status.get("watchers") or {}
    live_watchers = {name: watchers.get(name) or watchers.get(f"{name}/dev") or {}
                     for name in ("pod_watch", "event_watch")}
    if any(watch.get("state") != "running" or not watch.get("since") for watch in live_watchers.values()):
        raise live.Refused("LiveProbe Pod and Event watchers must both be running")
    return {name: watch["since"] for name, watch in live_watchers.items()}


def run(arm, baseline, reference, case_hash, output, journal, case=CASE):
    status = live.product_status("/api/status")
    if (not status["ready"] or status["target_namespaces"] != ["dev"]
            or status.get("watchers", {}).get("workload_poller/dev", {}).get("state") != "running"):
        raise live.Refused("dev-only LiveProbe is not available")
    api = live.get("deployment", "liveprobe-api", namespace="liveprobe-system")
    volumes = api["spec"]["template"]["spec"]["volumes"]
    config, = [v["configMap"]["name"] for v in volumes if v["name"] == "config"]
    value = live.get("configmap", config, namespace="liveprobe-system")["data"]["config.yaml"]
    settings = yaml.safe_load(value)
    observation_seconds = LIVENESS_OBSERVATION_SECONDS
    if case == VOLUME:
        observation_seconds = VOLUME_OBSERVATION_SECONDS
        if settings.get("workload_state_signals_enabled") and case.workload in (settings.get("workload_state_signal_workloads") or []):
            # Include time for the next kubelet event and workload poll after the two-minute criterion.
            observation_seconds = 200
    if (case not in (LIVENESS, VOLUME) and (not settings.get("workload_state_signals_enabled")
                              or case.workload not in (settings.get("workload_state_signal_workloads") or []))):
        raise live.Refused("the exact fixture observer scope is not active")
    can_record_miss = case in (LIVENESS, VOLUME)
    epochs = watcher_epochs(status) if can_record_miss else None
    if case == CONFIG:
        original = live.get("configmap", case.healthy)
        if (original["metadata"]["uid"] != baseline["configmap_uid"] or original.get("immutable") is not True
                or original.get("data") != {"message": case.body}
                or live.command("get", "configmap", case.broken, "--ignore-not-found", "-o", "name").strip()):
            raise live.Refused("original ConfigMap or reserved missing reference changed")
    if case == VOLUME:
        from evaluation.k8s_dev_volume_fixture import volume_bindings

        if volume_bindings() != baseline["volume_bindings"]:
            raise live.Refused("persistent volume binding no longer matches the baseline")
    before = verify(baseline, monotonic() + 10, case)
    gate = live.CHAOS.node_gate()
    if live.query(f"SELECT count(*) FROM incidents WHERE namespace='dev' AND workload='{case.workload}' "
                  "AND status IN ('open','analyzing','acting','action_pending')"):
        raise live.Refused("a previous incident is still open for this fixture")
    cursor = live.query("SELECT coalesce(max(id),0) FROM incidents")
    result = {"case": case.case_id, "target": case.target, "arm": arm, "protocol": PROTOCOL, "case_sha256": case_hash,
              "fixture_sha256": baseline["fixture_sha256"], "healthy_before": before, "node_gate": gate,
              "status": "trial_unverified"}
    if case == SCHEDULING:
        values = node_os_values()
        if case.healthy not in values or case.broken in values:
            raise live.Refused("real node OS labels no longer support the scheduling fixture")
        result["node_os_labels"] = values
    if epochs:
        result["watchers_at_start"] = epochs
    if case == VOLUME:
        result["volume_bindings"] = baseline["volume_bindings"]
    live.write_once(journal, {"target": case.target, "uid": baseline["uid"], "field": case.pointer,
                             "restore_only_if_value": case.broken, "original_value": case.healthy,
                             "receipt": str(output), "started_at": datetime.now(timezone.utc).isoformat()})
    began, lease, attempted = monotonic(), None, False
    seen_at = None
    repeated = not can_record_miss
    try:
        if arm != "liveprobe":
            lease = live.WorkloadLease.claim(case, arm)
            lease.require_active()
        attempted = True
        patch_path(current(case), baseline, case.healthy, case.broken, case)
        deadline = began + case.budget_seconds
        while monotonic() < deadline:
            observed = observe(baseline, before["generation"], case)
            if observed and seen_at is None:
                seen_at = monotonic()
                result["first_error_ms"] = round((seen_at - began) * 1000)
                result["fault_before"] = evidence(observed, case)
                result["input_sha256"] = hash_text(TASK + observed["error"])
            if case == LIVENESS and observed and observed["warnings"] >= 3 and observed["restarts"] >= 1:
                initial = result["fault_before"]
                if any(initial[key] != observed[key] for key in ("pod_uid", "event_uid", "sha256")):
                    raise live.Refused("liveness restart belongs to a different warning or Pod")
                repeated = True
                result.setdefault("fault_repeated", evidence(observed, case))
            if case == VOLUME and observed and observed["warnings"] >= 2:
                initial = result["fault_before"]
                if any(initial[key] != observed[key] for key in ("pod_uid", "event_uid", "sha256", "claim_binding")):
                    raise live.Refused("the repeated mount warning belongs to a different Pod, event or claim")
                repeated = True
                result.setdefault("fault_repeated", evidence(observed, case))
            if lease and repeated and seen_at is not None:
                if result["input_sha256"] != reference["input_sha256"]:
                    raise live.Refused("first observed error differs from LiveProbe")
                infer(result, arm, baseline, result.get("fault_repeated", result["fault_before"]), lease, deadline,
                      apply_request=partial(model_request, case=case), verify=partial(verify, case=case))
            ended = bool(lease and repeated and seen_at is not None)
            if not lease and seen_at is not None:
                result["incident"] = incident(cursor, case)
                ended = bool(repeated and result["incident"] and result["incident"]["status"] not in live.NONTERMINAL)
                if (can_record_miss and repeated and observed and not result["incident"]
                        and monotonic() - seen_at >= observation_seconds):
                    final_epochs = watcher_epochs(live.product_status("/api/status"))
                    if final_epochs != epochs:
                        raise live.Refused("watchers reconnected during the fault observation")
                    result.update(status="product_observation_only", observation_seconds=int(monotonic() - seen_at),
                                  watchers_at_end=final_epochs, still_failed_before_cleanup=evidence(observed, case))
                    break
            if ended:
                if confirmed_health(result.get("verified_before_cleanup")):
                    result["status"] = "verified_dev_recovered"
                elif not result.get("model_failed") and (lease or result["incident"]["status"] == "handed_off"):
                    still = observe(baseline, before["generation"], case)
                    if still and still["pod_uid"] == result["fault_before"]["pod_uid"]:
                        result["still_failed_before_cleanup"] = evidence(still, case)
                        result["status"] = "observed_terminal_unrecovered"
                break
            sleep(4)
    except READABLE_ERRORS as error:
        result["trial_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
    finally:
        result["duration_ms"] = round((monotonic() - seen_at) * 1000) if seen_at is not None else None
        try:
            if attempted:
                result["restored"], result["operator_restored"] = restore(baseline, case)
                if case == VOLUME:
                    result["restored_volume_bindings"] = volume_bindings()
                    if result["restored_volume_bindings"] != baseline["volume_bindings"]:
                        raise live.Refused("volume bindings changed during the trial")
            if result["status"] == "product_observation_only":
                result["incident"] = incident(cursor, case)
                if result["incident"]:
                    result["status"] = "trial_unverified"
                else:
                    result["matched_incident_count_after"] = 0
            if lease:
                lease.require_active()
                result["liveprobe_incident"] = incident(cursor, case)
                lease.release()
                result["lease_released"] = True
        except READABLE_ERRORS as error:
            result["cleanup_error"] = str(error) if isinstance(error, live.Refused) else type(error).__name__
        live.write_once(output, result)
        if (not result.get("cleanup_error") and (not attempted or "restored" in result)
                and (not lease or result.get("lease_released"))):
            journal.unlink()
    print(json.dumps({key: result.get(key) for key in ("arm", "status", "trial_error", "cleanup_error")}
                     | {"restored": bool(result.get("restored")), "incident": (result.get("incident") or {}).get("id")}))
    return 0 if (result["status"] in ("verified_dev_recovered", "observed_terminal_unrecovered", "product_observation_only")
                 and confirmed_health(result.get("restored")) and not result.get("trial_error")
                 and not result.get("cleanup_error") and not result.get("liveprobe_incident")) else 2


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(CASES), default=CASE.case_id)
    parser.add_argument("--arm", choices=("liveprobe", *MODELS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    fixture = CASES[args.case]
    baseline = json.loads((ROOT / f"dev-{fixture.case_id.lower().replace('-', '')}-baseline.json").read_text())
    catalog = json.loads((live.ROOT / "evaluation/catalogs/comparative-2026-11-23.json").read_text())
    case, = [row for row in catalog["kubernetes_new"]["cases"] if row["id"] == fixture.case_id]
    if (case["environment"], baseline.get("ready"), baseline.get("namespace"), baseline.get("isolated_deployment")) != (
            "isolated-workload", True, "dev", fixture.workload):
        raise live.Refused("isolated fixture does not match the catalog")
    case_hash = hash_text(json.dumps(case, sort_keys=True, ensure_ascii=False))
    reference = json.loads(args.reference.read_text()) if args.reference else None
    if args.arm != "liveprobe" and (not reference or reference.get("arm") != "liveprobe"
            or reference.get("case_sha256") != case_hash or reference.get("fixture_sha256") != baseline["fixture_sha256"]
            or not confirmed_health(reference.get("restored")) or not reference.get("input_sha256")):
        raise live.Refused("a comparable live product receipt is required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal = ROOT / "dev-k8s-fault-active.json"
    with (ROOT / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists():
            raise live.Refused("prior fault or receipt requires review")
        return run(args.arm, baseline, reference, case_hash, args.output, journal, fixture)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (*READABLE_ERRORS, live.CHAOS.Stop) as error:
        print(json.dumps({"eligible": False, "reason": str(error) if isinstance(error, (live.Refused, live.CHAOS.Stop))
                                                      else type(error).__name__}))
        raise SystemExit(2) from None
