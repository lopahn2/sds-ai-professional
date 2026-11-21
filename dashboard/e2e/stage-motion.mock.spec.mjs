import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { mock } from './p15-helpers.mjs';
import { mockUnifiedRoom } from './fixtures/unified-room.mjs';

test.skip(!mock, '격리된 관제실 모의 서버 전용');

const panel = page => page.locator('.semantic-stage');
const node = (page, stage) => page.locator(`.react-flow__node[data-id="stage:${stage}"] .flow-step`);
const activity = page => page.evaluate(() => window.stageArrivals);

async function recordMotion(page) {
  // Listen before the first render so an unwanted arrival on initial load is observable.
  await page.addInitScript(() => {
    window.stageArrivals = [];
    document.addEventListener('animationstart', event => {
      if (event.animationName === 'stage-edge-travel') {
        const style = getComputedStyle(event.target);
        window.stageArrivals.push({
          name: event.animationName, edge: event.target.closest('.react-flow__edge')?.dataset.id,
          direction: style.animationDirection, duration: style.animationDuration, iterations: style.animationIterationCount,
        });
        return;
      }
      if (!['stage-card-arrive', 'stage-node-arrive'].includes(event.animationName)) return;
      const card = event.target.closest('[data-selected-step]');
      const step = event.target.closest('[data-stage-id], .react-flow__node');
      window.stageArrivals.push({
        name: event.animationName,
        stage: card?.dataset.selectedStep ?? step?.dataset.stageId ?? step?.dataset.id,
        direction: event.target.closest('[data-travel]')?.dataset.travel,
      });
    });
  });
}

async function openRoom(page, room) {
  const update = { reason: null };
  await recordMotion(page);
  if (room === 'Kubernetes') {
    const narrative = JSON.parse(readFileSync(new URL('./fixtures/narrative-1976.json', import.meta.url)));
    await page.route('**/api/incidents/1976/narrative', route => {
      if (update.reason) narrative.flow.nodes.find(step => step.card?.kind === 'signal').card.what_ko = update.reason;
      return route.fulfill({ json: narrative });
    });
    await page.goto('/incidents/1976');
  } else {
    const { application } = await mockUnifiedRoom(page, { state: 'analyzing' });
    await page.route('**/api/application/v1/incidents/native-a/summary', route =>
      route.fulfill({ json: { id: application.id, state: application.state, service: application.service,
        environment: application.environment, created_at: application.created_at, reason: update.reason ?? '재고 요청을 조사합니다.' } }));
    await page.goto('/incidents/application/native-a');
  }
  await expect(panel(page)).toHaveAttribute('data-selected-step', /^stage:/);
  expect(await activity(page)).toEqual([]);
  return update;
}

async function chooseNode(page, stage, method = 'keyboard') {
  const back = page.getByRole('button', { name: /흐름으로 돌아가기/ });
  if (await back.isVisible()) await back.click();
  const full = page.getByRole('button', { name: '전체 흐름', exact: true });
  if (await full.isVisible()) await full.click();
  const button = node(page, stage).getByRole('button');
  await button.focus();
  if (method === 'pointer') await button.click();
  else await button.press('Enter');
  await expect(panel(page)).toHaveAttribute('data-selected-step', 'stage:' + stage);
  await expect(panel(page)).toBeVisible();
}

async function expectArrival(page, stage, direction, previousCount) {
  const arrival = (name) => ({ name, stage: 'stage:' + stage, direction });
  await expect.poll(async () => (await activity(page)).slice(previousCount)).toContainEqual(arrival('stage-card-arrive'));
  if (await node(page, stage).isVisible()) {
    await expect.poll(async () => (await activity(page)).slice(previousCount)).toContainEqual(arrival('stage-node-arrive'));
    const connector = {
      name: 'stage-edge-travel', edge: 'stage:detection>stage:evidence:flow',
      direction: direction === 'forward' ? 'normal' : 'reverse', duration: '0.36s', iterations: '1',
    };
    await expect.poll(async () => (await activity(page)).slice(previousCount)).toContainEqual(connector);
  } else {
    await expect(panel(page)).toBeFocused();
  }
}

for (const room of ['Kubernetes', 'Application']) {
  test(room + '에서 앞으로·뒤로 선택한 카드와 상세가 같은 방향으로 움직인다', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    const update = await openRoom(page, room);
    await chooseNode(page, 'detection');
    const beforeForward = (await activity(page)).length;
    await chooseNode(page, 'evidence', 'pointer');
    await expectArrival(page, 'evidence', 'forward', beforeForward);
    const beforeBackward = (await activity(page)).length;
    await chooseNode(page, 'detection');
    await expectArrival(page, 'detection', 'backward', beforeBackward);

    await expect(panel(page)).not.toHaveAttribute('data-travel', /forward|backward/);
    const before = await activity(page);
    await chooseNode(page, 'detection');
    // The changed observation must actually reach the open card before checking for replay.
    update.reason = '후속 관측에서 연결 대상이 확인됐습니다.';
    await expect(panel(page)).toContainText(update.reason, { timeout: 16000 });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    expect(await activity(page)).toEqual(before);
  });
}

test('동작 줄이기를 사용하면 카드 선택과 포커스 이동만 수행한다', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openRoom(page, 'Application');
  await chooseNode(page, 'evidence');
  await chooseNode(page, 'detection');
  expect(await panel(page).evaluate(el => getComputedStyle(el).animationName)).toBe('none');
  expect(await activity(page)).toEqual([]);
});

test('자동 추적은 처음 관측한 단계에서 대기하고 다음 단계가 실제로 접수될 때 한 번 움직인다', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'no-preference'});
  await recordMotion(page);
  const {application} = await mockUnifiedRoom(page);
  const path = '/api/application/v1/incidents/' + application.id;
  const stages = ['detected'];
  let reads = 0;
  await page.route('**' + path + '/summary', route => route.fulfill({
    json: {...application, state: stages.at(-1), reason: '재고 이상이 감지됐습니다.'},
  }));
  await page.route('**' + path + '/activity?*', route => {
    reads++;
    const after = Number(new URL(route.request().url()).searchParams.get('after'));
    const events = stages.map((state, index) => ({id:index+1, at:1000+index, event:state, data:{}}));
    return route.fulfill({json:{
      id:application.id, state:stages.at(-1), stages:events.map(e=>({state:e.event, occurred_at:e.at})),
      events:events.filter(e=>e.id > after), cursor:events.length, has_more:false, active_jobs:[],
    }});
  });
  await page.goto('/incidents/application/' + application.id);
  await expect(panel(page)).toHaveAttribute('data-selected-step', 'stage:detection');
  await expect.poll(() => reads).toBeGreaterThan(0);
  expect(await activity(page)).toEqual([]);
  stages.push('collecting');
  await expect(panel(page)).toHaveAttribute('data-selected-step','stage:evidence');
  const name = await panel(page).isVisible() ? 'stage-card-arrive' : 'stage-node-arrive';
  const arrivals = async () => (await activity(page)).filter(item => item.name === name);
  await expect.poll(arrivals).toEqual([{name, stage:'stage:evidence', direction:'forward'}]);
  const readsAtTransition = reads;
  await expect.poll(() => reads).toBeGreaterThan(readsAtTransition);
  expect(await arrivals()).toHaveLength(1);
});
