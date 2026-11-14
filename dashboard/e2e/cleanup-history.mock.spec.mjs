import { test, expect } from '@playwright/test';
import { capture, errors, layout, mock, resetMock, tab } from './p15-helpers.mjs';
// 정돈-02/03/04/05/07: 저장 이력 조회·필터·비용 표시·제품 역할 안내를 모의 API에서 확인한다(실제 서버·DB·모델 접속 없음).
test.skip(!mock, '정돈 계약 표본은 모의 API에서 검증합니다.');
test.beforeEach(async ({ request }) => resetMock(request));
const H = { 'X-LiveProbe-Client': 'dashboard' };

test('목록 환경·출처 필터는 서버로 전송되고 페이지 밖 사건을 찾는다', async ({ page }) => {
  const es = errors(page); const seen = [];
  // Only the list page request (limit=50); the header alert polls /incidents?status=… separately.
  page.on('request', r => { const u = new URL(r.url()); if (u.pathname === '/api/incidents' && u.searchParams.get('limit') === '50') seen.push(Object.fromEntries(u.searchParams)); });
  await page.goto('/incidents?environment=prd');
  await expect(page.locator('.incident-row')).toHaveCount(1);
  await expect(page.locator('.incident-row').first()).toContainText('#1976');
  await expect.poll(() => seen.at(-1)?.environment).toBe('prd');
  await page.getByText('상세 필터', {exact:true}).click();
  await expect(page.getByText('최대 50건')).toHaveCount(0);
  await page.getByRole('combobox', { name: '출처 필터' }).selectOption('evaluation');
  await expect(page.locator('.incident-list .empty')).toHaveText('조건에 맞는 장애가 없습니다.');
  await expect.poll(() => seen.at(-1)?.origin).toBe('evaluation');
  await layout(page); expect(es).toEqual([]);
});

test('비용은 부분 누락을 0으로 합치지 않고 확인분과 미수집 수를 함께 보여 준다', async ({ page, request }, info) => {
  const { items } = await (await request.get('/api/incidents?limit=200', { headers: H })).json();
  const base = items[0];
  const rows = [{ ...base, id: 5001, workload: 'partial-cost', total_cost_usd: null, known_cost_usd: 0.2, cost_missing_runs: 2, cost_status: 'partial', origin: 'unconfirmed' },
    { ...base, id: 5002, workload: 'zero-cost', total_cost_usd: 0, known_cost_usd: 0, cost_missing_runs: 0, cost_status: 'observed', origin: 'evaluation' }];
  await page.route('**/api/incidents?*', r => r.fulfill({ json: { items: rows, total: 2, as_of: 5002, newer_since_as_of: 0 } }));
  await page.goto('/incidents');
  await expect(page.locator('.incident-row').nth(0)).toContainText('확인분 $0.2000 · 2회 비용 미수집');
  await expect(page.locator('.incident-row').nth(0)).toContainText('출처 미확인');
  await expect(page.locator('.incident-row').nth(1)).toContainText('$0.0000');
  await expect(page.locator('.incident-row').nth(1)).toContainText('평가 실행');
  await layout(page); await capture(page, info, 'cleanup-costs');
});

test('과거 사건 조회는 해석을 시작하지 않고, 해석은 확인 후 명시 요청한다', async ({ page, request }) => {
  const { items } = await (await request.get('/api/incidents?limit=200', { headers: H })).json();
  const i = items.find(x => x.status === 'resolved') ?? items[0];
  const n = await (await request.get(`/api/incidents/${i.id}/narrative`, { headers: H })).json();
  Object.assign(n, { interpreting: false, interpret_state: { status: 'not_requested', reason_ko: '조회만으로는 한글 해석을 만들지 않습니다.' }, interpret_pending_count: 3 });
  let posts = 0, body = null;
  await page.route(`**/api/incidents/${i.id}/narrative`, r => r.fulfill({ json: n }));
  await page.route(`**/api/incidents/${i.id}/interpretations`, r => { posts++; body = r.request().postDataJSON(); return r.fulfill({ status: 202, json: { status: 'accepted', incident_id: i.id } }); });
  await page.goto(`/incidents/${i.id}`); await tab(page, '대화');
  await expect(page.getByText('조회만으로는 한글 해석을 만들지 않습니다.')).toBeVisible();
  await page.waitForTimeout(1500);
  expect(posts).toBe(0);                                             // viewing alone never requests interpretation
  await page.getByRole('button', { name: '한글 해석 요청 (3개 항목)' }).click();
  await expect(page.getByRole('group', { name: '한글 해석 요청 확인' })).toContainText('비용이 기록될 수 있습니다');
  expect(posts).toBe(0);
  await page.getByRole('button', { name: '해석 요청', exact: true }).click();
  await expect(page.getByText('한글 해석을 요청했습니다.')).toBeVisible();
  expect(posts).toBe(1); expect(body).toEqual({ confirm: true });
});

test('기존 코드 변경 기록 위에 제품 역할 안내를 표시한다', async ({ page, request }) => {
  const { items } = await (await request.get('/api/incidents?limit=200', { headers: H })).json();
  const i = items[0];
  const change = { id: 77, incident_id: i.id, system: 'samsung-trade', service: 'order', namespace_origin: 'prd', status: 'stg_verified', stages: {}, created_by: 'liveprobe', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), source_files: [], tests: {}, build: {} };
  await page.route('**/api/changes?*', r => r.fulfill({ json: { items: [change] } }));
  await page.goto(`/incidents/${i.id}`); await tab(page, '대화');
  await expect(page.getByRole('note')).toContainText('새 코드 수정·시험·환경 승격은 별도 제품인 LiveProbe for Application');
});

test('검토 원장 CSV 링크는 화면 조건과 같고 조건 일치 전체 건수를 밝힌다', async ({ page }) => {
  const seen = [];
  page.on('request', r => { const u = new URL(r.url()); if (u.pathname === '/api/review-ledger') seen.push(Object.fromEntries(u.searchParams)); });
  await page.goto('/ledger');
  await page.getByRole('combobox', { name: '환경 필터' }).selectOption('prd');
  await page.getByRole('combobox', { name: '사유 필터' }).selectOption('low_confidence');
  await expect.poll(() => seen.at(-1)?.environment).toBe('prd');
  expect(seen.at(-1).reason).toBe('low_confidence');
  const href = await page.getByRole('link', { name: /CSV 내보내기/ }).getAttribute('href');
  expect(href).toBe('/api/review-ledger.csv?reason=low_confidence&environment=prd');
  await expect(page.getByRole('link', { name: /CSV 내보내기/ })).toContainText('조건 일치');
});


test('기간과 서비스 범위는 검색과 페이지 주소에 함께 남는다', async ({ page }) => {
  await page.goto('/incidents');
  await page.getByText('상세 필터', {exact:true}).click();
  await page.getByLabel('시작일 (한국시간)').fill('2026-10-01');
  await page.getByLabel('종료일 (포함)').fill('2026-10-02');
  await page.getByLabel('시스템 ID').fill('samsung-trade');
  await page.getByLabel('서비스 이름').fill('order');
  const requested = page.waitForRequest(r => {
    const u = new URL(r.url());
    return u.pathname === '/api/incidents' && u.searchParams.get('service') === 'order';
  });
  await page.getByRole('button', {name:'검색',exact:true}).click();
  const api = new URL((await requested).url());
  for (const [key,value] of Object.entries({system:'samsung-trade', service:'order', start:'2026-10-01', end:'2026-10-02'})) {
    expect(api.searchParams.get(key)).toBe(value);
    expect(new URL(page.url()).searchParams.get(key)).toBe(value);
  }
});
