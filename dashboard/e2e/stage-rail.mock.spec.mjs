import { expect, test } from '@playwright/test';
import { mock } from './p15-helpers.mjs';
import { mockUnifiedRoom } from './fixtures/unified-room.mjs';

test.skip(!mock, '격리된 관제실 모의 서버 전용');

const rail = page => page.locator('.stage-rail-viewport');
const cards = page => rail(page).locator('button[data-stage-id]');
const detail = page => page.locator('.semantic-stage');
const card = (page, id) => cards(page).and(page.locator('[data-stage-id="' + id + '"]'));
const before = page => page.getByRole('button', {name:'이전 카드 둘러보기'});
const after = page => page.getByRole('button', {name:'다음 카드 둘러보기'});

async function openRoom(page, kind) {
  if (kind === 'Kubernetes') {
    await page.goto('/incidents/971');
  } else {
    const { application } = await mockUnifiedRoom(page, {state:'awaiting_prd_approval'});
    await page.route('**/api/application/v1/incidents/native-a/summary', route => route.fulfill({
      json:{id:application.id, state:application.state, environment:application.environment,
        service:application.service, created_at:application.created_at, reason:'재고 요청을 조사합니다.'},
    }));
    await page.goto('/incidents/application/' + application.id);
  }
  await expect(rail(page)).toBeVisible();
  await expect.poll(() => cards(page).count()).toBeGreaterThanOrEqual(7);
}

async function showFlow(page) {
  const back = page.getByRole('button', {name:/흐름으로 돌아가기/});
  if (await back.isVisible()) await back.click();
  await expect(rail(page)).toBeVisible();
}

// Read painted card bounds inside the clipping viewport, not CSS sizing variables.
async function layout(page) {
  return rail(page).evaluate(viewport => {
    const bounds = viewport.getBoundingClientRect();
    const fractions = [...viewport.querySelectorAll('button[data-stage-id]')].map(button => {
      const r = button.getBoundingClientRect();
      const width = Math.max(0, Math.min(r.right, bounds.right) - Math.max(r.left, bounds.left));
      return {id:button.dataset.stageId, fraction:width/r.width};
    });
    const visible = fractions.filter(c => c.fraction > 0.03);
    return {first:visible.at(0), last:visible.at(-1), full:visible.filter(c => c.fraction >= 0.99).length,
      width:viewport.clientWidth, left:viewport.scrollLeft, max:viewport.scrollWidth - viewport.clientWidth};
  });
}

async function expectEdge(page, edge) {
  const start = edge === 'start';
  const window = page.locator('.stage-rail-window');
  await expect.poll(async () => {
    const {left,max} = await layout(page);
    return Math.abs(start ? left : max - left);
  }).toBeLessThan(2);
  await expect(window).not.toHaveAttribute(start ? 'data-before' : 'data-after', 'true');
  await expect(window).toHaveAttribute(start ? 'data-after' : 'data-before', 'true');
  await expect(start ? before(page) : after(page)).toBeDisabled();
  await expect(start ? after(page) : before(page)).toBeEnabled();
}

for (const kind of ['Kubernetes', 'Application']) {
  test(kind + ' 단계 레일에서 실제 반 카드, 양쪽 페이드, 선택과 전체 흐름을 확인한다', async ({page}) => {
    await page.emulateMedia({reducedMotion:'reduce'});
    await openRoom(page, kind);
    const middle = cards(page).nth(3);
    const id = await middle.getAttribute('data-stage-id');
    await middle.click();
    await expect(detail(page)).toHaveAttribute('data-selected-step',id);
    await expect(detail(page)).toBeVisible();
    await showFlow(page);
    await expect(middle).toHaveAttribute('aria-pressed','true');
    await expect.poll(async () => {
      const {first,last,full,width} = await layout(page);
      const isHalf = stage => stage && stage.fraction >= 0.32 && stage.fraction <= 0.68;
      return {bothPeek:!!(isHalf(first) && isHalf(last)), enoughFullCards:full === (width >= 600 ? 3 : 1)};
    }).toEqual({bothPeek:true, enoughFullCards:true});
    const window = page.locator('.stage-rail-window');
    await expect(window).toHaveAttribute('data-before','true');
    await expect(window).toHaveAttribute('data-after','true');

    // A coordinate click cannot scroll an off-screen card into view before choosing it.
    const next = (await layout(page)).last.id;
    const point = await card(page,next).evaluate(button => {
      const c = button.getBoundingClientRect(), v = button.closest('.stage-rail-viewport').getBoundingClientRect();
      return {x:(Math.max(v.left,c.left) + Math.min(v.right,c.right))/2, y:(c.top + c.bottom)/2};
    });
    await page.mouse.click(point.x,point.y);
    await expect(detail(page)).toHaveAttribute('data-selected-step', next);
    await expect(detail(page)).toBeVisible();
    await showFlow(page);
    const chosen = card(page,next);
    await expect.poll(() => chosen.evaluate(button => {
      const c = button.getBoundingClientRect(), v = button.closest('.stage-rail-viewport').getBoundingClientRect();
      return c.left >= v.left - 1 && c.right <= v.right + 1;
    })).toBe(true);
    await page.getByRole('button',{name:'전체 흐름',exact:true}).click();
    await expect(page.locator('.react-flow__node[data-id="' + next + '"] button')).toHaveAttribute('aria-pressed','true');
    await page.getByRole('button',{name:'단계 흐름',exact:true}).click();
    await expect(chosen).toHaveAttribute('aria-pressed','true');
    await expect(detail(page)).toHaveAttribute('data-selected-step', next);

    await chosen.focus();
    await chosen.press('Home');
    await expect(cards(page).first()).toBeFocused();
    await expectEdge(page,'start');
    await expect(detail(page)).toHaveAttribute('data-selected-step', next);
    await cards(page).first().press('End');
    await expect(cards(page).last()).toBeFocused();
    await expectEdge(page,'end');
    await expect(detail(page)).toHaveAttribute('data-selected-step', next);
    await cards(page).last().press('Enter');
    await expect(detail(page)).toHaveAttribute('data-selected-step', await cards(page).last().getAttribute('data-stage-id'));
    await expect(detail(page)).toBeVisible();
  });
}

