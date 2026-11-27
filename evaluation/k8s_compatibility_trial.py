"""Compare log-only model repairs against a real, non-persisting Kubernetes API check."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from time import monotonic, sleep

from evaluation.application_fixture_replay import complete_sum
from evaluation.live_repair import RemoteModelSession, TASK
from evaluation.single_baseline import MODELS, estimate


CASE = "KI-01"
CONTEXT = "rancher-desktop"
NAMESPACE = "dev"


@dataclass(frozen=True)
class PreflightCase:
    id: str
    protocol: str
    kind: str
    name: str
    reason: str
    log: str
    namespace: str | None = NAMESPACE


CASES = {
    "KI-01": PreflightCase("KI-01", "live-repair/v8", "ingress", "lp-eval-ki01-ingress", "gvk_not_served",
                           'no matches for kind "Ingress" in version "networking.k8s.io/v1beta1"'),
    "KW-04": PreflightCase("KW-04", "live-repair/v9", "pod", "lp-eval-kw04-pod", "service_account_missing",
                           'error looking up service account dev/lp-eval-kw04-missing: '
                           'serviceaccount "lp-eval-kw04-missing" not found'),
    "KI-02": PreflightCase(
        "KI-02", "live-repair/v35", "validatingwebhookconfiguration", "lp-eval-ki02-review",
        "unsupported_admission_review_version",
        'webhooks[0].admissionReviewVersions: Invalid value: ["v9"]: must include at least one of v1, v1beta1',
        namespace=None,
    ),
}


def ingress(*, served):
    backend_name = "lp-eval-ki01-backend"
    backend = ({"service": {"name": backend_name, "port": {"number": 8080}}} if served
               else {"serviceName": backend_name, "servicePort": 8080})
    return {"apiVersion": "networking.k8s.io/" + ("v1" if served else "v1beta1"), "kind": "Ingress",
            "metadata": {"name": CASES["KI-01"].name, "namespace": NAMESPACE},
            "spec": {"rules": [{"host": "ki01.verify.invalid", "http": {"paths": [
                {"path": "/check", "pathType": "Prefix", "backend": backend},
            ]}}]}}


def manifest_for(case, *, corrected):
    if case.id == "KI-01":
        return ingress(served=corrected)
    if case.id == "KI-02":
        fixture_selector = {"matchLabels": {"liveprobe.example/fixture": "ki02"}}
        return {
            "apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingWebhookConfiguration",
            "metadata": {"name": case.name},
            "webhooks": [{
                "name": "review.ki02.verify.invalid",
                "admissionReviewVersions": ["v1" if corrected else "v9"],
                "failurePolicy": "Fail", "clientConfig": {"url": "https://ki02.verify.invalid/review"},
                "sideEffects": "None", "timeoutSeconds": 2, "matchPolicy": "Exact",
                "rules": [{"apiGroups": ["ki02.verify.invalid"], "apiVersions": ["v1"], "operations": ["CREATE"],
                           "resources": ["fixtures"], "scope": "Namespaced"}],
                "namespaceSelector": fixture_selector, "objectSelector": fixture_selector,
            }],
        }
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": case.name, "namespace": NAMESPACE},
            "spec": {
                "serviceAccountName": "default" if corrected else "lp-eval-kw04-missing",
                "automountServiceAccountToken": False, "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "workload", "image": "example.invalid/liveprobe/compat:1",
                                "imagePullPolicy": "Never",
                                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                                "resources": {"requests": {"cpu": "5m", "memory": "8Mi"},
                                              "limits": {"cpu": "25m", "memory": "24Mi"}}}],
            }}


def kubectl(*args, stdin=None, timeout=25):
    return subprocess.run(["kubectl", "--context", CONTEXT, "--request-timeout=20s", *args],
                          input=stdin, text=True, capture_output=True, timeout=timeout)


def discovery(case):
    if case.id == "KW-04":
        available = {}
        for name in ("lp-eval-kw04-missing", "default"):
            response = kubectl("-n", NAMESPACE, "get", "serviceaccount", name, "--ignore-not-found", "-o", "name")
            if response.returncode:
                raise RuntimeError("the service account lookup was not observed")
            available[name] = bool(response.stdout.strip())
        return {"service_accounts": available}
    group = "admissionregistration.k8s.io" if case.id == "KI-02" else "networking.k8s.io"
    response = kubectl("get", "--raw", f"/apis/{group}")
    if response.returncode:
        raise RuntimeError("the API discovery check failed")
    document = json.loads(response.stdout)
    result = {"served_versions": [row["groupVersion"] for row in document["versions"]],
              "preferred": document["preferredVersion"]["groupVersion"]}
    if case.id == "KI-02":
        result["fixture_webhook_review_versions"] = ["v1"]
    return result


def resource_absent(case=CASES[CASE]):
    namespace = ["-n", case.namespace] if case.namespace else []
    result = kubectl(*namespace, "get", case.kind, case.name, "--ignore-not-found", "-o", "name")
    if result.returncode:
        raise RuntimeError("could not verify absence of the dry-run resource")
    return not result.stdout.strip()


def check_manifest(case, manifest):
    namespace = ["-n", case.namespace] if case.namespace else []
    result = kubectl(*namespace, "create", "--dry-run=server", "--validate=false", "-f", "-", "-o", "json",
                     stdin=json.dumps(manifest))
    if result.returncode:
        expected = case.log in result.stderr
        return {"accepted": False, "contract_ok": False,
                "failure": case.reason if expected else "server_dry_run_failed",
                **({"error_log": case.log} if expected else {})}
    actual = json.loads(result.stdout)
    corrected = manifest_for(case, corrected=True)
    if case.id == "KI-02":
        absent = resource_absent(case)
        observed = {"apiVersion": actual.get("apiVersion"), "kind": actual.get("kind"),
                    "metadata": {key: actual["metadata"][key] for key in ("name", "namespace")
                                 if key in actual["metadata"]},
                    "webhooks": actual.get("webhooks")}
        return {"accepted": True, "resource_absent": absent, "contract_ok": absent and observed == corrected,
                "observed": observed}
    spec = actual.get("spec", {})
    comparable = {key: spec.get(key) for key in corrected["spec"]}
    if case.id == "KW-04":
        container_keys = corrected["spec"]["containers"][0].keys()
        comparable["containers"] = [{key: row.get(key) for key in container_keys} for row in spec.get("containers", [])]
    observed = {"apiVersion": actual.get("apiVersion"), "kind": actual.get("kind"),
                "metadata": {key: actual["metadata"].get(key) for key in ("namespace", "name")},
                "spec": comparable}
    absent = resource_absent(case)
    declared_scope = case.id != "KI-01" or set(spec) <= {"ingressClassName", "rules"}
    return {"accepted": True, "resource_absent": absent,
            "contract_ok": observed == corrected and absent and declared_scope,
            "observed": observed, **({"default_ingress_class": spec.get("ingressClassName")} if case.id == "KI-01" else {})}


def prepare(case=CASES[CASE]):
    if not resource_absent(case):
        raise RuntimeError("the synthetic resource name is already in use")
    available = discovery(case)
    corrected = manifest_for(case, corrected=True)
    original = manifest_for(case, corrected=False)
    if case.id == "KI-01":
        versions = available["served_versions"]
        reference_observed = "networking.k8s.io/v1" in versions and "networking.k8s.io/v1beta1" not in versions
    elif case.id == "KW-04":
        reference_observed = available["service_accounts"] == {"lp-eval-kw04-missing": False, "default": True}
    else:
        reference_observed = "admissionregistration.k8s.io/v1" in available["served_versions"]
    before = check_manifest(case, corrected)
    failed = check_manifest(case, original)
    if not (reference_observed and before["contract_ok"] and failed.get("failure") == case.reason):
        raise RuntimeError("the known API compatibility boundary was not reproduced")
    log = failed["error_log"]
    return {"case": case.id, "protocol": case.protocol, "initial_error_log": log,
            "input_sha256": hashlib.sha256((TASK + log).encode()).hexdigest(),
            "fixture_sha256": hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
            "healthy_before": before, "fault_before": failed, "api_discovery": available,
            "scope": "server-side Kubernetes dry-run; admission and manifest compatibility only"}


def operate(case, request, baseline, proposal):
    tool = request.get("tool")
    if tool == "workload":
        return discovery(case), proposal
    if tool == "source":
        return {"manifest": manifest_for(case, corrected=False)}, proposal
    if tool == "logs":
        return {"error": baseline["initial_error_log"]}, proposal
    if tool == "probe":
        return {"configured": proposal is not None, "resource_absent": resource_absent(case)}, proposal
    if tool != "configure":
        return {"finished": tool == "finish"}, proposal
    try:
        candidate = json.loads(request.get("value") or "")
    except (TypeError, ValueError):
        return {"accepted": False, "reason": "submit the complete manifest as JSON"}, proposal
    if (case.id == "KW-04" and isinstance(candidate, dict) and isinstance(candidate.get("spec"), dict)
            and "serviceAccountName" not in candidate["spec"]):
        candidate["spec"]["serviceAccountName"] = "default"
    if candidate != manifest_for(case, corrected=True):
        return {"accepted": False, "reason": "preserve the frozen workload settings when correcting the rejected reference"}, proposal
    result = check_manifest(case, candidate)
    return result, candidate if result["contract_ok"] else proposal


def run_model(case, arm):
    model, effort, _ = MODELS[arm]
    baseline = prepare(case)
    start = monotonic()
    history, calls = [], []
    proposal = None
    session = RemoteModelSession(model, effort, case.protocol)
    try:
        for _ in range(14):
            if monotonic() - start >= 240:
                break
            reply = session.turn(model, effort, baseline["initial_error_log"], history)
            calls.append({key: value for key, value in reply.items() if key != "data"})
            if (reply["model"], reply["effort"]) != (model, effort):
                raise RuntimeError("unexpected standalone model identity")
            if reply["status"] != "ok":
                break
            request = reply["data"]
            observation, proposal = operate(case, request, baseline, proposal)
            history.append({"request": request, "result": observation})
            if proposal or request["tool"] == "finish":
                break
        proof = [check_manifest(case, proposal), check_manifest(case, proposal)] if proposal else []
        fixed = len(proof) == 2 and all(row["contract_ok"] for row in proof)
        usage = {key: complete_sum(call["usage"].get(key) for call in calls)
                 for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")}
        return {**baseline, "arm": arm, "model": model, "effort": effort,
                "status": "verified_manifest_only" if fixed else "preflight_not_verified",
                "calls": calls, "investigation": history, "manifest_probes": proof,
                "resource_absent_after": resource_absent(case), "usage": usage,
                "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls),
                "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
                "wall_duration_ms": round((monotonic() - start) * 1000)}
    finally:
        session.close()


def incident_count(case, start):
    since = start.strftime("%Y-%m-%d %H:%M:%S+00")
    namespace_filter = f"namespace = '{case.namespace}' AND " if case.namespace else ""
    statement = ("BEGIN READ ONLY; SELECT COUNT(*) FROM incidents "
                 f"WHERE {namespace_filter}created_at >= '{since}' "
                 f"AND (workload = '{case.name}' OR title LIKE '%{case.name}%'); ROLLBACK;")
    r = kubectl("-n", "liveprobe-system", "exec", "postgres-0", "--", "psql", "-U", "liveprobe",
                "-d", "liveprobe", "-X", "-q", "-A", "-t", "-c", statement)
    text = r.stdout.strip()
    if r.returncode or not text.isdigit():
        raise RuntimeError("the scoped incident count could not be verified")
    return int(text)


def run_observation(case):
    start = datetime.now(timezone.utc)
    before = incident_count(case, start)
    baseline = prepare(case)
    sleep(20)
    after = incident_count(case, start)
    available = kubectl("-n", "liveprobe-system", "get", "deployment", "liveprobe-api", "-o",
                        "jsonpath={.status.availableReplicas}")
    if available.returncode or not available.stdout.isdigit() or int(available.stdout) < 1:
        raise RuntimeError("LiveProbe was not available during the compatibility check")
    return {**baseline, "arm": "liveprobe", "status": "product_observation_only",
            "matched_incident_count_before": before, "matched_incident_count_after": after,
            "resource_absent_after": resource_absent(case), "observation_seconds": 20,
            "scope": "deployed LiveProbe observation of the rejected preflight, with no workflow forced"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=tuple(CASES), default=CASE)
    parser.add_argument("--arm", choices=(*MODELS, "liveprobe"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("the compatibility receipt already exists")
    case = CASES[args.case]
    record = run_observation(case) if args.arm == "liveprobe" else run_model(case, args.arm)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({key: record.get(key) for key in ("case", "arm", "status", "usage", "estimated_usd",
                                                       "wall_duration_ms", "matched_incident_count_after")}))


if __name__ == "__main__":
    main()
