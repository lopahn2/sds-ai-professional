import type { FlowNode, Narrative } from './api.ts';
import { isAppStep } from './applicationFlow.ts';
import { isSemanticNode } from './semanticFlow.ts';

export const roomPhases = [
  { id: 'observe', label: '관측', icon: 'signal' },
  { id: 'analyze', label: '판단·분석', icon: 'group' },
  { id: 'repair', label: '수정·복구', icon: 'action' },
  { id: 'qa', label: 'dev QA', icon: 'verify' },
  { id: 'approve', label: '운영 승인', icon: 'steer' },
  { id: 'recover', label: '복구 확인', icon: 'resource' },
  { id: 'result', label: '결과', icon: 'outcome' },
] as const;
export type RoomPhase = typeof roomPhases[number]['id'];

const projectedPhases: Record<string,RoomPhase> = { analysis:'analyze', fix:'repair', qa:'qa', approval:'approve', recovery:'recover', done:'result', stopped:'result' };
export function phaseOf(node: FlowNode, codeState?: string, projection?: string): RoomPhase {
  if (isSemanticNode(node)) return node.semantic.phase;
  if (isAppStep(node)) return node.app.phase;
  if (node.type === 'code_action') {
    if (projection && Object.hasOwn(projectedPhases, projection)) return projectedPhases[projection];
    if (['detected','collecting','source_pinning','source_queued','analysis_queued','analyzing'].includes(codeState ?? '')) return 'analyze';
    if (['awaiting_prd_approval', 'awaiting_merge'].includes(codeState ?? '')) return 'approve';
    if (['dev_targets_collecting', 'dev_deploy_queued', 'dev_verifying', 'dev_verified', 'qa_planning', 'qa_queued', 'qa_passed', 'qa_failed', 'awaiting_user_test'].includes(codeState ?? '')) return 'qa';
    if (['prd_targets_collecting', 'prd_deploy_queued', 'prd_verifying', 'prd_verified', 'awaiting_recovery'].includes(codeState ?? '')) return 'recover';
    if (['completed', 'failed', 'handoff'].includes(codeState ?? '')) return 'result';
    return 'repair';
  }
  if (node.type === 'action') return node.card?.kind === 'action' && node.card.status === 'pending_approval' ? 'approve' : 'repair';
  if (node.type === 'command') return 'repair';
  if (node.type === 'outcome') return 'result';
  if (['signal', 'evidence', 'interpret'].includes(node.type)) return 'observe';
  // The analysis verifier checks evidence. dev QA belongs to the version tested on dev.
  return 'analyze';
}

/** Only edges recorded by the engine may connect steps. Include the immediately
 * adjacent nodes so the operator can follow a branch across a phase boundary. */
