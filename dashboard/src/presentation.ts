import type { FlowNode, HandoffView, Incident } from './api.ts';
import { EVENT_REASON_KO, incidentStatusKo } from './labels.ts';

// A missing reading must not promote a technical record into the primary UI.
// This is a display boundary, not HTML sanitization: React still escapes all text.
const TECHNICAL = /(?:https?:\/\/|\b(?:kubectl|curl|wget|Traceback|Exception|Error:|sha256|resourceVersion)\b|\b\d{1,3}(?:\.\d{1,3}){3}\b|[a-f\d]{32,}|[{}]|<\/?[a-z]|\b\w*(?:Error|Exception)\b|HTTP\/\d|\bat \S+\([^)]*:\d+|\n\s*(?:at |File |[{$]))/i;
export function summaryKo(value: unknown, fallback: string, max = Infinity): string {
  if (typeof value !== 'string') return fallback;
  const text = value.trim();
  return text && text.length <= max && /[가-힣]/.test(text) && !TECHNICAL.test(text) ? text : fallback;
}
/** Show recorded findings, never infer a confirmed cause from the incident status. */
export function incidentCause(nodes: FlowNode[], handoff: HandoffView | null): string {
  const decision = handoff?.decisions.find(d => d.key === 'cause');
  const recorded = summaryKo(decision?.value_ko, '');
  if (recorded && !/^(한글 해석 준비 중|아래 요약 참고|판단 확인 중|원인 미확정|미기록)$/.test(recorded)) return recorded;
  for (const node of [...nodes].reverse()) {
    if (node.card?.kind !== 'agent') continue;
    const candidate = node.card.candidates.map(c => summaryKo(c.cause_ko, '')).find(Boolean);
    if (candidate) return `후보 · ${candidate} (확정 여부는 검증 결과를 확인하세요.)`;
  }
  return '아직 확정되지 않았습니다. 분석 결과와 근거를 확인하세요.';
}
/** Translate only observed warning signatures; the complete original stays folded. */
export function warningSummary(reason: string, message: string | null | undefined): string {
  const text = message ?? '';
  if (/probe failed/i.test(text)) {
    const probe = /readiness/i.test(text) ? '서비스 준비 상태' : /liveness/i.test(text) ? '프로세스 생존 상태' : '상태';
    if (/timed out|timeout|deadline exceeded/i.test(text)) return `${probe} 점검이 제한 시간 안에 응답하지 못했습니다. 응답 지연의 근본 원인은 아직 확인되지 않았습니다.`;
    if (/connection refused/i.test(text)) return `${probe} 점검 대상이 연결을 거부했습니다. 서비스가 해당 포트에서 응답하는지 확인이 필요합니다.`;
    if (/statuscode:\s*[45]\d\d/i.test(text)) return `${probe} 점검에 실패 응답이 돌아왔습니다. 애플리케이션 상태 확인이 필요합니다.`;
    return `${probe} 점검이 실패했습니다. 실패 원인은 추가 확인이 필요합니다.`;
  }
  if (/oomkilled|out of memory/i.test(text) || reason === 'OOMKilling') return '메모리 부족으로 프로세스가 종료되었습니다. 사용량과 메모리 한도 확인이 필요합니다.';
  if (/imagepull|pullimage/i.test(reason) || /failed to pull image/i.test(text)) return '실행 이미지를 가져오지 못해 시작이 지연됩니다. 이미지 이름·접근 권한·저장소 연결을 확인해야 합니다.';
  if (reason === 'BackOff') return /restarting failed container/i.test(text) ? '프로세스 실패가 반복되어 재시작을 대기하고 있습니다. 종료 원인은 추가 확인이 필요합니다.' : '실패한 작업의 재시도 간격이 늘어났습니다. 최초 실패 원인을 확인해야 합니다.';
  if (reason === 'FailedScheduling') return '파드를 배치할 노드를 찾지 못했습니다. 자원 여유와 배치 조건 확인이 필요합니다.';
  if (reason === 'FailedMount') return '저장소를 연결하지 못해 시작이 지연됩니다. 볼륨과 연결 상태 확인이 필요합니다.';
  return `${EVENT_REASON_KO[reason] ?? '경고'}가 기록되었습니다. 구체적인 원인 요약은 아직 없습니다.`;
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
  return reading;
}
export const executionStageKo = (kind: string, phase?: string, status?: string) => kind === 'agent_started' ? '분석 요청을 시작했습니다.' : kind === 'agent_finished' ? status === 'succeeded' ? '분석 응답을 확인했습니다.' : '분석이 종료되었습니다. 결과 확인이 필요합니다.' : ({ started: '모델에 요청 중입니다.', waiting: '공개 답변을 기다리고 있습니다.', responding: '공개 답변 수신 중입니다.', succeeded: '공개 답변 수신 완료', failed: '응답 수신에 실패했습니다.' } as Record<string, string>)[phase ?? ''] ?? '진행 상태를 확인하고 있습니다.';
export { incidentStatusKo };

export function resultSummary(value: unknown): string {
  if (!value || typeof value !== 'object') return summaryKo(value, '결과 요약 미기록');
  const result = value as Record<string, unknown>;
  return summaryKo(result.summary_ko, result.ok === true ? '성공으로 기록되었습니다.' : result.ok === false ? '실패로 기록되었습니다. 후속 확인이 필요합니다.' : '결과 판정 미기록 · 원문 기록을 확인하세요.');
}
