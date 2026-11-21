import type {AgentQuote, EvidenceItem, Incident, RawItem} from './api.ts';

const questions: Record<string,string> = {
  deployment:'배포 준비', pod_status:'Pod 준비', service:'연결 대상', endpoints:'연결 대상',
  k8s_event:'상태 변화', prometheus:'서비스 지표', loki:'오류 로그',
  node:'노드 상태', configmap:'설정 변경', code_refs:'코드 위치',
};
export function evidenceQuestion(item: EvidenceItem) {
  return questions[item.source] ?? item.source_ko ?? '기타 관측';
}
export function chronological(items: EvidenceItem[]) {
  const time = (it: EvidenceItem) => Date.parse(it.collected_at ?? '') || Infinity;
  return [...items].sort((a,b) => time(a) - time(b) || a.id - b.id);
}
export function latestServiceReadings(i: Incident, items: EvidenceItem[]) {
  return ['Service','Deployment'].flatMap(kind => {
    const candidates = items.filter(it => it.ok && it.source === kind.toLowerCase() &&
      it.target === i.namespace + '/' + kind + '/' + i.workload && Number.isFinite(Date.parse(it.collected_at ?? '')));
    return chronological(candidates).slice(-1);
  });
}
/** A recurring source id can refer to many observations. Only attach the
 * reading of a row that contains the verbatim cited fragment. */
export function citedReading(quote: AgentQuote, items: EvidenceItem[], raw: RawItem[]) {
  if (!quote.ref_known || !quote.quote.trim()) return null;
  const norm = (s: string) => s.replace(/\s+/g,' ').trim();
  const fragment = norm(quote.quote);
  const candidates = items.filter(i => i.ref === quote.ref).flatMap(i => i.raw_items ?? []);
  for (const row of [...candidates, ...raw]) {
    if (row.ref === quote.ref && row.ko && row.raw && norm(row.raw).includes(fragment)) return row.ko;
  }
  return null;
}
