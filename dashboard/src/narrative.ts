import type { Action, FlowEdge, FlowNode, HandoffView, Incident, Narrative, RawItem } from './api.ts';
import { INCIDENT_STATUS_KO, roleKo } from './labels.ts';
import { summaryKo } from './presentation.ts';

export const RAW_FILTERS = [['all', '전체'], ['error', '오류'], ['event', '이벤트'], ['log', '로그'], ['status', '상태'], ['agent', '에이전트']] as const;
export type RawFilter = typeof RAW_FILTERS[number][0];
const humanWaitLabels:Record<string,string>={awaiting_approval:'승인 대기',awaiting_operator:'인수 대기',awaiting_review:'소명 확인 대기',awaiting_user_test:'수동 시험 대기',manual_record:'당시 수동 절차'};
export const FLOW_STATUS_KO: Record<string, string> = { ...humanWaitLabels, queued:'대기', reported:'결과 보고', unobserved:'종료 미관측', planned: '예정', running: '실행 중', done: '완료', failed: '실패', skipped: '건너뜀', escalated: '상위 모델 승격' };
/** Short status for the two-line node; the full wording stays in the accessible name and the step detail. */
export const FLOW_STATUS_SHORT: Record<string, string> = { ...humanWaitLabels, queued:'대기', reported:'결과 보고', unobserved:'종료 미관측', planned: '예정', running: '실행 중', done: '완료', failed: '실패', skipped: '건너뜀', escalated: '승격됨' };
export const FLOW_TYPE_KO: Record<string, string> = { code_action: '코드 조치', signal: '신호', evidence: '근거', router: '분류', group: '그룹', agent: '에이전트', verify: '검증', action: '조치', outcome: '결과', steer: '운영자 지시', command: '지시 실행', interpret: '한글 해석' };
export const EDGE_KIND_KO: Record<string, string> = { flow: '진행', escalate: '상위 모델 승격', reroute: '경로 변경', steer: '지시 반영', verify: '검증' };
export const STAGE_KO: Record<string, string> = { routing: '사건 수준 판단 중', collecting: '근거 수집 중', analyzing: '에이전트 분석 중', verifying: '결과 확인 중', acting: '조치 실행 중', approval: '운영자 승인 대기', resolved: '자동 복구 완료', handed_off: '운영자 인계', failed: '처리 실패', closed: '운영자 종료' };
export const flowStatusKo = (v: string) => FLOW_STATUS_KO[v] ?? '상태 미확인';
export const stageKo = (key: string, label?: string | null) => label || STAGE_KO[key] || '단계 미확인';

export function filterRaw(items: RawItem[], filter: RawFilter): RawItem[] {
  if (filter === 'all') return items;
  if (filter === 'error') return items.filter(it => it.severity === 'error' || it.severity === 'warn');
  if (filter === 'status') return items.filter(it => ['status', 'metric', 'config', 'node'].includes(it.kind));
  return items.filter(it => it.kind === filter);
}

/** An input may refer to the entire source or a specific row, never a prefix lookalike (E1 != E10). */
export function matchesRef(item: Pick<RawItem, 'id' | 'ref'>, ref: string): boolean {
  return ref === item.id || ref === item.ref;
}
export function citedByNode(item: RawItem, node: FlowNode | null | undefined): boolean {
  if (!node) return false;
  return (node.inputs ?? []).some(ref => matchesRef(item, ref)) || (!!node.role && (item.cited_by ?? []).includes(node.role));
}
export const rawForRef = (items: RawItem[], ref: string) => items.filter(it => matchesRef(it, ref));

