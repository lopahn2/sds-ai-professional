"""Replay a benchmark fixture through the deployed Application analysis worker in temporary SQLite."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib import import_module
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic, time


ORDER_RUNTIME_CASES = ("order-01", "order-04", "order-16")
ORDER_FILTER = "src/main/java/io/liveprobe/target/order/AppFaultFilter.java"
CASES = {**dict.fromkeys(ORDER_RUNTIME_CASES, ("order", "samsung-trade", ORDER_FILTER)),
         "catalog-05": ("catalog", "samsung-trade", "runtime.py"),
         "catalog-06": ("catalog", "samsung-trade", "runtime.py"),
         "catalog-14": ("catalog", "samsung-trade", "app.py"),
         "catalog-18": ("catalog", "samsung-trade", "app.py"),
         "catalog-24": ("catalog", "samsung-trade", "app.py"),
         "payment-02": ("payment", "samsung-trade", "runtime.py"),
         "payment-08": ("payment", "samsung-trade", "runtime.py"),
         "payment-12": ("payment", "samsung-trade", "app.py"),
         "payment-22": ("payment", "samsung-trade", "app.py"),
         "payment-27": ("payment", "samsung-trade", "runtime.py"),
         "member-03": ("member", "member", "chaos.js"),
         "member-13": ("member", "member", "business.js"),
         "member-17": ("member", "member", "chaos.js"),
         "member-23": ("member", "member", "business.js"),
         "member-28": ("member", "member", "chaos.js"),
         "notification-19": ("notification", "samsung-delivery", "runtime.py"),
         "notification-20": ("notification", "samsung-delivery", "runtime.py"),
         "notification-26": ("notification", "samsung-delivery", "runtime.py"),
         "notification-30": ("notification", "samsung-delivery", "app.py"),
         "delivery-07": ("delivery", "samsung-delivery", "chaos.js"),
         "delivery-09": ("delivery", "samsung-delivery", "chaos.js"),
         "delivery-15": ("delivery", "samsung-delivery", "business.js"),
         "delivery-25": ("delivery", "samsung-delivery", "business.js"),
         "delivery-29": ("delivery", "samsung-delivery", "business.js"),
         "order-11": ("order", "samsung-trade", "src/main/java/io/liveprobe/target/order/AppFaultBusiness.java"),
         "order-21": ("order", "samsung-trade", "src/main/java/io/liveprobe/target/order/AppFaultBusiness.java"),
         "notification-10": ("notification", "samsung-delivery", "runtime.py")}
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
WORKTREE_MODULES = ("result_evidence", "releases", "evidence_inquiry", "control", "analysis_schema", "worker")
RUNTIME_FIXTURE_SETTINGS = {
    "order-01": {"latency": {"seconds": 2}},
    "order-04": {"not_ready": {}},
    "order-16": {"dependency_failure": {}},
    "payment-02": {"error_rate": {"ratio": 1.0}},
    "payment-08": {"cpu_spin": {"threads": 1}},
    "payment-27": {"worker_exhaustion": {}, "intermittent_exception": {"ratio": 0.5}},
    "member-03": {"crash": {}},
    "member-17": {"latency_spike": {"ratio": 0.5, "seconds": 3}},
    "member-28": {"latency_spike": {"ratio": 0.7, "seconds": 4}, "cpu_spin": {"threads": 2}},
    "notification-10": {"worker_exhaustion": {}},
    "notification-19": {"error_rate": {"ratio": 0.4}, "latency_spike": {"ratio": 0.6, "seconds": 3}},
    "notification-20": {"memory_leak": {"mib_per_second": 4}, "latency": {"seconds": 1}},
    "notification-26": {"dependency_failure": {}, "latency": {"seconds": 2}},
    "notification-30": {"startup_config_error": {}, "not_ready": {}},
    "catalog-05": {"liveness_fail": {}},
    "catalog-06": {"hang": {"seconds": 40}},
    "catalog-14": {"background_job_failure": {"interval_seconds": 3}},
    "catalog-18": {"startup_config_error": {}},
    "delivery-07": {"intermittent_exception": {"ratio": 0.5}},
    "delivery-09": {"memory_leak": {"mib_per_second": 8}},
    "delivery-15": {"business_invariant": {}},
}


def complete_sum(values) -> int | float | None:
    numbers = list(values)
    return sum(numbers) if numbers and all(v is not None for v in numbers) else None


def fixture_review(role, evidence):
    result = role["result"]
    citations = []
    raw = result.get("citations")
    for item in raw if isinstance(raw, list) else []:
        ref, quote = (item.get("ref"), item.get("quote")) if isinstance(item, dict) else (None, None)
        grounded = None if evidence is None else (
            isinstance(ref, str) and ref in evidence and isinstance(quote, str)
            and len(quote.strip()) >= 8 and quote in evidence[ref])
        citations.append({"ref": ref, "grounded": grounded})
    review = {"role": role["role"], "confidence": result.get("confidence"), "fixable": result.get("fixable"),
              "accepted": result.get("accepted"),
              **{key: result[key] for key in ("level", "risk", "hypotheses") if key in result},
              "citations": citations}
    candidates = result.get("cause_candidates")
    if isinstance(candidates, list):
        from app.application.safety import scrub

        grounded_refs = {c["ref"] for c in citations if c["grounded"] is True}
        reviewed_candidates = []
        for index, candidate in enumerate(candidates[:3], 1):
            cause = candidate.get("cause_ko") if isinstance(candidate, dict) else None
            refs = candidate.get("evidence_refs") if isinstance(candidate, dict) else None
            if (not isinstance(cause, str) or not 0 < len(cause.strip()) <= 300 or not isinstance(refs, list)
                    or not 1 <= len(refs) <= 8 or any(not isinstance(ref, str) for ref in refs)):
                continue
            reviewed_candidates.append({"rank": index, "cause_ko": scrub(cause), "evidence_refs": scrub(refs),
                                        "grounded": None if evidence is None else set(refs) <= grounded_refs})
        review["cause_candidates"] = reviewed_candidates
    return review


def prompt_evidence(prompt, role):
    """Read the exact evidence shown on this call, including during retries and follow-up."""
    _, marker, payload = prompt.partition(f"\nRole: {role}\n")
    if not marker:
        return None
    try:
        context, _ = json.JSONDecoder().raw_decode(payload)
        collection, snapshot = context["collection"], context["source_snapshot"]
        allowed = {item["id"]: item["text"] for item in collection["evidence"]}
        if len(allowed) != len(collection["evidence"]):
            return None
        if context.get("round_request") and context.get("original_signal"):
            original = context["original_signal"]
            allowed[original["ref"]] = original["text"]
        allowed.update((f"source:{path}", text) for path, text in snapshot["files"].items())
        allowed.update((f"file_sha256:{path}", text) for path, text in snapshot["file_sha256"].items())
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return allowed if allowed and all(isinstance(text, str) for text in allowed.values()) else None


def runtime_evidence_rows(observations):
    first_log = observations["first_log"]
    if not isinstance(first_log, str) or not first_log.strip():
        raise ValueError("a runtime observation requires the original first log")
    rows = [first_log]
    for name, value in sorted(observations.items()):
        if name != "first_log":
            rows.append(json.dumps({name: value}, ensure_ascii=False, sort_keys=True))
    return rows


def admit_runtime_fixture(control, target, snapshot, evidence, observed_at):
    """Run the product intake and both fenced runner reports against an isolated store."""
    from app.application.contracts import Claim, CreateIncident, Report, Signal

    if not control.settings.testing or control.store.engine.url.get_backend_name() != "sqlite":
        raise RuntimeError("runtime fixture intake requires isolated test storage")
    initial = evidence[0]["text"]
    created = control.create(CreateIncident(environment=target["environment"], service=target["service"], reason=initial,
                             signal=Signal(source="fixture_application_log", observed_at=observed_at, sample=initial,
                                           sha256=hashlib.sha256(initial.encode()).hexdigest())),
                             {"subject": "isolated-fixture-intake"})
    identity = created["id"]
    if created["state"] != "collecting":
        raise RuntimeError("runtime fixture was not admitted to collection")
    initial_log = {k: evidence[0][k] for k in ("service", "environment", "observed_at", "text")}
    reports = []
    for kind, role, expected_state, payload in (
        ("collect", "cluster", "source_pinning", {"ok": True, "target": target, "observed_at": observed_at,
                                                   "logs": [initial_log], "evidence": evidence}),
        ("source", "source", "analysis_queued", {"ok": True, "source_snapshot": snapshot}),
    ):
        actor = {"subject": f"isolated-fixture-{role}", "runner_role": role}
        job = control.claim(Claim(worker_id=f"runtime-{role}", capabilities=[kind]), actor)["job"]
        if not job or job["incident_id"] != identity or job["kind"] != kind:
            raise RuntimeError(f"runtime {kind} lease was not bound to the admitted incident")
        accepted = control.report(job["id"], Report(lease_token=job["lease_token"], fencing_token=job["fencing_token"],
                                                    result=payload), actor)
        if accepted["state"] != expected_state:
            raise RuntimeError(f"runtime {kind} result did not reach {expected_state}")
        reports.append({"kind": kind, "state": accepted["state"], "result_sha256": accepted["result_sha256"]})
    return identity, {"intake_state": created["state"], "reports": reports,
                      "initial_error_sha256": hashlib.sha256(initial.encode()).hexdigest()}


def replay(worktree_sources=None, *, interactive=False) -> None:
    sources = worktree_sources or {}
    if sources and set(sources) != set(WORKTREE_MODULES):
        raise ValueError("incomplete isolated Application source bundle")
    for name in WORKTREE_MODULES:
        if name in sources:
            module = import_module(f"app.application.{name}")
            exec(compile(sources[name], f"<isolated-worktree-{name}>", "exec"), vars(module))
    from app.application.catalog import SERVICE_SYSTEMS
    from app.application import evidence_inquiry
    from app.application.contracts import Claim, Report
    from app.application.control import ControlPlane
    from app.application.releases import ReleaseControl
    if not issubclass(ControlPlane, ReleaseControl):
        raise RuntimeError("isolated ControlPlane did not load the release policy")
    from app.application.settings import ApplicationSettings, ROLE_DEFAULTS
    from app.application.storage import Incident, Store
    from app.application.worker import APP_PREAMBLE, AnalysisBlocked, AnalysisWorker
    from app.config import get_settings
    from app.llm.base import LLMError
    from app.llm.codex_cli import CodexCLIProvider

    packet = json.loads(sys.stdin.readline()) if interactive else json.load(sys.stdin)
    case = packet["case"]
    service, system, source_path = CASES[case]
    fixture_profile = packet.get("isolated_fixture_profile")
    if fixture_profile is not None and (fixture_profile != "legacy-member" or (service, system) != ("member", "member")):
        raise ValueError("invalid isolated fixture profile")
    obs, source = packet["observations"], packet["source"]
    source_limit = 4000 if case in ORDER_RUNTIME_CASES else 3000
    if not 0 < len(source) < source_limit or packet["source_path"] != source_path:
        raise ValueError("invalid fixture packet")
    release = packet.get("release")
    runtime_intake = packet.get("runtime_intake")
    if runtime_intake is not None:
        if case not in RUNTIME_FIXTURE_SETTINGS or release or not isinstance(runtime_intake, dict):
            raise ValueError("unsupported native runtime fixture")
        source_body = runtime_intake.get("versioned_source")
        fixture_sha = runtime_intake.get("fixture_sha256")
        if (not isinstance(source_body, str) or not 0 < len(source_body) < 60000
                or hashlib.sha256(source_body.encode()).hexdigest() != packet["source_sha256"]
                or not isinstance(fixture_sha, str) or len(fixture_sha) != 64
                or any(c not in "0123456789abcdef" for c in fixture_sha)):
            raise ValueError("runtime fixture requires the exact versioned source")
    separate_evidence = packet.get("separate_runtime_evidence") is True
    if separate_evidence and release:
        raise ValueError("runtime evidence layout is only available for runtime trials")
    if interactive and not (release or runtime_intake):
        raise ValueError("interactive feedback requires an isolated release or a fenced runtime intake")
    if release:
        if case in ("order-11", "order-21"):
            release_files = {source_path, "src/test/java/io/liveprobe/target/order/OrderControllerTest.java"}
        elif case in ("delivery-25", "delivery-29", "member-13", "member-23"):
            release_files = {"business.js", "test/index.js"}
        else:
            release_files = {"app.py", "tests/test_contract.py"}
        if case not in ("order-11", "order-21", "payment-12", "payment-22", "catalog-24",
                        "delivery-25", "delivery-29", "member-13", "member-23") or set(release["files"]) != release_files:
            raise ValueError("unsupported fixture release")
        if any(hashlib.sha256(body.encode()).hexdigest() != release["file_sha256"][path]
               for path, body in release["files"].items()):
            raise ValueError("the supplied source slice is not version-pinned")
    evidence = json.dumps(obs, ensure_ascii=False, sort_keys=True)
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    stamp = datetime.now(timezone.utc).isoformat()
    app_settings, live_settings = ApplicationSettings.from_env(), get_settings()
    roles = {role + "_" + suffix: value for role in ROLE_DEFAULTS
             for suffix, value in (("model", app_settings.model_for(role)), ("effort", app_settings.effort_for(role)))}
    critic_sol = packet.get("critic_sol") is True
    if critic_sol:
        if (not release or case not in ("payment-12", "member-13")
                or (case == "payment-12" and not sources) or roles["critic_model"] != "gpt-6.2-astra"):
            raise ValueError("the critic experiment needs the version-pinned release and its Astra control")
        roles["critic_model"] = "gpt-6.2-sol"
    provider = CodexCLIProvider(live_settings.codex_bin, app_settings.codex_home or live_settings.codex_home,
                                system_preamble=APP_PREAMBLE)
    calls = []

    class Meter:
        def complete(self, role, prompt, schema, model, effort, timeout):
            if len(prompt) > (150000 if release or runtime_intake else 25000):
                raise LLMError("config", "evaluation input exceeds the fixture bound")
            record = {"role": role, "model": model, "effort": effort}
            try:
                result = provider.complete(role, prompt, schema, model, effort, timeout)
            except LLMError as error:
                calls.append({**record, "error_kind": error.kind, "usage": asdict(error.usage),
                              "duration_ms": error.duration_ms})
                raise
            calls.append({**record, "model": result.model, "effort": result.effort,
                          "usage": asdict(result.usage), "duration_ms": result.duration_ms})
            if role != "patch" and isinstance(result.data, dict):
                evidence_at_call = prompt_evidence(prompt, role)
                review = fixture_review({"role": role, "result": result.data}, evidence_at_call)
                calls[-1]["citation_review"] = {
                    "scope": "evidence_as_presented_to_model",
                    "evidence_observed": evidence_at_call is not None,
                    "citations": [citation["grounded"] for citation in review["citations"]],
                }
                if "cause_candidates" in review:
                    calls[-1]["cause_candidates"] = review["cause_candidates"]
            return result

    with tempfile.TemporaryDirectory(prefix="liveprobe-fixture-") as scratch:
        db_file = Path(scratch) / "observation.sqlite3"
        settings = ApplicationSettings(database_url=f"sqlite:///{db_file}", testing=True,
                                       unified_mode=bool(runtime_intake),
                                       **({"lease_seconds": 480} if interactive else {}), **roles)
        store = Store(settings)
        try:
            if store.engine.url.get_backend_name() != "sqlite":
                raise RuntimeError("comparison replay requires a temporary SQLite database")
            if fixture_profile == "legacy-member":
                SERVICE_SYSTEMS[service] = system
                # The deployed intake and worker can retain separate allowlists from the catalog.
                for scope in (ControlPlane.create.__globals__, AnalysisWorker._evidence.__globals__):
                    allowed = scope["SERVICE_SYSTEMS"]
                    if not isinstance(allowed, dict):
                        raise RuntimeError("the isolated service allowlist could not be configured")
                    scope["SERVICE_SYSTEMS"] = {**allowed, service: system}
            store.initialize()
            control = ControlPlane(store, settings)
            identity = "fixture-" + case
            version = release["base_image"] if release else "fixture/local@sha256:" + (
                runtime_intake["fixture_sha256"] if runtime_intake else packet["source_sha256"])
            snapshot = release or {"id": "fixture-source", "base_image": version, "provenance_verified": True,
                                   "affected_services": [service], "profile_id": service,
                                   "files": {source_path: source}, "file_sha256": {source_path: source_sha}}
            target = {"environment": "dev", "system": system, "service": service, "uid": identity,
                      "resource_version": "1", "container": service, "image": version,
                      "template_sha256": packet["source_sha256"]}
            texts = [evidence]
            if release:
                texts = packet.get("evidence_rows", texts)
            elif separate_evidence or runtime_intake:
                texts = runtime_evidence_rows(obs)
            evidence_rows = [
                {"id": f"E{index}", "quality": "observed",
                 "source": "deployment" if runtime_intake and index > 1 else "pod_log", "observed_at": stamp,
                 "environment": "dev", "service": service, "text": text}
                for index, text in enumerate(texts, 1)
            ]
            start = monotonic()
            intake_receipt = None
            if runtime_intake:
                snapshot.update(files={source_path: source_body}, file_sha256={source_path: packet["source_sha256"]})
                snapshot["id"] = "fixture-" + runtime_intake["fixture_sha256"]
                identity, intake_receipt = admit_runtime_fixture(control, target, snapshot, evidence_rows, stamp)
            else:
                with store.transaction() as db:
                    db.add(Incident(id=identity, state="analysis_queued", created_at=time(), document={
                        "environment": "dev", "system": system, "service": service, "reason": evidence,
                        "stages": [], "error": None, "data": {
                            "collection": {"target": target, "observed_at": stamp, "logs": [], "evidence": evidence_rows},
                            "source_snapshot": snapshot,
                        },
                    }))
            worker = AnalysisWorker(control, provider=Meter())
            if not worker.run_once():
                raise RuntimeError("the fixture was not leased by the worker")
            followup = packet.get("followup")
            extra = None
            if followup:
                actor = {"subject": "isolated-fixture-collector", "runner_role": "cluster"}
                job = control.claim(Claim(worker_id="fixture", capabilities=["collect"]), actor)["job"]
                if job:
                    if job["payload"].get("purpose") != evidence_inquiry.PURPOSE or job["incident_id"] != identity:
                        raise RuntimeError("the follow-up job was not attributable to the replayed incident")
                    seen = followup["observed_at"]
                    log = {"service": service, "environment": "dev", "observed_at": seen,
                           "text": json.dumps(followup["trace"], ensure_ascii=False, sort_keys=True)}
                    result = {"ok": True, "target": target, "observed_at": seen, "logs": [log],
                              "evidence": [{**log, "id": "fixture-trace", "source": "pod_log", "quality": "observed"}]}
                    receipt = control.report(job["id"], Report(lease_token=job["lease_token"],
                                             fencing_token=job["fencing_token"], result=result), actor)
                    extra = {"report_state": receipt["state"], "trace_sha256": hashlib.sha256(log["text"].encode()).hexdigest()}
                    if receipt["state"] == "analysis_queued" and not worker.run_once():
                        raise RuntimeError("the model worker did not resume the investigated incident")
            elif runtime_intake and interactive:
                actor = {"subject": "isolated-fixture-cluster", "runner_role": "cluster"}
                job = control.claim(Claim(worker_id="runtime-cluster", capabilities=["collect"]), actor)["job"]
                if job:
                    if job["payload"].get("purpose") != evidence_inquiry.PURPOSE or job["incident_id"] != identity:
                        raise RuntimeError("runtime follow-up belongs to a different case or collection")
                    inquiry = control.detail(identity)["data"]["evidence_inquiry"]
                    print(json.dumps({"event": "runtime_followup", "question": inquiry["question"]}), flush=True)
                    observed = json.loads(sys.stdin.readline())
                    seen = observed["observed_at"]
                    rows = [{"id": f"R{index}", "environment": "dev", "service": service, "source": "deployment",
                             "quality": "observed", "observed_at": seen,
                             "text": json.dumps({name: value}, ensure_ascii=False, sort_keys=True)}
                            for index, (name, value) in enumerate(sorted(observed["facts"].items()), 1)]
                    receipt = control.report(job["id"], Report(lease_token=job["lease_token"],
                                             fencing_token=job["fencing_token"],
                                             result={"ok": True, "target": target, "observed_at": seen,
                                                     "logs": [], "evidence": rows}), actor)
                    extra = {"report_state": receipt["state"], "result_sha256": receipt["result_sha256"],
                             "observed_fields": sorted(observed["facts"])}
                    if receipt["state"] == "analysis_queued" and not worker.run_once():
                        raise RuntimeError("the model worker did not resume the runtime inquiry")
            patch = None
            build_reports = []
            builder_wait = 0.0
            if release:
                actor = {"subject": "isolated-fixture-builder", "runner_role": "source"}
                for _ in range(3 if interactive else 1):
                    job = control.claim(Claim(worker_id="fixture", capabilities=["test_build"]), actor)["job"]
                    if not job:
                        break
                    if (job["incident_id"] != identity or job["kind"] != "test_build"
                            or job["payload"]["source_snapshot"]["id"] != snapshot["id"]):
                        raise RuntimeError("the proposed repair job is not bound to the frozen release")
                    patch = job["payload"]["patch"]
                    if not interactive:
                        break
                    print(json.dumps({"event": "test_build", "patch": patch}), flush=True)
                    began = monotonic()
                    response = json.loads(sys.stdin.readline())
                    builder_wait += monotonic() - began
                    receipt = control.report(job["id"], Report(lease_token=job["lease_token"],
                                             fencing_token=job["fencing_token"], result=response), actor)
                    build_reports.append({"diff_sha256": patch["diff_sha256"], "error": response.get("error"),
                                          "reported_ok": response.get("ok"), "state": receipt["state"]})
                    if receipt["state"] == "analysis_queued":
                        if not worker.run_once():
                            raise RuntimeError("the patch revision did not resume")
                    elif receipt["state"] != "test_build_queued":
                        break
            elapsed = round((monotonic() - start - builder_wait) * 1_000)
            with store.read() as db:
                row = control.get(db, identity)
                state, reason = row.state, (row.document.get("error") or {}).get("code")
                recorded = row.document["data"]
                routing = recorded.get("analysis_routing")
                analysis_roles = recorded.get("analysis", {}).get("roles", [])
                try:
                    observations = worker._evidence(row.document) if analysis_roles else None
                except AnalysisBlocked:
                    observations = None
                reviews = [fixture_review(role, observations) for role in analysis_roles]
                if extra:
                    followup_record = row.document["data"]["evidence_inquiry"]
                    extra.update({"question": followup_record["question"], "evidence_refs": followup_record["evidence_refs"],
                                  "inquiry_state": followup_record["state"]})
                if release:
                    raw_patch_sha256s = [hashlib.sha256(role["result"]["diff"].encode()).hexdigest()
                                         for role in analysis_roles if role["role"] == "patch"
                                         and isinstance(role["result"].get("diff"), str)]
        finally:
            store.engine.dispose()

    print(json.dumps({"case": case, "arm": "liveprobe", "status": "analysis_replay" if calls else "replay_no_model",
                      "scope": "isolated analysis worker replay; no primary database, build or deployment",
                      "application_state": state, "reason_code": reason, "calls": calls,
                      "fixture_reviews": reviews, "analysis_routing": routing,
                      **({"runtime_intake": intake_receipt} if intake_receipt else {}),
                      **({"queued_patch": patch, "raw_patch_sha256s": raw_patch_sha256s} if release else {}),
                      **({"native_build_reports": build_reports} if interactive else {}),
                      **({"isolated_fixture_profile": fixture_profile} if fixture_profile else {}),
                      **({"evidence_layout": "separate_runtime_observations"} if separate_evidence else {}),
                      **({"isolated_policy": "critic-sol", "role_configuration": roles} if critic_sol else {}),
                      **{f"{name}_source_sha256": hashlib.sha256(body.encode()).hexdigest() for name, body in sources.items()},
                      **({"followup_collection": extra} if followup or extra else {}),
                      "analysis_wall_ms": elapsed, "model_duration_ms": complete_sum(r["duration_ms"] for r in calls),
                      "usage": {name: complete_sum(r["usage"].get(name) for r in calls) for name in TOKEN_FIELDS}}), flush=True)


def remote_replay(packet: dict, *, use_worktree_worker=False, native_builder=None, runtime_collector=None) -> dict:
    from evaluation.single_baseline import MODELS, estimate

    feedback = native_builder or runtime_collector
    timeout = "0" if feedback else "250s"
    mode = "--runner-in-pod" if feedback else "--in-pod"
    command = ["kubectl", "--context", "rancher-desktop", f"--request-timeout={timeout}", "-n", "liveprobe-system",
               "exec", "-i", "deployment/liveprobe-application-model-worker", "-c", "model-worker", "--", "python",
               "-B", "-c", Path(__file__).read_text(encoding="utf-8"), mode]
    if use_worktree_worker:
        source = Path(__file__).resolve().parents[1] / "liveprobe" / "app" / "application"
        command.extend((source / f"{name}.py").read_text(encoding="utf-8") for name in WORKTREE_MODULES)
    if feedback:
        from evaluation.live_repair import JsonLineSession

        # The worker owns a 900-second analysis lease. Keep this transport open
        # long enough to receive its actual terminal result or a build request.
        exchange = JsonLineSession(command, timeout=930)
        try:
            record = exchange.request(packet)
            while record.get("event") == "test_build":
                record = exchange.request(native_builder(record["patch"]))
            if record.get("event") == "runtime_followup" and runtime_collector:
                record = exchange.request(runtime_collector())
        finally:
            exchange.close()
    else:
        result = subprocess.run(command, input=json.dumps(packet), text=True, capture_output=True, timeout=255)
        if result.returncode:
            raise SystemExit(f"the model worker replay failed to execute (exit {result.returncode})")
        record = json.loads(result.stdout)
    if record["case"] != packet["case"] or record["arm"] != "liveprobe":
        raise ValueError("the model worker returned a different case or arm")
    model_keys = {value[0]: key for key, value in MODELS.items()}
    for call in record["calls"]:
        key = model_keys.get(call["model"])
        call["estimated_usd"] = estimate(key, call["usage"]) if key else None
    record["estimated_usd"] = complete_sum(call["estimated_usd"] for call in record["calls"])
    return record


def main() -> None:
    from evaluation.single_baseline import checked_evidence

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    target = args.directory / "liveprobe-result.json"
    if target.exists():
        raise SystemExit("the comparison record already exists")
    evidence = checked_evidence(args.directory / "anonymized-observation.json")
    receipt = json.loads((args.directory / "application-observed.json").read_text(encoding="utf-8"))
    observed = {row["ref"]: row["observation"] for row in evidence["observations"]}
    if receipt["case"] != args.case or observed["E1"] != receipt["sections"]["during"]:
        raise ValueError("observation does not match the fixture receipt")
    service, _, source_path = CASES[args.case]
    source_file = Path(__file__).resolve().parents[1] / "services" / service / source_path
    source_sha = hashlib.sha256(source_file.read_text(encoding="utf-8").encode()).hexdigest()
    if observed["E2"]["path"] != source_path or receipt["source_sha256"] != source_sha:
        raise ValueError("the observed public fixture source has changed")
    packet = {"case": args.case, "source_sha256": receipt["source_sha256"],
              "observations": observed["E1"], "source_path": source_path, "source": observed["E2"]["source"]}
    record = remote_replay(packet)
    with target.open("x", encoding="utf-8") as output:
        json.dump(record, output, indent=2)
        output.write("\n")
    print(json.dumps(record))


if __name__ == "__main__":
    if len(sys.argv) in (2, 2 + len(WORKTREE_MODULES)) and sys.argv[1] in ("--in-pod", "--runner-in-pod"):
        replay(dict(zip(WORKTREE_MODULES, sys.argv[2:])), interactive=sys.argv[1] == "--runner-in-pod")
    else:
        main()
