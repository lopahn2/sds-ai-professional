"""The dedicated OOM launcher must never use the host's default Kubernetes context."""
from contextlib import contextmanager
import json
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from evaluation import oom_isolated_launch as launch


@pytest.fixture
def guest():
    return {
        "apiVersion": "v1", "kind": "Config", "current-context": "default",
        "clusters": [{"name": "local", "cluster": {"server": launch.GUEST_API,
                                                   "certificate-authority-data": "ephemeral-ca"}}],
        "users": [{"name": "local", "user": {"client-certificate-data": "ephemeral-certificate",
                                            "client-key-data": "ephemeral-private"}}],
        "contexts": [{"name": "default", "context": {"cluster": "local", "user": "local"}}],
    }


@pytest.mark.parametrize("tamper", ["server", "insecure", "proxy", "key_file", "exec", "context"])
def test_copy_rejects_external_transport_and_credentials(guest, tamper):
    cluster = guest["clusters"][0]["cluster"]
    user = guest["users"][0]["user"]
    if tamper == "server":
        cluster["server"] = "https://another-kubernetes.invalid:6443"
    elif tamper == "insecure":
        cluster["insecure-skip-tls-verify"] = True
    elif tamper == "proxy":
        cluster["proxy-url"] = "https://proxy.invalid"
    elif tamper == "key_file":
        user["client-key"] = "/some/local/file"
    elif tamper == "exec":
        user["exec"] = {"command": "unexpected"}
    else:
        guest["current-context"] = "unrelated"
    with pytest.raises(launch.target.TargetError):
        launch.private_kubeconfig(guest, 16443)


def test_copy_uses_the_same_ca_and_limits_the_ssh_forward_to_loopback(guest):
    host = launch.private_kubeconfig(guest, 16443)
    cluster, = host["clusters"]
    assert host["current-context"] == launch.target.CONTEXT
    assert cluster["cluster"] == {"server": "https://127.0.0.1:16443", "certificate-authority-data": "ephemeral-ca"}
    argv = launch.ssh_args(Path("/tmp/isolated/ssh.config"), 16443)
    assert argv[argv.index("-L") + 1] == "127.0.0.1:16443:127.0.0.1:6443"
    assert all(key in argv for key in ("-oExitOnForwardFailure=yes", "-oControlPath=none", "-oForwardAgent=no"))
    assert launch.ssh_args(Path("/tmp/isolated/ssh.config"), 16443, vm=launch.CACHE_VM)[-1] == "lima-lp-cache-check"


@pytest.mark.parametrize("state", ["read_only", "identity_changed", "campaign", "cached_model_check"])
def test_private_config_and_owned_tunnel_cleanup(tmp_path, monkeypatch, guest, state, capsys):
    home = tmp_path / "lima"
    cached = state == "cached_model_check"
    selected_vm = launch.CACHE_VM if cached else launch.VM
    (home / selected_vm).mkdir(parents=True)
    proof = tmp_path / "proof.json"
    proof.touch()
    ledger = tmp_path / "ledger.csv"
    ledger.touch()
    binary = tmp_path / "codex"
    binary.touch()
    args = SimpleNamespace(lima_home=home, proof=proof, port=16443, limit=8,
                           ledger=ledger if state in {"campaign", "identity_changed"} else None,
                           cases=["F2-07"] if state == "campaign" else [],
                           cached_docker=cached, host_codex=binary if cached else None, verify_models=cached)
    steps = []
    paths = []
    def guest_config(_, *, cached_docker):
        assert cached_docker == cached
        return guest

    monkeypatch.setattr(launch, "guest_kubeconfig", guest_config)

    def load(config, proof_file):
        paths.append(config)
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
        assert proof_file == proof.resolve()
        assert json.loads(config.read_text())["users"][0]["user"]["client-key-data"] == "ephemeral-private"

        def scope():
            steps.append("check identity")
            if state == "identity_changed":
                raise launch.target.TargetError("node identity changed")

        return SimpleNamespace(check_transport=lambda: steps.append("config"), command=lambda *a, **k: "ok",
                               check_scope=scope, scope=lambda: {"namespace_uid": "isolated"},
                               require_case=lambda case: steps.append(case),
                               cli_flags=lambda: ["--isolated-kubeconfig", str(config), "--isolated-proof", str(proof)])

    @contextmanager
    def forward(config, port, *, vm):
        assert config == home.resolve() / selected_vm / "ssh.config" and port == 16443 and vm == selected_vm
        steps.append("open")
        try:
            yield SimpleNamespace(poll=lambda: None)
        finally:
            steps.append("close")

    @contextmanager
    def relay(checked, cli):
        assert cli == binary and checked.scope() == {"namespace_uid": "isolated"}
        steps.append("open relay")
        try:
            yield
        finally:
            steps.append("close relay")

    class Child:
        def run(self, command, **kwargs):
            assert "-m" in command and "evaluation.k8s_dev_restart_campaign" in command
            assert command[-4:] == ["--isolated-kubeconfig", str(paths[0]), "--isolated-proof", str(proof)]
            assert "--execute" in command and "ephemeral-private" not in command
            assert command[command.index("--wait-seconds") + 1] == "600"
            assert command[command.index("--case") + 1] == "F2-07"
            assert paths[0].is_file()
            steps.append("campaign")
            return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launch.target.Target, "load", load)
    monkeypatch.setattr(launch, "loopback", forward)
    monkeypatch.setattr(launch, "model_relay", relay)
    monkeypatch.setattr(launch, "verify_models", lambda _: steps.append("model check"))
    monkeypatch.setattr(launch.subprocess, "run", Child().run)
    if state == "identity_changed":
        with pytest.raises(launch.target.TargetError, match="identity"):
            launch.run(args)
    else:
        assert launch.run(args) == 0
    expected = ["config", "open", "check identity"]
    if state == "campaign":
        expected.extend(["F2-07", "campaign"])
    if cached:
        expected.extend(["open relay", "model check", "close relay"])
    assert steps == [*expected, "close"]
    assert not paths[0].parent.exists()
    output = capsys.readouterr().out
    assert "ephemeral-private" not in output
    if state == "read_only":
        assert json.loads(output) == {"ready": True, "isolated_target": {"namespace_uid": "isolated"}}
    if cached:
        assert json.loads(output)["model_round_trips_verified"] is True
