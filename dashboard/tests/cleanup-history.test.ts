import { test } from 'node:test';
import assert from 'node:assert/strict';
import { costKo, money } from '../src/cost.ts';
import { incidentListPath, ledgerQuery } from '../src/historyQuery.ts';
import { resourceBreakdown } from '../src/overviewModel.ts';

test('정돈-07: 비용은 전부 관측·일부 누락·전부 누락·실제 0·실행 없음을 구분한다', () => {
  assert.equal(costKo({ total_cost_usd: 0.3, known_cost_usd: 0.3, cost_missing_runs: 0, cost_status: 'observed' }), '$0.3000');
  assert.equal(costKo({ total_cost_usd: 0, known_cost_usd: 0, cost_missing_runs: 0, cost_status: 'observed' }), '$0.0000');
  assert.equal(costKo({ total_cost_usd: null, known_cost_usd: 0.2, cost_missing_runs: 2, cost_status: 'partial' }), '확인분 $0.2000 · 2회 비용 미수집');
  assert.equal(costKo({ total_cost_usd: null, known_cost_usd: null, cost_missing_runs: 3, cost_status: 'unobserved' }), '비용 미수집');
  assert.equal(costKo({ total_cost_usd: 0, known_cost_usd: 0, cost_missing_runs: 0, cost_status: 'no_runs' }), '실행 기록 없음');
  // An older server without cost_status keeps the previous wording; a missing total is never shown as $0.
  assert.equal(costKo({ total_cost_usd: null }), money(null));
  assert.notEqual(costKo({ total_cost_usd: null, known_cost_usd: 0.2, cost_missing_runs: 1, cost_status: 'partial' }), '$0.2000');
});

test('정돈-04: 목록 요청은 서버 필터를 보내고 둘째 페이지부터만 as_of로 고정한다', () => {
  const first = new URL(incidentListPath({ offset: 0, environment: 'prd', origin: 'evaluation', status: 'resolved', search: 'order', asOf: 99 }), 'http://x');
  assert.equal(first.searchParams.get('environment'), 'prd');
  assert.equal(first.searchParams.get('origin'), 'evaluation');
  assert.equal(first.searchParams.get('status'), 'resolved');
  assert.equal(first.searchParams.get('search'), 'order');
  assert.equal(first.searchParams.get('as_of'), null);          // page 1 always reads the newest
  const second = new URL(incidentListPath({ offset: 50, environment: 'prd', asOf: 99 }), 'http://x');
  assert.equal(second.searchParams.get('as_of'), '99');
  assert.equal(second.searchParams.get('offset'), '50');
  assert.equal(new URL(incidentListPath({ offset: 50 }), 'http://x').searchParams.get('as_of'), null);
});

test('정돈-04: 검토 원장 화면과 CSV는 같은 조건 문자열을 쓴다', () => {
  assert.equal(ledgerQuery({ reason: 'low_confidence', environment: 'prd' }), '?reason=low_confidence&environment=prd');
  assert.equal(ledgerQuery({ reason: 'low_confidence', environment: 'prd', limit: 100, offset: 200 }), '?limit=100&offset=200&reason=low_confidence&environment=prd');
  assert.equal(ledgerQuery({}), '');
});

test('정돈-05: 지도 상자는 자원 수와 서비스 수를 구분하고 종류를 표시한다', () => {
  const b = resourceBreakdown([
    { kind: 'Deployment', name: 'order' }, { kind: 'Ingress', name: 'order' }, { kind: 'Deployment', name: 'payment' },
    { kind: 'Service', name: 'order' }, { kind: 'ContractEndpoint', name: 'kafka' }, { kind: 'StatefulSet', name: 'db' },
  ]);
  assert.equal(b.resources, 5);           // contract endpoint is not a real resource
  assert.equal(b.services, 3);            // order, payment, db — the Ingress/Service named order are not extra services
  assert.equal(b.text, 'Deployment 2 · Ingress 1 · Service 1 · StatefulSet 1');
  assert.deepEqual(resourceBreakdown([{ kind: 'Ingress', name: 'a' }, { kind: 'Ingress', name: 'b' }]).services, 0);
});
