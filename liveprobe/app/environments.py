"""활성 업무 환경과 퇴역(이력 전용) 환경의 단일 정의. 2026-11-13 dev/prd 전환 합의.

- 활성 환경(dev, prd)만 감시·수집·조치·배포·승격 대상이다.
- 퇴역 환경(stg)은 과거 사건·감사·CSV·release 증거를 조회/필터할 때만 쓴다. 새 실행·쓰기 대상이 아니다.
- release protocol 버전: 기존 dev→stg→prd 기록은 `dev-stg-prd/v1`(필드 부재 포함)로 그대로 읽고,
  신규 release는 `dev-prd/v2`(dev 수정·시험·배포·업무 검증 → dev 사용자 시험 → prd 계획·승인 → 동일 digest 배포·검증)를 쓴다.

순수 모듈: DB·네트워크·설정을 import하지 않는다(두 제품과 렌더러가 함께 읽는다).
"""
from __future__ import annotations

ACTIVE_ENVIRONMENTS: tuple[str, ...] = ('dev', 'prd')
RETIRED_ENVIRONMENTS: tuple[str, ...] = ('stg',)
# 화면·CSV 필터 표시 순서(과거 승격 순서 유지).
HISTORICAL_ENVIRONMENTS: tuple[str, ...] = ('dev', 'stg', 'prd')

LEGACY_RELEASE_PROTOCOL = 'dev-stg-prd/v1'
RELEASE_PROTOCOL = 'dev-prd/v2'
PROTOCOL_STAGES = {LEGACY_RELEASE_PROTOCOL: ('dev', 'stg', 'prd'), RELEASE_PROTOCOL: ('dev', 'prd')}
# 사람 사용자 시험을 수행하는 환경(프로토콜별). prd 승인 직전 단계다.
USER_TEST_ENVIRONMENT = {LEGACY_RELEASE_PROTOCOL: 'stg', RELEASE_PROTOCOL: 'dev'}
RETIRED_CODE = 'environment_retired'


def is_active(environment: str | None) -> bool:
    return environment in ACTIVE_ENVIRONMENTS


def is_retired(environment: str | None) -> bool:
    return environment in RETIRED_ENVIRONMENTS


def is_known(environment: str | None) -> bool:
    return environment in HISTORICAL_ENVIRONMENTS


def release_protocol(release: dict | None) -> str:
    """protocol 필드가 없는 기존 release는 v1(dev→stg→prd) 기록으로 읽는다. 기록을 다시 쓰지 않는다."""
    value = (release or {}).get('protocol')
    return value if value in PROTOCOL_STAGES else LEGACY_RELEASE_PROTOCOL


def previous_stage(protocol: str, stage: str) -> str | None:
    stages = PROTOCOL_STAGES.get(protocol, ())
    if stage not in stages:
        return None
    index = stages.index(stage)
    return stages[index - 1] if index else None


def next_stage(protocol: str, stage: str) -> str | None:
    stages = PROTOCOL_STAGES.get(protocol, ())
    if stage not in stages:
        return None
    index = stages.index(stage)
    return stages[index + 1] if index + 1 < len(stages) else None
