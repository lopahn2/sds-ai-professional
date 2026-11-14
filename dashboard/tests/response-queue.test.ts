import { test } from 'node:test';
import assert from 'node:assert/strict';
import { needsOperator, nextStepShort, queueCounts, responseQueue } from '../src/responseQueue.ts';

const inc = (id: number, status: string, level: string | null = 'NORMAL', extra: Record<string, unknown> = {}) =>
  ({ id, title: `t${id}`, namespace: 'ns', workload: `w${id}`, status, severity_level: level, created_at: '2026-10-01T00:00:00Z', stage: null, ...extra }) as never;

test('승인 대기와 미인수 인계만 운영자 결정이 필요한 상태다', () => {
  assert.equal(needsOperator('action_pending'), true);
  assert.equal(needsOperator('handed_off'), true);
  assert.equal(needsOperator('handed_off', true), false);
  for (const s of ['open', 'analyzing', 'acting', 'resolved', 'failed', 'closed']) assert.equal(needsOperator(s), false, s);
});

test('대응 대기열은 승인 → 미인수 인계 → 조치 → 분석 순이고 같은 상태는 수준 높은 순이다', () => {
  const q = responseQueue([inc(1, 'analyzing', 'HIGH_RISK'), inc(2, 'action_pending', 'SIMPLE'), inc(3, 'acting'), inc(4, 'action_pending', 'COMPLEX'), inc(5, 'handed_off')]);
  assert.deepEqual(q.map(i => i.id), [4, 2, 5, 3, 1]);
});

test('같은 사건은 한 번만 나오고 실시간 목록의 상태가 운영 관제 응답보다 우선한다', () => {
  const q = responseQueue([inc(7, 'action_pending')], [inc(7, 'analyzing'), inc(8, 'analyzing')]);
  assert.deepEqual(q.map(i => [i.id, i.status]), [[7, 'action_pending'], [8, 'analyzing']]);
});

test('인수된 인계는 대응 필요가 아니며 뒤로 밀린다', () => {
  const q = responseQueue([inc(1, 'handed_off', 'HIGH_RISK', { handoff: { claimed_by: 'ops' } }), inc(2, 'analyzing', 'SIMPLE')]);
  assert.deepEqual(q.map(i => i.id), [2, 1]);
  assert.equal(q[1].claimed, true);
});

test('짧은 다음 행동은 상태마다 문구와 강조가 다르다', () => {
  assert.deepEqual(nextStepShort('action_pending'), { label: '승인·거절 필요', tone: 'danger' });
  assert.deepEqual(nextStepShort('handed_off'), { label: '인수 필요', tone: 'warning' });
  assert.equal(nextStepShort('handed_off', true).tone, 'neutral');
  assert.equal(nextStepShort('analyzing').label, 'AI 분석 중');
  assert.equal(nextStepShort('acting').label, '조치 실행 중');
  assert.equal(nextStepShort('resolved').label, '기록 열람');
});

test('집계는 서버 값을 우선하고, 없으면 완전히 조회된 실시간 목록에서만 센다', () => {
  const live = [inc(1, 'action_pending'), inc(2, 'analyzing'), inc(3, 'analyzing')];
  assert.deepEqual(queueCounts({ awaiting_approval: 9, unclaimed_handoff: 4, in_progress: 20 } as never, live, true), { approval: 9, handoff: 4, inProgress: 20 });
  assert.deepEqual(queueCounts(undefined, live, true), { approval: 1, handoff: null, inProgress: 3 });
});

test('실시간 목록 조회가 끝나지 않았으면 0이 아니라 미관측이다', () => {
  assert.deepEqual(queueCounts(undefined, [], false), { approval: null, handoff: null, inProgress: null });
});
