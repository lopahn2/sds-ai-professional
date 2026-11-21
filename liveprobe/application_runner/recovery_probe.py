"""Read-only post-release proof for the original production operation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import time

from .deploy import summarize, validate_business
from .profiles import digest, manifest_image


HEX = re.compile(r"[a-f0-9]{64}")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# No origin, user-controlled params, administrative paths or generic readiness checks.
GET_OPERATIONS = {"order": "/api/orders/check?item=a", "stock": "/api/stock/a",
                  "catalog": "/api/products", "payment": "/api/payments",
                  "notification": "/api/notifications", "delivery": "/api/deliveries"}
MAX_LOG_BYTES = 65536
WINDOW_SECONDS = 120


class Missing(ValueError):
    """No eligible observation. Only fixed reason codes escape the runner."""


def _timestamp(value):
    if isinstance(value, str):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timezone missing")
        value = stamp.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid time")
    return value


def _id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def _hash(value):
    return value if isinstance(value, str) and HEX.fullmatch(value) else None


def http_error_signature(service, method, path, status):
    """Fingerprint for an actual observed request error. A release alone cannot produce this evidence."""
    return digest({"type": "http_access", "service": service, "method": method, "path": path, "status": status})


def settled_target(registry, kube, target, image, environment):
    service = target["service"]
    doc = kube.read(environment, service)
    observed = summarize(doc, environment, service, registry)
    md, st = doc["metadata"], doc.get("status") or {}
    count = doc["spec"].get("replicas", 1)
    gen = md.get("generation")
    if (md.get("namespace") != environment or md.get("name") != service or md.get("deletionTimestamp")
            or observed["uid"] != target["uid"] or observed["image"] != image or type(gen) is not int
            or st.get("observedGeneration", 0) < gen or count < 1
            or any(st.get(k) != count for k in ("updatedReplicas", "readyReplicas", "availableReplicas"))):
        raise Missing("deployment_unverified")
    pods = kube.pods(environment, service)
    if len(pods) != count or not all(p.get("owned") is True and p.get("ready") is True and
                                      p.get("image_id") == image for p in pods):
        raise Missing("pod_image_unverified")
    return gen, observed["template_sha256"], count


def _pod_logs(kube, pod, service, start):
    # A retained entry at/before the window boundary proves the file still covers its start.
    # Without this anchor rotation could hide failures just before the first retained success.
    since = datetime.fromtimestamp(start - 30, timezone.utc).isoformat().replace("+00:00", "Z")
    response = kube.api.call_api(f"/api/v1/namespaces/prd/pods/{pod}/log", "GET",
        query_params=[("container", service), ("sinceTime", since), ("timestamps", True),
                      ("limitBytes", MAX_LOG_BYTES + 1)], auth_settings=["BearerToken"],
        response_types_map={200: "str"}, _return_http_data_only=True, _preload_content=False, _request_timeout=5)
    try:
        raw = response.read(MAX_LOG_BYTES + 1)
        if len(raw) > MAX_LOG_BYTES:
            return None
        return raw.decode("utf-8").splitlines()
    finally:
        response.close()
        response.release_conn()


def _current_log_members(kube, scope, image, replicas):
    service = scope["service"]
    query = [("labelSelector", "app=" + service)]
    sets = kube._call("/apis/apps/v1/namespaces/prd/replicasets", "GET", query=query)
    owners = {r["metadata"]["uid"] for r in sets["items"] if any(
        o.get("kind") == "Deployment" and o.get("uid") == scope["deployment_uid"]
        and o.get("controller") is True for o in r["metadata"].get("ownerReferences", []))}
    pods = kube._call("/api/v1/namespaces/prd/pods", "GET", query=query)
    members = {}
    for p in pods["items"]:
        md = p["metadata"]
        if md.get("deletionTimestamp"):
            continue
        if not any(o.get("kind") == "ReplicaSet" and o.get("uid") in owners and o.get("controller") is True
                   for o in md.get("ownerReferences", [])):
            return {}
        c = next((c for c in p.get("status", {}).get("containerStatuses", []) if c["name"] == service), {})
        if (not _id(md.get("name")) or not _id(md.get("uid")) or c.get("ready") is not True
                or c.get("imageID", "").removeprefix("docker-pullable://") != image
                or _timestamp(c.get("state", {}).get("running", {}).get("startedAt")) > scope["window_start"]):
            return {}
        members[md["name"]] = (md["uid"], c.get("containerID"))
    return members if len(members) == replicas <= 8 else {}


def _http_window(kube, scope, replicas, status):
    """Observe a complete bounded slice of each current container's access log, or return no proof."""
    if (type(status) is not int or not 400 <= status <= 599
            or scope["original_error_sha256"] != http_error_signature(scope["service"], scope["method"], scope["path"], status)):
        return None
    before = _current_log_members(kube, scope, scope["image"], replicas)
    if not before:
        return None
    successes = failures = 0
    complete = True
    for pod in before:
        lines = _pod_logs(kube, pod, scope["service"], scope["window_start"])
        if lines is None:
            return None
        covers_start = False
        for line in lines:
            timestamp, sep, body = line.partition(" ")
            if not sep:
                return None
            try:
                observed = _timestamp(timestamp)
            except ValueError:
                return None
            covers_start |= observed <= scope["window_start"]
            if not body.startswith("{"):
                continue
            try:
                event = json.loads(body)
            except (ValueError, TypeError):
                return None
            if not isinstance(event, dict) or event.get("event") != "http_access":
                continue
            if (scope["window_start"] < observed <= scope["window_end"] and
                    event.get("service") == scope["service"] and event.get("environment") == "prd"
                    and event.get("method") == scope["method"] and event.get("uri") == scope["path"]):
                code = event.get("status")
                if type(code) is not int or not 100 <= code <= 599:
                    return None
                successes += int(200 <= code < 300)
                # Another failure of the same operation also rules out verified recovery.
                failures += int(code >= 400)
        complete &= covers_start
    if _current_log_members(kube, scope, scope["image"], replicas) != before:
        return None
    if not complete and not failures:
        return None
    return {**scope, "complete": complete, "success_count": successes, "error_count": failures}


