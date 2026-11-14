import { test } from 'node:test';
import assert from 'node:assert/strict';
import { incidentSymptom, nextActionKo, publicAnswer, summaryKo, executionStageKo } from '../src/presentation.ts';

test('primary readings never fall back to raw titles, technical errors, commands or JSON', () => {
  for (const text of ['HTTPStatusError: server raw', '한글 요약 HTTPStatusError', '오류 {"raw":"data"}', '한글 kubectl get pods', '추적 http://internal/path', '이미지 sha256:' + 'a'.repeat(64), '긴 요약'.repeat(200)]) assert.equal(summaryKo(text, '확인 중'), '확인 중', text);
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
  assert.ok(publicAnswer('한글'.repeat(1000)).length <= 1601);
});
test('action guidance follows latest operational state and never claims ownership or recovery without it', () => {
  assert.match(nextActionKo({ status: 'handed_off', handoff: null }), /인수.*필요/);
  assert.match(nextActionKo({ status: 'handed_off', handoff: { claimed_by: 'operator' } }), /인수했습니다/);
  assert.match(nextActionKo({ status: 'action_pending' }), /승인하거나 거절/);
  assert.match(nextActionKo({ status: 'failed' }), /실패/);
  assert.match(nextActionKo({ status: 'closed' }), /종료/);
  assert.equal(executionStageKo('agent_progress', 'unknownRawCode'), '진행 상태를 확인하고 있습니다.');
});
