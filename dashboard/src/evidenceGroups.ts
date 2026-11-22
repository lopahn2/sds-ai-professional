// 단계 카드의 근거: 출처 종류별로 묶고 같은 출처(ref)는 한 행으로 합친다. (pure, no React)
import type { AgentCard, EvidenceItem, RawItem } from './api.ts';
import { readingOf, type ReadingEra, type ReadingView } from './readingText.ts';
import { citedReading } from './stageEvidence.ts';

export type EvidenceRow = {
  key: string; group: string; label: string; detail: string | null;
  /** citations that point at this source (after merging duplicates) */
  count: number; roles: string[]; quotes: string[];
  at: number | null; summary: ReadingView; known: boolean; fingerprint: boolean;
};
export type EvidenceGroup = { key: string; label: string; refs: number; citations: number; rows: EvidenceRow[] };
export type EvidenceSummary = { groups: EvidenceGroup[]; refs: number; citations: number };

/** One collected Application observation, already resolved by the caller (appObservations). */
export type AppObservation = { id: string; source: string; service: string | null; at: number | null; ko: unknown; state: string | null };
/** One Application citation from a completed role. */
export type AppCitation = { ref: string; quote: string; role: string };

const K8S_GROUP: Record<string, string> = {
  '1': '배포 상태', '2': 'Pod 상태', '3': '서비스·엔드포인트', '4': '호출 관계', '5': 'Kubernetes 이벤트',
  '6': '지표', '7': '오류 로그', '8': '노드 상태', '9': '설정',
};
const APP_GROUP: Record<string, string> = { pod_log: '애플리케이션 로그', k8s_event: '리소스 이벤트', deployment: 'Pod 준비', rollout: '배포 변경' };
const LAST_GROUPS = ['수집되지 않은 근거', '출처 미확인'];
/** Fixed order after the count: what operators read first when counts tie. */
const ORDER = ['최초 장애 신호', '소스 코드', '코드 위치', '애플리케이션 로그', '오류 로그', '배포 변경', '배포 상태', 'Pod 준비', 'Pod 상태',
  '리소스 이벤트', 'Kubernetes 이벤트', '서비스·엔드포인트', '호출 관계', '지표', '노드 상태', '설정', '추가 근거 탐색', '참고 문서'];

const norm = (s: string) => s.replace(/\s+/g, ' ').trim();
const timeOf = (value: unknown) => {
  const t = typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(t) ? t : null;
};
/** Drop repeated excerpts and ones contained in a longer excerpt of the same source. */
export function uniqueQuotes(quotes: string[]): string[] {
  const clean = [...new Set(quotes.map(norm).filter(Boolean))].sort((a, b) => b.length - a.length);
  const kept: string[] = [];
  for (const q of clean) if (!kept.some(k => k.includes(q))) kept.push(q);
  return kept;
}

