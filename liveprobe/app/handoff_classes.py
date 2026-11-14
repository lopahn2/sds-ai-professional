"""운영자 이관의 두 실질 경계(2026-11-13 합의, docs/operations/liveprobe-real-operation-dev-prd-2026-11-13.md).

1. 권한·데이터 안전 경계(authority_boundary): 허용 범위 밖 변경, 데이터 손실 위험, 소스 출처 미확인,
   인증/권한 제공 필요, 이미 시작한 쓰기의 결과 불명.
2. 제한된 자율 복구 소진(bounded_recovery_exhausted): 재수집·재시도·상위 모델·재검증을 정해진 횟수·비용·시간 안에서
   수행했지만 안전한 복구를 증명하지 못함.

세부 원인 코드(reason_code)는 바꾸지 않고 그대로 보존하며 분류만 덧붙인다. 분류는 권한을 새로 주지 않는다.
결과 불명 쓰기는 언제나 1번 경계다. 표에 없는 원인 코드는 추정하지 않고 1번 경계로 닫는다(fail closed).
실제 복구 시도가 기록되지 않은 경우 `recovery_attempted=False`로 남겨 복구를 시도한 것처럼 꾸미지 않는다.

순수 모듈: DB·네트워크·제품 설정을 import하지 않는다(K8s·Application 두 제품이 함께 쓴다).
"""
from __future__ import annotations

from typing import Any, Mapping

SCHEMA_VERSION = 'handoff-class/v1'
AUTHORITY_BOUNDARY = 'authority_boundary'
RECOVERY_EXHAUSTED = 'bounded_recovery_exhausted'
CLASSES = (AUTHORITY_BOUNDARY, RECOVERY_EXHAUSTED)
LABEL_KO = {AUTHORITY_BOUNDARY: '권한·데이터 안전 경계', RECOVERY_EXHAUSTED: '제한된 자율 복구 소진'}


def classify(reason_code: str | None, table: Mapping[str, str], *, unknown_write: bool = False) -> tuple[str, str]:
    """(class, basis). basis: unknown_write | mapped | unmapped_fail_closed."""
    if unknown_write:
        return AUTHORITY_BOUNDARY, 'unknown_write'
    cls = table.get(reason_code or '')
    if cls in CLASSES:
        return cls, 'mapped'
    return AUTHORITY_BOUNDARY, 'unmapped_fail_closed'


def record(reason_code: str | None, table: Mapping[str, str], *, attempts: list[dict] | None = None,
           budget: Any = None, confidence: Any = None, authorization: Any = None, unknown_write: bool = False,
           provenance: Any = None) -> dict:
    """정제된 이관 분류 레코드. 호출자는 원문 예외·비밀값·프롬프트를 넣지 않는다."""
    cls, basis = classify(reason_code, table, unknown_write=unknown_write)
    attempts = list(attempts or [])
    return {'schema': SCHEMA_VERSION, 'class': cls, 'label_ko': LABEL_KO[cls], 'basis': basis,
            'reason_code': reason_code, 'recovery_attempted': bool(attempts), 'attempts': attempts,
            'budget': budget, 'confidence': confidence, 'authorization': authorization,
            'unknown_write': bool(unknown_write), 'provenance': provenance}
