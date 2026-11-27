import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { errors, layout, tab } from './p15-helpers.mjs';

const fixture = () => JSON.parse(readFileSync(new URL('./fixtures/narrative-1976.json', import.meta.url)));
const makeEvent = (seq, phase, text = '', attempt = 'attempt-1') => ({
  seq, kind: 'agent_progress', actor: 'liveprobe', at: new Date().toISOString(),
  summary: phase === 'responding' ? '공개 답변을 수신했습니다.' : '모델 응답을 기다립니다.',
  detail: { call_id: 'stream-test', role: 'k8s_analyst', attempt_id: attempt, phase, output_mode: 'messages', text },
});

const panelFor = page => page.getByRole('region',{name:'선택 단계 입력과 결과'});
async function openCall(page) {
  const panel = panelFor(page);
  await panel.focus();
  const widen = page.getByRole('button',{name:'단계 넓게 보기'});
  if(await widen.isVisible()) await widen.click();
  await expect(panel).toBeVisible();
  return panel;
}
async function historyCard(page,id='planned:k8s_analyst') {
  await tab(page,'전체 대화·이력');
  const disclosure=page.getByText(/지난 분석 단계 .+개 보기/);
  if(await disclosure.count())await disclosure.click();
  return page.locator('.step-card[data-node-id="'+id+'"]');
}
async function setup(page, { streaming = true, snapshot = false } = {}) {
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
  await tab(page, '현재 단계');
  const mobile=page.getByRole('navigation',{name:'관제실 화면 전환'});
  if(await mobile.isVisible())await mobile.getByRole('button',{name:'현재 단계'}).click();
  const panel = panelFor(page);
  if (!snapshot) await expect(panel.locator('.execution-stream')).toContainText('공개 답변을 기다리고 있습니다.');
  return { n, rows, panel, fail: v => { fail = v; },
    send: async e => {
      rows.push(e);
      await page.evaluate(event => window.__runStreams.at(-1).send(event), e);
    } };
}

test('현재 작업의 공개 답변이 중복 없이 갱신되고 전체 대화 입력 초안과 스크롤이 유지된다', async ({page})=>{
  const es=errors(page);
  const {panel,send}=await setup(page);
  const card=await historyCard(page);
  await expect(card.getByRole('button',{name:'펼치기',exact:true})).toHaveCount(0);   // step cards are always open
  const draft=card.getByRole('textbox');
  await draft.fill('다음 단계에서 네트워크 상태도 확인해 주세요.');
  const before=await page.locator('.chat-feed').evaluate(el=>el.scrollTop);
  const event=makeEvent(3,'responding','요약\n서비스 상태와 이벤트를 확인하고 있습니다.');
  await send(event);await send(event);
  await expect(card.locator('.execution-output')).toHaveText(event.detail.text);
  await expect(draft).toBeFocused();
  await expect(draft).toHaveValue('다음 단계에서 네트워크 상태도 확인해 주세요.');
  expect(await page.locator('.chat-feed').evaluate(el=>el.scrollTop)).toBeCloseTo(before,0);
  await tab(page,'현재 단계');await expect(panel.locator('.execution-output')).toHaveText(event.detail.text);
  const expanded=await openCall(page);
  await expect(expanded.locator('.execution-output')).toHaveCount(1);
  await expect(expanded).toContainText('생성 중인 임시 답변');
  await expect(expanded.getByRole('button',{name:/실행 내용 (접기|펼치기)/})).toHaveCount(0);
  await send(makeEvent(4,'responding','근거 확인 완료'));
  await expect(expanded.locator('.execution-output')).toHaveText('근거 확인 완료');
  await layout(page);
  expect(es).toEqual([]);
});

test('재연결은 저장된 이벤트 뒤에서 이어지고 다시 연 호출에도 재시도별 답변이 구분된다',async({page})=>{
  const {rows,send}=await setup(page);
  let expanded=await openCall(page);
  await send(makeEvent(3,'failed','첫 호출의 임시 응답'));
  rows.push(makeEvent(4,'responding','두 번째 호출 응답','attempt-2'));
  await page.evaluate(()=>window.__runStreams.at(-1).onerror({}));
  await expect(expanded.locator('.execution-output')).toHaveText(['첫 호출의 임시 응답','두 번째 호출 응답']);
  await expect.poll(()=>page.evaluate(()=>window.__runStreams.at(-1).url)).toContain('since=4');
  await page.reload();await tab(page,'현재 단계');
  const mobile=page.getByRole('navigation',{name:'관제실 화면 전환'});if(await mobile.isVisible())await mobile.getByRole('button',{name:'현재 단계'}).click();
  expanded=await openCall(page);
  await expect(expanded.locator('.execution-output')).toHaveText(['첫 호출의 임시 응답','두 번째 호출 응답']);
});

test('폴링만 가능한 API는 최근 공개 답변과 갱신 오류를 함께 표시한다',async({page})=>{
  const data=await setup(page,{streaming:false});
  const expanded=await openCall(page);
  data.rows.push(makeEvent(3,'responding','폴링으로 받은 공개 답변'));
  await expect(expanded).toContainText('저장 기록으로 갱신');
  await expect(expanded.locator('.execution-output')).toHaveText('폴링으로 받은 공개 답변');
  expect(await page.evaluate(()=>window.__runStreams.length)).toBe(0);
  data.fail(true);
  await expect(expanded).toContainText('갱신이 지연되고 있습니다.');
  await expect(expanded.locator('.execution-output')).toHaveText('폴링으로 받은 공개 답변');
});

