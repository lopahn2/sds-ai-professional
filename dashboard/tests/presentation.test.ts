import { test } from 'node:test';
import assert from 'node:assert/strict';
import { incidentSymptom, nextActionKo, publicAnswer, summaryKo, executionStageKo, incidentCause, warningSummary } from '../src/presentation.ts';

test('primary readings never fall back to raw titles, technical errors, commands or JSON', () => {
  for (const text of ['HTTPStatusError: server raw', '한글 요약 HTTPStatusError', '오류 {"raw":"data"}', '한글 kubectl get pods', '추적 http://internal/path', '이미지 sha256:' + 'a'.repeat(64)]) assert.equal(summaryKo(text, '확인 중'), '확인 중', text);
  assert.equal(summaryKo('서비스 연결이 지연됩니다.', '확인 중'), '서비스 연결이 지연됩니다.');
  assert.equal(incidentSymptom({workload:'order',title_ko:'응답 지연 · App/order'}), '응답 지연');
  assert.equal(incidentSymptom({workload:'order',title_ko:'응답 지연 · App/inventory'}), '응답 지연 · App/inventory');
  assert.equal(incidentSymptom({ signal_reasons: ['BackOff', 'UnknownPayload'], title_ko: 'HTTPStatusError' }), '재시작 대기(반복 실패)');
  assert.equal(incidentSymptom({ signal_reasons: ['UnknownPayload'], title_ko: null }), '증상 미기록');
});
test('public answer supports prose and completed structured summaries, partial JSON stays raw-only', () => {
  assert.equal(publicAnswer('공개 답변을 생성 중입니다.'), '공개 답변을 생성 중입니다.');
  assert.equal(publicAnswer('{"summary_ko":"분석 중입니다.","internal":"RAW_SENTINEL"}'), '분석 중입니다.');
  assert.equal(publicAnswer('{"summary_ko":"생성 중'), '생성 중');
  assert.equal(publicAnswer('{"trace":"생성 중'), '');
  assert.equal(publicAnswer('{"error":"RAW_SENTINEL"}'), '');
  assert.equal(publicAnswer('curl http://internal'), '');
  assert.equal(publicAnswer('한글'.repeat(1000)), '한글'.repeat(1000));
});
test('action guidance follows latest operational state and never claims ownership or recovery without it', () => {
  assert.match(nextActionKo({ status: 'handed_off', handoff: null }), /인수.*필요/);
  assert.match(nextActionKo({ status: 'handed_off', handoff: { claimed_by: 'operator' } }), /인수했습니다/);
  assert.match(nextActionKo({ status: 'action_pending' }), /승인하거나 거절/);
  assert.match(nextActionKo({ status: 'failed' }), /실패/);
  assert.match(nextActionKo({ status: 'closed' }), /종료/);
  assert.equal(executionStageKo('agent_progress', 'unknownRawCode'), '진행 상태를 확인하고 있습니다.');
});

test('long human explanations stay visible and warnings distinguish symptom from unconfirmed cause', () => {
  assert.equal(summaryKo('긴 설명입니다. '.repeat(200), '미기록'), '긴 설명입니다. '.repeat(200).trim());
  assert.match(warningSummary('Unhealthy', 'Readiness probe failed: command timed out'), /준비 상태.*제한 시간.*근본 원인은 아직/);
  assert.match(warningSummary('Unhealthy', 'Liveness probe failed: connection refused'), /생존 상태.*연결을 거부/);
  assert.match(warningSummary('BackOff', 'Back-off restarting failed container'), /재시작.*종료 원인은 추가/);
  assert.match(incidentCause([], null), /아직 확정되지/);
  assert.equal(incidentCause([], {decisions:[{key:'cause',value_ko:'데이터베이스 연결 누수로 판단했습니다.'}]} as any), '데이터베이스 연결 누수로 판단했습니다.');
  assert.match(incidentCause([{card:{kind:'agent',candidates:[{cause_ko:'연결 풀 부족'}]}}] as any, null), /^후보 · 연결 풀 부족/);
});