/** Kubernetes evidence ids: E3, E3_service and E3_service#0 are one source; E8_code_refs is code, not the node. */
export function k8sSource(ref: string, refKo?: string | null): { key: string; group: string; label: string } {
  const r = ref.trim();
  if (/^R\d+$/i.test(r)) return { key: r.toUpperCase(), group: '참고 문서', label: refKo || '참고 문서' };
  const m = r.match(/^E(\d+)(?:_([a-z_]+))?(?:#\d+)?$/i);
  if (m) {
    const n = m[1], suffix = (m[2] ?? '').toLowerCase();
    if (suffix === 'code_refs') return { key: 'E8_code_refs', group: '코드 위치', label: refKo || '코드 위치' };
    if (suffix === 'investigation' || Number(n) >= 10) return { key: 'E' + n, group: '추가 근거 탐색', label: refKo || '추가 근거 탐색' };
    const group = K8S_GROUP[n] ?? '추가 근거 탐색';
    return { key: 'E' + n, group, label: refKo || group };
  }
  return { key: r, group: '수집되지 않은 근거', label: refKo || '수집되지 않은 근거' };
}

/** Application refs: the first signal, a source file (path or file fingerprint), a collected observation or an unknown id. */
export function appSource(ref: string, observations: AppObservation[]): { key: string; group: string; label: string; detail: string | null; fingerprint: boolean; obs?: AppObservation } {
  const r = ref.trim();
  if (r === 'original:signal') return { key: r, group: '최초 장애 신호', label: '최초 장애 신호', detail: null, fingerprint: false };
  const file = r.match(/^(source|file_sha256):(.+)$/);
  if (file) {
    const path = file[2].trim();
    return { key: 'source:' + path, group: '소스 코드', label: path.split('/').pop() || path, detail: path, fingerprint: file[1] === 'file_sha256' };
  }
  const obs = observations.find(o => o.id === r);
  if (obs) {
    const group = APP_GROUP[obs.source] ?? '기타 관측';
    return { key: r, group, label: obs.service ? obs.service + ' · ' + group : group, detail: null, fingerprint: false, obs };
  }
  if (r.startsWith('followup:')) return { key: r, group: '추가 근거 탐색', label: '추가 수집 근거', detail: null, fingerprint: false };
  return { key: r, group: '출처 미확인', label: '출처 미확인 근거', detail: null, fingerprint: false };
}

function finish(rows: Map<string, EvidenceRow & { raw: string[] }>): EvidenceSummary {
  const groups = new Map<string, EvidenceGroup>();
  for (const row of rows.values()) {
    const { raw, ...clean } = row;
    clean.quotes = uniqueQuotes(raw);
    const g = groups.get(row.group) ?? { key: row.group, label: row.group, refs: 0, citations: 0, rows: [] };
    g.refs += 1; g.citations += row.count; g.rows.push(clean);
    groups.set(row.group, g);
  }
  const rank = (label: string) => LAST_GROUPS.includes(label) ? 1000 : (ORDER.indexOf(label) + 1 || 500);
  const list = [...groups.values()];
  for (const g of list) g.rows.sort((a, b) => b.count - a.count || (b.at ?? 0) - (a.at ?? 0) || a.label.localeCompare(b.label));
  list.sort((a, b) => (LAST_GROUPS.includes(a.label) ? 1 : 0) - (LAST_GROUPS.includes(b.label) ? 1 : 0) ||
    b.citations - a.citations || rank(a.label) - rank(b.label));
  return { groups: list, refs: list.reduce((n, g) => n + g.refs, 0), citations: list.reduce((n, g) => n + g.citations, 0) };
}

type QuoteWithReading = { ref: string; ref_ko: string | null; ref_known: boolean; quote: string; quote_ko?: string | null };
/** Kubernetes: quotes and reference documents from every agent run in the stage. */
export function k8sEvidence(runs: { role: string; card: AgentCard & { reading_era?: ReadingEra } }[], items: EvidenceItem[], raw: RawItem[], preparing = false): EvidenceSummary {
  const rows = new Map<string, EvidenceRow & { raw: string[] }>();
  for (const { role, card } of runs) {
    const era = card.reading_era;
    for (const q of (card.quotes ?? []) as QuoteWithReading[]) {
      const src = k8sSource(q.ref, q.ref_ko);
      const row = rows.get(src.key) ?? { key: src.key, group: src.group, label: src.label, detail: null, count: 0, roles: [], quotes: [], raw: [],
        at: null, summary: null, known: false, fingerprint: false };
      row.count += 1; row.known ||= q.ref_known;
      if (role && !row.roles.includes(role)) row.roles.push(role);
      if (q.quote) row.raw.push(q.quote);
      const matching = items.filter(i => i.ref && k8sSource(i.ref).key === src.key);
      const at = matching.map(i => timeOf(i.collected_at)).filter((t): t is number => t != null).sort((a, b) => b - a)[0] ?? null;
      if (at != null && (row.at == null || at > row.at)) row.at = at;
      if (!row.summary || !('text' in row.summary)) {
        const ko = q.quote_ko ?? citedReading({ ...q, supports_raw: '', candidate_index: null }, items, raw) ?? matching.map(i => i.summary_ko).filter(Boolean).at(-1) ?? null;
        const view = readingOf(ko, { era: era ?? (q.quote_ko !== undefined ? 'current' : undefined), preparing });
        if (view && ('text' in view || !row.summary)) row.summary = view;
      }
      rows.set(src.key, row);
    }
    for (const c of card.citations ?? []) {
      const key = 'chunk:' + c.chunk_id;
      const row = rows.get(key) ?? { key, group: '참고 문서', label: c.section || c.doc || '참고 문서', detail: c.locator || c.doc || null, count: 0, roles: [], quotes: [], raw: [],
        at: null, summary: readingOf(c.content, { era: era ?? 'current' }), known: true, fingerprint: false };
      row.count += 1;
      if (role && !row.roles.includes(role)) row.roles.push(role);
      if (c.content) row.raw.push(c.content);
      rows.set(key, row);
    }
  }
  return finish(rows);
}

/** Application: citations of completed roles in the selected round, joined to the collected observations. */
export function appEvidence(citations: AppCitation[], observations: AppObservation[]): EvidenceSummary {
  const rows = new Map<string, EvidenceRow & { raw: string[] }>();
  for (const c of citations) {
    const src = appSource(c.ref, observations);
    const row = rows.get(src.key) ?? { key: src.key, group: src.group, label: src.label, detail: src.detail, count: 0, roles: [], quotes: [], raw: [],
      at: src.obs?.at ?? null, known: src.group !== '출처 미확인', fingerprint: false,
      // Source code never enters the reading worker: the row explains itself with file, count and roles.
      summary: src.obs ? readingOf(src.obs.ko, { state: src.obs.state }) : null };
    row.count += 1; row.fingerprint ||= src.fingerprint;
    if (c.role && !row.roles.includes(c.role)) row.roles.push(c.role);
    if (c.quote) row.raw.push(c.quote);
    rows.set(src.key, row);
  }
  return finish(rows);
}

/** "코드 2 · 로그 2 · 배포 1": distinct sources per group, short names, most first. */
const SHORT: Record<string, string> = { '소스 코드': '코드', '코드 위치': '코드', '애플리케이션 로그': '로그', '오류 로그': '로그', '배포 변경': '배포', '배포 상태': '배포',
  'Pod 준비': 'Pod', 'Pod 상태': 'Pod', '리소스 이벤트': '이벤트', 'Kubernetes 이벤트': '이벤트', '서비스·엔드포인트': '서비스', '최초 장애 신호': '최초 신호',
  '참고 문서': '문서', '추가 근거 탐색': '추가 탐색', '호출 관계': '호출 관계', '지표': '지표', '노드 상태': '노드', '설정': '설정' };
export function composition(summary: EvidenceSummary): string {
  const counts = new Map<string, number>();
  for (const g of summary.groups) {
    if (LAST_GROUPS.includes(g.label)) continue;
    const name = SHORT[g.label] ?? g.label;
    counts.set(name, (counts.get(name) ?? 0) + g.refs);
  }
  return [...counts].sort((a, b) => b[1] - a[1]).map(([name, n]) => name + ' ' + n).join(' · ');
}
