"""P20 모의 fixture 보강: 관제실 설명 필드(인용·반대 근거·후보별 판정·승격 사유·참고 문서)를 계약 모양으로 채운다.

값의 출처:
- 승격 사유(escalation / escalated_from): 같은 fixture events의 `escalated` 기록 그대로.
- 인용문(quotes): 실제 에이전트 출력 기록이 fixture에 없어, **같은 fixture의 원문 근거(raw)에서 잘라 만든 모의 값**이다.
  E99_unknown 한 건은 "수집되지 않은 근거 번호" 표시를 시험하기 위한 모의 값이며 승격된 실행에만 넣는다.
- 반대 근거·후보별 판정: 같은 카드의 기존 후보·판정·한글 결론에서 만든다.
- 참고 문서(citations): 저장소의 판단 참고자료 문서 이름을 쓴 모의 값(복구 계획 단계 1건).
한 번 더 실행해도 같은 결과(멱등). 실행: python3 e2e/enrich-p20-fixtures.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent / 'fixtures'
ESC_KO = [(r'(\d+) quote\(s\) not found', lambda m: f'인용문 {m.group(1)}건이 원본 근거에서 확인되지 않았습니다.'),
          (r'contradicted by reviewer', lambda m: '검토 Agent가 결론을 반박했습니다.'),
          (r'confidence ([0-9.]+) below ([0-9.]+)', lambda m: f'신뢰도 {m.group(1)}이(가) 기준 {m.group(2)}보다 낮습니다.')]
VERDICT_KO = {'supported': '근거로 지지', 'contradicted': '반박됨', 'insufficient': '근거 부족'}


def reason_ko(reason: str) -> str:
    for pattern, fn in ESC_KO:
        m = re.search(pattern, reason or '')
        if m:
            return fn(m)
    return '근거 검사를 통과하지 못했습니다.'


def excerpt(raw: str, needle: str, width: int = 90) -> str | None:
    i = raw.find(needle)
    return raw[max(0, i - 10): i + width].strip() if i >= 0 else None


def enrich(iid: int) -> None:
    path = ROOT / f'narrative-{iid}.json'
    d = json.loads(path.read_text(encoding='utf-8'))
    events = json.loads((ROOT / f'events-{iid}.json').read_text(encoding='utf-8'))
    events = events.get('items') or events.get('events') or []
    nodes = {n['id']: n for n in d['flow']['nodes']}
    for n in nodes.values():
        n.pop('escalation', None)
        n.pop('escalated_from', None)
    queue: dict[str, list[dict]] = {}
    for e in events:
        if e['kind'] == 'escalated':
            queue.setdefault(e['detail'].get('role'), []).append(e['detail'])
    for edge in d['flow']['edges']:
        src, dst = nodes.get(edge['source']), nodes.get(edge['target'])
        if edge['kind'] != 'escalate' or not src or not dst or not queue.get(src.get('role')):
            continue
        det = queue[src['role']].pop(0)
        info = {'reason_raw': det.get('reason') or '', 'reason_ko': reason_ko(det.get('reason') or ''),
                'from_model': det.get('from_model'), 'from_effort': det.get('from_effort'),
                'to_model': det.get('to_model'), 'to_effort': det.get('to_effort')}
        src['escalation'], dst['escalated_from'] = info, info

    labels = {}
    ev = nodes.get('evidence', {}).get('card') or {}
    for item in ev.get('items') or []:
        if item.get('ref'):
            labels.setdefault(item['ref'], item.get('source_ko') or item['source'])
    raws = {}
    for r in d.get('raw') or []:
        raws.setdefault(r['ref'], r['raw'])
    picks = [(ref, excerpt(raws.get(ref, ''), needle)) for ref, needle in
             (('E2_pods', '"last_exit_code"'), ('E7_error_logs', '"message"'), ('E5_recent_events', 'reason'))]
    picks = [(ref, q) for ref, q in picks if q]

    for n in nodes.values():
        c = n.get('card') or {}
        if c.get('kind') != 'agent':
            continue
        cands = c.get('candidates') or []
        for cand in cands:
            cand['evidence_refs'] = [ref for ref, _ in picks][:2]
        c['confidence'] = cands[0]['confidence'] if cands else None
        quotes = [{'ref': ref, 'ref_ko': labels.get(ref), 'ref_known': ref in labels, 'quote': q,
                   'supports_raw': cands[0]['cause_raw'] if cands else '', 'candidate_index': 0 if cands else None}
                  for ref, q in picks] if cands else []
        if n.get('escalation') and quotes:
            quotes.append({'ref': 'E99_unknown', 'ref_ko': None, 'ref_known': False, 'quote': 'memory usage above limit',
                           'supports_raw': cands[0]['cause_raw'], 'candidate_index': 0})
        c['quotes'] = quotes
        counter = []
        if n.get('role') == 'challenger' and len(cands) > 1:
            counter.append({'text_raw': cands[1]['cause_raw'], 'text_ko': None})
            if n.get('conclusion_ko'):
                counter.append({'text_raw': n['conclusion_ko'], 'text_ko': n['conclusion_ko']})
        c['counter_evidence'] = counter
        c['counter_evidence_count'] = len(counter)
        verdict = c.get('verdict')
        c['candidate_verdicts'] = ([{'index': 0, 'verdict': verdict, 'verdict_ko': VERDICT_KO.get(verdict),
                                     'reason_raw': 'verified quotes resolve to the cited sources',
                                     'reason_ko': n.get('conclusion_ko')}] if verdict in VERDICT_KO and cands else [])
        c['citations'] = ([{'chunk_id': 9001, 'doc': 'liveprobe-judgement-guide.md', 'source': 'reference', 'chunk_ix': 3, 'score': 0.71,
                            'section': '메모리 초과 종료', 'locator': '모의 표본', 'source_uri': None,
                            'content': '메모리 한도 조정은 되돌릴 수 있는 완화 조치다. 증가 원인은 별도로 조사한다. (모의 표본 발췌)'}]
                          if n.get('role') == 'recovery_planner' and cands else [])
        c['acceptance'] = None
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    for incident_id in (971, 976, 987, 990, 1976):
        enrich(incident_id)
