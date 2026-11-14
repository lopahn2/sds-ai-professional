import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { errors, layout, tab } from './p15-helpers.mjs';

const fixture = () => JSON.parse(readFileSync(new URL('./fixtures/narrative-1976.json', import.meta.url)));
const makeEvent = (seq, phase, text = '', attempt = 'attempt-1') => ({
  seq, kind: 'agent_progress', actor: 'liveprobe', at: new Date().toISOString(),
  summary: phase === 'responding' ? '공개 답변을 수신했습니다.' : '모델 응답을 기다립니다.',
  detail: { call_id: 'stream-test', role: 'k8s_analyst', attempt_id: attempt, phase, output_mode: 'messages', text },
});

async function setup(page, { streaming = true, expanded = true, snapshot = false } = {}) {
  const n = fixture();
  const agent = n.flow.nodes.find(x => x.role === 'k8s_analyst');
  agent.call_id = 'stream-test';
  const rows = snapshot ? [] : [{ seq: 1, kind: 'agent_started', actor: 'liveprobe', at: new Date().toISOString(),
    summary: '쿠버네티스 분석을 시작했습니다.', detail: { role: 'k8s_analyst', call_id: 'stream-test' } },
    makeEvent(2, 'waiting')];
  let fail = false;
  await page.route('**/api/incidents/1976/narrative', route => route.fulfill({ json: n }));
  await page.route('**/api/incidents/1976', route => route.fulfill({ json: { incident: n.incident, actions: [] } }));
  await page.route('**/api/incidents/1976/events?*', route => {
    if (fail) return route.fulfill({ status: 503, json: {} });
    const since = Number(new URL(route.request().url()).searchParams.get('since') || 0);
    return route.fulfill({ json: { events: rows.filter(e => e.seq > since), control: 'running', streaming, execution_mode: snapshot ? 'snapshot' : 'live' } });
  });
  await page.addInitScript(() => {
    const Native = window.EventSource;
    window.__runStreams = [];
    window.EventSource = class {
      constructor(url) {
        if (!url.includes('/events/stream')) return new Native(url);
        this.url = url; this.listeners = {}; this.readyState = 0;
        window.__runStreams.push(this);
        setTimeout(() => { this.readyState = 1; this.onopen?.({}); }, 20);
      }
      addEventListener(name, fn) { this.listeners[name] = fn; }
      send(event) { this.listeners.run_event?.({ data: JSON.stringify(event), lastEventId: String(event.seq) }); }
      close() { this.readyState = 2; }
    };
  });
  await page.goto('/incidents/1976');
  await tab(page, '대화');
  const card = page.locator('.step-card[data-node-id="planned:k8s_analyst"]');
  const expand = card.getByRole('button', { name: '펼치기', exact: true });
  if (expanded && await expand.count()) await expand.click();
  if (!snapshot) await expect(card.getByText('공개 답변 메시지를 기다리고 있습니다.')).toBeVisible();
  return { n, rows, card, fail: v => { fail = v; },
    send: async e => {
      rows.push(e);
      await page.evaluate(event => window.__runStreams.at(-1).send(event), e);
    } };
}

test('streamed answer stays inside the running card and preserves draft, focus and scroll', async ({ page }, info) => {
  const es = errors(page);
  const { card, send } = await setup(page);
  const draft = card.getByRole('textbox');
  await draft.fill('다음 단계에서 네트워크 상태도 확인해 주세요.');
  await draft.focus();
  const before = await page.locator('.chat-feed').evaluate(el => el.scrollTop);
  await send(makeEvent(3, 'responding', '요약\n서비스 상태와 이벤트를 확인하고 있습니다.'));
  await expect(card.locator('.execution-output')).toHaveText('요약\n서비스 상태와 이벤트를 확인하고 있습니다.');
  await expect(draft).toHaveValue('다음 단계에서 네트워크 상태도 확인해 주세요.');
  await expect(draft).toBeFocused();
  expect(await page.locator('.chat-feed').evaluate(el => el.scrollTop)).toBeCloseTo(before, 0);
  await expect(card).toContainText('생성 중인 임시 답변');
  await send(makeEvent(3, 'responding', '요약\n서비스 상태와 이벤트를 확인하고 있습니다.'));
  await expect(card.locator('.execution-output')).toHaveCount(1);
  await card.getByRole('button', { name: '실행 내용 접기' }).click();
  await send(makeEvent(4, 'responding', '요약\n근거 확인 완료. 다음 검증 단계로 진행합니다.'));
  await expect(card.locator('.execution-output')).toHaveCount(0);
  await card.getByRole('button', { name: '실행 내용 펼치기' }).click();
  await expect(card.locator('.execution-output')).toContainText('근거 확인 완료');
  await layout(page);
  await page.screenshot({ path: `../analysis-stream-${info.project.name}.png`, fullPage: true });
  expect(es).toEqual([]);
});

