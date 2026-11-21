// R1 · 운영자가 "지금 내가 해야 할 일"을 먼저 보도록 진행 중 장애를 정렬한다 (pure, no React).
import type { Incident, IncidentCounts, OpenIncident } from './api.ts';

export type QueueTone = 'danger' | 'warning' | 'info' | 'neutral';
export type QueueItem = {
  id: number; workload: string; status: string; severity_level: string | null; created_at: string;
  environment?: string | null; system?: string | null; namespace: string; claimed: boolean;
};
type Source = (Incident | OpenIncident) & { handoff?: Record<string, unknown> | null };

const LEVEL_RANK: Record<string, number> = { HIGH_RISK: 0, COMPLEX: 1, NORMAL: 2, SIMPLE: 3 };
/** 승인·인수처럼 운영자 결정이 없으면 멈춰 있는 상태만 '대응 필요'다. 분석·조치 중은 AI가 진행한다. */
export const needsOperator = (status: string, claimed = false) => status === 'recovery_review' || status === 'action_pending' || (status === 'handed_off' && !claimed);
const STATUS_RANK = (status: string, claimed: boolean) =>
  status === 'action_pending' ? 0 : status === 'handed_off' && !claimed ? 1 : status === 'acting' ? 2 : status === 'analyzing' ? 3 : status === 'open' ? 4 : 5;

/** 목록·지도·관제실이 같은 말을 쓰도록 한 곳에서 정한 짧은 다음 행동. */
export function nextStepShort(status: string, claimed = false): { label: string; tone: QueueTone } {
  if (status === 'recovery_review') return { label: '소명 확인 대기', tone: 'warning' };
  if (status === 'action_pending') return { label: '승인·거절 필요', tone: 'danger' };
  if (status === 'handed_off') return claimed ? { label: '후속 지시 가능', tone: 'neutral' } : { label: '인수 필요', tone: 'warning' };
  if (status === 'acting') return { label: '조치 실행 중', tone: 'info' };
  if (status === 'analyzing' || status === 'open') return { label: 'AI 분석 중', tone: 'info' };
  if (status === 'failed') return { label: '기록 확인 필요', tone: 'warning' };
  return { label: '기록 열람', tone: 'neutral' };
}

/** 실시간 목록과 운영 관제 응답을 합쳐 한 사건은 한 번만, 운영자 결정이 필요한 순으로 내보낸다. */
export function responseQueue(live: Source[], overviewOpen: Source[] = []): QueueItem[] {
  const byId = new Map<number, QueueItem>();
  // 실시간 목록이 더 최신이므로 나중에 넣어 같은 사건을 덮어쓴다.
  for (const i of [...overviewOpen, ...live]) {
    byId.set(i.id, {
      id: i.id, workload: i.workload, status: i.status, severity_level: i.severity_level ?? null, created_at: i.created_at,
      environment: i.environment, system: i.system, namespace: i.namespace, claimed: Boolean(i.handoff?.claimed_by),
    });
  }
  return [...byId.values()].sort((a, b) =>
    STATUS_RANK(a.status, a.claimed) - STATUS_RANK(b.status, b.claimed)
    || (LEVEL_RANK[a.severity_level ?? ''] ?? 9) - (LEVEL_RANK[b.severity_level ?? ''] ?? 9)
    || b.id - a.id);
}

export type QueueCounts = { approval: number | null; handoff: number | null; inProgress: number | null };
export function currentResponse(queue: QueueItem[], observed: boolean) {
  if (!observed) return { services: null, automatic: null, approval: null };
  const automatic = queue.filter(row => ['open', 'analyzing', 'acting'].includes(row.status)).length;
  const approval = queue.filter(row => row.status === 'action_pending').length;
  const services = new Set(queue.filter(row => ['open', 'analyzing', 'acting', 'action_pending'].includes(row.status))
    .map(row => JSON.stringify([row.environment ?? null, row.system ?? null, row.namespace, row.workload]))).size;
  return { services, automatic, approval };
}
/** 서버 집계를 우선한다. 없으면 완전히 조회된 실시간 목록에서만 세고, 그것도 없으면 미관측(null)이다. */
export function queueCounts(counts: IncidentCounts | undefined | null, live: Pick<Incident, 'status'>[], liveReady: boolean): QueueCounts {
  const fromLive = (status?: string) => liveReady ? live.filter(i => !status || i.status === status).length : null;
  return {
    approval: counts?.awaiting_approval ?? fromLive('action_pending'),
    // 미인수 이관은 진행 중 목록에 없는 누적 값이라 서버 집계가 없으면 추정하지 않는다.
    handoff: counts?.unclaimed_handoff ?? null,
    inProgress: counts?.in_progress ?? fromLive(),
  };
}
