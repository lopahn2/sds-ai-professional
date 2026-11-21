"""역할별 제한된 근거 보기와 검색·전달·실제 인용의 구별."""
import json
import re


def bounded_view(sources, rag, errors, limit):
    blocks = []
    for key, value in sources.items():
        if key.split('_', 1)[0] == 'E1' and isinstance(value, dict) and 'revision_context' in value:
            # JSONB may reorder stored keys. Pin collected revision facts before abridging this source.
            value = {'revision_context': value['revision_context'],
                     **{k: v for k, v in value.items() if k != 'revision_context'}}
        if isinstance(value, list):
            value = sorted(value, key=lambda item: str(item.get('observed_at') or item.get('last')
                           or item.get('timestamp') or item.get('at') or '')
                           if isinstance(item, dict) else str(item))
        blocks.append((key, json.dumps(value, ensure_ascii=False, default=str)))
    if errors:
        blocks.insert(0, ('수집 실패', json.dumps(errors, ensure_ascii=False)))
    for ref, hit in rag.items():
        blocks.append((f'[{ref}] source={hit.get("source")} 위치={hit.get("locator") or hit.get("chunk_ix")}',
                       str(hit.get('content') or '')))
    if not blocks:
        return '관측 근거 없음'[:limit]
    # 짧은 출처가 쓰지 않는 몫을 재배분한다. 조치 경계와 파드별 상태는
    # 상대적으로 큰 몫을 주되, 다른 출처와 뒤쪽 반대 근거도 계속 예약한다.
    remaining = max(0, limit - len(blocks) + 1)
    sizes = [len(title) + 1 + len(text) for title, text in blocks]
    weights = [2 if title.split('_', 1)[0] in ('E1', 'E2') else 1 for title, _ in blocks]
    budgets = [0] * len(blocks)
    pending = set(range(len(blocks)))
    while pending and remaining:
        total_weight = sum(weights[i] for i in pending)
        complete = [i for i in pending if sizes[i] * total_weight <= remaining * weights[i]]
        if not complete:
            for i in sorted(pending):
                budgets[i] = remaining * weights[i] // total_weight
            break
        for i in complete:
            budgets[i] = sizes[i]
            remaining -= sizes[i]
            pending.remove(i)
    sections = []
    for (title, text), share in zip(blocks, budgets):
        header = title + '\n'
        available = max(0, share - len(header))
        if len(text) > available:
            keep = max(0, (available - 7) // 2)
            text = text[:keep] + '\n…생략…\n' + (text[-keep:] if keep else '')
        sections.append((header + text)[:share])
    return '\n'.join(sections)[:limit]


def rag_usage(hits, prompt, output):
    from .agent_groups import _norm
    retrieved = [h['ref'] for h in hits if h.get('ref')]
    normalized_prompt = _norm(prompt)
    delivered = []
    for hit in hits:
        body = _norm(hit.get('content') or '')
        # A reference header is metadata, not proof that even a body excerpt was shown.
        width = min(16, len(body))
        if (hit.get('ref') and width >= 4
                and re.search(r'\[' + re.escape(hit['ref']) + r'\]', prompt)
                and (body[:width] in normalized_prompt or body[-width:] in normalized_prompt)):
            delivered.append(hit['ref'])
    cited = []
    for hit in hits:
        ref = hit.get('ref')
        if ref not in delivered:
            continue
        if any(q.get('ref') == ref and len(_norm(q.get('quote') or '')) >= 4
               and _norm(q.get('quote') or '') in normalized_prompt
               and _norm(q.get('quote') or '') in _norm(hit.get('content') or '')
               for q in (output or {}).get('evidence') or []):
            cited.append(ref)
    return {'retrieved': retrieved, 'delivered': delivered, 'cited': cited}
