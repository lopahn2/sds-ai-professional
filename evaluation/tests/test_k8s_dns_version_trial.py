import json

from app.llm.base import LLMResult, Usage
from evaluation.k8s_dns_version_trial import ERROR, LEGACY, select_repair
from evaluation.live_repair import TASK


class ScriptedModel:
    def __init__(self, *operations):
        self.operations = iter(operations)
        self.packets = []

    def complete(self, role, packet, schema, model, effort, timeout):
        self.packets.append(json.loads(packet))
        return LLMResult("", next(self.operations), Usage(15, 3, 0, 1), 50, model=model, effort=effort)


def test_first_model_turn_only_gets_the_error_and_can_remove_the_legacy_arguments(tmp_path):
    path = tmp_path / "Corefile"
    path.write_text("ki10.verify.invalid:1053 { bind 127.0.0.1 }")
    provider = ScriptedModel({"tool": "source"}, {"tool": "configure", "key": "launchArguments", "value": "[]"})
    accepted, receipt = select_repair(provider, "single_astra", ERROR, tmp_path, 1053)
    assert provider.packets[0] == {"task": TASK, "initial_error_log": ERROR}
    assert provider.packets[1]["investigation"][0]["result"]["launchArguments"] == list(LEGACY)
    assert accepted is True
    assert receipt["usage"]["input_tokens"] == 30


def test_repair_cannot_modify_the_listener_or_add_unapproved_launch_arguments(tmp_path):
    provider = ScriptedModel(
        {"tool": "configure", "key": "listener", "value": '"0.0.0.0"'},
        {"tool": "configure", "key": "launchArguments", "value": '["-dns.port","53"]'},
        {"tool": "finish"},
    )
    accepted, receipt = select_repair(provider, "single_luna", ERROR, tmp_path, 1053)
    assert accepted is False
    assert [row["result"]["accepted"] for row in receipt["investigation"][:2]] == [False, False]
