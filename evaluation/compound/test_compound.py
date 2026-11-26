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