/** One Korean reading for many identical-meaning raw lines. Raw strings and their order are kept untouched. */
export type RawGroup = { key: string; kind: string; source: string; severity: string; ko: string | null; ko_source: RawItem['ko_source']; items: RawItem[]; cited_by: string[]; first_at: string | null; last_at: string | null };
const SEVERITY_RANK: Record<string, number> = { error: 2, warn: 1, info: 0 };
export function groupRaw(items: RawItem[]): RawGroup[] {
  const out: RawGroup[] = []; const by = new Map<string, RawGroup>();
  for (const it of items) {
    // Without a reading there is nothing to merge on: every unread line stays its own group.
    const k = it.ko ? `${it.kind}\u0000${it.source}\u0000${it.ko}` : `raw\u0000${it.id}`;
    let g = by.get(k);
    if (!g) { g = { key: k, kind: it.kind, source: it.source, severity: it.severity, ko: it.ko, ko_source: it.ko_source, items: [], cited_by: [], first_at: it.at, last_at: it.at }; by.set(k, g); out.push(g); }
    g.items.push(it);
    if ((SEVERITY_RANK[it.severity] ?? 0) > (SEVERITY_RANK[g.severity] ?? 0)) g.severity = it.severity;
    for (const r of it.cited_by ?? []) if (!g.cited_by.includes(r)) g.cited_by.push(r);
    if (it.at && (!g.first_at || it.at < g.first_at)) g.first_at = it.at;
    if (it.at && (!g.last_at || it.at > g.last_at)) g.last_at = it.at;
  }
  return out;
}
/** Evidence pane groups. With a step selected, rows are filtered before grouping so a shared Korean reading never pulls in raw lines the step did not cite. */
export function evidenceGroups(items: RawItem[], node: FlowNode | null, filter: RawFilter): RawGroup[] {
  return groupRaw(node ? items.filter(it => citedByNode(it, node)) : filterRaw(items, filter));
}

/** Preserve the API datetime's sub-millisecond precision while comparing timezone-normalized milliseconds. */
function incidentTime(value: string) {
  const ms = Date.parse(value);
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? '';
  return { ms, subMs: fraction.slice(3).padEnd(6, '0') };
}
/** Operational state follows the newer incident snapshot, not a stale interpretation. Detail wins ties and retains ownership fields when the status agrees. */
export function currentIncident(narrative: Incident, detail?: Incident | null): Incident {
  if (!detail) return narrative;
  const n = incidentTime(narrative.updated_at), d = incidentTime(detail.updated_at);
  const newer = n.ms > d.ms || (n.ms === d.ms && n.subMs > d.subMs);
  if (Number.isFinite(n.ms) && (!Number.isFinite(d.ms) || newer)) return { ...detail, ...narrative,
    handoff: narrative.handoff ?? (narrative.status === detail.status ? detail.handoff : null),
    status_reason: narrative.status_reason ?? (narrative.status === detail.status ? detail.status_reason : null) };
  return { ...narrative, ...detail };
}

/** Engine actions still waiting for the operator, in proposal order. */
export const pendingActions = (actions: Action[] | null | undefined) => (actions ?? []).filter(a => a.status === 'pending_approval').sort((a, b) => a.id - b.id);

/** The requested values only (`args`, or flat scalar params); the full params incl. snapshots stay in the raw view. */
export function actionChanges(params: unknown): string[] {
  if (!params || typeof params !== 'object') return [];
  const p = params as Record<string, unknown>;
  const src = p.args && typeof p.args === 'object' ? p.args as Record<string, unknown> : p;
  return Object.entries(src).filter(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v)).map(([k, v]) => `${k}=${String(v)}`).slice(0, 6);
}