def business_evidence(profile, response):
    checked = validate_business(profile, response)
    body = response["body"]
    if profile["verification"]["kind"] == "order" and body["inventory"]["available"] is not (body["inventory"]["quantity"] > 0):
        raise ValueError("stock_inconsistent")
    if profile["verification"]["kind"] == "catalog" and not all(isinstance(x.get("name"),str) and x["name"].strip() for x in body["products"]):
        raise ValueError("product_name_missing")
    return checked


def _evaluate(registry, kube, p, r, read_signals, clock):
    c = p.get("recovery_contract") or {}
    service = c.get("service")
    if not c or not _id(service) or p.get("stage") != "prd" or c.get("environment", "prd") != "prd":
        raise Missing("recovery_contract_unavailable")
    artifact = p["artifact"]
    profile = registry.get(service)
    image = manifest_image(artifact["image"], profile["image_repository"])
    if (not r["release_id"] or not r["plan_sha256"] or r["artifact_sha256"] != digest(artifact)
            or c.get("release_id", r["release_id"]) != r["release_id"] or c.get("image", image) != image):
        raise Missing("release_fingerprint_mismatch")
    matches = [t for t in p["targets"] if t["service"] == service and t["environment"] == "prd"]
    if len(matches) != 1 or not _id(matches[0].get("uid")) or registry.target(matches[0]) != profile:
        raise Missing("deployment_target_unmatched")
    target = matches[0]
    if c.get("deployment_uid", target["uid"]) != target["uid"]:
        raise Missing("deployment_target_unmatched")
    r.update(service=service, image=image, deployment_uid=target["uid"])
    method, path = (c.get("operation") or {}).get("method"), (c.get("operation") or {}).get("path")
    registered = GET_OPERATIONS.get(profile["verification"]["kind"])
    if (not registered or path != registered or path != profile["verification"]["path"]
            or method not in ("GET", "POST", "PUT", "PATCH", "DELETE")
            or set(c.get("operation") or {}) != {"method", "path"}):
        raise Missing("original_operation_unmatched")
    now, deployed = clock(), _timestamp(c.get("deployed_at"))
    if not 0 < deployed < now:
        raise Missing("postdeployment_window_unavailable")
    r.update(window_start=max(deployed, now-WINDOW_SECONDS), window_end=now, path=registered)
    before = settled_target(registry, kube, target, image, "prd")
    signature = _hash(c.get("original_error_sha256"))
    scope = {k:r[k] for k in ("release_id","artifact_sha256","plan_sha256","image","service","environment",
                              "deployment_uid","window_start","window_end","path")}
    scope.update(method=method, original_error_sha256=signature)
    sample = None
    if signature:
        try:
            sample = (read_signals(scope.copy()) if read_signals else
                      _http_window(kube, scope, before[2], c.get("http_status")))
        except Exception:
            # No usable passive reader. The explicitly registered GET may still observe the same operation.
            sample = None
    if sample is not None:
        if (not isinstance(sample, dict) or any(sample.get(k) != v for k,v in scope.items())
                or type(sample.get("complete")) is not bool
                or any(type(sample.get(k)) is not int or sample[k] < 0 for k in ("success_count","error_count"))):
            raise Missing("signal_window_unverified")
        r.update(original_error_unmatched=False, success_count=sample["success_count"], error_count=sample["error_count"])
        if sample["error_count"]:
            r.update(outcome="failed", reason="recovery_error_still_observed", method="original_error_absent")
        elif sample["complete"] and sample["success_count"]:
            r.update(outcome="passed", method="original_error_absent")
    if r["outcome"] == "unverifiable" and method == "GET" and c.get("allow_harmless_check") is True:
        response = kube.business("prd", service, registered)
        code = response.get("status")
        r.update(method="harmless_check", status_code=code if type(code) is int and 100 <= code <= 599 else None)
        if type(code) is int and 400 <= code <= 599:
            r.update(outcome="failed", reason="recovery_error_still_observed")
        elif code == 200:
            try:
                checked = business_evidence(profile, response)
                r.update(outcome="passed",body_sha256=checked["body_sha256"])
            except (TypeError, KeyError, AttributeError, ValueError):
                r.update(outcome="failed",reason="recovery_business_invariant_failed")
    if settled_target(registry, kube, target, image, "prd") != before:
        raise Missing("deployment_changed_during_probe")


def run_recovery(registry, kube, payload, *, read_signals=None, clock=time.time):
    """Return an auditable proof; an absent contract or inconclusive retry stays unverifiable."""
    r = {"protocol": "prd-recovery/v1", "release_id": _id(payload.get("release_id")),
         "artifact_sha256": _hash(payload.get("artifact_sha256")), "plan_sha256": _hash(payload.get("plan_sha256")),
         "environment": "prd", "image": None, "service": None, "deployment_uid": None, "method": None,
         "outcome": "unverifiable", "original_error_unmatched": True, "window_start": None, "window_end": None,
         "path": None, "status_code": None, "body_sha256": None}
    try:
        _evaluate(registry, kube, payload, r, read_signals, clock)
    except Missing as exc:
        r.update(outcome="unverifiable", reason=str(exc))
    except Exception:
        # Transport details can contain headers, response bodies or local credential paths.
        r.update(outcome="unverifiable", reason="recovery_observation_unavailable")
    if r["outcome"] == "unverifiable":
        r.setdefault("reason","recovery_not_observed")
    r["observed_at"] = clock()
    r["receipt_id"] = digest(r)
    return {"ok":r["outcome"] == "passed", "write_outcome":"not_written", "recovery":r, "error":r.get("reason")}
