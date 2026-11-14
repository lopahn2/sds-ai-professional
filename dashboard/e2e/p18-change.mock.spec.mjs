// 실제 GitHub 호출·배포 없이 변경 요청 카드의 확인 경계만 검증한다.
import { test, expect } from '@playwright/test';
import { capture, layout, mock, resetMock, tab } from './p15-helpers.mjs';
test.skip(!mock, '모의 API 전용');
test.beforeEach(async ({ request }) => resetMock(request));
test('기존 변경 이력에 패치와 시험 결과를 표시한다', async ({ page, request }, info) => {
  await request.post('/api/mock/change', { headers: {'X-LiveProbe-Client':'dashboard'}, data: {incident_id:990} });
  await page.goto('/incidents/990'); await tab(page, '대화');
  const card = page.getByRole('article', { name: '변경 요청 1', exact:true });
  await expect(card).toContainText('회귀 시험'); await expect(card).toContainText('Application 관제');
  await card.getByText('패치 원문 보기').click();
  await expect(card.locator('pre')).toContainText('+++ b/tests/test_fix.py');
  await expect(card.getByRole('button',{name:/로 승격/})).toHaveCount(0);
  // 2026-11-13: dev 이후 갈 수 없는 퇴역 stg 칸은 '대기'가 아니라 퇴역·해당 없음으로 보인다.
  await expect(card.getByRole('list', { name: '변경 단계' })).toContainText('stg(퇴역·이력) · 퇴역·해당 없음');
  // 모의 API도 실제 서버처럼 새 승격(특히 퇴역 stg)을 410으로 거절하고 상태를 바꾸지 않는다.
  for (const stage of ['dev', 'stg', 'prd']) expect((await request.post('/api/changes/1/promote', { headers: {'X-LiveProbe-Client':'dashboard'}, data: {stage, plan_sha256:'a'.repeat(64), confirm:true} })).status()).toBe(410);
  expect((await (await request.get('/api/changes/1')).json()).status).toBe('built');
  expect(await card.evaluate(e=>e.getBoundingClientRect().height)).toBeGreaterThan(250);
  await card.evaluate(e=>e.scrollIntoView({block:'start'})); await layout(page); await capture(page,info,'p18-change-dev-verified');
});
