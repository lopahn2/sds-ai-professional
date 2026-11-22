import { test, expect } from '@playwright/test';
import { capture, errors, layout, mock, openInfraMap } from './p15-helpers.mjs';
// 부모의 API·대시보드 이미지 배포 뒤 명시적으로 켠다. mock 표본은 읽지 않는다.
test.skip(mock || process.env.LIVEPROBE_SAMSUNG_LIVE !== '1', '삼성 실배포 인수는 부모의 명시적 실행 이후에만 검증합니다.');
test('실배포에서 세 시스템 경계와 관측된 Kafka·계약을 확인한다', async ({ page, request }, info) => {
  const response = await request.get('/api/overview'); expect(response.ok()).toBe(true);
  const o = await response.json();
  expect(o.topology.contract_edges).toHaveLength(15);
  expect(o.topology.nodes.some(n => n.namespace === 'liveprobe-messaging' && n.name === 'kafka')).toBe(true);
  expect(o.topology.contract_edges.every(e => e.observed === false && e.deployment_state === 'deployed' && !('req_rate' in e) && !('flows' in e))).toBe(true);
  const es = errors(page); await page.goto('/'); await openInfraMap(page);
  // 2026-11-13 dev/prd 전환: 활성 환경은 dev·prd뿐이다. 퇴역 stg는 지도 환경 선택지에 없어야 한다.
  await expect(page.getByRole('combobox', { name: '지도 환경' }).locator('option')).toHaveText(['dev', 'prd', '전체 환경']);
  for (const env of ['dev', 'prd']) {
    await page.getByRole('combobox', { name: '지도 환경' }).selectOption(env);
    await expect(page.locator('.system-box')).toHaveCount(3);
    for (const name of ['삼성 트레이드 시스템', '삼성 딜리버리 시스템', '삼성 비즈니스 시스템']) await expect(page.locator('.system-box').filter({ hasText: name })).toHaveCount(1);
    await expect(page.locator('.contract-endpoint')).toHaveCount(0);
    await expect(page.locator('.react-flow__edge.declared_event')).toHaveCount(3);
    await expect(page.locator('.react-flow__edge.declared_snapshot_api')).toHaveCount(2);
    await layout(page); await capture(page, info, `samsung-live-${env}`);
  }
  await page.getByRole('combobox', { name: '지도 환경' }).selectOption('');
  await expect(page.locator('.environment-box')).toHaveCount(2);
  await expect(page.locator('.system-box')).toHaveCount(6);
  await layout(page); await capture(page, info, 'samsung-live-all');
  expect(es).toEqual([]);
});
