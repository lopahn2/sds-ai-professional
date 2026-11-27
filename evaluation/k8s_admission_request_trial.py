"""Compare a standalone model on the real KI-02 API request path."""
from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from evaluation.application_fixture_replay import TOKEN_FIELDS, complete_sum
from evaluation.k8s_dev_image_trial import CHAOS, EVIDENCE, write_once
from evaluation.ki02_admission_fixture import API_SERVER_VERSION, CASE, PROTOCOL, WEBHOOK
from evaluation.live_repair import JsonLineSession, RemoteModelSession, TOOLS, TASK, host_model_turn
from evaluation.single_baseline import MODELS, estimate


DOCKER = ["docker", "--context", "rancher-desktop"]
IMAGE = "liveprobe/liveprobe:dev-prd-scope-api-20261124"
IMAGE_SHA = "sha256:8e76baf4e81e3416a02ee297f3f0c4d6060afd6ef5124fa93a2c457544df92eb"
BINARY_SHA = "bad32c0c798bce0e883216ed755bbfb3c6a9fbd9b9f1dee32a6be16d256ad033"
FIXTURE = Path(__file__).with_name("ki02_admission_fixture.py")
EXACT_FIRST_ERROR = "ff813055cb561d262850ad62a692a2240cfb6f5f5b19edb11217f5fcc2b57a48"
MAX_TURNS = 10


def fixture_digest(scope):
    contract = (CASE, PROTOCOL, API_SERVER_VERSION, scope, BINARY_SHA)
    return sha256(json.dumps(contract).encode()).hexdigest()


def node_gate(wait_seconds):
    deadline = monotonic() + wait_seconds
    while True:
        try:
            return CHAOS.node_gate()
        except CHAOS.Stop:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            sleep(min(8, remaining))


def docker(*args, checked=True):
    return subprocess.run([*DOCKER, *args], check=checked, text=True, capture_output=True, timeout=20)


def temporary_tls(directory):
    """Generate a private loopback certificate only for the disposable fixture."""
    cert, key = directory / "webhook-cert.pem", directory / "webhook-key.pem"
    config = directory / "tls.cnf"
    config.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=x509\n"
        "[dn]\nCN=ki02-admission\n"
        "[x509]\nbasicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n",
        encoding="utf-8",
    )
    subprocess.run(["/usr/bin/openssl", "req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-config", str(config), "-out", str(cert), "-keyout", str(key)],
                   check=True, capture_output=True, timeout=10)
    for path in (cert, key):
        path.chmod(0o444)
    return cert, key


@contextmanager
def isolated_fixture(binary):
    if docker("image", "inspect", "--format", "{{.Id}}", IMAGE).stdout.strip() != IMAGE_SHA:
        raise ValueError("unexpected fixture container image")
    if sha256(binary.read_bytes()).hexdigest() != BINARY_SHA:
        raise ValueError("unexpected K3s release checksum")
    name = "lp-ki02-" + uuid4().hex[:12]
    with TemporaryDirectory(prefix="lp-ki02-model-", dir="/private/tmp") as location:
        directory = Path(location)
        cert, key = temporary_tls(directory)
        executable = directory / "k3s-arm64"
        shutil.copyfile(binary, executable)
        executable.chmod(0o555)
        args = [
            *DOCKER, "run", "--pull=never", "--rm", "-i", "--name", name, "--label", "liveprobe.example/fixture=ki02",
            "--network", "none", "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
            "--memory=1250m", "--memory-swap=1250m", "--cpus=0.55", "--pids-limit=150",
            "--tmpfs", "/tmp:rw,exec,size=450m,mode=1777", "--tmpfs", "/run:rw,size=8m",
            "--tmpfs", "/etc/rancher:rw,size=8m",
            "--env", "HOME=/tmp", "--env", "K3S_DATA_DIR=/tmp/ki02/state", "--env", "GOMAXPROCS=1",
        ]
        for source, filename in ((cert, cert.name), (key, key.name), (executable, executable.name), (FIXTURE, "fixture.py")):
            args.extend(("--mount", f"type=bind,source={source},target=/opt/isolated/{filename},readonly"))
        args.extend(("--entrypoint", "/app/.venv/bin/python", IMAGE, "-B", "/opt/isolated/fixture.py"))
        fixture = JsonLineSession(args, timeout=115)
        try:
            yield fixture
        finally:
            fixture.close()
            if docker("container", "ls", "-aq", "--filter", f"name=^{name}$").stdout.strip():
                docker("container", "rm", "-f", name)
            if docker("container", "ls", "-aq", "--filter", f"name=^{name}$").stdout.strip():
                raise RuntimeError("the isolated container did not clean up")