test('선택한 호출이 완료되면 확정 요약과 수신한 답변을 구별하고 전체 이력에서 다시 읽는다',async({page})=>{
  const {n,send}=await setup(page);
  const expanded=await openCall(page);
  await send(makeEvent(3,'responding','임시 답변'));
  await expanded.getByText('공개 답변 원문 보기',{exact:true}).click();
  await expect(expanded.locator('.execution-attempt .raw-text')).toHaveText('임시 답변');
  await send(makeEvent(4,'succeeded','수신 완료 답변'));
  const agent=n.flow.nodes.find(x=>x.role==='k8s_analyst');
  const old=agent.id;agent.id='run:990001';agent.aliases=[old];
  agent.status='done';agent.agent_run_id=990001;agent.card.agent_run_id=990001;agent.card.summary_ko='검증된 최종 분석 요약';
  n.flow.edges.forEach(edge=>{if(edge.source===old)edge.source=agent.id;if(edge.target===old)edge.target=agent.id;});
  await send({seq:5,kind:'agent_finished',actor:'liveprobe',at:new Date().toISOString(),summary:'분석 완료',
    detail:{call_id:'stream-test',role:'k8s_analyst',agent_run_id:990001,status:'succeeded'}});
  await expect(expanded).toContainText('검증된 최종 분석 요약');
  await expect(expanded.locator('.execution-output')).toHaveText('수신 완료 답변');
  await expect(expanded.locator('.execution-attempt .raw-text')).toHaveText('수신 완료 답변');
  await expect(expanded).toContainText('확정 결과는 위 분석 결과를 확인하세요.');
  const narrow=page.getByRole('button',{name:'기본 너비로'});if(await narrow.isVisible())await narrow.click();
  const history=await historyCard(page,agent.id);
  await expect(history).toContainText('검증된 최종 분석 요약');
});

test('현재 작업은 공개 답변을 바로 표시하고 넓은 호출 화면은 같은 연결을 계속 사용한다',async({page})=>{
  const {panel,send}=await setup(page);
  await send(makeEvent(3,'responding','선택 단계의 현재 답변'));
  await expect(panel.locator('.execution-output')).toHaveText('선택 단계의 현재 답변');
  const expanded=await openCall(page);
  await expect(expanded.locator('.execution-output')).toHaveText('선택 단계의 현재 답변');
  await send(makeEvent(4,'responding','넓게 보는 동안 도착한 다음 답변'));
  await expect(expanded.locator('.execution-output')).toHaveText('넓게 보는 동안 도착한 다음 답변');
  const narrow=page.getByRole('button',{name:'기본 너비로'});if(await narrow.isVisible())await narrow.click();
  await expect(panel.locator('.execution-output')).toHaveText('넓게 보는 동안 도착한 다음 답변');
  if(await page.getByRole('button',{name:'단계 넓게 보기'}).isVisible())await expect(page.getByRole('button',{name:'단계 넓게 보기'})).toBeFocused();
  expect(await page.evaluate(()=>window.__runStreams.length)).toBe(1);
});

test('저장 당시 실행 중인 단계는 이력임을 밝히고 새 모델 호출을 시작하지 않는다',async({page})=>{
  await setup(page,{streaming:false,snapshot:true});
  const card=await historyCard(page);
  await expect(card).toContainText('저장 표본');
  await expect(card).toContainText('이 화면에서 실제 분석은 실행되지 않으며 새 답변은 발생하지 않습니다.');
  await card.getByRole('button',{name:'원문 보기',exact:true}).click();
  const detail=page.getByRole('complementary',{name:'단계 상세'});
  await expect(detail).toContainText('저장된 시점에 확정 결론 원문이 없었습니다.');
  expect(await page.evaluate(()=>window.__runStreams.length)).toBe(0);
});

test('구조화 응답은 공개 요약을 먼저 보여주고 키보드로 연 원문에서만 기술 문자열이 보인다',async({page})=>{
  const {panel,send}=await setup(page);
  const e=makeEvent(3,'responding','{"summary_ko":"생성 중');
  e.summary='RAW_EVENT_SENTINEL http://internal';
  await send(e);await expect(panel.locator('.execution-output')).toHaveText('생성 중');
  const expanded=await openCall(page);
  const raw='{"summary_ko":"서비스 상태를 확인 중입니다.","trace":"RAW_STREAM_SENTINEL <script>window.unsafe=true</script>"}';
  await send(makeEvent(4,'responding',raw));
  await expect(expanded.locator('.execution-output')).toHaveText('서비스 상태를 확인 중입니다.');
  expect(await page.locator('body').textContent()).not.toMatch(/RAW_(STREAM|EVENT)_SENTINEL/);
  const disclosure=expanded.getByText('공개 답변 원문 보기',{exact:true});
  await disclosure.focus();await disclosure.press('Space');
  await expect(expanded.locator('.execution-attempt .raw-text')).toHaveText(raw);
  await send(makeEvent(5,'responding',raw.replace('상태를','연결을')));
  await expect(expanded.locator('.execution-attempt .raw-text')).toContainText('서비스 연결을 확인 중입니다.');
  await expect(disclosure).toBeFocused();
  expect(await page.evaluate(()=>window.unsafe)).toBeUndefined();
  await disclosure.press('Enter');await expect(expanded.locator('.execution-attempt .raw-text')).toHaveCount(0);
});
