"""Explicit transport and identity binding for the dedicated OOM campaign."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit
from uuid import UUID

from evaluation import oom_isolated_preflight as preflight
from evaluation.dev_oom_case import CASES as NATIVE_OOM_CASES


CONTEXT = "liveprobe-oom-isolated"
CASES = frozenset({"F2-07", "F2-10", "F2-13", "F2-15"})
SYSTEM = preflight.SYSTEM
_active = None


class TargetError(ValueError):
    pass


def active():
    return _active


def arguments(parser):
    parser.add_argument("--isolated-kubeconfig", type=Path)
    parser.add_argument("--isolated-proof", type=Path)


def _private_file(path):
    try:
        info = path.lstat()
    except OSError:
        raise TargetError("the dedicated kubeconfig is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TargetError("the dedicated kubeconfig must be a private regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise TargetError("missing or invalid isolated object identity")
    return value


@dataclass(frozen=True)
class Target:
    kubeconfig: Path
    proof_path: Path
    proof: dict
    file_id: tuple

    @classmethod
    def load(cls, kubeconfig, proof_path):
        # abspath keeps an explicitly supplied symlink visible to the permission check.
        kubeconfig = Path(os.path.abspath(kubeconfig))
        proof_path = proof_path.resolve(strict=True)
        proof = json.loads(proof_path.read_text())
        if ((proof.get("node"), proof.get("kubernetes"), proof.get("fault_injections"))
                != (preflight.NODE, preflight.KUBERNETES, 0) or not proof.get("gate")):
            raise TargetError("the proof is not an admitted isolated OOM snapshot")
        _uuid(proof["node_uid"])
        _uuid(proof["namespace_uid"])
        fixture_scope = proof.get("fixture_scope", "all")
        if (fixture_scope not in preflight.FIXTURE_SCOPES
                or set(proof["workloads"]) != set(preflight.FIXTURE_SCOPES[fixture_scope])
                or proof["business"] != dict.fromkeys(preflight.business_requests(fixture_scope), 200)):
            raise TargetError("the proof lacks the complete business fixture")
        for workload in proof["workloads"].values():
            for field in ("service_uid", "deployment_uid", "pod_uid"):
                _uuid(workload[field])
        for name in ("liveprobe-api", "postgres"):
            pod = proof["receiver"]["pods"][name]
            _uuid(pod["uid"])
            if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", pod["name"])
                    or not preflight.DIGEST.search(pod["image_id"])):
                raise TargetError("the isolated receiver has an invalid Pod name")
        retention = proof["receiver"]["pods"]["liveprobe-api"].get("rag_retention_sha256")
        if retention is not None and not re.fullmatch(r"[0-9a-f]{64}", retention):
            raise TargetError("the isolated receiver has an invalid RAG retention fingerprint")
        return cls(kubeconfig, proof_path, proof, _private_file(kubeconfig))

    def cli_flags(self):
        return ["--isolated-kubeconfig", str(self.kubeconfig), "--isolated-proof", str(self.proof_path)]

    def prefix(self):
        if _private_file(self.kubeconfig) != self.file_id:
            raise TargetError("the isolated kubeconfig changed during the campaign")
        return ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", CONTEXT]

    def command(self, *args, namespace="dev", timeout=14, stdin=None):
        result = subprocess.run(self.prefix() + [f"--request-timeout={timeout - 2}s", "-n", namespace, *args],
                                input=stdin, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise TargetError(f"isolated Kubernetes request failed: {args[0]}")
        return result.stdout

    def check_transport(self):
        metadata = json.loads(self.command("config", "view", "--minify", "-o", "json"))
        context, = metadata["contexts"]
        cluster, = metadata["clusters"]
        user, = metadata["users"]
        server = urlsplit(cluster["cluster"]["server"])
        if (context["name"] != CONTEXT or server.scheme != "https" or server.hostname not in ("127.0.0.1", "::1")
                or server.username or server.path not in ("", "/") or server.query or server.fragment
                or any(key in cluster["cluster"] for key in ("proxy-url", "tls-server-name"))
                or cluster["cluster"].get("insecure-skip-tls-verify")
                or any(key in user["user"] for key in ("exec", "auth-provider", "tokenFile"))):
            raise TargetError("the dedicated kubeconfig does not describe the approved loopback transport")

    def check_scope(self):
        version = json.loads(self.command("get", "--raw=/version"))
        node, = json.loads(self.command("get", "nodes", "-o", "json"))["items"]
        meta = json.loads(self.command("get", "namespace", "dev", "-o", "json"))["metadata"]
        if ((version.get("gitVersion"), version.get("platform")) != (preflight.KUBERNETES, "linux/arm64")
                or (node["metadata"]["name"], node["metadata"]["uid"], meta["uid"])
                != (preflight.NODE, self.proof["node_uid"], self.proof["namespace_uid"])
                or meta.get("labels", {}).get("liveprobe.io/evaluation") != preflight.MARKER):
            raise TargetError("the live control plane does not match the isolated proof")
        if not preflight.matching_fixture_scope(self.command, meta, self.fixture_scope):
            raise TargetError("the live dev fixture scope does not match the isolated proof")
        for name in ("liveprobe-api", "postgres"):
            pinned = self.proof["receiver"]["pods"][name]
            pod = json.loads(self.command("get", "pod", pinned["name"], "-o", "json", namespace=SYSTEM))
            meta = pod["metadata"]
            status, = pod["status"]["containerStatuses"]
            if (meta["uid"] != pinned["uid"] or meta.get("deletionTimestamp")
                    or status.get("ready") is not True or status.get("imageID") != pinned["image_id"]
                    or (name == "liveprobe-api" and meta.get("annotations", {}).get(preflight.RETENTION_ANNOTATION)
                        != pinned.get("rag_retention_sha256"))):
                raise TargetError(f"the isolated {name} Pod changed")

    def scope(self):
        retention = self.proof["receiver"]["pods"]["liveprobe-api"].get("rag_retention_sha256")
        return {"node_uid": self.proof["node_uid"], "namespace_uid": self.proof["namespace_uid"],
                **({"fixture_scope": self.fixture_scope} if self.fixture_scope != "all" else {}),
                **({"rag_retention_sha256": retention} if retention is not None else {}),
                **{name + "_uid": self.proof["receiver"]["pods"][name]["uid"] for name in ("liveprobe-api", "postgres")}}

    @property
    def fixture_scope(self):
        return self.proof.get("fixture_scope", "all")

    @property
    def case_ids(self):
        return {case for case in CASES if self.fixture_scope in ("all", NATIVE_OOM_CASES[case][0])}

    def require_case(self, case_id):
        if case_id not in self.case_ids:
            raise TargetError("the requested case is outside the admitted isolated fixture scope")

    def verify_receipt(self, receipt):
        if receipt.get("isolated_target") != self.scope():
            raise TargetError("the receipt is from a different isolated runtime")

    def evidence(self, root):
        return root / ("isolated-oom-" + self.proof["namespace_uid"])

    def product_status(self, path):
        pod = self.proof["receiver"]["pods"]["liveprobe-api"]["name"]
        args = preflight.product_read_args(pod, path)
        return json.loads(self.command(*args, namespace=SYSTEM))

    def model_command(self, model, effort, protocol):
        from evaluation.live_repair import model_command

        receiver = self.proof["receiver"]["pods"]["liveprobe-api"]["name"]
        return model_command(model, effort, protocol, stream=True, kube_prefix=self.prefix(), receiver=f"pod/{receiver}")

    def approval(self, case, baseline, pod):
        from evaluation.dev_endpoint_pulse_case import INVENTORY_CALLER, INVENTORY_FIXTURE

        self.require_case(case.case_id)
        for name, current in [(case.workload, baseline)] + (
                [(INVENTORY_CALLER, baseline["dependency_caller"])] if case.workload == INVENTORY_FIXTURE else []):
            fixture = self.proof["workloads"][name]
            fields = {"service_uid": "service_uid", "deployment_uid": "deployment_uid",
                      "deployment_sha256": "deployment_sha256", "endpoint_uid": "pod_uid"}
            if any(current[key] != fixture[saved] for key, saved in fields.items()):
                raise TargetError("the isolated fixture identity changed")
        fixture = self.proof["workloads"][case.workload]
        if pod != fixture["pod_name"] or baseline["service_spec"] != fixture["service_spec"]:
            raise TargetError("the isolated fixture Pod or Service contract changed")
        digest = sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest()
        image_id = fixture["containers"][case.application]["image_id"]
        return {"namespace": "dev", "workload": case.workload, "ready": True,
                "fixture_sha256": digest, "image": {"candidate_id": image_id}}


def bind(args):
    global _active
    path, proof = args.isolated_kubeconfig, args.isolated_proof
    if bool(path) != bool(proof):
        raise TargetError("isolated mode requires both a kubeconfig and a receiver preflight proof")
    if path is None:
        return None
    if _active is not None:
        raise TargetError("this process has already selected an isolated runtime")
    try:
        target = Target.load(path, proof)
        target.check_transport()
        target.check_scope()
    except TargetError:
        raise
    except (OSError, TypeError, KeyError, ValueError, subprocess.TimeoutExpired):
        raise TargetError("the isolated transport or proof is incomplete") from None
    _active = target
    return target
