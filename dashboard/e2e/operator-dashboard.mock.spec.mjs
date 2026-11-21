import { test, expect } from '@playwright/test';
import { mock, resetMock } from './p15-helpers.mjs';

test.skip(!mock, '모의 API 전용');
test.beforeEach(async ({ request }) => resetMock(request));

test('선택한 카드를 유지하고 흐름도에서 현재 작업으로 돌아간다', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/incidents/1976');
  const panel = page.locator('.step-panel');
  await panel.waitFor({state:'attached'});
  const mobileTabs = page.getByRole('navigation',{name:'관제실 화면 전환'});
  if (await mobileTabs.isVisible()) await mobileTabs.getByRole('button',{name:'현재 단계'}).click();
  const heading = panel.getByRole('heading',{level:2});
  const initial = await heading.textContent();
  const initialId = await panel.getAttribute('data-selected-step');
  const back = page.getByRole('button',{name:'흐름으로 돌아가기',exact:false});
  if (await back.isVisible()) await back.click();
  const node = page.locator('.stage-rail-viewport button[data-stage-id][aria-pressed="false"]').first();
  const id = await node.getAttribute('data-stage-id');
  await node.focus();
  await node.press('Enter');
  await expect(panel).toHaveAttribute('data-selected-step',id);
  await expect(panel.getByRole('button', {name:/다음 작업|현재 작업으로/})).toHaveCount(0);
  if (await back.isVisible()) await back.click();
  const full = page.getByRole('button',{name:'전체 흐름',exact:true});
  if (await full.isVisible()) await full.click();
  const current = page.locator('.react-flow__node').and(page.locator(`[data-id=${JSON.stringify(initialId)}]`)).getByRole('button');
  await current.focus();
  await current.press('Enter');
  await expect(heading).toHaveText(initial);
  const tabs = page.getByRole('navigation', { name: '관제실 화면 전환' });
  const tabsVisible = await tabs.isVisible();
  const paneHeading = page.locator('.conversation-pane .pane-heading');
  await (tabsVisible ? tabs : paneHeading).getByRole('button', { name: '전체 대화·이력' }).click();
  await expect(page.getByRole('heading', { name: '분석 · 운영자 대화' })).toBeVisible();
  await expect(panel).toHaveCount(0);
  await (tabsVisible ? tabs : paneHeading).getByRole('button', { name: tabsVisible ? '현재 단계' : '단계 관제로' }).click();
  await expect(panel).toBeVisible();
  expect(await panel.evaluate(e => getComputedStyle(e).animationName)).toBe('none');
});

for (const status of [404, 403, 500]) {
  test(`통합 Application 카탈로그 ${status} 응답을 구분한다`, async ({ page }) => {
    const incident = {id:'original-order',service:'order',state:'completed',reason:'재고 조회가 복구됐습니다.',environment:'dev',system:'trade'};
    await page.route('**/api/console/incidents?*',route=>route.fulfill({json:{application_coverage:'available',total:1,items:[{
      ...incident,native_id:incident.id,kind:'application',application_state:incident.state,workload:incident.service,title:incident.reason,
      status:'resolved',signal_reasons:[],created_at:null,updated_at:null,
    }]}}));
    await page.route('**/api/application/v1/incidents/original-order/summary', route => route.fulfill({json:incident}));
    await page.route('**/api/unified/catalog', route => route.fulfill({ status, json: { detail: status === 404 ? 'Not Found' : 'unavailable' } }));
    const response = page.waitForResponse(r => r.url().endsWith('/api/unified/catalog'));
    await page.goto('/application');
    await response;
    const intake=page.getByRole('region',{name:'새 애플리케이션 사건'});
    await expect(intake).toContainText(status===404?'이전 설치 버전':status===403?'조회할 권한이 필요':'조회가 지연');
    await expect(intake.getByRole('button',{name:'다시 확인'})).toBeVisible();
    await expect(intake.locator('form')).toHaveCount(0);
    await expect(page.locator('.application-page a[href*="application.liveprobe.localhost"]')).toHaveCount(0);
    const saved=page.locator('.incident-list .incident-row').filter({hasText:'재고 조회가 복구됐습니다.'});
    await expect(saved).toHaveAttribute('href','/incidents/application/original-order');
    await saved.click();
    await expect(page.getByRole('region',{name:'장애 상황 요약'})).toContainText('dev · trade');
  });
}
