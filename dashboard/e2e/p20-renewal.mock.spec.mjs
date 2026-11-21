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
  await expect(page.locator('.app>.nav .nav-icon svg')).toHaveCount(4);
});

test('홈에서 지도·운영자 개입·자동 처리·이관 이력을 구분한다', async ({ page }) => {
  await page.goto('/');
  const rail = page.getByRole('complementary',{name:'운영자 작업'});
  await expect(page.getByRole('heading',{name:'서비스 지도'})).toBeVisible();
  await expect(rail.locator('.atlas-auto-title')).toContainText('자동 처리 중');
  await expect(rail.getByRole('link',{name:/전체 기간 미인수/})).toHaveAttribute('href','/incidents?status=handed_off');
  const first=rail.locator('.atlas-cases a').filter({hasText:'AI 분석 중'}).first();
  await first.click();await expect(page).toHaveURL(/\/incidents\/\d+$/);
});

test('승인 대기 장애는 대기열·알림·목록에서 분석 중 장애보다 먼저 온다', async ({ page, request }) => {
  const { i } = await pending(request);
  await page.goto('/');
  const first = page.locator('.atlas-interventions .atlas-cases a').first();
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

// ---- P20 3차: 관제실이 구성·근거·검증을 설명한다 (과제계획서 대조 1~9번) ----
async function openHistory(page, id) {
  await page.goto(`/incidents/${id}`);
  await page.getByRole('button', {name:'전체 대화·이력', exact:true}).filter({visible:true}).first().click();
  const history = page.getByText(/지난 분석 단계 \d+개 보기/);
  await history.click();
}
const expand = async card => { await card.getByRole('button', { name: '펼치기' }).click(); };

test('그룹 카드는 접힌 상태에서도 역할별 모델과 추론 강도를 보여 준다', async ({ page, request }) => {
  const n = await (await request.get('/api/incidents/971/narrative')).json();
  const roles = n.flow.nodes.find(x => x.id === 'group').card.roles;
  await openHistory(page, 971);
  const list = page.locator('.step-card.type-group').getByRole('list', { name: '역할별 모델 배치' });
  await expect(list.getByRole('listitem')).toHaveCount(roles.length);
  for (const r of roles) await expect(list.locator(`[data-role="${r.role}"]`)).toContainText(r.model);
  await expect(list.locator('[data-role="cause_reasoner"]')).toContainText('추론 높음');
  // 서로 다른 모델이 섞여 배치된 것이 보인다.
  expect(new Set(roles.map(r => r.model)).size).toBeGreaterThan(1);
});

test('수준 판단 카드는 주 분석 모델과 선택 이유를, 펼치면 후보 점수 표를 보여 준다', async ({ page }) => {
  await openHistory(page, 971);
  const card = page.locator('.step-card.type-router');
  await expect(card.locator('.model-choice')).toContainText('주 분석 모델');
  await expect(card.locator('.model-choice')).toContainText('gpt-5.6-terra');
  await expect(card.getByRole('table', { name: '분석 모델 후보 점수' })).toHaveCount(0);
  await expand(card);
  const table = card.getByRole('table', { name: '분석 모델 후보 점수' });
  await expect(table.getByRole('row').nth(1)).toContainText('0.82');
  await expect(table.locator('tr.selected')).toContainText('선택');
});

test('승격된 단계와 재실행 단계는 승격 사유와 모델 변화를 보여 준다', async ({ page }) => {
  await openHistory(page, 971);
  const escalated = page.locator('.step-card.status-escalated').first();
  await expect(escalated.locator('.escalation-note')).toContainText('상위 모델로 승격');
  await expect(escalated.locator('.escalation-note')).toContainText('인용문 1건이 원본 근거에서 확인되지 않았습니다.');
  await expect(escalated.locator('.escalation-path')).toContainText('gpt-5.6-terra');
  await expect(escalated.locator('.escalation-path')).toContainText('gpt-6-sol');
  // 영문 사유 원문은 요청하기 전에는 DOM에 없다.
  await expect(escalated.getByText('1 quote(s) not found in cited sources')).toHaveCount(0);
  await escalated.getByText('승격 사유 원문 보기').click();
  await expect(escalated.getByText('1 quote(s) not found in cited sources')).toBeVisible();
  await expect(page.locator('.step-card[data-node-id="run:4097"] .escalation-note')).toContainText('승격 후 재실행');
});

test('그룹 카드를 펼치면 승격·종료 같은 진행 기록이 보인다', async ({ page }) => {
  await openHistory(page, 971);
  const card = page.locator('.step-card.type-group');
  await expand(card);
  const progress = card.getByRole('list', { name: 'Group 진행 기록' });
  await expect(progress).toContainText('상위 모델 재실행');
  await expect(progress).toContainText('Group 종료: 검증 통과');
});

test('분석 단계는 원인 후보·근거 인용·반대 근거를 보여 주고 인용 원문은 요청할 때만 연다', async ({ page }) => {
  await openHistory(page, 971);
  const card = page.locator('.step-card[data-node-id="run:4099"]');     // 반론 검토
  await expand(card);
  const basis = card.locator('.agent-basis');
  await expect(basis.locator('.cause-list > li')).toHaveCount(2);
  await expect(basis.locator('.cause-refs').first()).toContainText('E2_pods');
  await expect(basis).toContainText('인용 2건 · 수집된 근거로 확인되는 출처 2건');
  await expect(basis).toContainText('Pod 상태');
  await expect(basis).toContainText('반대 근거 2건');
  await expect(basis.locator('.counter-list').first()).toContainText('상태 점검이 실패했습니다');
  await expect(basis.locator('.quote-list')).toHaveCount(0);
  await basis.getByText('인용 원문 보기 (2건)').click();
  await expect(basis.locator('.quote-list > li')).toHaveCount(2);
  await expect(basis.locator('.quote-list .raw-text').first()).toContainText('"last_exit_code": 137');
  await expect(basis.locator('.quote-meta').first()).toContainText('후보 1 지지');
});

test('수집되지 않은 근거 번호를 가리킨 인용은 경고로 드러난다', async ({ page }) => {
  await openHistory(page, 971);
  const card = page.locator('.step-card.status-escalated').first();
  await expand(card);
  await expect(card.locator('.agent-basis')).toContainText('수집되지 않은 근거 번호를 가리킨 인용이 있습니다');
  await expect(card.locator('.agent-basis .notice')).toContainText('E99_unknown');
  await expect(card.locator('.agent-basis')).toContainText('인용 3건 · 수집된 근거로 확인되는 출처 2건');
});

test('검증 단계는 판정과 후보별 수락 여부, 검증 대상을 보여 준다', async ({ page }) => {
  await openHistory(page, 971);
  const card = page.locator('.step-card.type-verify');
  await expand(card);
  const result = card.getByLabel('독립 검증 결과');
  await expect(result).toHaveClass(/ok/);
  await expect(result.locator('.verify-head')).toContainText('근거로 지지');
  await expect(result.locator('.verify-head')).toContainText('다음 행동 · Kubernetes 조치 제안');
  await expect(result.locator('.verdict-list > li')).toHaveCount(1);
  await expect(result.locator('.verdict-list > li')).toContainText('후보 1');
  await expect(result.locator('.verdict-list > li')).toContainText('검증 대상');
});

test('참고 문서를 인용한 단계는 문서 이름과 발췌를 보여 준다', async ({ page }) => {
  await openHistory(page, 971);
  const card = page.locator('.step-card[data-node-id="run:4100"]');     // 복구 계획
  await expand(card);
  const list = card.getByRole('list', { name: '이 단계가 인용한 참고 문서' });
  await expect(list.getByRole('listitem')).toHaveCount(1);
  await expect(list).toContainText('liveprobe-judgement-guide.md');
  await expect(list).toContainText('판단 참고자료');
  await list.getByText('문서 발췌 보기').click();
  await expect(list.locator('.raw-text')).toContainText('되돌릴 수 있는 완화 조치');
  // 인용하지 않은 단계에는 참고 문서 영역이 없다.
  await expect(page.locator('.step-card[data-node-id="run:4099"] .citation-list')).toHaveCount(0);
});

test('상황 요약은 사건의 모델 호출 수·모델별 호출·토큰·비용·시간을 보여 준다', async ({ page, request }) => {
  const n = await (await request.get('/api/incidents/971/narrative')).json();
  const runs = n.flow.nodes.filter(x => x.agent_run_id != null || (x.model != null && ['done', 'failed', 'escalated'].includes(x.status)));
  expect(runs.length).toBeGreaterThan(3);
  await page.goto('/incidents/971');
  const usage = page.getByLabel('AI 사용 요약');
  await expect(usage).toContainText(`${runs.length}회`);
  await expect(usage).toContainText('gpt-6-sol');
  await expect(usage).toContainText('gpt-5.6-terra');
  const tokensIn = runs.reduce((a, x) => a + x.tokens.in, 0);
  await expect(usage).toContainText(tokensIn.toLocaleString('ko-KR'));
  await expect(usage).toContainText('비용');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
