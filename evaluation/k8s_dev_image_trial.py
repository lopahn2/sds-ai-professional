"""One bounded dev image-pull trial through the deployed LiveProbe watcher."""
from __future__ import annotations

from argparse import ArgumentParser
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from subprocess import PIPE, TimeoutExpired, run
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from app.rollback_controller import plan_undo, trusted_images
from app.runtime_fencing import application_owned
from evaluation import isolated_oom_target
from evaluation.dev_image_case import DevImageCase, IMAGE_WORKLOADS, load_image_case, successful_probes
from evaluation.harness.real import _load_chaos
from evaluation.live_repair import RemoteModelSession, TASK
from evaluation.single_baseline import MODELS, estimate


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".hermes/evidence/liveprobe-comparison-2026-11-23"
CONTEXT, NS = "rancher-desktop", "dev"
PROTOCOL = "dev-image-trial/v1"
STANDALONE_PROTOCOL = "live-repair/v36"
CONTROLLER_ID = "system:serviceaccount:liveprobe-system:rollback-controller"
PULL_ERRORS = {"ErrImagePull", "ImagePullBackOff"}
NONTERMINAL = {"open", "analyzing", "action_pending", "acting"}
IMAGE_PTR = "/spec/template/spec/containers/0/image"
CHAOS = _load_chaos()
_shared_gate_kubectl = CHAOS.kubectl


class Refused(RuntimeError):
    pass


def kube_prefix():
    target = isolated_oom_target.active()
    return target.prefix() if target else ["kubectl", "--context", CONTEXT]


def _gate_kubectl(*args, **kwargs):
    target = isolated_oom_target.active()
    if not target:
        return _shared_gate_kubectl(*args, **kwargs)
    try:
        return target.command(*args, **kwargs)
    except isolated_oom_target.TargetError as error:
        raise CHAOS.Stop(str(error)) from None


CHAOS.kubectl = _gate_kubectl


def command(*args, namespace=NS, timeout=14, stdin=None):
    result = run([*kube_prefix(), f"--request-timeout={timeout - 2}s",
                  "-n", namespace, *args], input=stdin, stdout=PIPE, stderr=PIPE, text=True, timeout=timeout)
    if result.returncode:
        raise Refused(f"Kubernetes request failed: {args[0]}")
    return result.stdout


def lease_command(case, operation, owner, *, token=None, ttl=None):
    args = ["exec"]
    if token is not None:
        args.append("-i")
    target = isolated_oom_target.active()
    receiver = f"pod/{target.proof['receiver']['pods']['liveprobe-api']['name']}" if target else "deployment/liveprobe-api"
    args.extend((receiver, "-c", "api", "--", "python", "-m", "app.evalrun", operation,
                 "--namespace", NS, "--workload", case.workload, "--owner", owner))
    if token is None:
        args.extend(("--ttl", str(600 if ttl is None else ttl)))
    else:
        args.append("--token-stdin")
        if ttl is not None:
            args.extend(("--ttl", str(ttl)))
    return json.loads(command(*args, namespace="liveprobe-system", stdin=None if token is None else token + "\n"))


