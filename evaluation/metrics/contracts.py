"""과거 평가 정의·보고서 원문과 독립 재계산을 연결하는 계약."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def legacy_definition() -> dict:
    return json.loads((Path(__file__).parent / "snapshots/kan59-metrics-v1.json").read_text("utf-8"))


def definition_hash(definition: dict) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def definition_by_hash(sha256: str) -> dict:
    """Look up a fixed archive, validating content rather than trusting its name."""
    names = {
        "31a6bb63e9740b5afa6c0343d45d80a19828018cbdb152a8f121b411ce531703": "kan59-metrics-v1-31a6bb63.json",
        "c25bd41edc4805203f9be0685a420bd041e04514a471a6166a3677d3087de2f6": "kan59-metrics-v1.json",
    }
    if sha256 not in names:
        raise ValueError("보관되지 않은 평가 정의 지문이다")
    value = json.loads((Path(__file__).parent / "snapshots" / names[sha256]).read_text("utf-8"))
    if definition_hash(value) != sha256:
        raise ValueError("보관된 평가 정의의 지문이 변경되었다")
    return validate_legacy_definition(value)


def validate_legacy_definition(definition: dict) -> dict:
    frozen = legacy_definition()
    candidate = copy.deepcopy(definition)
    models = candidate.get("model_class")
    if not isinstance(models, dict) or not all(isinstance(k, str) and v in ("small", "balanced", "high")
                                              for k, v in models.items()):
        raise ValueError("과거 모델 분류 정의가 올바르지 않다")
    candidate["model_class"] = frozen["model_class"]
    if candidate != frozen:
        raise ValueError("수식·목표·정책 변경은 새 평가 정의 버전이 필요하다")
    return copy.deepcopy(definition)


def seal_legacy_report(original: bytes, definition: dict) -> dict:
    from .compute import canon
    validated = validate_legacy_definition(definition)
    report = json.loads(original)
    if report.get("definitions_sha256") != definition_hash(validated):
        raise ValueError("보고서와 보관 정의가 서로 다르다")
    body = {k: v for k, v in report.items() if k != "report_digest"}
    if hashlib.sha256(canon(body)).hexdigest() != report.get("report_digest"):
        raise ValueError("과거 보고서 원본 지문이 일치하지 않는다")
    return {"schema": "liveprobe-evaluation-archive/v1", "definition": validated,
            "original_report_utf8": original.decode("utf-8"),
            "original_sha256": hashlib.sha256(original).hexdigest()}


def reproduce_legacy_report(archive: dict, store, truth) -> dict:
    from .compute import compute, verify_report
    if archive.get("schema") != "liveprobe-evaluation-archive/v1":
        raise ValueError("지원하지 않는 과거 원본 계약이다")
    raw = archive["original_report_utf8"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != archive.get("original_sha256"):
        raise ValueError("과거 보고서 원본이 변경되었다")
    seal_legacy_report(raw, archive["definition"])
    original = json.loads(raw)
    recomputed = compute(store, truth, definition=archive["definition"])
    if verify_report(original, recomputed):
        raise ValueError("원장·정답·보관 정의의 과거 재현 결과가 다르다")
    return recomputed