export const FLOW_W = 216;
export const FLOW_H = 56;
export const FLOW_X_GAP = 40;
export const FLOW_Y_GAP = 24;
/** Vertical flow: depth runs top to bottom and lane left to right, keeping negative lanes for steering. */
export function layoutFlow(nodes: FlowNode[]): { id: string; x: number; y: number }[] {
  const finite = (n: number) => Number.isFinite(n) ? n : 0;
  const minLane = Math.min(0, ...nodes.map(n => finite(n.lane)));
  return nodes.map(n => ({ id: n.id, x: (finite(n.lane) - minLane) * (FLOW_W + FLOW_X_GAP), y: finite(n.depth) * (FLOW_H + FLOW_Y_GAP) }));
}
/** Vertical flows connect bottom to top; same-row links use the facing left/right sides. */
export function flowHandles(a: { x: number; y: number }, b: { x: number; y: number }) {
  return a.y < b.y ? { sourceHandle: 'sb', targetHandle: 'tt' } : a.y > b.y ? { sourceHandle: 'st', targetHandle: 'tb' } : a.x <= b.x ? { sourceHandle: 'sr', targetHandle: 'tl' } : { sourceHandle: 'sl', targetHandle: 'tr' };
}
/** Two-line node text. The full label stays in the accessible name and in the step detail. */
export function flowTitle(n: Pick<FlowNode, 'role' | 'role_ko' | 'label' | 'type'>): string {
  if ('semantic' in n) return n.label;
  if ('app' in n || n.type==='investigation') return n.role_ko || n.label;
  if (n.role) return roleKo(n.role, n.role_ko);
  if (n.type === 'outcome' && Object.values(INCIDENT_STATUS_KO).includes(n.label)) return n.label;
  if (n.type === 'signal') return '감지 신호';
  return FLOW_TYPE_KO[n.type] || '단계';
}
export function flowSub(n: Pick<FlowNode, 'role' | 'label' | 'type' | 'model'>): string {
  const kind = FLOW_TYPE_KO[n.type] ?? '단계';
  if ('semantic' in n) {
    const stage = n as import('./semanticFlow.ts').SemanticNode;
    const active = stage.semantic.members.filter(m=>m.status==='running' && m.role);
    if(active.length) return kind+' · '+active.map(m=>m.role_ko || m.role).join(' · ');
  }
  const rest = n.role ? (n.model || '모델 미기록') : '';
  return rest ? `${kind} · ${rest}` : kind;
}
export function validFlowEdges(nodes: FlowNode[], edges: FlowEdge[]): FlowEdge[] {
  const ids = new Set(nodes.map(n => n.id));
  return edges.filter(e => ids.has(e.source) && ids.has(e.target));
}
const FINISHED = ['resolved', 'handed_off', 'failed', 'closed'];
export const narrativePollMs = (status: string) => FINISHED.includes(status) ? 10000 : 2000;
export const isFinished = (status: string) => FINISHED.includes(status);
/** 운영자가 종료한 장애는 기록만 남고 더 이상 지시를 받지 않는다(서버도 409로 거부). */
export const acceptsInstructions = (status: string) => status !== 'closed';
/** 종료할 수 있는 상태: AI가 멈춘 인계, 또는 아무도 처리하지 않을 승인 대기. */
export const closable = (status: string) => status === 'handed_off' || status === 'action_pending';
/** Missing collection arrays are empty, not a crash and never a fabricated healthy result. */
export function normalizeNarrative(data: Narrative): Narrative {
  return { ...data, raw: data.raw ?? [], flow: { nodes: data.flow?.nodes ?? [], edges: data.flow?.edges ?? [] } };
}

