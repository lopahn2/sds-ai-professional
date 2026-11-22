// 분석 카드의 목표 점검. 백엔드가 실제로 쓰는 차단 규칙을 그대로 옮긴다. (pure, no React)
// Application: liveprobe/app/application/worker.py — 근거가 있는 모든 역할에서
//   fixable=false, confidence<0.8, unresolved 비어 있지 않음, accepted=false 이면 차단하고,
//   인용은 수집 근거에 글자 그대로 있어야 한다(citation_not_grounded / citations_missing).
import type { AgentCard } from './api.ts';

export type GoalState = 'ok' | 'fail' | 'working' | 'open';
export type Goal = { key: string; label: string; state: GoalState; detail: string | null };

export type AppRoleResult = { role: string; label: string; confidence?: number | null; unresolved?: unknown[] | null; accepted?: boolean | null; fixable?: boolean | null };
export type AppGoalInput = {
  /** role_completed results of the selected round, in order */
  roles: AppRoleResult[];
  /** roles still working in this round */
  running: string[];
  /** effective classification level (2+ adds the specialist) */
  level: number | null;
  /** the stop code of the round, when the analysis was stopped */
  errorCode: string | null;
  /** analysis_retry reasons recorded in the round */
  retries: string[];
};

const pct = (v: number) => Math.round(v * 100) + '%';
const ANALYSIS = ['analyst', 'specialist'];
const latestPerRole = <T extends { role: string }>(rows: T[]) => [...new Map(rows.map(r => [r.role, r])).values()];

export function appInvestigationGoals({ roles, running, level, errorCode, retries }: AppGoalInput): Goal[] {
  const latest = latestPerRole(roles);
  const analysis = latest.filter(r => ANALYSIS.includes(r.role));
  const working = (names: string[]) => names.some(n => running.includes(n));
  const goals: Goal[] = [];

  const scored = analysis.filter(r => typeof r.confidence === 'number');
  goals.push(scored.length ? {
    key: 'confidence', label: '신뢰도 80% 이상',
    state: scored.every(r => (r.confidence as number) >= 0.8) ? 'ok' : 'fail',
    detail: scored.map(r => r.label + ' ' + pct(r.confidence as number)).join(' · '),
  } : { key: 'confidence', label: '신뢰도 80% 이상', state: working(ANALYSIS) ? 'working' : 'open', detail: null });

  const reviewed = latest.filter(r => ANALYSIS.includes(r.role) || r.role === 'critic');
  const gaps = reviewed.reduce((n, r) => n + (Array.isArray(r.unresolved) ? r.unresolved.length : 0), 0);
  goals.push(reviewed.length ? { key: 'unresolved', label: '부족한 근거 0건', state: gaps ? 'fail' : 'ok', detail: gaps ? '부족한 근거 ' + gaps + '건' : null }
    : { key: 'unresolved', label: '부족한 근거 0건', state: working(ANALYSIS) ? 'working' : 'open', detail: null });

  const citationStop = !!errorCode && /^citation/.test(errorCode);
  const retried = retries.some(r => /^citation/.test(r));
  goals.push(citationStop ? { key: 'citations', label: '인용이 수집 근거와 일치', state: 'fail', detail: '인용이 수집 근거와 맞지 않아 중단' }
    : latest.length ? { key: 'citations', label: '인용이 수집 근거와 일치', state: 'ok', detail: retried ? '재시도 후 통과' : null }
    : { key: 'citations', label: '인용이 수집 근거와 일치', state: working(ANALYSIS) ? 'working' : 'open', detail: null });

  const needed = (level ?? 0) >= 2 ? ['specialist', 'critic'] : ['critic'];
  const done = needed.filter(role => latest.some(r => r.role === role));
  const objection = latest.find(r => r.role === 'critic' && (r.accepted === false || r.fixable === false))
    ?? latest.find(r => ANALYSIS.includes(r.role) && r.fixable === false);
  const pending = needed.filter(role => !done.includes(role));
  goals.push(objection ? { key: 'review', label: '반론·심층 검토', state: 'fail', detail: objection.fixable === false ? '코드 수정으로 해결할 수 없다는 판단' : '반론 검토에서 원인 판단을 받아들이지 않음' }
    : !pending.length ? { key: 'review', label: '반론·심층 검토', state: 'ok', detail: needed.map(role => latest.find(r => r.role === role)?.label ?? role).join(' · ') + ' 완료' }
    : { key: 'review', label: '반론·심층 검토', state: working(pending) ? 'working' : 'open',
        detail: working(pending) ? pending.filter(role => running.includes(role)).map(role => ROLE_LABEL[role] ?? role).join(' · ') + ' 진행 중' : null });
  return goals;
}
const ROLE_LABEL: Record<string, string> = { specialist: '심층 원인 추론', critic: '반론 검토' };

export function appVerificationGoals(verifier: AppRoleResult | undefined, running: boolean): Goal[] {
  if (!verifier || verifier.accepted == null) return [{ key: 'verdict', label: '독립 검증 수락', state: running ? 'working' : 'open', detail: null }];
  return [{ key: 'verdict', label: '독립 검증 수락', state: verifier.accepted ? 'ok' : 'fail', detail: verifier.accepted ? null : '원인 근거 반려' }];
}

export type K8sRun = { role: string | null; label: string; status: string; card: AgentCard };
export function k8sInvestigationGoals(runs: K8sRun[]): Goal[] {
  const latest = [...new Map(runs.map(r => [r.role ?? r.label, r])).values()];
  const running = runs.some(r => r.status === 'running');
  const goals: Goal[] = [];
  const rejected = latest.filter(r => r.card.acceptance?.rejected);
  goals.push(latest.length ? { key: 'accepted', label: '근거 검사 통과', state: rejected.length ? 'fail' : 'ok', detail: rejected.length ? rejected.map(r => r.label).join(' · ') + ' 반려' : null }
    : { key: 'accepted', label: '근거 검사 통과', state: running ? 'working' : 'open', detail: null });
  const quotes = latest.flatMap(r => r.card.quotes ?? []);
  const unknown = quotes.filter(q => !q.ref_known).length;
  goals.push(quotes.length ? { key: 'citations', label: '인용이 수집 근거와 연결', state: unknown ? 'fail' : 'ok', detail: unknown ? '연결되지 않은 인용 ' + unknown + '건' : quotes.length + '건 연결' }
    : { key: 'citations', label: '인용이 수집 근거와 연결', state: running ? 'working' : 'open', detail: null });
  const challenger = runs.find(r => r.role === 'challenger');
  goals.push({ key: 'review', label: '반론 검토', state: challenger ? (challenger.status === 'running' ? 'working' : 'ok') : 'open', detail: null });
  return goals;
}

export function k8sVerificationGoals(card: AgentCard | undefined, running: boolean): Goal[] {
  const verdict = card?.verdict;
  if (!verdict) return [{ key: 'verdict', label: '독립 검증 수락', state: running ? 'working' : 'open', detail: null }];
  return [{ key: 'verdict', label: '독립 검증 수락', state: verdict === 'supported' ? 'ok' : 'fail', detail: verdict === 'supported' ? null : card?.verdict_ko ?? null }];
}
