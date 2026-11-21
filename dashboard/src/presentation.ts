import type { Incident } from './api.ts';
import { EVENT_REASON_KO, incidentStatusKo } from './labels.ts';

// A missing reading must not promote a technical record into the primary UI.
// This is a display boundary, not HTML sanitization: React still escapes all text.
const TECHNICAL = /(?:https?:\/\/|\b(?:kubectl|curl|wget|Traceback|Exception|Error:|sha256|resourceVersion)\b|\b\d{1,3}(?:\.\d{1,3}){3}\b|[a-f\d]{32,}|[{}]|<\/?[a-z]|\b\w*(?:Error|Exception)\b|HTTP\/\d|\bat \S+\([^)]*:\d+|\n\s*(?:at |File |[{$]))/i;
export function summaryKo(value: unknown, fallback: string, max = 240): string {
  if (typeof value !== 'string') return fallback;
  // Older Application summaries appended their deduplication key to an otherwise readable symptom.
  const text = value.replace(/(?:^|[\s·])사건\s*서명\s*[:：]?\s*[a-f0-9]{16,64}\.?\s*$/i, '').trim();
  return text && text.length <= max && /[가-힣]/.test(text) && !TECHNICAL.test(text) ? text : fallback;
}
export function incidentSymptom(i: Pick<Incident, 'signal_reasons' | 'title_ko'> & {workload?: string}): string {
  const reasons = [...new Set((i.signal_reasons ?? []).map(r => EVENT_REASON_KO[r]).filter(Boolean))];
  if (reasons.length) return reasons.slice(0, 2).join(' · ');
  const title = summaryKo(i.title_ko, '증상 미기록', 100);
  if (i.workload) {
    const resource = title.match(/ · (?:App|Deployment|StatefulSet|DaemonSet|Pod|Service)\/(.+)$/);
    if (resource && resource[1] === i.workload) return title.slice(0, resource.index);
  }
  return title;
}
export function nextActionKo(i: Pick<Incident, 'status' | 'handoff' | 'recovery_kind'>): string {
  if (i.status === 'handed_off') return i.handoff?.claimed_by ? '담당자가 인수했습니다. 원인을 확인하고 필요한 후속 작업을 지시하세요.' : '담당자 인수가 필요합니다. 인수한 뒤 필요한 후속 작업을 지시하세요.';
  if (i.status === 'action_pending') return '대상과 변경 영향·부작용을 확인하고 조치를 승인하거나 거절하세요.';
  if (i.status === 'failed') return '자동 처리가 실패했습니다. 기록을 확인하고 후속 작업을 지시하세요.';
  if (i.status === 'closed') return '운영자가 종료한 사건입니다. 같은 증상이 다시 감지되면 새 사건으로 접수됩니다.';
  if (i.status === 'resolved') return i.recovery_kind === 'observed' || i.handoff?.observed_recovery ? '정상 상태를 확인했습니다. 원인은 미확정이며 AI 조치는 없었습니다. 재발 여부를 확인하세요.' : '자동 복구가 완료되었습니다. 서비스 상태를 확인하고 필요하면 후속 작업을 지시하세요.';
  if (i.status === 'acting') return '조치와 결과 확인이 진행 중입니다. 완료 상태를 확인하세요.';
  return '분석 중입니다. 공개 답변을 확인하거나 조사 방향을 지시할 수 있습니다.';
}
export function publicAnswer(text: string): string {
  let reading = text.trim();
  if (/^[\[{]/.test(reading)) {
    try {
      const result = JSON.parse(reading);
      reading = [result.summary_ko, result.summary, result.answer_ko, result.answer].find(v => typeof v === 'string') ?? '';
    } catch {
      // Only a named public reading may stream out of incomplete JSON. Never
      // expose the provider envelope, arbitrary fields or malformed escapes.
      const partial = /"(?:summary_ko|summary|answer_ko|answer)"\s*:\s*"((?:\\.|[^"\\])*)/.exec(reading)?.[1];
      if (!partial) return '';
      try { reading = JSON.parse(`"${partial}"`); } catch { return ''; }
    }
  }
  if (TECHNICAL.test(reading)) return '';
  return reading.length > 1600 ? `${reading.slice(0, 1600)}…` : reading;
}
export const executionStageKo = (kind: string, phase?: string, status?: string) => kind === 'agent_started' ? '분석 요청을 시작했습니다.' : kind === 'agent_finished' ? status === 'succeeded' ? '분석 응답을 확인했습니다.' : '분석이 종료되었습니다. 결과 확인이 필요합니다.' : ({ started: '모델에 요청 중입니다.', waiting: '공개 답변을 기다리고 있습니다.', responding: '공개 답변 수신 중입니다.', succeeded: '공개 답변 수신 완료', failed: '응답 수신에 실패했습니다.' } as Record<string, string>)[phase ?? ''] ?? '진행 상태를 확인하고 있습니다.';
export { incidentStatusKo };
