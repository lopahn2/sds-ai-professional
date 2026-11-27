"""Run a blinded Spring JDBC dev recovery with an independently verified reservation journey."""

from argparse import ArgumentParser
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import secrets
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from evaluation import reservation_dev_runtime as runtime
from evaluation.jdbc_dev_manifest import NAME, manifest
from evaluation.isolated_recovery_agent import Unobserved, run_agent
from evaluation.matched_recovery_campaign import ARMS, RECOVERY_BUDGET_S, canonical_digest, dev_preflight, write_new
from evaluation.prepare_jdbc_fixture import VARIANTS
from evaluation.reservation_runtime_image import package
from evaluation.reservation_source_trial import SourceTrial


cluster = runtime.cluster
STANDALONE = tuple(arm for arm in ARMS if arm != "liveprobe")
STRING = {"type": "string"}
BOOL = {"type": "boolean"}
TOOLS = [
    {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False,
    }}
    for name, description, fields in (
        ("source_files", "List this reservation application's allowed sources and test files.", {}),
        ("read_java", "Read a Java source or test file by its basename or the path listed by source_files.", {"name": STRING, "test": BOOL}),
        ("write_java", "Replace Java source or add a focused regression test using the last read digest or null for a new file.", {
            "name": STRING, "test": BOOL, "expected": {"type": ["string", "null"]}, "text": STRING,
        }),
        ("verify_patch", "Run the same created tests on the original and current code in the isolated Maven runner.", {}),
        ("deploy_dev", "Deploy the code whose same regression just failed on the original and passed on the current code.", {}),
    )
]


def seed(artifact):
    fixture = manifest(artifact["image"])
    fixture["items"].insert(0, {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                "metadata": {"name": NAME+"-db", "namespace": "dev", "labels": {"app": NAME}},
                                "stringData": {"password": secrets.token_urlsafe(36)}})
    for mode in (["--dry-run=server"], []):
        print(cluster.command("create", "-f", "-", *mode, "-o", "name", stdin=json.dumps(fixture)).strip())


def sealed_variant(arm, mapping_path, commitment):
    if mapping_path.stat().st_mode & 0o077:
        raise Unobserved("the blinded assignment must be readable only by the coordinator")
    mapping = json.loads(mapping_path.read_text())
    if (set(mapping) != set(ARMS) or set(mapping.values()) != set(VARIANTS)
            or canonical_digest(mapping) != commitment):
        raise Unobserved("the blinded assignment did not match its pre-test commitment")
    return mapping[arm]


def last_incident():
    return cluster.query("SELECT coalesce(max(id),0) FROM incidents")


def isolated_since(cursor):
    return not cluster.query(f"SELECT count(*) FROM incidents WHERE id>{int(cursor)} "
                             f"AND namespace='dev' AND workload='{NAME}'")


class SourceBroker:
    def __init__(self, source, baseline, faulty, lease, deadline):
        self.source, self.baseline, self.faulty, self.lease = source, baseline, faulty, lease
        self.deadline = deadline
        self.test = self.tested_files = self.candidate = None
        self.proof = self.confirmed_at = None

    def __call__(self, method, args):
        if method in ("read_java", "write_java"):
            try:
                if method == "read_java":
                    return self.source.read(args["name"], test=args["test"])
                return self.source.replace(args["name"], args["expected"], args["text"], test=args["test"])
            except ValueError as error:
                return {"accepted": False, "reason": str(error)}
        if args:
            raise Unobserved("the requested tool takes no arguments")
        if method == "source_files":
            return self.source.index()
        if method == "verify_patch":
            self.lease.renew(900)
            try:
                self.test = self.source.verify_patch()
            except ValueError as exc:
                return {"passed": False, "reason": str(exc)}
            self.tested_files = self.source.index()
            return {key: value for key, value in self.test.items() if key != "artifact"}
        if method == "deploy_dev":
            artifact = (self.test or {}).get("artifact")
            if not artifact or self.tested_files != self.source.index() or self.candidate is not None:
                return {"deployed": False, "reason": "the current code must reproduce and pass its regression tests"}
            self.lease.require_active()
            runtime.switch(self.baseline, self.faulty["image"], artifact["image"])
            self.candidate = artifact
            self.proof = verify_healthy(self.baseline, artifact, self.deadline)
            self.confirmed_at = monotonic()
            return {"deployed": True, "target": NAME, "independent_reservation_contract": "passed"}
        raise Unobserved("tool is outside the current source trial")


def verify_healthy(baseline, artifact, deadline):
    ready = runtime.read_workload(baseline, artifact, deadline)
    if ready["status"] != "recovered":
        raise Unobserved("the exact reservation release failed the independent HTTP or pool checks")
    return ready


