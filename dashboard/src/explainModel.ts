// P20 · 관제실이 "왜 이 구성이었고 무엇을 근거로 판단했는가"를 보여 주기 위한 계산 (pure, no React).
import type { AgentCard, FlowNode } from './api.ts';

export type Usage = {
  calls: number; byModel: [string, number][];
  tokensIn: number | null; tokensOut: number | null; durationMs: number | null;
};
const observed = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v);
/** 값이 하나라도 빠지면 합계는 미관측이다. 부분 합계를 전체처럼 보이지 않는다. */
const sumAll = (values: (number | null | undefined)[]) => values.length && values.every(observed) ? values.reduce<number>((a, b) => a + (b as number), 0) : null;

/** 실제로 실행된 모델 호출(기록 ID가 있는 노드)만 센다. 예정·건너뜀 단계는 호출이 아니다. */
export function usageSummary(nodes: FlowNode[]): Usage {
  const runs = nodes.filter(n => n.agent_run_id != null);
  const counts = new Map<string, number>();
  for (const n of runs) { const m = n.model || '모델 미기록'; counts.set(m, (counts.get(m) ?? 0) + 1); }
  return {
    calls: runs.length,
    byModel: [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
    tokensIn: sumAll(runs.map(n => n.tokens?.in)), tokensOut: sumAll(runs.map(n => n.tokens?.out)),
    durationMs: sumAll(runs.map(n => n.duration_ms)),
  };
}

export type QuoteSummary = { total: number; known: number; unknownRefs: string[]; sources: { label: string; count: number }[] };
/** 인용 요약: 수집된 근거로 확인되는 인용 수와, 수집되지 않은 번호를 가리킨 인용. */
export function quoteSummary(card: Pick<AgentCard, 'quotes'>): QuoteSummary {
  const quotes = card.quotes ?? [];
  const sources = new Map<string, number>();
  for (const q of quotes) if (q.ref_known) { const label = q.ref_ko || q.ref; sources.set(label, (sources.get(label) ?? 0) + 1); }
  return {
    total: quotes.length, known: quotes.filter(q => q.ref_known).length,
    unknownRefs: [...new Set(quotes.filter(q => !q.ref_known).map(q => q.ref))],
    sources: [...sources].map(([label, count]) => ({ label, count })),
  };
}

const VERIFIER_KO: Record<string, string> = { supported: '근거로 지지', unconfirmed: '미확인', contradicted: '반박됨', insufficient: '근거 부족' };
export const verifierKo = (v: string | null | undefined) => v ? VERIFIER_KO[v] ?? '판정 미확인' : null;
export const verdictTone = (v: string | null | undefined): 'ok' | 'danger' | 'warn' | '' =>
  v === 'supported' ? 'ok' : v === 'contradicted' ? 'danger' : v === 'insufficient' ? 'warn' : '';

/** 후보 번호는 화면에서 1부터 센다. */
export const candidateLabel = (index: number | null | undefined) => index == null ? '후보 미지정' : `후보 ${index + 1}`;
