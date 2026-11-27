"""Run the remaining OOM campaign over a temporary SSH forward to the dedicated VM."""
from argparse import ArgumentParser
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from evaluation import isolated_oom_target as target


VM = "lp-work"
CACHE_VM = "lp-cache-check"
RELAY = "/opt/oom-evaluation/bridge.py"
GUEST_API = "https://127.0.0.1:6443"
HOST_ADDRESS = "127.0.0.1"
HOST_PORT = 16443


def private_kubeconfig(source, port):
    """Copy only inline credentials from the single ephemeral K3s context."""
    if source.keys() - {"kind", "apiVersion", "preferences", "current-context", "clusters", "users", "contexts"}:
        raise target.TargetError("unexpected kubeconfig fields in the isolated VM")
    cluster_entry, = source["clusters"]
    user_entry, = source["users"]
    context_entry, = source["contexts"]
    cluster, user, context = cluster_entry["cluster"], user_entry["user"], context_entry["context"]
    if ((context_entry["name"], context["cluster"], context["user"])
            != (source["current-context"], cluster_entry["name"], user_entry["name"])
            or context.keys() - {"cluster", "user", "namespace"}
            or cluster.keys() != {"server", "certificate-authority-data"}
            or cluster["server"] != GUEST_API
            or user.keys() != {"client-certificate-data", "client-key-data"}
            or not all(isinstance(value, str) and value for value in user.values())
            or not isinstance(cluster["certificate-authority-data"], str) or not cluster["certificate-authority-data"]):
        raise target.TargetError("invalid inline TLS credentials for the isolated VM")
    name = target.CONTEXT
    return {
        "apiVersion": "v1", "kind": "Config", "current-context": name,
        "clusters": [{"name": name, "cluster": {**cluster, "server": f"https://{HOST_ADDRESS}:{port}"}}],
        "users": [{"name": name, "user": user}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": name, "namespace": "dev"}}],
    }


def guest_kubeconfig(lima_home, *, cached_docker=False):
    vm = CACHE_VM if cached_docker else VM
    if lima_home.name != "lima" or not (lima_home / vm / "ssh.config").is_file():
        raise target.TargetError("the dedicated Lima VM configuration is missing")
    binary, config, state = (("/run/lp-cache/bin/k3s", "/run/lp-cache/kubeconfig", "/mnt/lp-data/lp-k3s")
                             if cached_docker else ("/run/lp-oci/k3s", "/run/lp-oci/kubeconfig", "/mnt/lp-btrfs/k3s"))
    args = ["limactl", "shell", "--tty=false", "--workdir", "/", vm, "--", "sudo", "env", f"K3S_DATA_DIR={state}",
            binary, "kubectl", "--kubeconfig", config,
            "config", "view", "--raw", "--minify", "--flatten", "-o", "json"]
    result = subprocess.run(args, env={**os.environ, "LIMA_HOME": str(lima_home)},
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    if result.returncode:
        raise target.TargetError("could not read the dedicated VM's kubeconfig")
    return json.loads(result.stdout)


def ssh_args(config, port, *, vm=VM):
    return [
        "ssh", "-F", str(config), "-nNT", "-oBatchMode=yes", "-oExitOnForwardFailure=yes",
        "-oControlMaster=no", "-oControlPath=none", "-oProxyCommand=none", "-oProxyJump=none",
        "-oHostName=127.0.0.1", "-oIdentitiesOnly=yes", "-oIdentityAgent=none",
        "-oForwardAgent=no", "-oForwardX11=no", "-oPermitLocalCommand=no", "-oConnectTimeout=8",
        "-L", f"{HOST_ADDRESS}:{port}:{HOST_ADDRESS}:6443", f"lima-{vm}",
    ]


@contextmanager
def owned_process(command):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def loopback(config, port, *, vm=VM):
    return owned_process(ssh_args(config, port, vm=vm))


def wait_for_api(checked, process):
    until = monotonic() + 15
    while monotonic() < until:
        if process.poll() is not None:
            raise target.TargetError("the isolated SSH forward stopped")
        try:
            reply = checked.command("get", "--raw=/readyz", timeout=3).strip()
        except (target.TargetError, subprocess.TimeoutExpired):
            sleep(.25)
            continue
        if reply == "ok" and process.poll() is None:
            checked.check_scope()
            return
        sleep(.25)
    raise target.TargetError("the pinned isolated Kubernetes endpoint did not become ready")


@contextmanager
def model_relay(checked, binary):
    from evaluation.oom_codex_bridge import SOCKET_PATH

    checked.check_scope()
    pod = "pod/" + checked.proof["receiver"]["pods"]["liveprobe-api"]["name"]
    command = [sys.executable, "-B", "-m", "evaluation.oom_codex_bridge", "--codex", str(binary.resolve(strict=True)),
               "--", *checked.prefix(), "-n", target.SYSTEM, "exec", "-i", pod, "-c", "api",
               "--", "python", "-u", RELAY, "--pod-server"]
    with owned_process(command) as process:
        until = monotonic() + 12
        while monotonic() < until and process.poll() is None:
            try:
                checked.command("exec", pod, "-c", "api", "--", "test", "-S", SOCKET_PATH,
                                timeout=3, namespace=target.SYSTEM)
            except (target.TargetError, subprocess.TimeoutExpired):
                sleep(.25)
                continue
            yield
            return
        raise target.TargetError("the verified receiver did not start its model relay")


def verify_models(checked):
    from evaluation.k8s_dev_image_trial import EVIDENCE, Refused
    from evaluation.k8s_dev_oom_trial import MODELS, check_isolated_model

    evidence = checked.evidence(EVIDENCE)
    evidence.mkdir(mode=0o700, exist_ok=True)
    try:
        for arm in MODELS:
            check_isolated_model(checked, arm, evidence)
    except Refused as error:
        raise target.TargetError(str(error)) from None


def run(args):
    lima_home = args.lima_home.resolve(strict=True)
    proof = args.proof.resolve(strict=True)
    with TemporaryDirectory(prefix="lp-oom-transport-") as directory:
        config = Path(directory) / "kubeconfig.json"
        payload = private_kubeconfig(guest_kubeconfig(lima_home, cached_docker=args.cached_docker), args.port)
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file)
        checked = target.Target.load(config, proof)
        checked.check_transport()
        vm = CACHE_VM if args.cached_docker else VM
        with loopback(lima_home / vm / "ssh.config", args.port, vm=vm) as process:
            wait_for_api(checked, process)
            if args.ledger is None and not args.verify_models:
                print(json.dumps({"ready": True, "isolated_target": checked.scope()}))
                return 0
            command = None
            if args.ledger:
                command = [sys.executable, "-B", "-m", "evaluation.k8s_dev_restart_campaign",
                           "--ledger", str(args.ledger.resolve(strict=True)), "--limit", str(args.limit),
                           "--wait-seconds", "600", "--execute"]
                for case_id in args.cases:
                    checked.require_case(case_id)
                    command.extend(("--case", case_id))
                command.extend(checked.cli_flags())
            if args.host_codex:
                with model_relay(checked, args.host_codex):
                    if args.verify_models:
                        verify_models(checked)
                        print(json.dumps({"ready": True, "model_round_trips_verified": True,
                                          "isolated_target": checked.scope()}), flush=True)
                    if command is None:
                        return 0
                    return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode
            return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--lima-home", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--port", type=int, default=HOST_PORT)
    parser.add_argument("--ledger", type=Path, help="explicitly execute the remaining isolated OOM cases")
    parser.add_argument("--case", dest="cases", action="append", choices=sorted(target.CASES), default=[],
                        help="complete the five comparison methods on one case before changing the VM")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--cached-docker", action="store_true", help="connect to the dedicated Docker cache VM")
    parser.add_argument("--host-codex", type=Path, help="run the pinned host CLI for the isolated receiver")
    parser.add_argument("--verify-models", action="store_true", help="verify four real model calls without issuing a fault")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= args.limit <= 8:
        parser.error("use a non-privileged loopback port and at most 8 isolated attempts per batch")
    if args.cases and not args.ledger:
        parser.error("select a case only when running the comparison ledger")
    if (args.host_codex and not args.cached_docker or args.verify_models and not args.host_codex
            or args.ledger and args.cached_docker and not args.host_codex):
        parser.error("the cached Docker campaign requires the pinned host CLI")
    try:
        return run(args)
    except (OSError, KeyError, AttributeError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
        # Parsing errors can contain credential material. Report only our fixed messages.
        message = str(exc) if isinstance(exc, target.TargetError) else "invalid isolated transport or proof"
        parser.exit(2, f"{message}\n")


if __name__ == "__main__":
    raise SystemExit(main())