@dataclass(frozen=True)
class WorkloadLease:
    case: DevImageCase
    owner: str
    token: str = field(repr=False)

    @classmethod
    def claim(cls, case, arm, *, ttl=None):
        owner = f"{case.case_id.lower()}-{arm.replace('_', '-')}-{uuid4().hex[:12]}"
        value = lease_command(case, "lock", owner) if ttl is None else lease_command(case, "lock", owner, ttl=ttl)
        token = value.get("token")
        if ((value.get("namespace"), value.get("workload"), value.get("owner")) != (NS, case.workload, owner)
                or not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{24,256}", token)):
            raise Refused("scoped evaluation lease returned an invalid ownership receipt")
        return cls(case, owner, token)

    def require_active(self):
        if lease_command(self.case, "lock-status", self.owner, token=self.token).get("active") is not True:
            raise Refused("the scoped evaluation lease is no longer owned")

    def renew(self, ttl):
        receipt = lease_command(self.case, "lock-renew", self.owner, token=self.token, ttl=ttl)
        try:
            expiration = datetime.fromisoformat(receipt["expires_at"])
        except (KeyError, ValueError, TypeError):
            raise Refused("could not renew the scoped evaluation lease") from None
        if not expiration.tzinfo or expiration <= datetime.now(timezone.utc):
            raise Refused("the renewed lease is not active")

    def release(self):
        if lease_command(self.case, "unlock", self.owner, token=self.token).get("released") is not True:
            raise Refused("could not release the exact scoped evaluation lease")


def get(kind, name=None, *, namespace=NS, selector=None):
    args = ["get", kind]
    if name is not None:
        args.append(name)
    if selector is not None:
        args.extend(("-l", selector))
    return json.loads(command(*args, "-o", "json", namespace=namespace))


def query(body):
    statement = f"BEGIN READ ONLY; {body}; ROLLBACK;"
    target = isolated_oom_target.active()
    database = f"pod/{target.proof['receiver']['pods']['postgres']['name']}" if target else "postgres-0"
    result = command("exec", database, "--", "psql", "-U", "liveprobe", "-d", "liveprobe",
                     "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", statement, namespace="liveprobe-system")
    return json.loads(result)


def image(case, deployment):
    containers = deployment["spec"]["template"]["spec"]["containers"]
    if len(containers) != 1 or containers[0]["name"] != case.workload:
        raise Refused("unexpected primary container layout")
    return containers[0]["image"]


def same_workload(case, current, baseline):
    spec = deepcopy(current["spec"])
    actual = image(case, current)
    spec["template"]["spec"]["containers"][0]["image"] = image(case, baseline)
    return (current["metadata"]["uid"] == baseline["metadata"]["uid"]
            and current["metadata"].get("labels") == baseline["metadata"].get("labels")
            and spec == baseline["spec"]), actual


def patch_image(case, current, replacement):
    metadata = current["metadata"]
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": case.workload},
        {"op": "test", "path": IMAGE_PTR, "value": image(case, current)},
        {"op": "replace", "path": IMAGE_PTR, "value": replacement},
    ]
    command("patch", "deployment", case.workload, "--type=json", "-p", json.dumps(operations))


def ready_endpoints(case):
    slices = get("endpointslice", selector=f"kubernetes.io/service-name={case.workload}")
    endpoints = [item for group in slices["items"] for item in group["endpoints"]
                 if item["conditions"].get("ready") is True and item["conditions"].get("terminating") is not True]
    if len(endpoints) != 1 or endpoints[0].get("targetRef", {}).get("kind") != "Pod":
        raise Refused("the Service no longer has one ready Pod")
    uid = endpoints[0]["targetRef"].get("uid")
    if not uid:
        raise Refused("endpoint Pod identity is missing")
    return uid


def probe(case):
    proxy = f"/api/v1/namespaces/{NS}/services/{case.workload}:8080/proxy"
    health = json.loads(command("get", "--raw", proxy + "/actuator/health/readiness"))
    order = getattr(case, "application", case.workload) == "order"
    path = "/api/orders/1" if order else "/api/stock/a"
    item = json.loads(command("get", "--raw", proxy + path))
    valid_quantity = type(item.get("quantity")) is int and item["quantity"] >= 0
    if order:
        valid_item = (type(item.get("id")) is int and item["id"] == 1
                      and isinstance(item.get("item"), str) and bool(item["item"])
                      and type(item.get("amount")) is int and item["amount"] >= 0)
    else:
        valid_item = item.get("item") == "a" and type(item.get("available")) is bool
    if health.get("status") != "UP" or not valid_quantity or not valid_item:
        raise Refused("independent Service business/readiness probe failed")
    result = {"ready": 200, "business": 200, "contract_ok": True, "endpoint_uid": ready_endpoints(case)}
    if case.case_id != "F1-01":
        caller = getattr(case, "dependency_caller", case.workload if order else "order")
        path = f"/api/v1/namespaces/dev/services/{caller}:8080/proxy/api/orders/check?item=a"
        dependency = json.loads(command("get", "--raw", path))
        inventory = dependency.get("inventory") or {}
        if (dependency.get("item") != "a" or type(dependency.get("orderable")) is not bool
                or inventory.get("item") != "a" or type(inventory.get("quantity")) is not int
                or inventory["quantity"] < 0 or type(inventory.get("available")) is not bool):
            raise Refused("independent order-to-inventory check failed")
        result["order_dependency"] = 200
    return result


