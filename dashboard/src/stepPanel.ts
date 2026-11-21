import type { FlowNode, Dispatch } from './api.ts';
import type { RunEvent } from './feed.ts';
/** 호출 ID로만 결합한다. 구버전 프롬프트를 결과에서 역산하지 않는다. */
export function dispatchFor(node: FlowNode, events: RunEvent[]): Dispatch | null {
  if (node.dispatch?.version === 'dispatch/v1') return node.dispatch;
  if (!node.call_id) return null;
  const value = events.find(e => e.kind === 'agent_started' && e.detail?.call_id === node.call_id)?.detail?.dispatch;
  return value?.version === 'dispatch/v1' ? value : null;
}
export function focusStep(nodes: FlowNode[], selected: string | null, historical: boolean): FlowNode | null {
  if (selected) return nodes.find(n => n.id === selected || n.aliases?.includes(selected)) ?? null;
  if (historical) return [...nodes].reverse().find(n => n.type === 'outcome') ?? nodes.at(-1) ?? null;
  return nodes.find(n => n.status === 'running' && ['agent', 'verify', 'router'].includes(n.type)) ?? nodes.find(n => n.status === 'running') ?? [...nodes].reverse().find(n => n.status === 'done') ?? nodes[0] ?? null;
}
