import { test } from 'node:test';
import assert from 'node:assert/strict';
import { candidateLabel, quoteSummary, usageSummary, verdictTone, verifierKo } from '../src/explainModel.ts';
import type { FlowNode } from '../src/api.ts';

const node = (extra: Partial<FlowNode>): FlowNode => ({ id: 'n', type: 'agent', label: '', role: null, role_ko: '', job_ko: '', status: 'done', model: null, effort: null,
  tokens: null, cost_usd: null, duration_ms: null, inputs: [], conclusion_ko: null, conclusion_raw: null, lane: 0, depth: 0, group: null, ...extra });

test('모델 호출은 실행 기록이 있는 단계만 세고 모델별로 많은 순이다', () => {
  const u = usageSummary([
    node({ agent_run_id: 1, model: 'sol', tokens: { in: 100, out: 10 }, duration_ms: 1000 }),
    node({ agent_run_id: 2, model: 'luna', tokens: { in: 50, out: 5 }, duration_ms: 500 }),
    node({ agent_run_id: 3, model: 'sol', tokens: { in: 200, out: 20 }, duration_ms: 2000 }),
    node({ status: 'planned', model: 'sol' }), node({ status: 'running', model: 'sol' }), node({ status: 'skipped', model: 'sol' }), node({ type: 'signal' }),
  ]);
  assert.equal(u.calls, 3);
  assert.deepEqual(u.byModel, [['sol', 2], ['luna', 1]]);
  assert.deepEqual([u.tokensIn, u.tokensOut, u.durationMs], [350, 35, 3500]);
});

test('실행 기록 ID가 없는 이전 응답도 모델이 기록된 끝난 단계는 호출로 센다', () => {
  const u = usageSummary([node({ model: 'luna', status: 'done', tokens: { in: 10, out: 1 }, duration_ms: 100 }), node({ model: 'terra', status: 'escalated', tokens: { in: 20, out: 2 }, duration_ms: 200 }),
    node({ model: 'sol', status: 'failed', tokens: { in: 30, out: 3 }, duration_ms: 300 }), node({ status: 'done' })]);
  assert.equal(u.calls, 3);
  assert.deepEqual([u.tokensIn, u.tokensOut, u.durationMs], [60, 6, 600]);
});

test('토큰이나 시간이 하나라도 없으면 합계는 0이나 부분 합이 아니라 미관측이다', () => {
  const u = usageSummary([node({ agent_run_id: 1, model: 'sol', tokens: { in: 100, out: 10 }, duration_ms: 1000 }), node({ agent_run_id: 2, model: null, tokens: null })]);
  assert.equal(u.calls, 2);
  assert.deepEqual([u.tokensIn, u.tokensOut, u.durationMs], [null, null, null]);
  assert.deepEqual(u.byModel, [['sol', 1], ['모델 미기록', 1]]);
  assert.deepEqual(usageSummary([]), { calls: 0, byModel: [], tokensIn: null, tokensOut: null, durationMs: null });
});

test('인용 요약은 확인된 출처와 수집되지 않은 번호를 구분한다', () => {
  const q = (ref: string, ref_known: boolean, ref_ko: string | null) => ({ ref, ref_ko, ref_known, quote: 'x', supports_raw: '', candidate_index: null });
  const s = quoteSummary({ quotes: [q('E2_pods', true, 'Pod 상태'), q('E2', true, 'Pod 상태'), q('R1', true, '참고 문서'), q('E99', false, null), q('E99', false, null)] });
  assert.deepEqual([s.total, s.known], [5, 3]);
  assert.deepEqual(s.unknownRefs, ['E99']);
  assert.deepEqual(s.sources, [{ label: 'Pod 상태', count: 2 }, { label: '참고 문서', count: 1 }]);
  assert.deepEqual(quoteSummary({}), { total: 0, known: 0, unknownRefs: [], sources: [] });
});

test('판정 문구와 강조, 후보 번호', () => {
  assert.equal(verifierKo('supported'), '근거로 지지');
  assert.equal(verifierKo('something'), '판정 미확인');
  assert.equal(verifierKo(null), null);
  assert.deepEqual(['supported', 'contradicted', 'insufficient', 'x'].map(verdictTone), ['ok', 'danger', 'warn', '']);
  assert.equal(candidateLabel(0), '후보 1');
  assert.equal(candidateLabel(null), '후보 미지정');
});