// ---------------------------------------------------------------- P18 W6 two-region room: step cards, step chat, handoff summary
/** Steps that own a chat box in their card (contract 2.2). */
export const STEERABLE_TYPES = ['router', 'group', 'agent', 'verify'];
export const stepDomId = (nodeId: string) => `step-${nodeId}`;
/** Match exact execution ids first; aliases must uniquely name one card, never guess by role. */
export function resolveStepId(nodeId: string, nodes: FlowNode[]): string | null {
  const exact = nodes.find(n => n.id === nodeId);
  if (exact) return exact.id;
  const aliases = nodes.filter(n => n.aliases?.includes(nodeId));
  return aliases.length === 1 ? aliases[0].id : null;
}
/** Operator commands render as their own confirmation card; every other node is a step card in flow order. */
export const stepNodes = (nodes: FlowNode[]) => nodes.filter(n => n.type !== 'command');
/** DOM target for a flow node: commands jump to their command card, steering nodes to the operator message. */
export function nodeTargetId(n: Pick<FlowNode, 'id' | 'type'>): string { return stepDomId(n.id); }
/** target_role for a step chat: agent/verify → that role, group → its next not-yet-run role (or none), router → none. */
export function stepTargetRole(n: FlowNode, nodes: FlowNode[]): string | null {
  if (n.type === 'agent' || n.type === 'verify') return (n.card?.kind === 'agent' && n.card.steer_target_role) || n.role || null;
  if (n.type !== 'group') return null;
  if (n.card?.kind === 'group') { const next = n.card.roles.find(r => r.status === 'planned'); if (next) return next.role; }
  const later = nodes.filter(x => (x.type === 'agent' || x.type === 'verify') && x.role && x.depth > n.depth && x.status === 'planned').sort((a, b) => a.depth - b.depth || a.lane - b.lane);
  return later[0]?.role ?? null;
}
/** Messages, AI acknowledgements and application records that name this step. */
export const THREAD_KINDS = ['operator_message', 'steering_interpreted', 'operator_applied'];
/** Actual backend application events carry message_ids; acknowledgements carry message_id. */
export function eventMessageIds(e: { detail?: Record<string, any> | null }): string[] {
  const d = e.detail ?? {};
  const ids: unknown[] = [...(Array.isArray(d.message_ids) ? d.message_ids : []), ...(d.message_id == null ? [] : [d.message_id])];
  return [...new Set(ids.filter(v => typeof v === 'string' || typeof v === 'number').map(String))];
}
export function stepThread<E extends { kind: string; detail?: Record<string, any> | null }>(events: E[], nodeId: string, aliases: string[] = []): E[] {
  const targets = new Set([nodeId, ...aliases]);
  const ids = new Set(events.filter(e => targets.has(e.detail?.target_node)).flatMap(eventMessageIds));
  return events.filter(e => THREAD_KINDS.includes(e.kind) && (targets.has(e.detail?.target_node)
    || (e.kind === 'operator_applied' && eventMessageIds(e).some(id => ids.has(id)))));
}
/** Run-record events already represented by step cards (contract 2.1): kept out of the conversation list. */
export const ABSORBED_EVENT_KINDS = ['evidence_collected', 'agent_started', 'agent_finished', 'agent_progress', 'routing_decided', 'group_composed', 'escalated', 'action_proposed', 'action_executed', 'verification_result', 'completed', 'command_planned', 'command_confirmed', 'command_executed', 'command_failed', 'command_rolled_back'];
/** `owned` holds message ids already shown inside a step card (targeted sends, steering nodes). */
export function conversationEvents<E extends { kind: string; detail?: Record<string, any> | null }>(events: E[], owned: Set<unknown> = new Set(), renderedTargets?: Set<string>): E[] {
  const ids = new Set([...owned].map(String));
  return events.filter(e => {
    if (ABSORBED_EVENT_KINDS.includes(e.kind)) return false;
    if (e.detail?.target_node && (!renderedTargets || renderedTargets.has(e.detail.target_node))) return false;
    const messages = eventMessageIds(e);
    return !messages.length || !messages.every(id => ids.has(id));
  });
}
/** Events of one operator message (message, AI acknowledgement, application record). */
export function messageEvents<E extends { kind: string; detail?: Record<string, any> | null }>(events: E[], messageId: unknown): E[] {
  return events.filter(e => THREAD_KINDS.includes(e.kind) && eventMessageIds(e).includes(String(messageId)));
}
/** 실행 제어·메모 이벤트의 한국어 제목. 주체(actor)와 기록 필드로만 판단하고, 엔진 자동 재개를 운영자 행동으로 꾸미지 않는다. */
export function controlEventKo(e: { kind: string; actor?: string | null; detail?: Record<string, any> | null }): string | null {
  const d = e.detail ?? {}; const engine = !e.actor || e.actor === 'liveprobe';
  if (e.kind === 'resumed') return engine && d.after_s != null ? '사용 한도가 풀려 분석을 이어갔습니다.' : engine ? '분석을 이어갔습니다.' : '운영자가 분석을 재개했습니다.';
  if (e.kind === 'paused') return engine ? '분석이 일시정지되었습니다.' : '운영자가 분석을 일시정지했습니다.';
  if (e.kind === 'stopped') return engine ? '분석 중단이 기록되었습니다.' : '운영자가 분석 중단을 요청했습니다.';
  if (e.kind === 'waiting_quota') return '두 Codex 계정이 사용 한도라 분석을 대기했습니다.';
  if (e.kind === 'note') return d.account && d.blocked_for_s != null ? 'Codex 계정 사용 한도 기록' : '실행 메모가 기록되었습니다.';
  if (e.kind === 'status') return '실행 상태가 기록되었습니다.';
  return null;
}
/** Status line for a targeted instruction. Only recorded facts: an applied record, a hold, or the target having finished. */
export function steerStateKo(opts: { applied: { applied_to_role?: string | null; fallback?: boolean } | null; hold: boolean | null; targetStatus: string | null; roleName: string; targetRole?: string | null; appliedRoleName?: string | null }): string {
  const { applied, hold, targetStatus, roleName, targetRole, appliedRoleName } = opts;
  if (applied) {
    // fallback은 배치 전체의 값이다. 두 역할이 기록돼 있으면 메시지 자체의 대상과 비교한다.
    const fallback = targetRole && applied.applied_to_role ? targetRole !== applied.applied_to_role : applied.fallback;
    return fallback ? `${roleName} 대신 ${appliedRoleName || '다음 단계'}에 반영됨` : `${roleName} 시작 전에 반영됨`;
  }
  if (targetStatus && ['done', 'failed', 'skipped', 'escalated'].includes(targetStatus) && hold) return '대상 단계가 실행되지 않아 반영되지 않음';
  return '반영 예정';
}
/** Ordered by recorded time when every card has `at`; otherwise keep the server's flow order (depth, lane). */
export function orderSteps(nodes: FlowNode[]): FlowNode[] {
  const xs = stepNodes(nodes);
  const byFlow = (a: FlowNode, b: FlowNode) => a.depth - b.depth || a.lane - b.lane;
  if (xs.length && xs.every(n => n.at)) return [...xs].sort((a, b) => Date.parse(a.at!) - Date.parse(b.at!) || byFlow(a, b));
  return [...xs].sort(byFlow);
}
/** Rule-only handoff summary when the server has not sent `handoff_view` yet. Never shows English lists in place of a summary. */
export function handoffFallback(i: Pick<Incident, 'status' | 'handoff'>, conclusionKo: string | null = null): HandoffView {
  conclusionKo = summaryKo(conclusionKo, '') || null;
  const h = (i.handoff ?? {}) as { recommended_steps?: unknown; reason?: unknown; summary?: unknown; observed_recovery?: { ai_remediation?: boolean; cause_confirmed?: boolean } };
  const steps = Array.isArray(h.recommended_steps) ? h.recommended_steps.filter((s): s is string => typeof s === 'string') : [];
  const observed = h.observed_recovery?.ai_remediation === false && h.observed_recovery?.cause_confirmed === false;
  const outcome = i.status === 'closed' ? '운영자 종료 · 서비스 복구 판정 아님' : i.status === 'resolved' ? observed ? '정상 상태 확인 · 원인 미확정 · AI 조치 없음' : '자동 복구 완료' : i.status === 'failed' ? '처리 실패 · 자동 복구 아님' : '운영자 인계 · 자동 복구 아님';
  const operator = i.status === 'handed_off' ? '원인을 확인한 뒤 대화로 조치를 지시하거나 장애를 종료하세요.' : i.status === 'closed' ? '종료된 장애입니다. 같은 증상이 다시 감지되면 새 장애로 접수됩니다.' : '기록을 확인하고 필요하면 후속 작업을 지시하세요.';
  return { summary_ko: conclusionKo, summary_raw: typeof h.summary === 'string' ? h.summary : null,
    decisions: [{ key: 'outcome', label_ko: '결과', value_ko: outcome }, { key: 'cause', label_ko: '원인 판단', value_ko: conclusionKo ? '아래 요약 참고' : '한글 해석 준비 중' }, { key: 'operator', label_ko: '운영자 결정', value_ko: operator }],
    next_steps_ko: [], next_steps_raw: steps, interpreting: !conclusionKo };
}
export const confidenceKo = (v: number | null | undefined) => v == null ? '신뢰도 미기록' : `신뢰도 ${v.toFixed(2)}`;
