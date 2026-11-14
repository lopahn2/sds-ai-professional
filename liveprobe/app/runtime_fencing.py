"""제품 사이의 대상 소유 표식과 안전한 구성 지문 계약."""
import hashlib
import json

OWNER_ANNOTATION = 'liveprobe.io/application-owner'
LEASE_ANNOTATION = 'liveprobe.io/application-lease'
FENCE_ANNOTATION = 'liveprobe.io/application-fence'


def application_owned(deployment):
    annotations = (deployment or {}).get('metadata', {}).get('annotations') or {}
    # 만료는 이전 쓰기의 취소 증명이 아니다. 조정 후 명시 해제 전까지 차단한다.
    return any(annotations.get(key) for key in (OWNER_ANNOTATION, LEASE_ANNOTATION, FENCE_ANNOTATION))


def template_fingerprint(deployment):
    template = (deployment or {}).get('spec', {}).get('template')
    if not isinstance(template, dict):
        raise ValueError('구성 지문 대상 없음')
    return hashlib.sha256(json.dumps(template, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()