def run_fixture(fixture, arm, *, host_provider=None):
    baseline = fixture.request({"tool": "prepare"})
    initial = baseline["injected_request"]["message"]
    if sha256(initial.encode()).hexdigest() != EXACT_FIRST_ERROR:
        raise ValueError("the initial error differs from both independently reproduced trials")
    calls, history = [], []
    model, effort, _ = MODELS[arm]
    model_failed = None
    deadline = monotonic() + 245
    session = RemoteModelSession(model, effort, PROTOCOL) if host_provider is None else None
    try:
        for _ in range(MAX_TURNS):
            if monotonic() >= deadline:
                break
            reply = (session.turn(model, effort, initial, history) if session else
                     host_model_turn(host_provider, model, effort, initial, history))
            calls.append({key: value for key, value in reply.items() if key != "data"})
            if (reply.get("model"), reply.get("effort")) != (model, effort) or reply.get("status") != "ok":
                model_failed = reply.get("status") or "invalid_model_record"
                break
            operation = reply.get("data")
            if not isinstance(operation, dict) or operation.get("tool") not in TOOLS:
                model_failed = "invalid_tool_request"
                break
            if operation["tool"] == "finish":
                history.append({"request": operation, "result": {"finished": True}})
                break
            outcome = fixture.request({key: operation.get(key) for key in ("tool", "key", "value")})
            history.append({"request": operation, "result": outcome})
            if operation["tool"] == "configure" and outcome.get("applied") and outcome["after"]["versions"][0] == "v1":
                break
    finally:
        if session:
            session.close()
    verification = fixture.request({"tool": "verify"})
    proven = all(item["request"]["stored"] for item in verification["independent_probes"])
    return {**baseline, **verification, "arm": arm, "protocol": PROTOCOL, "model": model, "effort": effort,
            "status": "incomplete" if model_failed else ("verified_recovered" if proven else "unrecovered"),
            "model_failed": model_failed, "initial_error_log": initial, "fault_before": baseline["injected_request"],
            "input_sha256": sha256((TASK + initial).encode()).hexdigest(),
            "fixture_sha256": fixture_digest(baseline["initial_registration"]["scope_sha256"]),
            "scope": "isolated K3s API admission operation; two independent ConfigMap writes and readbacks",
            "investigation": history, "calls": calls,
            "usage": {name: complete_sum(call["usage"].get(name) for call in calls) for name in TOKEN_FIELDS},
            "model_duration_ms": complete_sum(call["duration_ms"] for call in calls),
            "estimated_usd": complete_sum(estimate(arm, call["usage"]) for call in calls)}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=MODELS, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=95, choices=range(0, 601), metavar="0-600")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("the receipt already exists")
    with (EVIDENCE / "native-dev-comparison-campaign.lock").open("a+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (EVIDENCE / "dev-k8s-fault-active.json").exists():
            parser.error("a dev fault is still active")
        try:
            headroom = node_gate(args.wait_seconds)
        except CHAOS.Stop as error:
            report = {"phase": "waiting_for_node", "case": CASE, "arm": args.arm, "reason": str(error)}
            if isinstance(error, CHAOS.NodeGateStop):
                report.update(node=error.snapshot, breaches=error.breaches)
            print(json.dumps(report), flush=True)
            return 2
        print(json.dumps({"phase": "starting", "case": CASE, "arm": args.arm, "node_gate": headroom}), flush=True)
        with isolated_fixture(args.binary) as fixture:
            record = run_fixture(fixture, args.arm)
        record.update({"node_gate": headroom, "cleanup_verified": True,
                       "recorded_at": datetime.now(timezone.utc).isoformat()})
        write_once(args.output, record)
        print(json.dumps({key: record[key] for key in ("case", "arm", "status", "model_failed")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
