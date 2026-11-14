import { test, expect } from '@playwright/test';
import { mock, resetMock, tab } from './p15-helpers.mjs';

// P20 · naria 디자인 시스템 리뉴얼 1차: 운영자 결정이 필요한 장애가 화면마다 먼저 보이는지 확인한다.
test.skip(!mock, '모의 API 전용');
test.beforeEach(async ({ request }) => { await resetMock(request); });
const H = { headers: { 'X-LiveProbe-Client': 'dashboard' } };
async function pending(request) {
  const { items } = await (await request.get('/api/incidents?limit=200')).json();
  const i = items.find(i => i.severity_level !== 'HIGH_RISK' && i.status === 'resolved') ?? items[0];
  const a = await (await request.post('/api/mock/pending-action', { ...H, data: { incident_id: i.id } })).json();
  return { i, a };
}

test('디자인 시스템 토큰과 서체가 화면 전체에 적용된다', async ({ page }) => {
  await page.goto('/incidents');
  const theme = page.locator('.nds-theme').first();
  await expect(theme).toHaveAttribute('data-nds-theme', 'light');
  expect(await page.locator('.page h1').evaluate(e => getComputedStyle(e).fontFamily)).toContain('Wanted Sans');
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => document.fonts.check('600 16px "Wanted Sans"', '장애 Aa'))).toBe(true);
  const vars = await theme.evaluate(e => { const s = getComputedStyle(e); return [s.getPropertyValue('--text').trim(), s.getPropertyValue('--nds-text').trim()]; });
  expect(vars[0]).toBe(vars[1]);
  // 메뉴 아이콘은 문자가 아니라 SVG다.
  await expect(page.locator('.app>.nav .nav-icon svg')).toHaveCount(5);
});

test('운영 관제 상단은 지금 대응할 일을 보여 주고 없는 집계는 미관측이다', async ({ page }) => {
  await page.goto('/');
  const queue = page.getByRole('region', { name: '지금 대응할 일' });
  await expect(queue).toBeVisible();
  await expect(queue.getByRole('status')).toContainText('운영자 결정이 필요한 장애는 없습니다');
  const counts = queue.locator('.incident-counts');
  await expect(counts).toContainText('승인 대기0');
  await expect(counts).toContainText('미인수 이관미관측');
  const first = queue.locator('.queue-item').first();
  await expect(first).toContainText('AI 분석 중');
  // 상단 띠가 지도를 밀어내지 않는다: 페이지 가로 넘침이 없다.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await first.click();
  await expect(page).toHaveURL(/\/incidents\/\d+$/);
});

test('승인 대기 장애는 대기열·알림·목록에서 분석 중 장애보다 먼저 온다', async ({ page, request }) => {
  const { i } = await pending(request);
  await page.goto('/');
  const queue = page.getByRole('region', { name: '지금 대응할 일' });
  await expect(queue.getByRole('status')).toContainText('운영자 결정이 필요한 장애가 1건', { timeout: 12000 });
  const first = queue.locator('.queue-item').first();
  await expect(first).toContainText('승인·거절 필요');
  await expect(first).toHaveAttribute('href', `/incidents/${i.id}`);
  const bell = page.locator('.alert-button');
  await expect(bell).toHaveClass(/active/);
  await bell.click();
  await expect(page.locator('.alert-item').first()).toContainText('승인·거절 필요');
  await page.keyboard.press('Escape');
  await page.goto('/incidents');
  const strip = page.getByRole('region', { name: '운영자 결정이 필요한 장애' });
  await expect(strip).toContainText('내 결정이 필요한 장애 1건', { timeout: 12000 });
  await expect(strip.getByRole('link')).toHaveAttribute('href', `/incidents/${i.id}`);
});

test('장애 목록의 빠른 필터는 대응 중 상태를 먼저 두고 주소에 남긴다', async ({ page }) => {
  await page.goto('/incidents');
  const nav = page.getByRole('navigation', { name: '장애 상태별 보기' });
  await expect(nav.getByRole('button')).toHaveText(['전체', '승인 대기', '운영자 이관', '분석 중', '자동 복구', '운영자 종료', '처리 실패']);
  await nav.getByRole('button', { name: '분석 중', exact: true }).click();
  await expect(page).toHaveURL(/status=analyzing/);
  await expect(nav.getByRole('button', { name: '분석 중', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.incident-row')).toHaveCount(1);
});

test('관제실의 지금 할 일 버튼은 승인 카드로 이동하고 포커스를 옮긴다', async ({ page, request }) => {
  const { i, a } = await pending(request);
  await page.goto(`/incidents/${i.id}`);
  const next = page.locator('.headline-next.needs-decision');
  await expect(next).toContainText('지금 할 일', { timeout: 12000 });
  await next.getByRole('button', { name: '승인 카드로 이동' }).click();
  const card = page.getByRole('article', { name: `조치 승인 요청 ${a.id}` });
  await expect(card).toBeInViewport();
  // 카드가 접히거나 잘리지 않고 승인·거절 버튼이 실제로 보인다.
  for (const name of ['승인', '거절']) await expect(card.getByRole('button', { name, exact: true })).toBeInViewport();
  expect((await card.boundingBox()).height).toBeGreaterThan(200);
  expect(await card.evaluate(e => e.contains(document.activeElement))).toBe(true);
});

test('결정이 필요 없는 진행 중 장애에는 지금 할 일 띠가 없다', async ({ page }) => {
  await page.goto('/incidents/1976');
  await tab(page, '흐름');
  await expect(page.locator('.headline-next')).toContainText('다음');
  await expect(page.locator('.headline-next.needs-decision')).toHaveCount(0);
});