def run(args, healthy, broken, variant, allocation, journal):
    gate = dev_preflight()
    baseline = runtime.deployment()
    if runtime.image_of(baseline) != healthy["image"]:
        raise Unobserved("the previously verified neutral dev release is missing")
    cursor = last_incident()
    result = {"case": runtime.CASE.case_id, "arm": args.arm, "headroom": gate, "status": "unobserved",
              "deployment_uid": baseline["metadata"]["uid"]}
    write_new(journal, {"target": runtime.CASE.target, "uid": baseline["metadata"]["uid"], "original": healthy["image"],
                        "faulty": broken["image"], "receipt": str(args.output)})
    lease = broker = None
    clean = False
    try:
        lease = cluster.WorkloadLease.claim(runtime.CASE, args.arm, ttl=900)
        result["healthy_before"] = verify_healthy(baseline, healthy, monotonic()+70)
        runtime.switch(baseline, healthy["image"], broken["image"])
        began = monotonic()
        result["injected_at"] = datetime.now(timezone.utc).isoformat()
        before = runtime.read_workload(baseline, broken, began+75)
        if before["status"] != "leak_reproduced":
            raise Unobserved("the deployed blinded variant did not reproduce the independent JDBC leak")
        error = runtime.first_error(before["runtime"]["pod"])
        result.update(fault=before, first_error=error, first_error_ms=round((monotonic()-began)*1000))
        if args.arm == "calibrate":
            runtime.switch(baseline, broken["image"], healthy["image"])
            result["verified_before_cleanup"] = verify_healthy(baseline, healthy, began+180)
            result["status"] = "calibrated"
        else:
            with TemporaryDirectory(prefix="lp-jdbc-blind-") as directory:
                source = SourceTrial(variant, Path(directory), args.maven_cache)
                stop = began + RECOVERY_BUDGET_S
                broker = SourceBroker(source, baseline, broken, lease, stop)
                result["standalone"] = run_agent(binary=args.binary, auth_file=args.auth, arm=args.arm,
                                                 error_line=error, tools=TOOLS, broker=broker, deadline=stop)
                if broker.test:
                    result["regression"] = broker.test
                if broker.proof and result["standalone"]["status"] == "completed":
                    result.update(status="recovered", verified_before_cleanup=broker.proof,
                                  recovery_ms=round((broker.confirmed_at-began)*1000))
                elif result["standalone"]["status"] == "completed" and runtime.image_of(runtime.deployment()) == broken["image"]:
                    failure = runtime.failure_persists(baseline, broken, monotonic()+10)
                    result.update(status="unrecovered", failed_before_cleanup=failure)
        if not isolated_since(cursor):
            raise Unobserved("LiveProbe created a case during the standalone comparison")
        result.update(variant_sha256=broken["jar_sha256"], baseline_image=broken["image"],
                      neutral_release_sha256=healthy["jar_sha256"], allocation_sha256=allocation,
                      trial_elapsed_ms=round((monotonic()-began)*1000))
    except (Unobserved, ValueError, OSError, TimeoutExpired, cluster.Refused) as exc:
        result.update(status="unobserved", trial_error=str(exc))
    finally:
        try:
            current = runtime.deployment()
            current_image = runtime.image_of(current)
            approved = (healthy["image"], broken["image"], (broker.candidate or {}).get("image") if broker else None)
            if current_image not in approved or not runtime.same_resource(current, baseline):
                raise Unobserved("a concurrent change prevents restoration of the App trial")
            if current_image != healthy["image"]:
                runtime.switch(baseline, current_image, healthy["image"])
            result["restored"] = verify_healthy(baseline, healthy, monotonic()+85)
            if lease:
                for _ in range(12):
                    sleep(10)
                    lease.require_active()
                if not isolated_since(cursor):
                    result.update(status="unobserved", trial_error="a LiveProbe incident occurred inside the isolated lease")
                lease.release()
            clean = True
        finally:
            if not clean:
                result.update(status="unobserved", cleanup_incomplete=True)
            write_new(args.output, result)
            if clean:
                journal.unlink()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("seed", "calibrate", *STANDALONE), required=True)
    parser.add_argument("--healthy-receipt", type=Path, required=True)
    parser.add_argument("--jars", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--commitment", type=Path)
    parser.add_argument("--maven-cache", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--auth", type=Path)
    args = parser.parse_args()
    healthy = json.loads(args.healthy_receipt.read_text())["artifact"]
    journal = cluster.EVIDENCE / "dev-k8s-fault-active.json"
    with (cluster.EVIDENCE / "dev-k8s-fault.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if journal.exists() or args.output.exists() or not args.output.parent.is_dir():
            parser.error("another trial journal or output exists")
        if args.arm == "seed":
            dev_preflight()
            seed(healthy)
            proof = verify_healthy(runtime.deployment(), healthy, monotonic()+80)
            write_new(args.output, proof)
            print(json.dumps({"status": proof["status"], "runtime": proof["runtime"]}))
            return 0
        if args.jars is None:
            parser.error("the locally built variant JARs are required to inject the fault")
        allocation = None
        if args.arm == "calibrate":
            variant = VARIANTS[0]
        else:
            if not all((args.assignments, args.commitment, args.maven_cache, args.binary, args.auth)):
                parser.error("standalone mode needs the precommitted allocation, offline dependencies and CLI")
            allocation = json.loads(args.commitment.read_text())["sha256"]
            variant = sealed_variant(args.arm, args.assignments, allocation)
        broken = package(args.jars / variant / "target/reservation-1.0.0.jar")
        result = run(args, healthy, broken, variant, allocation, journal)
    print(json.dumps({"arm": args.arm, "status": result["status"]}))
    return 2 if result["status"] == "unobserved" else 0


if __name__ == "__main__":
    raise SystemExit(main())