test('휠·탐색 버튼은 선택을 유지하며 실제 활동 갱신이 도착해도 둘러본 위치가 유지된다', async ({page}) => {
  await page.emulateMedia({reducedMotion:'reduce'});
  const reads = [];
  page.on('response',response => {
    if (new URL(response.url()).pathname === '/api/application/v1/incidents/native-a/activity' && response.ok()) reads.push(response);
  });
  await openRoom(page,'Application');
  const current = await detail(page).getAttribute('data-selected-step');
  await expectEdge(page,'end');
  const end = (await layout(page)).left;
  await before(page).click();
  await expect.poll(async () => (await layout(page)).left).toBeLessThan(end-4);
  const browsed = (await layout(page)).left;
  await rail(page).hover();
  await page.mouse.wheel(0,-135);
  await expect.poll(async () => (await layout(page)).left).toBeLessThan(browsed-4);
  await expect(rail(page)).not.toHaveAttribute('data-wheeling','true');
  const left = await rail(page).evaluate(view => new Promise(resolve => {
    let last = view.scrollLeft, steady = 0;
    function measure() {
      steady = last === view.scrollLeft ? steady+1 : 0;
      last = view.scrollLeft;
      if (steady >= 3) resolve(last);
      else requestAnimationFrame(measure);
    }
    requestAnimationFrame(measure);
  }));
  const readCount = reads.length;
  await expect.poll(() => reads.length, {timeout:12000}).toBeGreaterThan(readCount);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect((await layout(page)).left).toBeCloseTo(left, 0);
  await after(page).click();
  await expect.poll(async () => (await layout(page)).left).toBeGreaterThan(left+4);
  const previous = (await layout(page)).left;
  await rail(page).hover();
  await page.mouse.wheel(-125,0);
  await expect.poll(async () => (await layout(page)).left).toBeLessThan(previous-4);
  await expect(detail(page)).toHaveAttribute('data-selected-step',current);
});

test('실제 기록이 한 장이면 나머지 단계·페이드·탐색 버튼이 생기지 않는다', async ({page}) => {
  const {application} = await mockUnifiedRoom(page);
  const path = '/api/application/v1/incidents/' + application.id;
  await page.route('**' + path + '/summary', route => route.fulfill({json:{
    id:application.id, state:'detected', created_at:1000, service:'inventory', reason:'재고 오류가 감지됐습니다.',
  }}));
  await page.route('**' + path + '/activity?*', route => route.fulfill({json:{
    id:application.id, state:'detected', stages:[{state:'detected',occurred_at:1000}],
    events:[], cursor:0, has_more:false, active_jobs:[],
  }}));
  await page.goto('/incidents/application/' + application.id);
  await expect(cards(page)).toHaveCount(1);
  expect((await layout(page)).max).toBeLessThanOrEqual(1);
  await expect(page.locator('.stage-rail-window')).not.toHaveAttribute('data-before','true');
  await expect(page.locator('.stage-rail-window')).not.toHaveAttribute('data-after','true');
  await expect(before(page)).toHaveCount(0);
  await expect(after(page)).toHaveCount(0);
});
