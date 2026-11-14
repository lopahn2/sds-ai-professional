import { test, expect } from '@playwright/test';
import { capture, errors, layout, mock, resetMock } from './p15-helpers.mjs';
test.skip(!mock, '다중 환경 계약 표본은 모의 API에서 검증합니다.');
test.beforeEach(async ({ request }) => resetMock(request));
const allInside = page => page.locator('.resource-button').evaluateAll(es => {
  const f = document.querySelector('.topology-canvas .react-flow').getBoundingClientRect();
  return es.every(e => { const r = e.getBoundingClientRect(); return r.left >= f.left - 1 && r.right <= f.right + 1 && r.top >= f.top - 1 && r.bottom <= f.bottom + 1; });
});
// 2026-11-13 dev/prd 전환: 표본 overview-envs.json은 전환 전 서버처럼 퇴역 stg 환경·자원을 아직 담고 있다(배포 순서 차이 재현).
// 새 기대: 지도 환경 선택지·전체 보기는 활성 dev·prd만이다(예전 기대: dev·stg·prd 3환경, 자원 21개, 환경 상자 3개).
test('환경 선택·시스템 여섯 묶음·장애 위치·관제실 이동은 세 화면 폭에서 유지된다', async ({ page }, info) => {
  if (info.project.name === 'width-1440') await page.setViewportSize({ width: 1440, height: 900 });
  const es = errors(page); await page.goto('/');
  const scope = page.getByRole('combobox', { name: '지도 환경' });
  await expect(scope).toHaveValue('prd');
  await expect(scope.locator('option')).toHaveText(['dev', 'prd', '전체 환경']);
  await expect(scope.locator('option[value="stg"]')).toHaveCount(0);
  await expect(page.getByLabel('환경별 관측 요약').locator('.environment-chip b')).toHaveText(['dev', 'prd']);
  await expect(page.locator('.system-box')).toHaveCount(6);
  await expect(page.locator('.resource-button')).toHaveCount(7);
  // 정돈-05: the heading no longer calls resources “services”; services (distinct workloads) and resources are counted apart.
  await expect(page.locator('.system-box').first()).toContainText('order');
  await expect(page.locator('.system-box').first()).toContainText('서비스 2개 · 자원 2개');
  await expect(page.locator('.system-box').first()).toContainText('Deployment 2');
  await expect(page.getByLabel('환경별 관측 요약')).toContainText('진행 중 장애 1건');
  await expect.poll(() => allInside(page)).toBe(true);
  await layout(page); await capture(page, info, 'p18-envs-prd');
  const before = await page.locator('.react-flow__viewport').getAttribute('style');
  await page.getByRole('button', { name: '지도 확대', exact: true }).click();
  await expect(page.locator('.react-flow__viewport')).not.toHaveAttribute('style', before);
  for (const env of ['dev']) {
    await scope.selectOption(env);
    await expect(page.locator('.system-box')).toHaveCount(6);
    await expect(page.locator('.incident-pin')).toHaveCount(0);
    expect(await page.locator('.react-flow__node-resource').evaluateAll(es => es.every(e => e.dataset.id.startsWith(`${document.querySelector('[aria-label="지도 환경"]').value}/`)))).toBe(true);
    await expect.poll(() => allInside(page)).toBe(true); await layout(page);
  }
  await scope.selectOption('');
  // 7개 서비스 × 활성 환경 2개. 퇴역 stg 자원은 응답에 있어도 그리지 않는다.
  await expect(page.locator('.environment-box')).toHaveCount(2);
  await expect(page.locator('.resource-button')).toHaveCount(14);
  expect(await page.locator('.react-flow__node-resource').evaluateAll(es => es.filter(e => e.dataset.id.startsWith('stg/')).length)).toBe(0);
  await expect(page.locator('.resource-top small').first()).toHaveText('catalog');
  await expect.poll(() => allInside(page)).toBe(true); await layout(page); await capture(page, info, 'p18-envs-all');
  await scope.selectOption('prd');
  await expect(page.locator('.resource-place')).toHaveText('prd · order · inventory');
  await page.getByRole('button', { name: '진행 중 장애 1건', exact: true }).click();
  await expect(page.locator('.alert-item')).toContainText('prd · order · inventory');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'inventory 장애 1976 관제실 열기', exact: true }).click();
  await expect(page).toHaveURL(/\/incidents\/1976$/);
  await expect(page.locator('.headline-place')).toHaveText('prd · order · inventory');
  await layout(page); await capture(page, info, 'p18-envs-room'); expect(es).toEqual([]);
});
test('장애 목록의 환경 필터는 서버에서 전체 이력에 적용된다', async ({ page }, info) => {
  // 정돈-04: the filter used to apply to the current page only; it now runs on the server over the whole history.
  await page.goto('/incidents');
  const scope = page.getByRole('combobox', { name: '장애 환경 필터' });
  // 과거 이력 조회를 위해 stg 필터는 남기되 퇴역·이력으로 구분한다(값은 그대로 stg).
  await expect(scope.locator('option')).toHaveText(['전체 환경', 'dev', 'stg (퇴역·이력)', 'prd']);
  await expect(page.locator('.environment-filter-note')).toContainText('stg는 퇴역 환경으로 과거 이력만 조회합니다');
  await scope.selectOption('prd');
  await expect(page.locator('.incident-row')).toHaveCount(1);
  await expect(page.locator('.incident-row')).toContainText('prd · order · inventory');
  await expect(page.locator('.environment-filter-note')).toContainText('서버에서 전체 이력에 먼저 적용');
  await expect(page.locator('.badge').filter({ hasText: '조건 일치 1건' })).toBeVisible();
  await scope.selectOption('dev'); await expect(page.locator('.incident-list')).toContainText('조건에 맞는 장애가 없습니다');
  const stgRequest = page.waitForRequest(r => { const u = new URL(r.url()); return u.pathname === '/api/incidents' && u.searchParams.get('environment') === 'stg'; });
  await scope.selectOption('stg');
  await stgRequest;
  await expect(page).toHaveURL(/environment=stg/);
  await expect(page.locator('.incident-list')).toContainText('조건에 맞는 장애가 없습니다');
  await layout(page); await capture(page, info, 'p18-envs-list');
});
