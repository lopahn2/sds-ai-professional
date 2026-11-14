import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import type { FlowNode, Narrative } from '../src/api.ts';
import { historyResult, impactKo, isResultStatus, spanKo, whenKo } from '../src/history.ts';

// 지난 장애 결과 표시: 구조화 필드(status·closed_at·조치 카드·종료 기록)만 쓰고, 원문·추정은 결과 줄에 올리지 않는다.
const fixture = (id: number) => JSON.parse(readFileSync(new URL(`../e2e/fixtures/narrative-${id}.json`, import.meta.url), 'utf8')) as Narrative;
const RAW = 'RAW_HISTORY_SENTINEL HTTPStatusError http://internal kubectl get pods';
const base = { created_at: '2026-10-11T12:00:00Z', closed_at: '2026-10-11T12:30:00Z', handoff: null };
const text = (r: ReturnType<typeof historyResult>) => JSON.stringify(r);

test('자동 복구 완료는 실제 성공 조치와 복구 시각·소요 시간을 먼저 제공한다', () => {
  const d = fixture(971); const i = d.incident;
  const r = historyResult(i, d.flow.nodes)!;
  assert.equal(r.label, '자동 복구 완료');
  assert.equal(r.detail, '메모리 한도 조정 · Deployment/inventory · 실행 후 복구 검증 통과');
  assert.equal(r.timeLabel, '복구 완료');
  assert.equal(r.at, new Date(i.closed_at!).toLocaleString('ko-KR'));
  assert.equal(r.detected, new Date(i.created_at).toLocaleString('ko-KR'));
  assert.equal(r.duration, '복구까지 2분'); // 12:02:05 → 12:04:23
  for (const id of [976, 987]) assert.match(historyResult(fixture(id).incident, fixture(id).flow.nodes)!.detail ?? '', /Deployment\/order · 실행 후 복구 검증 통과$/);
});

test('성공 조치 기록이 없는 구버전 자동 복구는 조치를 추정하지 않는다', () => {
  const d = fixture(971); const legacy = d.flow.nodes.map(n => ({ ...n, card: null })) as FlowNode[];
  assert.equal(historyResult(d.incident, legacy)!.detail, null);
  assert.equal(historyResult(d.incident)!.detail, null);
  // 실패한 조치는 복구 근거로 쓰지 않고, 실제 성공한 마지막 조치만 이름을 붙인다.
  const action = d.flow.nodes.find(n => n.card?.kind === 'action')!;
  const failed = { ...action, id: 'action:1', card: { ...action.card!, type: 'rollout_undo', status: 'failed' } } as FlowNode;
  assert.equal(historyResult(d.incident, [failed])!.detail, null);
  assert.match(historyResult(d.incident, [action, failed])!.detail!, /^메모리 한도 조정/);
  // 원문 대상·영문 조치 이름은 결과 줄에 올리지 않는다.
  const odd = { ...action, card: { ...action.card!, type: 'custom_op', type_ko: RAW, target: `Deployment/x <img src=x>`, status_ko: RAW } } as FlowNode;
  assert.equal(historyResult(d.incident, [odd])!.detail, '조치');
});

test('운영자 인계는 자동 복구가 아님을 밝히고, 저장 표본에는 인수 행동을 요구하지 않는다', () => {
  const d = fixture(990); const i = d.incident;
  const r = historyResult(i, d.flow.nodes)!;
  assert.equal(r.label, '운영자 인계');
  assert.equal(r.detail, '담당자 인수 대기');
  assert.equal(r.timeLabel, '인계'); assert.equal(r.duration, '인계까지 2분');
  assert.ok(!text(r).includes('kubectl'), '권장 조치 원문은 결과에 오르지 않는다');
  assert.equal(historyResult({ ...i, handoff: { claimed_by: 'operator' } })!.detail, '담당자 인수 완료');
  assert.equal(historyResult(i, d.flow.nodes, true)!.detail, '저장 당시 미인수');
});

test('운영자 종료는 복구 판정이 아니며, 구버전은 종료 기록의 시각을 쓰고 원문 사유는 숨긴다', () => {
  const closed = { ...base, status: 'closed' as const, handoff: { closed_at: '2026-10-11T13:00:00Z', close_reason: '대상 정상 확인, 메모리 한도 변경 불필요' } };
  const r = historyResult(closed)!;
  assert.equal(r.label, '운영자 종료');
  assert.equal(r.detail, '대상 정상 확인, 메모리 한도 변경 불필요');
  assert.equal(r.at, new Date(base.closed_at).toLocaleString('ko-KR'), 'incident.closed_at이 있으면 그 값을 쓴다');
  const legacy = historyResult({ ...closed, closed_at: null })!;
  assert.equal(legacy.at, new Date('2026-10-11T13:00:00Z').toLocaleString('ko-KR'));
  assert.equal(legacy.duration, '종료까지 1시간 0분');
  for (const reason of [RAW, 'closed after manual check of the order service', '정리 {"raw":1}', null])
    assert.equal(historyResult({ ...closed, handoff: { close_reason: reason } })!.detail, null, String(reason));
});

