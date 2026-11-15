import {test, expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {layout, resetMock} from './p15-helpers.mjs';
const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url)));
test.skip(process.env.LIVEPROBE_MOCK !== '1', '합성 API 전용');
test.beforeEach(async ({request}) => resetMock(request));

test('진행·지난 사건 모두 원인·판단·검증이 기본 표시되고 원문만 접힌다', async ({page}, info) => {
  const raw = 'RAW_DETAIL_SENTINEL {"stack":"private log"}';
  for (const status of ['analyzing', 'closed']) {
    const n = fixture('narrative-1976');
    n.incident.status = status; n.view_mode = 'live';
    n.incident.workload = 'order'; n.incident.title_ko = '데이터베이스 연결 실패';
    n.handoff_view = null;
    const node = n.flow.nodes.find(n => n.card?.kind === 'agent');
    node.status = 'done'; node.conclusion_raw = raw;
    node.card.summary_ko = '주문 요청이 연결을 기다리다가 실패했습니다.';
    node.card.candidates = [{cause_ko:'반환되지 않은 연결이 누적되어 연결 풀이 고갈되었습니다.',cause_raw:raw,confidence:0.92}];
    node.card.verdict_ko = '연결 반환 누락을 근거로 확인했습니다.';
    node.card.next_action_ko = '연결 반환 경로를 수정하고 부하 시험으로 검증합니다.';
    await page.route('**/api/incidents/1976/narrative',r=>r.fulfill({json:n}));
    await page.route('**/api/incidents/1976',r=>r.fulfill({json:{incident:n.incident,actions:[]}}));
    await page.goto('/incidents/1976');
    const card = page.locator(`.step-card[data-node-id="${node.id}"]`);
    await expect(card.locator('.step-body')).toBeVisible();
    if (status === 'closed') {
      await expect(page.locator('.step-card.status-running .step-lead')).toContainText('저장 당시');
      await expect(page.locator('.step-card.status-running .step-head')).toContainText('당시 실행 중');
    }
    await expect(card).toContainText('연결 풀이 고갈');
    await expect(card).toContainText('연결 반환 누락을 근거로');
    await expect(card).toContainText('부하 시험으로 검증');
    await expect(page.locator('.headline-cause')).toContainText('후보');
    await expect(page.locator('.step-card .step-body')).toHaveCount(n.flow.nodes.length);
    await expect(page.getByRole('button',{name:'펼치기',exact:true})).toHaveCount(0);
    expect(await page.locator('body').textContent()).not.toContain(raw);
    await card.getByRole('button',{name:'원문 보기',exact:true}).click();
    await expect(page.getByRole('complementary',{name:'단계 상세'})).toContainText(raw);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('complementary',{name:'단계 상세'})).toHaveCount(0);
    await layout(page);
    await card.scrollIntoViewIfNeeded();
    await page.screenshot({path:info.outputPath(`summaries-${status}.png`)});
    await page.unroute('**/api/incidents/1976/narrative');
    await page.unroute('**/api/incidents/1976');
  }
});

test('경고는 증상 설명을 보여주고 집계에 미인수 이관이 있으면 할 일 없음으로 표시하지 않는다', async ({page}) => {
  const o = fixture('overview-envs');
  o.incidents.open = [];
  o.incidents.counts = {awaiting_approval:0,unclaimed_handoff:4,in_progress:0};
  o.events = [{object:'Pod/order-0',namespace:'dev',reason:'Unhealthy',message:'Readiness probe failed: command timed out RAW_PROBE_SENTINEL',at:'2026-11-15T00:00:00Z',count:2}];
  await page.route('**/api/overview',r=>r.fulfill({json:o}));
  await page.goto('/');
  await expect(page.locator('.queue-lead')).toContainText('장애가 4건');
  await expect(page.getByRole('region',{name:'클러스터 진단',exact:true})).toBeVisible();
  await expect(page.locator('.warning-summary')).toContainText('제한 시간 안에 응답하지 못했습니다');
  expect(await page.locator('body').textContent()).not.toContain('RAW_PROBE_SENTINEL');
  await page.locator('.warning-event summary').click();
  await expect(page.locator('.warning-event pre')).toContainText('RAW_PROBE_SENTINEL');
  await layout(page);
});

test('공개 답변과 실행 상태는 접힘 없이 보이며 원시 실행 기록은 접힌다', async ({page}) => {
  await page.goto('/incidents/1976');
  await expect(page.locator('.execution-stream').first()).toBeVisible();
  await expect(page.locator('.execution-stage').first()).toBeVisible();
  await expect(page.getByRole('button',{name:/실행 내용 (접기|펼치기)/})).toHaveCount(0);
  await expect(page.locator('.execution-records')).toHaveCount(0);
});
