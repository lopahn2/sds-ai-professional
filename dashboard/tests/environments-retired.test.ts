import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ACTIVE_ENVIRONMENTS, RETIRED_ENVIRONMENTS, HISTORY_ENVIRONMENTS, environmentFilterLabel, historyEnvironment, historyEnvironmentOptions, isRetiredEnvironment } from '../src/environments.ts';
import { incidentListPath, ledgerQuery } from '../src/historyQuery.ts';
import { placeKo } from '../src/labels.ts';

// 2026-11-13 dev/prd 전환: 활성 환경은 dev·prd, stg는 퇴역(이력 전용). 백엔드 liveprobe/app/environments.py와 같은 값이어야 한다.
test('활성·퇴역·이력 환경 정의는 백엔드 공유 모듈과 같다', () => {
  assert.deepEqual([...ACTIVE_ENVIRONMENTS], ['dev', 'prd']);
  assert.deepEqual([...RETIRED_ENVIRONMENTS], ['stg']);
  assert.deepEqual([...HISTORY_ENVIRONMENTS], ['dev', 'stg', 'prd']);
  assert.equal(isRetiredEnvironment('stg'), true);
  for (const e of ['dev', 'prd', '', null, undefined, 'liveprobe-target']) assert.equal(isRetiredEnvironment(e), false, String(e));
});

test('이력 필터는 stg를 지우지 않고 퇴역·이력으로 구분해 보이며 값은 그대로 stg다', () => {
  const options = historyEnvironmentOptions();
  assert.deepEqual(options.map(o => o.value), ['dev', 'stg', 'prd']);
  assert.deepEqual(options.map(o => o.label), ['dev', 'stg (퇴역·이력)', 'prd']);
  assert.equal(environmentFilterLabel('prd'), 'prd');
  assert.equal(environmentFilterLabel('stg'), 'stg (퇴역·이력)');
});

test('URL의 환경 필터는 알려진 이력 환경(퇴역 stg 포함)만 받고 나머지는 전체로 되돌린다', () => {
  assert.equal(historyEnvironment('stg'), 'stg');
  assert.equal(historyEnvironment('dev'), 'dev');
  assert.equal(historyEnvironment('prd'), 'prd');
  for (const raw of [null, undefined, '', 'STG', 'qa', 'stg ', 'liveprobe-target']) assert.equal(historyEnvironment(raw), '', String(raw));
});

test('과거 stg 사건·검토 기록 조회와 CSV는 environment=stg 그대로 서버에 보낸다', () => {
  assert.equal(incidentListPath({ offset: 0, environment: historyEnvironment('stg') }), '/incidents?limit=50&offset=0&environment=stg');
  assert.equal(ledgerQuery({ environment: 'stg' }), '?environment=stg');
  assert.equal(ledgerQuery({ environment: 'stg', reason: 'low_confidence', offset: 100, limit: 100 }), '?limit=100&offset=100&reason=low_confidence&environment=stg');
  // 과거 사건의 위치 표시는 기록된 환경 이름을 바꾸지 않는다.
  assert.equal(placeKo('stg', 'samsung-trade', 'order'), 'stg · 삼성 트레이드 시스템 · order');
});
