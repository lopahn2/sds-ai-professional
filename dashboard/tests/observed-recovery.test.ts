import { test } from 'node:test';
import assert from 'node:assert/strict';
import { handoffFallback } from '../src/narrative.ts';
import { historyResult } from '../src/history.ts';
import { incidentStatusKo } from '../src/labels.ts';
import { nextActionKo } from '../src/presentation.ts';

const proof = { cause_confirmed: false, ai_remediation: false, observations: [{}, {}] };
const incident = { status: 'resolved' as const, created_at: '2026-11-14T10:00:00Z', closed_at: '2026-11-14T10:01:00Z', handoff: { observed_recovery: proof } };

test('관측 회복은 자동 복구·원인 확정·AI 조치로 표시하지 않는다', () => {
  const fallback = handoffFallback(incident);
  assert.match(fallback.decisions[0].value_ko, /정상 상태 확인 · 원인 미확정 · AI 조치 없음/);
  const result = historyResult(incident)!;
  assert.equal(result.label, '정상 상태 확인');
  assert.equal(result.timeLabel, '정상 확인');
  assert.equal(result.detail, '원인 미확정 · AI 조치 없음');
  assert.equal(result.duration, '정상 확인까지 1분');
  assert.match(nextActionKo(incident), /원인은 미확정이며 AI 조치는 없었습니다/);
  assert.match(nextActionKo({ ...incident, handoff: null, recovery_kind: 'observed' }), /정상 상태를 확인/);
  assert.doesNotMatch(JSON.stringify({ fallback, result }), /자동 복구 완료/);
});

test('목록의 관측 회복 표식도 정상 확인으로 표시하고 기존 해결 이력은 바꾸지 않는다', () => {
  assert.equal(incidentStatusKo('resolved', 'observed'), '정상 상태 확인');
  assert.equal(incidentStatusKo('resolved'), '자동 복구 완료');
  assert.equal(historyResult({ ...incident, handoff: null, recovery_kind: 'observed' })!.label, '정상 상태 확인');
  assert.equal(historyResult({ ...incident, handoff: null })!.label, '자동 복구 완료');
  assert.equal(handoffFallback({ status: 'resolved', handoff: null }).decisions[0].value_ko, '자동 복구 완료');
});
