import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mergeRunEvents, stepExecution } from '../src/execution.ts';
import type { FlowNode } from '../src/api.ts';
import type { RunEvent } from '../src/feed.ts';

const event = (seq: number, kind: string, detail: Record<string, unknown>): RunEvent => ({ seq, kind, detail, actor: 'liveprobe', at: '2026-11-11T13:00:00Z', summary: kind });
const node = (extra = {}): FlowNode => ({ role: 'k8s_analyst', id: 'pending:k8s', type: 'agent', status: 'running', ...extra } as FlowNode);

test('replay merges out-of-order and duplicate SSE/snapshot records once', () => {
  const a = event(1, 'agent_started', {}), b = event(2, 'agent_progress', {});
  assert.deepEqual(mergeRunEvents([b], [a, b]).map(e => e.seq), [1, 2]);
});
test('same role calls and pool retries never merge their public answers', () => {
  const rows = [event(1, 'agent_started', { role: 'k8s_analyst', call_id: 'old' }),
    event(2, 'agent_progress', { call_id: 'old', attempt_id: 'a', phase: 'failed', text: 'old' }),
    event(3, 'agent_started', { role: 'k8s_analyst', call_id: 'new' }),
    event(4, 'agent_progress', { call_id: 'new', attempt_id: 'b', phase: 'failed', text: 'failed response' }),
    event(5, 'agent_progress', { call_id: 'new', attempt_id: 'c', phase: 'responding', text: 'new response' }),
    event(6, 'agent_progress', { call_id: 'new', attempt_id: 'c', phase: 'succeeded', text: 'new response' })];
  const view = stepExecution(node({ call_id: 'new' }), rows);
  assert.deepEqual(view.records.map(e => e.seq), [3, 4, 5, 6]);
  assert.deepEqual(view.attempts.map(a => [a.id, a.text]), [['b', 'failed response'], ['c', 'new response']]);
  assert.equal(view.attempts[1].phase, 'succeeded');
});
test('completed node binds by stored run id rather than most recent role', () => {
  const rows = [event(1, 'agent_started', { role: 'k8s_analyst', call_id: 'a' }),
    event(2, 'agent_finished', { role: 'k8s_analyst', call_id: 'a', agent_run_id: 12 }),
    event(3, 'agent_started', { role: 'k8s_analyst', call_id: 'b' })];
  assert.deepEqual(stepExecution(node({ agent_run_id: 12, status: 'done' }), rows).records.map(e => e.seq), [1, 2]);
});
test('older completed history is bounded and planned cards borrow no live output', () => {
  const rows = [event(1, 'agent_started', { role: 'k8s_analyst' }),
    event(2, 'agent_finished', { role: 'k8s_analyst', agent_run_id: 12 }),
    event(3, 'agent_started', { role: 'k8s_analyst', call_id: 'new' })];
  assert.deepEqual(stepExecution(node({ agent_run_id: 12, status: 'done' }), rows).records.map(e => e.seq), [1, 2]);
  assert.equal(stepExecution(node({ status: 'planned' }), rows).records.length, 0);
});
