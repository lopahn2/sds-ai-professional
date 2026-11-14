"""출처 확인과 의미 검토를 분리하는 공통 최종 수용 계약."""
from __future__ import annotations
import re


def claim_quote_related(cause, quote):
    """명백히 무관한 상태 인용을 거른다. 일반 인과성 판정의 대체물이 아니다."""
    cause, quote = str(cause).lower(), str(quote).lower()
    families = (
        (('oom', '메모리 부족', 'out of memory'), ('oom', 'memory', '메모리')),
        (('imagepull', '이미지 풀', '이미지 다운로드'), ('imagepull', 'errimage', 'pull', 'manifest')),
        (('crashloop',), ('crash', 'backoff', 'exit', 'exception')),
        (('selector', '선택자'), ('selector', 'match', 'endpoint', '선택자')),
        (('latency', '지연'), ('latency', 'timeout', '지연', 'pool')),
    )
    for causes, anchors in families:
        if any(token in cause for token in causes):
            return any(token in quote for token in anchors)
    tokens = re.findall(r'[a-z0-9_]{4,}|[가-힣]{2,}', cause)
    return any(token in quote for token in tokens)


def assess(store, primary: dict, verifier: dict, floor: float, *, other_messages=()) -> dict:
    """상위 후보의 독립 지지 없이 부분 후보를 자동 조치로 승격하지 않는다."""
    from .agent_groups import check_citations, _norm
    primary, verifier = primary or {}, verifier or {}
    report = check_citations(store, primary)
    review = check_citations(store, verifier)
    confidence = min(primary.get('confidence', 0), verifier.get('confidence', 0))
    candidates = primary.get('root_cause_candidates') or []
    verdicts = verifier.get('candidate_verdicts') or []
    by_index = {}
    duplicates = set()
    for row in verdicts:
        index = row.get('index')
        if index in by_index:
            duplicates.add(index)
        by_index[index] = row.get('verdict')
    accepted = []
    for index, candidate in enumerate(candidates):
        # 인용문의 존재는 인과성의 증명이 아니다. 검토자의 별도 주장 연결도 요구한다.
        keys = {store.key(r) for r in candidate.get('evidence_refs') or []}
        linked = any(store.key(q.get('ref', '')) in keys and
                     _norm(q.get('supports') or '') == _norm(candidate.get('cause') or '')
                     and claim_quote_related(candidate.get('cause'), q.get('quote'))
                     for q in verifier.get('evidence') or [])
        if (index in report.grounded and index not in duplicates and by_index.get(index) == 'supported'
                and linked and candidate.get('confidence', 0) >= floor):
            accepted.append(index)
    messages = [primary, verifier, *(m for m in other_messages if m)]
    counter = list(dict.fromkeys(str(c) for m in messages for c in m.get('counter_evidence') or []))
    reasons = []
    if not primary or not verifier:
        reasons.append('독립 분석 또는 검증 결과 없음')
    if not report.clean or not review.clean or not review.verified_quotes:
        reasons.append('출처·원문 인용 검증 실패')
    if any(q.get('ref') == 'E8_code_refs' and q.get('why')
           for q in [*report.forged_quotes, *review.forged_quotes]):
        reasons.append('remediation 원문 위치·재현 계약 검증 실패')
    if confidence < floor:
        reasons.append('최종 신뢰도 하한 미달')
    if 0 not in accepted:
        reasons.append('최상위 후보의 독립 지지 또는 주장별 인용 부족')
    if verifier.get('verdict') != 'supported':
        reasons.append('독립 검증 미지지')
    if any(m.get('_acceptance_rejected') for m in messages):
        reasons.append('승격 후에도 미해결인 역할 존재')
    if any(m.get('verdict') == 'contradicted' for m in messages[2:]):
        reasons.append('미해결 반대 근거 존재')
    return {'accepted': not reasons, 'candidate_indexes': accepted, 'confidence': confidence,
            'reasons': reasons, 'counter_evidence': counter, 'primary_report': report, 'review_report': review}
