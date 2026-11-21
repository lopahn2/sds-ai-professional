import type { FlowNode } from './api.ts';
import type { RunEvent } from './feed.ts';

export type StreamState = 'connecting' | 'live' | 'reconnecting' | 'polling' | 'snapshot' | 'record';
export type AttemptView = { id: string; text: string; phase: string; mode: string; records: RunEvent[] };
export type ExecutionView = { records: RunEvent[]; attempts: AttemptView[]; final: RunEvent | null };
const KINDS = new Set(['agent_started', 'agent_progress', 'agent_finished']);

export function mergeRunEvents(previous: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  return [...new Map([...previous, ...incoming].map(e => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq);
}

/** Exact call identity wins. Legacy history is bounded by its actual start/finish
 * records and never borrows newer output merely because the role is the same. */
export function stepExecution(node: FlowNode, events: RunEvent[]): ExecutionView {
  const all = events.filter(e => KINDS.has(e.kind));
  const runId = node.agent_run_id ?? (node.card?.kind === 'agent' ? node.card.agent_run_id : null);
  const final = runId ? [...all].reverse().find(e => e.kind === 'agent_finished' && e.detail?.agent_run_id === runId) : undefined;
  const callId = node.call_id || final?.detail?.call_id;
  let records: RunEvent[] = [];
  if (callId) records = all.filter(e => e.detail?.call_id === callId);
  else if (final) {
    const start = [...all].reverse().find(e => e.kind === 'agent_started' && e.detail?.role === node.role && e.seq < final.seq);
    records = all.filter(e => e.detail?.role === node.role && e.seq >= (start?.seq ?? final.seq) && e.seq <= final.seq);
  } else if (node.status === 'running' && node.role) {
    const start = [...all].reverse().find(e => e.kind === 'agent_started' && e.detail?.role === node.role);
    // This fallback is for an older narrative response that hasn't received call_id yet.
    if (start && !all.some(e => e.kind === 'agent_finished' && e.detail?.role === node.role && e.seq > start.seq))
      records = all.filter(e => e.detail?.role === node.role && e.seq >= start.seq &&
        (!start.detail.call_id || e.detail.call_id === start.detail.call_id));
  }
  const attempts = new Map<string, AttemptView>();
  for (const e of records) {
    if (e.kind !== 'agent_progress' || typeof e.detail?.attempt_id !== 'string') continue;
    const d = e.detail;
    const a: AttemptView = attempts.get(d.attempt_id) ?? { id: d.attempt_id, text: '', phase: '', mode: '', records: [] };
    a.records.push(e); a.phase = d.phase; a.mode = d.output_mode;
    if (typeof d.text === 'string') a.text = d.text;
    attempts.set(a.id, a);
  }
  return { records, attempts: [...attempts.values()], final: records.find(e => e.kind === 'agent_finished') ?? null };
}