test('reconnect recovers stored records and retry answers stay separate', async ({ page }) => {
  const { rows, card, send } = await setup(page);
  await send(makeEvent(3, 'failed', '첫 호출의 임시 응답'));
  const recovered = makeEvent(4, 'responding', '두 번째 호출 응답', 'attempt-2');
  rows.push(recovered);
  await page.evaluate(() => window.__runStreams.at(-1).onerror({}));
  await expect(card.locator('.execution-output')).toHaveCount(2);
  await expect(card).toContainText('두 번째 호출 응답');
  await expect.poll(() => page.evaluate(() => window.__runStreams.length)).toBeGreaterThan(1);
  const url = await page.evaluate(() => window.__runStreams.at(-1).url);
  expect(url).toContain('since=4');
  // Reload restores the same saved output through the snapshot, without re-running analysis.
  await page.reload(); await tab(page, '대화');
  const restored = page.locator('[data-node-id="planned:k8s_analyst"]');
  const expand = restored.getByRole('button', { name: '펼치기', exact: true });
  if (await expand.count()) await expand.click();
  await expect(restored.locator('.execution-output')).toHaveCount(2);
});

test('older API uses polling honestly and shows update failure while keeping received output', async ({ page }) => {
  const data = await setup(page, { streaming: false });
  data.rows.push(makeEvent(3, 'responding', '폴링으로 받은 공개 답변'));
  await expect(data.card).toContainText('저장 기록으로 갱신');
  await expect(data.card.locator('.execution-output')).toContainText('폴링으로 받은 공개 답변');
  expect(await page.evaluate(() => window.__runStreams.length)).toBe(0);
  data.fail(true);
  await expect(data.card.getByText('갱신이 지연되고 있습니다.', { exact: false })).toBeVisible();
  await expect(data.card.locator('.execution-output')).toContainText('폴링으로 받은 공개 답변');
});

test('completed card keeps replayed answer and distinguishes it from the final conclusion', async ({ page }) => {
  const { n, card, send } = await setup(page);
  await card.getByRole('textbox').fill('입력 초안 유지');
  await send(makeEvent(3, 'responding', '임시 답변'));
  await card.getByText('공개 답변 원문 보기', { exact: true }).click();
  await expect(card.locator('.execution-attempt .raw-text')).toHaveText('임시 답변');
  await send(makeEvent(4, 'succeeded', '수신 완료 답변'));
  const agent = n.flow.nodes.find(x => x.role === 'k8s_analyst');
  agent.id = 'run:990001'; agent.aliases = ['planned:k8s_analyst'];
  agent.status = 'done'; agent.agent_run_id = 990001;
  agent.card.agent_run_id = 990001; agent.card.summary_ko = '검증된 최종 분석 요약';
  await send({ seq: 5, kind: 'agent_finished', actor: 'liveprobe', at: new Date().toISOString(),
    summary: '분석 완료', detail: { call_id: 'stream-test', role: 'k8s_analyst', agent_run_id: 990001, status: 'succeeded' } });
  const completed = page.locator('[data-node-id="run:990001"]');
  await expect(completed).toContainText('검증된 최종 분석 요약');
  await expect(completed.locator('.execution-output')).toHaveText('수신 완료 답변');
  await expect(completed.locator('.execution-attempt .raw-text')).toHaveText('수신 완료 답변');
  await expect(completed).toContainText('확정 결과는 위 분석 결과를 확인하세요.');
  await expect(completed.getByRole('textbox')).toHaveValue('입력 초안 유지');
});