def product_status(path):
    target = isolated_oom_target.active()
    if target:
        return target.product_status(path)
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:4175{path}", timeout=8) as response:
        return json.load(response)


def is_settled(deployment):
    state = deployment["status"]
    return (state.get("observedGeneration") == deployment["metadata"]["generation"]
            and all(state.get(key) == 1 for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")))


def preflight(case):
    deployment = get("deployment", case.workload)
    spec = deployment["spec"]
    strategy = spec["strategy"]
    labels = deployment["metadata"]["labels"]
    if not (spec.get("replicas") == 1 and not spec.get("paused") and spec.get("revisionHistoryLimit", 0) > 0
            and strategy == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
            and labels.get("liveprobe.io/managed") == "true" and labels.get("app.kubernetes.io/part-of") == "samsung-trade"
            and re.fullmatch(rf"liveprobe/{case.workload}@sha256:[0-9a-f]{{64}}", image(case, deployment))
            and is_settled(deployment) and not application_owned(deployment)):
        raise Refused("baseline Deployment is not a single managed, digest-pinned, no-unavailability rollout")
    status = product_status("/api/status")
    if (status.get("ready") is not True or NS not in status.get("target_namespaces", [])
            or "rollout_undo" not in status.get("allowlist", [])
            or not any(key in ("pod_watch", "pod_watch/dev") and row.get("state") == "running"
                       for key, row in status.get("watchers", {}).items())
            or product_status("/api/settings").get("automation_mode") != "auto"):
        raise Refused("automatic dev image detection and rollback are not ready")

    controller = get("deployment", "rollback-controller", namespace="liveprobe-system")
    if controller["status"].get("availableReplicas") != 1:
        raise Refused("rollback-controller is not ready")
    controller_spec = controller["spec"]["template"]["spec"]
    if controller_spec.get("serviceAccountName") != "rollback-controller" or len(controller_spec["containers"]) != 1:
        raise Refused("unexpected rollback controller identity")
    env = {}
    for entry in controller_spec["containers"][0].get("env", []):
        if entry["name"] not in ("ROLLBACK_NAMESPACES", "ROLLBACK_APPROVED_IMAGES"):
            continue
        if "value" in entry:
            env[entry["name"]] = entry["value"]
        else:
            ref = entry.get("valueFrom", {}).get("configMapKeyRef")
            if not ref:
                raise Refused("rollback policy must be readable without credential access")
            env[entry["name"]] = get("configmap", ref["name"], namespace="liveprobe-system")["data"][ref["key"]]
    approved = trusted_images(env).get(f"{NS}/{case.workload}", [])
    previous_image = image(case, deployment)
    if previous_image not in approved or command("auth", "can-i", "patch", f"deployments.apps/{case.workload}",
                                                  "--as", CONTROLLER_ID).strip() != "yes":
        raise Refused("original image or exact controller write is not allowlisted")
    current_rev = int(deployment["metadata"]["annotations"]["deployment.kubernetes.io/revision"])
    proposed = deepcopy(deployment)
    proposed["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = str(current_rev + 1)
    proposed["spec"]["template"]["spec"]["containers"][0]["image"] = case.bad_image
    _, previous_revision, _ = plan_undo(proposed, get("replicasets")["items"], case.workload, approved)
    if previous_revision != current_rev:
        raise Refused("rollback would not select the exact current image and template")

    service = get("service", case.workload)
    selector = service["spec"].get("selector") or {}
    pod_labels = spec["template"]["metadata"]["labels"]
    if not selector or any(pod_labels.get(key) != value for key, value in selector.items()):
        raise Refused("Service selector does not match the original Deployment")
    baseline = [probe(case), probe(case)]
    if baseline[0]["endpoint_uid"] != baseline[1]["endpoint_uid"]:
        raise Refused("original Service endpoint is changing")
    cursor = query(
        "SELECT json_build_object('cursor',coalesce(max(id),0),'open',"
        " count(*) FILTER (WHERE status IN ('open','analyzing','action_pending','acting')), 'active_actions',"
        f" (SELECT count(*) FROM actions WHERE target='{case.target}'"
        " AND status IN ('approved','executing','indeterminate','pending_approval')))"
        f" FROM incidents WHERE namespace='dev' AND workload='{case.workload}'"
    )
    if cursor["open"] or cursor["active_actions"]:
        raise Refused("an existing incident or action affects the selected workload")
    gate = CHAOS.node_gate()
    return deployment, baseline, cursor["cursor"], gate


def fault_observation(case, expected_uid, old_endpoint):
    bad = case.bad_image
    for pod in get("pod", selector=f"app={case.workload}")["items"]:
        if pod["metadata"]["uid"] == old_endpoint or pod["metadata"].get("deletionTimestamp"):
            continue
        if not any(c.get("name") == case.workload and c.get("image") == bad for c in pod["spec"]["containers"]):
            continue
        for state in pod.get("status", {}).get("containerStatuses", []):
            waiting = state.get("state", {}).get("waiting", {})
            if state.get("name") != case.workload or waiting.get("reason") not in PULL_ERRORS:
                continue
            owners = pod["metadata"].get("ownerReferences", [])
            owner, = [row for row in owners if row.get("controller") and row.get("kind") == "ReplicaSet"]
            rs = get("replicaset", owner["name"])
            if (rs["metadata"]["uid"] != owner["uid"] or not any(
                    row.get("controller") and row.get("uid") == expected_uid
                    for row in rs["metadata"].get("ownerReferences", []))):
                continue
            name = pod["metadata"]["name"]
            message = waiting.get("message", "")
            marker = re.search(r'failed to pull(?: and unpack)? image ["\'](?:docker\.io/)?'
                               + re.escape(bad) + r'["\']', message, re.IGNORECASE)
            first_log = marker.group(0) if marker else f'{waiting["reason"]}: {bad}'
            return {"pod": name, "reason": waiting["reason"], "initial_error_log": first_log,
                    "initial_error_source": "safe waiting-message excerpt" if marker else "waiting reason and declared image"}
    return None


def observation(case, cursor, pod):
    if not re.fullmatch(rf"{case.workload}-[a-z0-9-]{{1,60}}", pod):
        raise Refused("unexpected evidence Pod name")
    return query(f"""
        SELECT coalesce(json_agg(row_to_json(result)), '[]'::json) FROM (
          SELECT i.id,i.status,i.severity_level,i.signal_reasons,
            (SELECT json_agg(json_build_object('id',a.id,'type',a.type,'target',a.target,'executed',a.executed,
                      'status',a.status,'from_images',a.before_snapshot->'images','to_images',a.params->'to_images'))
               FROM actions a WHERE a.incident_id=i.id) AS actions,
            (SELECT json_agg(json_build_object('id',r.id,'role',r.role,'model',r.model,'status',r.status,'attempts',r.attempts,
                      'input_tokens',r.input_tokens,'output_tokens',r.output_tokens,
                      'duration_ms',r.duration_ms,'estimated_usd',r.estimated_cost_usd))
               FROM agent_runs r WHERE r.incident_id=i.id) AS calls
          FROM incidents i
          WHERE i.id>{int(cursor)} AND i.namespace='dev' AND i.workload='{case.workload}'
            AND EXISTS (SELECT 1 FROM audit_log log WHERE log.incident_id=i.id AND log.event='signal_observed'
                 AND ('{pod}'=log.detail#>>'{{signal,detail,pod}}'
                      OR '{pod}'=log.detail#>>'{{signal,detail,labels,pod}}'))
          LIMIT 2
        ) result
    """)


def attributed_recovery(case, record, original):
    executed = [action for action in (record.get("actions") or []) if action.get("executed") is True]
    return (len(executed) == 1 and executed[0].get("type") == "rollout_undo"
            and executed[0].get("target") == case.target and executed[0].get("status") == "succeeded"
            and executed[0].get("from_images") == [case.bad_image] and executed[0].get("to_images") == [original])


def restore(case, baseline):
    bad = case.bad_image
    deadline = monotonic() + 90
    while monotonic() < deadline:
        current = get("deployment", case.workload)
        unchanged, actual = same_workload(case, current, baseline)
        if not unchanged or actual not in (image(case, baseline), bad):
            raise Refused("cleanup sees a concurrent change and will not overwrite it")
        if actual == bad:
            try:
                patch_image(case, current, image(case, baseline))
            except (Refused, TimeoutExpired):
                # Ambiguous writes are safe to re-read. Another attempt always uses current CAS.
                pass
        elif is_settled(current):
            return [probe(case), probe(case)]
        sleep(3)
    raise Refused("original rollout did not stabilize during cleanup")


def write_once(path, payload):
    with path.open("x", encoding="utf-8") as out:
        json.dump(payload, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def fixture_digest(deployment):
    return hashlib.sha256(json.dumps(deployment["spec"], sort_keys=True).encode()).hexdigest()


def load_reference(case, path, baseline):
    bad = case.bad_image
    reference = json.loads(path.read_text(encoding="utf-8"))
    if (reference.get("namespace"), reference.get("target"), reference.get("case"), reference.get("arm")) != (
            NS, case.workload, case.case_id, "liveprobe") or reference.get("protocol") != PROTOCOL:
        raise Refused("reference is not a dev LiveProbe observation for this case")
    fault = reference.get("fault_before") or {}
    saved_digest = reference.get("fixture_sha256")
    scope_ok = (fault.get("reason") in PULL_ERRORS and isinstance(fault.get("initial_error_log"), str)
                and bad in fault["initial_error_log"] and not reference.get("trial_error")
                and (saved_digest == fixture_digest(baseline)
                     or saved_digest is None and case.case_id == "F1-01")
                and all(successful_probes(reference.get(key)) for key in ("baseline", "restored")))
    recovered = (reference.get("status") == "verified_dev_recovered"
                 and attributed_recovery(case, reference.get("incident") or {}, image(case, baseline))
                 and successful_probes(reference.get("pre_cleanup_verified")))
    observed_failure = (reference.get("status") == "product_observation_only"
                        and reference.get("baseline_image") == image(case, baseline) and reference.get("injected_image") == bad
                        and (reference.get("fault_service_probe") or {}).get("contract_ok") is True)
    if not (scope_ok and (recovered or observed_failure)):
        raise Refused("reference does not prove the same observed fault, starting image and final baseline")
    return reference


def comparable_first_error(observed, reference, bad):
    reference_fault = reference["fault_before"]
    reason = observed["reason"]
    if (reason == reference_fault["reason"] and reason in PULL_ERRORS
            and all(bad in fault["initial_error_log"] for fault in (observed, reference_fault))):
        # Use the actual waiting reason and declared image. Keep the safe excerpt in the raw receipt.
        return f"{reason}: {bad}"
    raise Refused("first observed error differs from the LiveProbe reference")


def rollout_source(case, current):
    metadata = current["metadata"]
    versions = []
    for rs in get("replicaset")["items"]:
        if not any(row.get("uid") == metadata["uid"] and row.get("controller") is True
                   and row.get("kind") == "Deployment" for row in rs["metadata"].get("ownerReferences", [])):
            continue
        containers = rs["spec"]["template"]["spec"]["containers"]
        if len(containers) != 1 or containers[0].get("name") != case.workload:
            continue
        revision = rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
        if isinstance(revision, str) and revision.isdigit():
            versions.append({"revision": int(revision), "image": containers[0]["image"]})
    return {"strategy": current["spec"]["strategy"], "replicas": current["spec"]["replicas"],
            "revisions": sorted(versions, key=lambda item: item["revision"], reverse=True)}


def standalone_operation(case, request, baseline, fault, lease):
    bad = case.bad_image
    tool = request.get("tool")
    if tool == "logs":
        return {"error": fault["initial_error_log"], "reason": fault["reason"]}
    if tool == "probe":
        result = probe(case)
        return {key: result[key] for key in ("ready", "business", "contract_ok")}
    if tool == "finish":
        return {"finished": True}
    current = get("deployment", case.workload)
    unchanged, actual = same_workload(case, current, baseline)
    if not unchanged or actual != bad or application_owned(current):
        raise Refused("independent model operation lost the original fault scope")
    if tool == "workload":
        return {"namespace": NS, "deployment": case.workload, "container": case.workload, "current_image": actual,
                "ready_replicas": current["status"].get("readyReplicas", 0),
                "failed_pod_reason": fault["reason"]}
    if tool == "source":
        return rollout_source(case, current)
    if (tool != "configure" or request.get("key") not in (
            "image", "spec.template.spec.containers[0].image", IMAGE_PTR)
            or request.get("value") != image(case, baseline)):
        return {"accepted": False, "reason": "outside the allowed image-only scope or no trusted version observed"}
    lease.require_active()
    patch_image(case, current, image(case, baseline))
    return {"accepted": True, "action": "image_cas", "from_images": [bad], "to_images": [image(case, baseline)]}


def verified_repair(case, baseline, lease, deadline):
    while monotonic() < deadline:
        current = get("deployment", case.workload)
        unchanged, actual = same_workload(case, current, baseline)
        if not unchanged or actual != image(case, baseline):
            raise Refused("model's change was replaced or exceeded the allowed patch")
        if is_settled(current):
            lease.require_active()
            return [probe(case), probe(case)]
        sleep(3)
    raise Refused("model repair did not settle before the trial deadline")


def standalone_inference(case, result, baseline, fault, arm, lease, deadline):
    model, effort, _ = MODELS[arm]
    result.update(model=model, effort=effort, calls=[], investigation=[])
    session = RemoteModelSession(model, effort, STANDALONE_PROTOCOL)
    try:
        for _ in range(8):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            lease.require_active()
            session.timeout = min(50, remaining)
            response = session.turn(model, effort, fault["initial_error_log"], result["investigation"])
            result["calls"].append({key: value for key, value in response.items() if key != "data"})
            if (response.get("model"), response.get("effort")) != (model, effort):
                raise Refused("standalone model identity differed from the comparison catalog")
            if response["status"] != "ok":
                result["status"] = "model_failed"
                break
            request = response["data"]
            answer = standalone_operation(case, request, baseline, fault, lease)
            result["investigation"].append({"request": request, "result": answer})
            if answer.get("accepted") is True:
                result["standalone_action"] = answer
                result["pre_cleanup_verified"] = verified_repair(case, baseline, lease, deadline)
                break
            if request.get("tool") == "finish":
                break
    finally:
        session.close()
        calls = result["calls"]

        def measured_sum(values):
            return sum(values) if values and all(value is not None for value in values) else None

        values = [call.get("usage") or {} for call in calls]
        result["usage"] = {key: measured_sum([usage.get(key) for usage in values])
                           for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}
        result["model_duration_ms"] = measured_sum([call.get("duration_ms") for call in calls])
        result["estimated_usd"] = measured_sum([estimate(arm, usage) for usage in values])


def standalone_trial(case, baseline, probes, cursor, gate, limit, output, journal, *, arm, reference):
    bad = case.bad_image
    result = {"case": case.case_id, "protocol": STANDALONE_PROTOCOL, "arm": arm, "namespace": NS, "target": case.workload,
              "status": "unrecovered", "healthy_before": probes, "resource_gate": gate,
              "fixture_sha256": fixture_digest(baseline),
              "reference_incident": (reference.get("incident") or {}).get("id"),
              "baseline_image": image(case, baseline), "injected_image": bad}
    original = image(case, baseline)
    lease = None
    attempted = False
    write_once(journal, {"target": case.target, "uid": baseline["metadata"]["uid"], "arm": arm,
                         "restore_only_if_image": bad, "original_image": original,
                         "started_at": datetime.now(timezone.utc).isoformat(), "receipt": str(output)})
    began = monotonic()
    deadline = began + limit
    observed_at = None
    readable_errors = (Refused, TimeoutExpired, KeyError, ValueError, OSError)
    try:
        lease = WorkloadLease.claim(case, arm)
        lease.require_active()
        attempted = True
        patch_image(case, baseline, bad)
        while monotonic() < deadline:
            current = get("deployment", case.workload)
            unchanged, actual = same_workload(case, current, baseline)
            if not unchanged or actual != bad or ready_endpoints(case) != probes[0]["endpoint_uid"]:
                raise Refused("the controlled fault or original ready endpoint changed")
            fault = fault_observation(case, baseline["metadata"]["uid"], probes[0]["endpoint_uid"])
            if fault:
                observed_at = monotonic()
                result["first_error_ms"] = round((observed_at - began) * 1000)
                result["fault_before"] = fault
                initial = comparable_first_error(fault, reference, bad)
                result["initial_error_log"] = initial
                result["input_sha256"] = hashlib.sha256((TASK + initial).encode()).hexdigest()
                result["fault_service_probe"] = probe(case)
                standalone_inference(case, result, baseline, {**fault, "initial_error_log": initial}, arm, lease, deadline)
                break
            sleep(3)
        if "pre_cleanup_verified" in result:
            result["first_error_to_verified_ms"] = round((monotonic() - observed_at) * 1000)
    except readable_errors as error:
        result["trial_error"] = str(error) if isinstance(error, Refused) else type(error).__name__
    finally:
        result["wall_duration_ms"] = round((monotonic() - began) * 1000)
        if attempted:
            try:
                lease.require_active()
            except readable_errors as error:
                result["lease_error"] = str(error) if isinstance(error, Refused) else type(error).__name__
            try:
                # Operator cleanup may still restore its own exact image even after expiration.
                # The full spec/UID and resourceVersion/image CAS remain mandatory.
                result["restored"] = restore(case, baseline)
                if "fault_before" in result:
                    matched = observation(case, cursor, result["fault_before"]["pod"])
                    result["liveprobe_incidents"] = [row["id"] for row in matched]
            except readable_errors as error:
                result["cleanup_error"] = str(error) if isinstance(error, Refused) else type(error).__name__
        if lease is not None and (not attempted or "restored" in result):
            try:
                lease.release()
                result["lease_released"] = True
            except readable_errors as error:
                result["lease_error"] = str(error) if isinstance(error, Refused) else type(error).__name__
        if "cleanup_error" in result:
            result["status"] = "cleanup_not_verified"
        elif "lease_error" in result:
            result["status"] = "lease_not_verified"
        elif result.get("liveprobe_incidents"):
            result["status"] = "interfered_by_liveprobe"
        elif (result.get("standalone_action", {}).get("accepted") is True
                and successful_probes(result.get("pre_cleanup_verified")) and successful_probes(result.get("restored"))
                and result.get("liveprobe_incidents") == [] and result.get("lease_released") is True
                and "trial_error" not in result):
            result["status"] = "verified_dev_recovered"
        write_once(output, result)
        if (not attempted or "restored" in result) and (lease is None or result.get("lease_released") is True):
            journal.unlink()
    return result


def trial(case, baseline, probes, cursor, gate, limit, output, journal):
    bad = case.bad_image
    result = {"case": case.case_id, "protocol": PROTOCOL, "arm": "liveprobe", "namespace": NS, "target": case.workload,
              "status": "product_observation_only", "baseline": probes, "resource_gate": gate,
              "fixture_sha256": fixture_digest(baseline),
              "baseline_image": image(case, baseline), "injected_image": bad}
    old_endpoint = probes[0]["endpoint_uid"]
    original = image(case, baseline)
    write_once(journal, {"target": case.target, "uid": baseline["metadata"]["uid"],
                         "restore_only_if_image": bad, "original_image": original,
                         "started_at": datetime.now(timezone.utc).isoformat(), "receipt": str(output)})
    began = monotonic()
    readable_errors = (Refused, TimeoutExpired, KeyError, ValueError, OSError)
    try:
        patch_image(case, baseline, bad)
        while monotonic() - began < limit:
            current = get("deployment", case.workload)
            unchanged, actual = same_workload(case, current, baseline)
            if not unchanged or actual not in (original, bad):
                raise Refused("concurrent change after injection")
            if actual == bad and ready_endpoints(case) != old_endpoint:
                raise Refused("the original ready endpoint was lost")
            if "fault_before" not in result:
                observed = fault_observation(case, baseline["metadata"]["uid"], old_endpoint)
                if observed:
                    result["fault_before"] = observed
                    result["first_error_ms"] = round((monotonic() - began) * 1000)
                    result["fault_service_probe"] = probe(case)
                elif actual == original:
                    break
            if "fault_before" in result:
                cases = observation(case, cursor, result["fault_before"]["pod"])
                if len(cases) > 1:
                    raise Refused("multiple cases matched the same injected Pod")
                if cases:
                    result["incident"] = cases[0]
                    if attributed_recovery(case, cases[0], original) and actual == original and is_settled(current):
                        result["pre_cleanup_verified"] = [probe(case), probe(case)]
                        result["status"] = "verified_dev_recovered"
                        break
                    if cases[0]["status"] not in NONTERMINAL and (cases[0]["status"] != "normal" or actual == original):
                        break
            sleep(5)
    except readable_errors as error:
        result["trial_error"] = str(error) if isinstance(error, Refused) else type(error).__name__
    finally:
        result["wall_duration_ms"] = round((monotonic() - began) * 1000)
        try:
            result["restored"] = restore(case, baseline)
        except readable_errors as error:
            result["status"] = "cleanup_not_verified"
            result["cleanup_error"] = str(error) if isinstance(error, Refused) else type(error).__name__
        write_once(output, result)
        if "restored" in result:
            journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="read-only eligibility audit")
    parser.add_argument("--execute", action="store_true", help="attempt an approved dev image-pull trial")
    parser.add_argument("--case", choices=IMAGE_WORKLOADS, default="F1-01")
    parser.add_argument("--arm", choices=("liveprobe", *MODELS), default="liveprobe")
    parser.add_argument("--reference", type=Path, help="the LiveProbe observation for the same case and fixture")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hold-seconds", type=int, default=210)
    args = parser.parse_args()
    if args.preflight == args.execute or args.execute and args.output is None or not 30 <= args.hold_seconds <= 240:
        parser.error("choose --preflight or --execute --output PATH, with hold between 30 and 240 seconds")
    if (args.arm == "liveprobe") != (args.reference is None):
        parser.error("--reference is required for standalone arms only")
    case = load_image_case(args.case)
    if not args.execute:
        baseline, *_, gate = preflight(case)
        if args.reference:
            load_reference(case, args.reference, baseline)
        print(json.dumps({"eligible": True, "target": case.target, "gate": gate}))
        return 0
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    journal = EVIDENCE / "dev-k8s-fault-active.json"
    with (EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.output.exists() or journal.exists():
            raise Refused("an output or unfinished fault journal already exists")
        baseline, probes, cursor, gate = preflight(case)
        if args.arm == "liveprobe":
            result = trial(case, baseline, probes, cursor, gate, args.hold_seconds, args.output, journal)
        else:
            reference = load_reference(case, args.reference, baseline)
            result = standalone_trial(case, baseline, probes, cursor, gate, args.hold_seconds, args.output, journal,
                                      arm=args.arm, reference=reference)
    print(json.dumps({"status": result["status"], "incident": (result.get("incident") or {}).get("id"),
                      "restored": bool(result.get("restored"))}))
    return 0 if result.get("restored") and result["status"] not in ("cleanup_not_verified", "lease_not_verified") else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Refused, OSError, ValueError, RuntimeError, KeyError, TimeoutExpired) as error:
        if isinstance(error, (Refused, CHAOS.Stop)):
            print(json.dumps({"eligible": False, "reason": str(error)}))
        else:
            print(json.dumps({"eligible": False, "error_type": type(error).__name__}))
        raise SystemExit(2) from None
