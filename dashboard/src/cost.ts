/** 정돈-07: one cost wording for list, detail and narrative (server contract: app/accounting.py cost_view).
 *  A real 0 stays $0; a missing cost is never added as 0; historical costs are shown as stored. */
export type CostStatus = 'observed' | 'partial' | 'unobserved' | 'no_runs' | 'invalid';
export type CostFields = { total_cost_usd: number | null; known_cost_usd?: number | null; cost_missing_runs?: number | null; cost_status?: CostStatus | null };
export const money = (value: number | null | undefined) => value == null ? '추정 불가' : `$${value.toFixed(4)}`;
export const costKo = (i: CostFields) => {
  switch (i.cost_status) {
    case 'observed': return money(i.total_cost_usd);
    case 'no_runs': return '실행 기록 없음';
    case 'partial': return `확인분 ${money(i.known_cost_usd)} · ${i.cost_missing_runs ?? '?'}회 비용 미수집`;
    case 'unobserved': return '비용 미수집';
    case 'invalid': return '비용 기록 이상';
    default: return money(i.total_cost_usd);
  }
};
export type IncidentOrigin = 'evaluation' | 'unconfirmed';
export const ORIGIN_KO: Record<IncidentOrigin, string> = { evaluation: '평가 실행', unconfirmed: '출처 미확인' };