test('처리 실패는 한글 사유만 붙이고 원문 조각과 영문 문장은 제거한다', () => {
  const failed = { ...base, status: 'failed' as const };
  assert.equal(historyResult(failed)!.label, '처리 실패');
  assert.equal(historyResult(failed)!.timeLabel, '실패 기록');
  assert.equal(historyResult(failed)!.detail, null);
  assert.equal(historyResult({ ...failed, status_reason_ko: '조치 실패: 실행 중 오류가 발생했습니다(HTTPStatusError: 500 boom).' })!.detail,
    '조치 실패: 실행 중 오류가 발생했습니다.');
  const restart = 'LiveProbe가 분석 처리 중에 재시작되어 이 장애를 실패로 기록했습니다(다시 실행하지 않음).';
  assert.equal(historyResult({ ...failed, status_reason_ko: restart })!.detail, restart);
  for (const reason of ['조치 거절: action was rejected by the policy engine', RAW, '한글 kubectl get pods'])
    assert.equal(historyResult({ ...failed, status_reason_ko: reason })!.detail, null, reason);
});

test('진행 중 상태는 저장 표본이어도 결과로 표시하지 않는다', () => {
  for (const status of ['open', 'analyzing', 'action_pending', 'acting'] as const) {
    assert.equal(isResultStatus(status), false);
    assert.equal(historyResult({ ...base, status, closed_at: null }), null, status);
    assert.equal(historyResult({ ...base, status, closed_at: null }, [], true), null, `${status} snapshot`);
  }
  assert.equal(historyResult(fixture(1976).incident, fixture(1976).flow.nodes, true), null);
  for (const status of ['toString', 'constructor', '__proto__', '']) assert.equal(isResultStatus(status), false, status);
  for (const status of ['resolved', 'closed', 'failed', 'handed_off']) assert.equal(isResultStatus(status), true);
});

test('기록 시각이 없거나 잘못된 구버전은 시각 미기록으로 표시하고 소요 시간을 추정하지 않는다', () => {
  for (const [created, closed] of [['2026-10-11T12:00:00Z', null], ['', null], ['not-a-date', 'also-bad'], ['2026-10-11T12:30:00Z', '2026-10-11T12:00:00Z']] as const) {
    const r = historyResult({ status: 'resolved', created_at: created, closed_at: closed, handoff: null })!;
    assert.equal(r.duration, null, `${created} → ${closed}`);
    assert.ok(!/Invalid Date|NaN|undefined/.test(text(r)), text(r));
    if (!closed || closed === 'also-bad') assert.equal(r.at, '시각 미기록');
  }
  assert.equal(whenKo(null), '시각 미기록'); assert.equal(whenKo(undefined), '시각 미기록'); assert.equal(whenKo('bad'), '시각 미기록');
  assert.equal(spanKo('2026-10-11T12:00:00Z', '2026-10-11T12:59:59Z'), '59분');
  assert.equal(spanKo('2026-10-11T12:00:00Z', '2026-10-11T13:00:00Z'), '1시간 0분');
  assert.equal(spanKo('2026-10-11T12:00:00Z', '2026-10-13T12:00:00Z'), '2일');
  assert.equal(spanKo('2026-10-19T00:00:00+09:00', '2026-10-18T15:30:00Z'), '30분', '시간대가 달라도 같은 기준으로 비교한다');
});

test('완료·저장 기록의 영향 문장은 수집 중이라고 말하지 않고, 원문을 대신 넣지 않는다', () => {
  assert.equal(impactKo('주문 서비스에서 503 오류가 함께 발생했습니다.', true), '주문 서비스에서 503 오류가 함께 발생했습니다.');
  assert.equal(impactKo(null, false), '영향 범위를 확인 중입니다.');
  assert.equal(impactKo(null, true), '기록된 영향 정보가 없습니다.');
  assert.equal(impactKo(RAW, true), '기록된 영향 정보가 없습니다.');
  assert.equal(impactKo(RAW, false), '영향 범위를 확인 중입니다.');
  const server = 'liveprobe-target/order에서 이상 신호가 관측되었습니다. 영향 범위는 근거 수집 후 표시됩니다.';
  assert.equal(impactKo(server, false), server, '진행 중 사건은 서버 문장을 그대로 둔다');
  assert.equal(impactKo(server, true), 'liveprobe-target/order에서 이상 신호가 관측되었습니다. 영향 범위는 기록되지 않았습니다.');
  for (const id of [971, 976, 987, 990]) assert.equal(impactKo(fixture(id).headline.impact, true), fixture(id).headline.impact);
});

test('어떤 결과에도 원문 표식이 결과 줄에 나타나지 않는다', () => {
  const d = fixture(971); const action = d.flow.nodes.find(n => n.card?.kind === 'action')!;
  const tainted = { ...action, card: { ...action.card!, type_ko: RAW, status_ko: RAW, target: RAW } } as FlowNode;
  for (const status of ['resolved', 'closed', 'failed', 'handed_off'] as const) {
    const r = historyResult({ ...base, status, status_reason_ko: RAW, handoff: { close_reason: RAW, recommended_steps: [RAW], summary: RAW } }, [tainted]);
    assert.ok(r && !text(r).includes('RAW_HISTORY_SENTINEL'), `${status}: ${text(r)}`);
  }
});
