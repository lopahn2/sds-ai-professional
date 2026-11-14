import type { FlowNode, Incident } from './api.ts';
import { ACTION_KO, INCIDENT_STATUS_KO } from './labels.ts';
import { summaryKo } from './presentation.ts';

/** Finished incident results. Each one is an existing server `status` value; nothing here is inferred from text. */
const RESULT_TIME: Record<string, { at: string; until: string }> = {
  resolved: { at: '복구 완료', until: '복구까지' }, closed: { at: '종료', until: '종료까지' },
  failed: { at: '실패 기록', until: '실패까지' }, handed_off: { at: '인계', until: '인계까지' },
};
export const isResultStatus = (status: string) => Object.hasOwn(RESULT_TIME, status);

const parse = (value: string | null | undefined) => value ? Date.parse(value) : NaN;
/** A missing or unreadable timestamp is reported as unrecorded, never as "Invalid Date" or a guessed time. */
export function whenKo(value: string | null | undefined): string {
  const t = parse(value);
  return Number.isFinite(t) ? new Date(t).toLocaleString('ko-KR') : '시각 미기록';
}
/** Elapsed time between two recorded timestamps; null when either is missing or the order is impossible. */
export function spanKo(from: string | null | undefined, to: string | null | undefined): string | null {
  const a = parse(from), b = parse(to);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b < a) return null;
  const mins = Math.floor((b - a) / 60000);
  return mins < 60 ? `${mins}분` : mins < 1440 ? `${Math.floor(mins / 60)}시간 ${mins % 60}분` : `${Math.floor(mins / 1440)}일`;
}

export type HistoryResult = { status: string; label: string; detail: string | null; timeLabel: string; at: string; duration: string | null; detected: string };
type ResultIncident = Pick<Incident, 'status' | 'created_at' | 'closed_at' | 'handoff' | 'recovery_kind'> & { status_reason_ko?: string | null };
const K8S_NAME = /^[\w.\-/:]{1,120}$/;
/** A server reading may embed a raw fragment (e.g. "실패(<error text>)"). Drop non-Korean parentheses and reject
 *  readings that still carry English prose, so the primary result never shows untranslated evidence. */
function plainKo(value: unknown, max: number): string {
  const text = typeof value === 'string' ? value.replace(/\s*\([^()가-힣]*\)/g, '') : value;
  const s = summaryKo(text, '', max);
  return /[A-Za-z]{2,}(?:[\s,:;]+[A-Za-z]{2,}){2,}/.test(s) ? '' : s;
}

/** The recorded outcome of a finished incident, from structured fields only (status, closed_at, action cards, close record).
 *  Returns null for incidents that are still in progress (or were stored while in progress). */
export function historyResult(i: ResultIncident, nodes: FlowNode[] = [], snapshot = false): HistoryResult | null {
  const observed = i.status === 'resolved' && (i.recovery_kind === 'observed' || (i.handoff?.observed_recovery as { ai_remediation?: boolean } | undefined)?.ai_remediation === false);
  const t = observed ? { at: '정상 확인', until: '정상 확인까지' } : RESULT_TIME[i.status];
  if (!t) return null;
  const h = (i.handoff ?? {}) as { closed_at?: unknown; close_reason?: unknown; claimed_by?: unknown };
  const end = i.closed_at ?? (i.status === 'closed' && typeof h.closed_at === 'string' ? h.closed_at : null);
  let detail: string | null = null;
  if (observed) {
    detail = '원인 미확정 · AI 조치 없음';
  } else if (i.status === 'resolved') {
    // Only an action recorded as succeeded is named; otherwise the result stays the status alone.
    const card = [...nodes].reverse().map(n => n.card).find(c => c?.kind === 'action' && c.status === 'succeeded');
    if (card?.kind === 'action') detail = [ACTION_KO[card.type] ?? summaryKo(card.type_ko, '조치', 60),
      typeof card.target === 'string' && K8S_NAME.test(card.target) ? card.target : '', summaryKo(card.status_ko, '', 60)].filter(Boolean).join(' · ');
  } else if (i.status === 'closed') {
    const reason = plainKo(h.close_reason, 120);
    detail = reason || null;
  } else if (i.status === 'failed') {
    const reason = plainKo(i.status_reason_ko, 160);
    detail = reason || null;
  } else {
    detail = h.claimed_by ? '담당자 인수 완료' : snapshot ? '저장 당시 미인수' : '담당자 인수 대기';
  }
  const span = spanKo(i.created_at, end);
  return { status: i.status, label: observed ? '정상 상태 확인' : INCIDENT_STATUS_KO[i.status], detail, timeLabel: t.at, at: whenKo(end), duration: span ? `${t.until} ${span}` : null, detected: whenKo(i.created_at) };
}

/** Impact sentence for the headline. A finished or stored incident never says that collection is still under way. */
export function impactKo(text: unknown, recorded: boolean): string {
  const s = summaryKo(text, '');
  if (!recorded) return s || '영향 범위를 확인 중입니다.';
  if (!s) return '기록된 영향 정보가 없습니다.';
  return s.replace(/\s*영향 범위는 근거 수집 후 표시됩니다\.?/, ' 영향 범위는 기록되지 않았습니다.').trim();
}