test('running output is visible without expanding and updates the open detail panel too', async ({ page }, info) => {
  const { card, send } = await setup(page, { expanded: false });
  await expect(card.getByRole('button', { name: '펼치기', exact: true })).toBeVisible();
  await send(makeEvent(3, 'responding', '접힌 카드에서도 보이는 현재 답변'));
  await expect(card.locator('.execution-output')).toBeVisible();
  await card.getByRole('button', { name: '원문 보기', exact: true }).click();
  const detail = page.getByRole('complementary', { name: '단계 상세' });
  await expect(detail.locator('.execution-output')).toHaveText('접힌 카드에서도 보이는 현재 답변');
  await send(makeEvent(4, 'responding', '상세보기를 열어 둔 동안 도착한 다음 답변'));
  await expect(detail.locator('.execution-output')).toHaveText('상세보기를 열어 둔 동안 도착한 다음 답변');
  await expect(card.locator('.execution-output')).toHaveText('상세보기를 열어 둔 동안 도착한 다음 답변');
  expect(await page.evaluate(() => window.__runStreams.length)).toBe(1);
  await layout(page);
  await page.screenshot({ path: `../analysis-stream-visible-${info.project.name}.png`, fullPage: true });
});

test('stored running snapshot explains why it cannot generate another answer', async ({ page }) => {
  const { card } = await setup(page, { expanded: false, streaming: false, snapshot: true });
  await expect(card.getByText('저장 표본', { exact: true })).toBeVisible();
  await expect(card).toContainText('이 화면에서 실제 분석은 실행되지 않으며 새 답변은 발생하지 않습니다.');
  await card.getByRole('button', { name: '원문 보기', exact: true }).click();
  const detail = page.getByRole('complementary', { name: '단계 상세' });
  await expect(detail).toContainText('저장된 생성 중 답변이 없습니다.');
  await expect(detail).toContainText('저장된 시점에 확정 결론 원문이 없었습니다.');
  expect(await page.evaluate(() => window.__runStreams.length)).toBe(0);
});

test('structured streaming keeps provider JSON and technical event text raw-only while updating readable answer', async ({ page }) => {
  const { card, send } = await setup(page, { expanded: false });
  const raw = '{"summary_ko":"서비스 상태를 확인 중입니다.","trace":"RAW_STREAM_SENTINEL <script>window.unsafe=true</script>"}';
  const e = makeEvent(3, 'responding', '{"summary_ko":"생성 중');
  e.summary = 'RAW_EVENT_SENTINEL http://internal';
  await send(e);
  await expect(card.locator('.execution-output')).toHaveText('생성 중');
  expect(await page.locator('body').textContent()).not.toContain('RAW_EVENT_SENTINEL');
  await send(makeEvent(4, 'responding', raw));
  await expect(card.locator('.execution-output')).toHaveText('서비스 상태를 확인 중입니다.');
  expect(await page.locator('body').textContent()).not.toContain('RAW_STREAM_SENTINEL');
  const disclosure = card.getByText('공개 답변 원문 보기', { exact: true });
  await disclosure.focus(); await disclosure.press('Space');
  await expect(card.locator('.execution-attempt .raw-text')).toHaveText(raw);
  await send(makeEvent(5, 'responding', raw.replace('상태를', '연결을')));
  await expect(card.locator('.execution-attempt .raw-text')).toContainText('서비스 연결을 확인 중입니다.');
  await expect(disclosure).toBeFocused();
  expect(await page.evaluate(() => window.unsafe)).toBeUndefined();
  await disclosure.press('Enter');
  await expect(card.locator('.execution-attempt .raw-text')).toHaveCount(0);
});
