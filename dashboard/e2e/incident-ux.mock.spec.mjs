import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { layout } from './p15-helpers.mjs';
const fixture = id => JSON.parse(readFileSync(new URL(`./fixtures/narrative-${id}.json`, import.meta.url)));
const RAW = 'RAW_UX_SENTINEL <img src=x onerror="window.__unsafe=true"> HTTPStatusError http://internal';
async function keyboardOpen(summary) { await summary.focus(); await summary.press('Enter'); }
async function room(page, id=990, legacy=false) {
  const n=fixture(id); n.view_mode='live';
  n.incident.title=RAW; n.incident.title_ko=null; n.incident.status_reason=RAW; n.incident.handoff={recommended_steps:[RAW]};
  n.headline.what=RAW; n.headline.impact=RAW; n.headline.next=RAW;
  n.handoff_view={summary_ko:`응답 오류 HTTPStatusError ${RAW}`,summary_raw:RAW,decisions:[],next_steps_ko:[],next_steps_raw:[RAW],interpreting:false};
  for(const node of n.flow.nodes) { node.label=RAW; node.conclusion_raw=RAW; if(legacy) node.card=null; }
  await page.route(`**/api/incidents/${id}/narrative`, r=>r.fulfill({json:n}));
  await page.route(`**/api/incidents/${id}`, r=>r.fulfill({json:{incident:n.incident,actions:[]}}));
  await page.goto(`/incidents/${id}`);
  await expect(page.locator('.headline h2')).toContainText(n.incident.workload);
  return n;
}
test('handoff action first, raw absent from DOM/AX until keyboard open; polling and incident reset',async({page},info)=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await room(page);
  await expect(page.locator('.chat-feed').locator(':scope > *').first()).toHaveClass(/handoff-card/);
  await expect(page.getByRole('button',{name:'인계 인수',exact:true})).toBeVisible();
  expect(await page.locator('body').textContent()).not.toContain('RAW_UX_SENTINEL');
  expect(await page.locator('body').ariaSnapshot()).not.toContain('RAW_UX_SENTINEL');
  expect(await page.locator('.step-card').count()).toBeGreaterThan(0);
  const summary=page.locator('.handoff-raw > summary');
  await keyboardOpen(summary);
  await expect(page.locator('.handoff-raw pre').first()).toContainText('RAW_UX_SENTINEL');
  expect(await page.locator('body').ariaSnapshot()).toContain('RAW_UX_SENTINEL');
  expect(await page.evaluate(()=>window.__unsafe)).toBeUndefined();
  const draft=page.getByRole('textbox',{name:'운영자 메시지'});await draft.fill('입력 초안 보존');await draft.focus();
  await page.waitForResponse(r=>r.url().endsWith('/api/incidents/990/narrative'));
  await expect(page.locator('.handoff-raw')).toHaveAttribute('open','');await expect(draft).toHaveValue('입력 초안 보존');await expect(draft).toBeFocused();
  await keyboardOpen(summary);await expect(page.locator('.handoff-raw pre')).toHaveCount(0);expect(await page.locator('body').textContent()).not.toContain('RAW_UX_SENTINEL');
  await layout(page);await page.screenshot({path:info.outputPath('handoff.png'),fullPage:true});
  await room(page,976,true);await expect(page.locator('details[open]')).toHaveCount(0);await expect(draft).toHaveValue('');
  expect(errors).toEqual([]);
});
test('legacy stage failures never use raw label or conclusion; explicit panel remains escaped and closable',async({page})=>{
  await room(page,990,true);
  expect(await page.locator('.step-card').allTextContents()).not.toContain(RAW);
  await page.locator('.step-card').first().getByRole('button',{name:'원문 보기',exact:true}).click();
  const panel=page.getByRole('complementary',{name:'단계 상세'});
  await expect(panel).toContainText('RAW_UX_SENTINEL');
  expect(await panel.locator('img').count()).toBe(0);
  await page.keyboard.press('Escape');await expect(panel).toHaveCount(0);
});
test('approval and command safety details survive, technical payloads remain opt-in',async({page})=>{
  const n=fixture(1976);n.incident.status='action_pending';n.view_mode='live';
  const action={id:777,type:'scale_replicas',target:'Deployment/order',params:{args:{replicas:2},snapshot:{raw:RAW}},status:'pending_approval',reason:RAW};
  const c={id:12,incident_id:1976,status:'awaiting_confirmation',instruction:'서비스 수량 조정',plan:{summary_ko:'서비스 수량을 조정합니다.',operations:[{op:'scale',kind:'Deployment',namespace:'prd',name:'order',replicas:2}]},plan_sha256:'test-only-hash',dry_run:[{op_index:0,ok:true,diff:[{path:'/metadata',before:RAW,after:RAW}]}],side_effects:{items_ko:['파드 수량이 바뀝니다.'],targets:['prd/order'],restarts:false,downtime:'possible',reversible:true,rollback_ko:'원래 수량으로 되돌립니다.',warnings_ko:[],cluster_wide:false},result:{raw:RAW},verification:null,rollback:null,error:RAW};
  await page.route('**/api/incidents/1976/narrative',r=>r.fulfill({json:n}));
  await page.route('**/api/incidents/1976',r=>r.fulfill({json:{incident:n.incident,actions:[action]}}));
  await page.route('**/api/incidents/1976/commands',r=>r.fulfill({json:{items:[c]}}));
  const posts=[];page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
  await page.goto('/incidents/1976');
  await expect(page.getByRole('button',{name:'승인',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'진행',exact:true})).toBeEnabled();
  await expect(page.getByRole('region',{name:'예상 부작용'})).toContainText('중단 가능성 있음');
  expect(await page.locator('body').textContent()).not.toContain('RAW_UX_SENTINEL');
  await keyboardOpen(page.getByText('검사·변경 차이 원문 보기',{exact:true}));await expect(page.locator('.dry-run pre')).toContainText('RAW_UX_SENTINEL');
  expect(posts).toEqual([]);
});
test('ledger unknown codes, failure evidence and API errors never become default raw text',async({page})=>{
  await page.route('**/api/review-ledger?*',r=>r.fulfill({json:{total:1,by_reason:{[RAW]:1},items:[{id:1,incident_id:990,created_at:'2026-11-14T00:00:00Z',reason_code:RAW,profile:RAW,level:RAW,rationale:RAW,impact:RAW,outcome:RAW,rollback:RAW,workload:'order',operator_involved:false}]}}));
  await page.goto('/ledger');await expect(page.getByText('기타 검토 사유',{exact:true})).toBeVisible();
  expect(await page.locator('body').textContent()).not.toContain('RAW_UX_SENTINEL');
  await keyboardOpen(page.getByText('검토 원문 보기',{exact:true}));await expect(page.locator('table pre')).toContainText('RAW_UX_SENTINEL');
  await page.route('**/api/review-ledger?*',r=>r.fulfill({status:500,json:{detail:RAW}}));
  await page.reload();await expect(page.getByRole('alert')).toContainText('검토 기록을 불러오지 못했습니다.');
  expect(await page.locator('body').textContent()).not.toContain('RAW_UX_SENTINEL');
});
