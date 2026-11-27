"""Docker-free checks of the compound benchmark kit (python -m pytest evaluation/compound/test_compound.py)."""
import json
from pathlib import Path

import pytest

from evaluation.compound import bench, grade, k8s
from evaluation.compound.scenarios import SCENARIOS, Run

KEYS = json.loads((Path(__file__).parent / "answer_keys.json").read_text())


class _Stub:
    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *args, **kwargs):
        return _Stub()


def remedy_names(scenario_id: str) -> set[str]:
    run = Run(scenario_id, Path("."), events=None)
    run.apps = {"stock": _Stub(), "order": _Stub()}
    run.dbs = {"shop": _Stub(), "db0": _Stub(), "db1": _Stub()}
    return set(SCENARIOS[scenario_id]().remedies(run))


@pytest.mark.parametrize("scenario_id", ["C1", "C2", "C3"])
def test_answer_key_remedies_exist(scenario_id):
    names = remedy_names(scenario_id)
    key = KEYS[scenario_id]
    for combo in key["durable_remedies"]:
        assert set(combo) <= names
    assert set(key["insufficient_remedies"]) <= names


def test_bundle_refuses_harness_vocabulary():
    bundle = {"namespace": "shop", "events": [], "jobs": {}, "pods": {
        "stock-x": "2026-11-26T01:00:01.000000000Z INFO started lpcb-c1-stock\n"}}
    with pytest.raises(ValueError):
        bench.render(bundle, ("2026-11-26T01:00:00Z", "2026-11-26T01:10:00Z"))


def test_bundle_keeps_window_and_tail():
    lines = "".join(f"2026-11-26T01:00:{i % 60:02d}.000000000Z INFO line {i}\n" for i in range(2000))
    text = bench.render({"namespace": "shop", "events": [], "jobs": {}, "pods": {"stock-x": lines}},
                        ("2026-11-26T01:00:00Z", "2026-11-26T01:10:00Z"))
    assert "lines omitted" in text and "line 1999" in text


def test_answer_keys_never_reach_the_kit():
    rendered = "".join(json.dumps(k8s.scenario(s, "dev")[0]) for s in ("C1", "C2", "C3"))
    for word in ("leak", "PartnerLedger", "answer", "ground_truth", "LimitRange"):
        assert word not in rendered


def test_grader_screens_a_symptom_only_answer():
    result = {"answer": {"root_causes": [{"cause": "stock DB 연결 풀 고갈, liveness가 /actuator/health를 검사", "evidence": "x"}],
                         "actions": [{"step": 1, "target": "deployment/stock", "change": "livenessProbe를 tcpSocket으로 변경",
                                      "purpose": "재시작 완화"}], "verification": "x"}}
    verdict = grade.grade("C1", result)
    assert verdict["screening_verdict"] == "fail"
    assert "probe_liveness_group" in verdict["proposed_primitives"]


def test_provider_arms_mirror_each_other():
    from evaluation.compound.liveprobe_arm import ARMS
    codex, claude = ARMS["codex"], ARMS["claude"]
    assert claude["provider"] == "claude_cli" and "provider" not in codex
    assert set(codex["roles"]) == set(claude["roles"])
    assert all(r["model"].startswith("gpt-") for r in codex["roles"].values())
    assert all(r["model"].startswith("claude-") for r in claude["roles"].values())
    assert {k: r["effort"] for k, r in codex["roles"].items()} == {k: r["effort"] for k, r in claude["roles"].items()}
    opus = ARMS["claude-opus"]
    assert opus["only_model"] == "claude-opus-5-5" and opus["provider"] == "claude_cli"
    assert {r["model"] for r in opus["roles"].values()} == {"claude-opus-5-5"}
    assert {k: r["effort"] for k, r in opus["roles"].items()} == {k: r["effort"] for k, r in claude["roles"].items()}


def test_claude_baseline_prices_cache_writes_and_keeps_the_answer(monkeypatch):
    import subprocess
    from evaluation.compound import baseline
    answer = {"root_causes": [], "actions": [], "verification": "v"}
    raw = {"usage": {"input_tokens": 2, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100_000,
                     "cache_creation": {"ephemeral_1h_input_tokens": 60_000}, "output_tokens": 10_000},
           "num_turns": 1}
    seen = {}

    def fake_run(cmd, input, **kw):
        seen["cmd"], seen["req"] = cmd, json.loads(input)
        line = "RESULT " + json.dumps({"raw": raw, "answer": answer, "error": None, "wall_s": 3.0})
        return subprocess.CompletedProcess(cmd, 0, stdout="log\n" + line + "\n", stderr="")
    monkeypatch.setattr(baseline.subprocess, "run", fake_run)
    r = baseline.run("LOGS", "claude-opus-5-5", "xhigh", 60)
    assert "deploy/liveprobe-api" in seen["cmd"] and seen["req"]["model"] == "claude-opus-5-5"
    assert seen["req"]["prompt"].endswith("LOGS</logs>\n") and seen["req"]["schema"] == baseline.SCHEMA
    assert r["usage"] == {"input_tokens": 101_002, "cached_input_tokens": 1000, "cache_write_input_tokens": 100_000,
                          "cache_write_1h_input_tokens": 60_000, "output_tokens": 10_000}
    # 2 fresh + 40k 5-min writes x1.25 + 60k 1-h writes x2 at $4, 1k reads at $0.20, 10k out at $20
    expected = (2 * 4 + 40_000 * 4 * 1.25 + 60_000 * 4 * 2 + 1000 * 0.20 + 10_000 * 20) / 1e6
    assert r["estimated_usd"] == round(expected, 6) and r["answer"] == answer and r["exit_code"] == 0