export function phaseFlow(flow: Narrative['flow'], phase: RoomPhase | null, codeState?: string, projection?: string): Narrative['flow'] {
  if (!phase) return flow;
  const center = new Set(flow.nodes.filter(n => phaseOf(n, codeState, projection) === phase).map(n => n.id));
  const ids = new Set(center);
  for (const edge of flow.edges) {
    if (center.has(edge.source)) ids.add(edge.target);
    if (center.has(edge.target)) ids.add(edge.source);
  }
  return { nodes: flow.nodes.filter(n => ids.has(n.id)), edges: flow.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
}

/** Center the first view on the node under inspection and its real parallel
 * peers. Long incidents remain navigable through the full graph. */
export function stepNeighborhood(flow: Narrative['flow'], id: string): Narrative['flow'] {
  const center = flow.nodes.find(n => n.id === id);
  if (!center) return flow;
  const adjacent = new Set([id]);
  for (const e of flow.edges) {
    if (e.source === id) adjacent.add(e.target);
    if (e.target === id) adjacent.add(e.source);
  }
  const ids = new Set(adjacent);
  // Two roles are peers only when a real edge links them to a common neighbor.
  for (const n of flow.nodes) {
    if (n.depth === center.depth && flow.edges.some(e =>
      (n.id === e.source && adjacent.has(e.target)) || (n.id === e.target && adjacent.has(e.source)))) ids.add(n.id);
  }
  return { nodes: flow.nodes.filter(n => ids.has(n.id)), edges: flow.edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
}

/** Depth advances to the right. Roles at one depth stay in distinct lanes.
 * Rank only observed depths and lanes to avoid gaps in an isolated phase. */
export function phasePositions(nodes: FlowNode[], column: number, row: number, columns = Infinity) {
  const axes = (values: number[]) => new Map([...new Set(values)].sort((a,b) => a-b).map((v,i) => [v,i]));
  const finite = (v: number) => Number.isFinite(v) ? v : 0;
  const depths = axes(nodes.map(n => finite(n.depth)));
  const count = Math.max(1, Math.min(depths.size, Math.floor(columns)));
  const groups = new Map<number,FlowNode[]>();
  for (const n of nodes) {
    const band = Math.floor(depths.get(finite(n.depth))! / count);
    const group = groups.get(band) ?? [];
    group.push(n); groups.set(band, group);
  }
  const positions = new Map<string,{x:number; y:number}>();
  let top = 0;
  for (const band of [...groups.keys()].sort((a,b) => a-b)) {
    const group = groups.get(band)!;
    const lanes = axes(group.map(n => finite(n.lane)));
    for (const n of group) {
      const index = depths.get(finite(n.depth))! % count;
      const x = (band % 2 === 0 ? index : count - 1 - index) * column;
      positions.set(n.id, {x, y: top + lanes.get(finite(n.lane))! * row});
    }
    // Consecutive levels meet at the turning edge. Parallel roles keep their
    // own lanes inside each band, even when the graph wraps.
    top += lanes.size * row + row / 2;
  }
  return positions;
}

export function phaseStatus(nodes: FlowNode[], archived: boolean) {
  if (!nodes.length) return '기록 없음';
  const waiting = nodes.find(n => n.status.startsWith('awaiting_'));
  if (waiting) return {awaiting_approval:'승인 대기',awaiting_operator:'인수 대기',awaiting_review:'소명 확인 대기',awaiting_user_test:'당시 수동 시험 대기'}[waiting.status] ?? '확인 대기';
  if (nodes.some(n=>n.status==='queued')) return '실행 대기';
  if (nodes.some(n => n.status === 'running')) return archived ? '기록 당시 진행' : '진행 중';
  if (nodes.some(n => n.status === 'failed')) return '실패 기록';
  if (nodes.every(n => n.status === 'planned')) return '예정';
  if (nodes.every(n => n.status === 'skipped')) return '건너뜀';
  if (nodes.some(n => n.status === 'planned')) return '일부 기록';
  return nodes.every(n => ['done', 'skipped'].includes(n.status)) ? '완료' : '실행 기록';
}

export function serviceSignalImpact(narrative: Narrative): string | null {
  const i = narrative.incident;
  if (!/^(NoEndpoints\b|Service 연결 대상 없음)/.test(i.title ?? '') && !i.title_ko?.startsWith('Service 연결 대상 없음')) return null;
  const target = `${i.namespace}/Service/${i.workload}`;
  const readings = narrative.flow.nodes.flatMap(n => n.card?.kind === 'evidence' ? n.card.items : [])
    .filter(e => e.ok && e.source === 'service' && e.target === target && e.collected_at);
  const mostRecent = readings.sort((a,b) => Date.parse(b.collected_at!) - Date.parse(a.collected_at!))[0];
  const match = mostRecent?.summary_ko?.match(/^Service에 준비된 연결 대상\(엔드포인트\)이 (\d+)개입니다\.$/);
  const state = match ? `최근 수집 시점에는 준비된 연결 대상이 ${match[1]}개였습니다.` : '수집 시점의 연결 대상 수는 미관측입니다.';
  return `Service에 요청을 전달할 대상이 없다는 신호를 감지했습니다. ${state}` + (i.recovery_kind === 'observed' ? ' 원인은 미확정입니다.' : '');
}

/** Presentation-only readings remain in the evidence and in usage history. */
export function operatorFlow(flow: Narrative['flow']): Narrative['flow'] {
  const nodes = flow.nodes.filter(node => node.type !== 'interpret');
  const ids = new Set(nodes.map(n => n.id));
  return {nodes, edges: flow.edges.filter(e => ids.has(e.source) && ids.has(e.target))};
}

/** The outline points through a real outgoing edge to an actual recorded step. */
export function continuationHints(full: Narrative['flow'], visible: Narrative['flow'], completed: boolean) {
  if (!completed) return [];
  const ids = new Set(visible.nodes.map(n => n.id));
  const targets = new Set(full.edges.filter(e => ids.has(e.source) && !ids.has(e.target)).map(e => e.target));
  return full.nodes.filter(n => targets.has(n.id) && ['done','failed','escalated','reported'].includes(n.status));
}

export function groupSteps(flow: Narrative['flow'], group: FlowNode) {
  const seen = new Set([group.id]);
  const nodes = new Map(flow.nodes.map(n => [n.id, n]));
  const queue = [group.id];
  const roles: FlowNode[] = [];
  for (let i = 0; i < queue.length; i++) {
    for (const edge of flow.edges) {
      if (edge.source !== queue[i] || seen.has(edge.target)) continue;
      seen.add(edge.target);
      const n = nodes.get(edge.target);
      if (!n || ['group','router','outcome'].includes(n.type) || (n.group && group.group && n.group !== group.group)) continue;
      if (n.type === 'agent' || n.type === 'verify') roles.push(n);
      queue.push(n.id);
    }
  }
  return roles.sort((a,b) => a.depth - b.depth || a.lane - b.lane);
}
