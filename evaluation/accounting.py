"""호스트 평가용 계측: 운영 구현(liveprobe/app/accounting.py)을 그대로 쓰고 과거 모델 등급 정의만 기본값으로 넘긴다.

운영 이미지에는 이 패키지가 없으므로 구현은 liveprobe 쪽에 두고, 여기서는 파일 경로로 불러 같은 코드를 공유한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from evaluation.metrics.contracts import legacy_definition

_PATH = Path(__file__).resolve().parents[1] / "liveprobe" / "app" / "accounting.py"
_NAME = "_liveprobe_app_accounting"
_impl = sys.modules.get(_NAME)
if _impl is None:
    _spec = importlib.util.spec_from_file_location(_NAME, _PATH)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"운영 계측 구현을 찾을 수 없다: {_PATH}")
    _impl = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = _impl
    _spec.loader.exec_module(_impl)

_number = _impl._number
_sum = _impl._sum
export_runs = _impl.export_runs


def summarize_runs(rows: list[dict], *, escalation_events=None, model_class=None) -> dict:
    if model_class is None:
        return _impl.summarize_runs(rows, escalation_events=escalation_events,
                                    model_class=legacy_definition()["model_class"],
                                    model_class_source="legacy_definition")
    return _impl.summarize_runs(rows, escalation_events=escalation_events, model_class=model_class,
                                model_class_source="explicit")
