"""역할별 제한된 근거 보기와 검색·전달·실제 인용의 구별."""
import json
import re


def bounded_view(sources, rag, errors, limit):
    blocks = []
    for key, value in sources.items():
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
    # 한 출처의 긴 본문이 나중의 반대 근거·문서를 밀어내지 않게 균등 예약한다.
    share = max(1, limit // len(blocks))
    sections = []
    for title, text in blocks:
        header = title + '\n'
        available = max(0, share - len(header) - 2)
        if len(text) > available:
            keep = max(0, (available - 7) // 2)
            text = text[:keep] + '\n…생략…\n' + (text[-keep:] if keep else '')
        sections.append((header + text)[:share - 1])
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
