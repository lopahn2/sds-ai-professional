// 보완-11: 기존 mock-api + p15 helper만 사용, 운영 API 미접속.
import {test,expect} from '@playwright/test';
import {tab,resetMock,layout,mock,errors} from './p15-helpers.mjs';
test.skip(!mock,'로컬 모의 API에서만 실행');
test.beforeEach(async({request})=>resetMock(request));
const ready=async page=>{await expect(page.getByRole('region',{name:'선택 단계 입력과 결과'})).toBeVisible();};
async function open(page,id='evidence'){
  await tab(page,'대화');
  const history=page.locator('details:not([open]) > summary').filter({hasText:/지난 분석 단계 \d+개 보기/});
  if(await history.isVisible())await history.click();
  const card=page.locator(`[id="step-${id}"]`),trigger=card.locator('.step-actions').getByRole('button',{name:'원문 보기',exact:true});
  await trigger.scrollIntoViewIfNeeded();await trigger.focus();await page.keyboard.press('Enter');
  const panel=page.locator('.detail-panel');await expect(panel).toBeVisible();await expect(panel.getByRole('button',{name:'닫기',exact:true})).toBeFocused();
  return {card,trigger,panel};
}

test('좁은 상세는 배경 키보드를 차단하고 Tab 순환·Esc·닫기·가림막 후 트리거 초점을 복원한다',async({page},info)=>{
  const es=errors(page);await page.goto('/incidents/990');await ready(page);
  const {trigger,panel}=await open(page);
  const narrow=page.viewportSize().width<=1100;
  if(narrow){
    const protectedBackground=()=>page.locator('.app>.nav,.app>.bar,.headline,.room-tabs,.conversation-pane,.flow-pane').evaluateAll(es=>es.map(e=>({name:e.className,blocked:!!e.closest('[inert]')})));
    for(const b of await protectedBackground())expect(b.blocked,b.name).toBe(true);
    // native inert는 JS focus()조차 막아야 한다(탭 인덱스만 바꾸는 것으로 대체 불가).
    await page.locator('.headline a').evaluate(e=>e.focus());
    expect(await panel.evaluate(e=>e.contains(document.activeElement))).toBe(true);
    for(let i=0;i<30;i++){await page.keyboard.press('Tab');expect(await panel.evaluate(e=>e.contains(document.activeElement))).toBe(true);}
    await panel.getByRole('button',{name:'닫기',exact:true}).focus();await page.keyboard.press('Shift+Tab');expect(await panel.evaluate(e=>e.contains(document.activeElement))).toBe(true);
  }else{
    expect(await page.locator('.conversation-pane').evaluate(e=>!!e.closest('[inert]'))).toBe(false);
  }
  await layout(page);await page.screenshot({path:info.outputPath('keyboard-open.png')});
  await page.keyboard.press('Escape');await expect(panel).toHaveCount(0);await expect(trigger).toBeFocused();
  await expect(page.locator('.app [inert]')).toHaveCount(0);
  await trigger.press('Enter');await expect(panel).toBeVisible();await panel.getByRole('button',{name:'닫기',exact:true}).click();await expect(trigger).toBeFocused();
  if(narrow){
    // 375 시트는 전폭이므로 가림막 클릭 경로는 같은 시험에서 768로 넓혀 확인한다.
    if(page.viewportSize().width<500)await page.setViewportSize({width:768,height:1000});
    await trigger.press('Enter');await expect(panel).toBeVisible();const backdrop=page.locator('.detail-backdrop');await expect(backdrop).toBeVisible();
    await backdrop.click({position:{x:5,y:page.viewportSize().height-5}});await expect(panel).toHaveCount(0);await expect(trigger).toBeFocused();await expect(page.locator('.app [inert]')).toHaveCount(0);
  }
  expect(es).toEqual([]);
});

test('열린 상세의 화면 크기가 바뀌면 배경 차단을 전환하고 닫을 때 기존 inert 속성을 보존한다',async({page})=>{
  await page.goto('/incidents/990');await ready(page);await page.setViewportSize({width:1440,height:1000});
  await page.locator('.app>.nav').evaluate(e=>e.setAttribute('inert',''));
  const {panel,trigger}=await open(page);
  await page.setViewportSize({width:390,height:844});
  await expect.poll(()=>page.locator('.conversation-pane').evaluate(e=>!!e.closest('[inert]'))).toBe(true);
  await page.setViewportSize({width:1440,height:1000});
  await expect.poll(()=>page.locator('.conversation-pane').evaluate(e=>!!e.closest('[inert]'))).toBe(false);
  await expect(page.locator('.app>.nav')).toHaveAttribute('inert','');
  await page.keyboard.press('Escape');await expect(panel).toHaveCount(0);await expect(trigger).toBeFocused();
  await expect(page.locator('.app>.nav')).toHaveAttribute('inert','');
});

test('단계 전체의 중복 ref·빈 스냅샷·실패 원문을 각각 분리하고 특수문자를 실행하지 않는다',async({page},info)=>{
  const es=errors(page);const special='<img src=x onerror="window.p19Injected=true"> & 원문';
  await page.route('**/api/incidents/990/narrative',async route=>{
    const response=await route.fetch(),d=await response.json(),n=d.flow.nodes.find(x=>x.type==='evidence'),c=n.card,raw=d.raw.find(x=>x.ref==='E1_deployment'),base=c.items[0];
    c.items=[{...base,id:901,ref:raw.ref,summary_ko:'당시 첫 행',raw_items:[{...raw,id:'p19-raw-901',raw:special}]},
      {...base,id:902,ref:raw.ref,summary_ko:'당시 두번째 빈 행',raw_items:[]},
      {...base,id:903,ref:raw.ref,summary_ko:'구형 중복 ref 행',raw_items:undefined},
      {...base,id:904,ref:null,summary_ko:'수집 실패 행',ok:false,error:'HTTP 503 <실패> & 원문 사유',raw_items:[]}];
    c.total=4;c.ok=3;c.failed=1;n.inputs=[];d.raw=[{...raw,raw:'공통 최신값 혼입 금지'}];await route.fulfill({response,json:d});
  });
  await page.goto('/incidents/990');await ready(page);const {panel}=await open(page);
  await expect(panel.locator('[data-evidence-id]')).toHaveCount(4);
  // The detail panel is the explicit raw view: each evidence row shows its raw records without a second disclosure.
  for(const id of [901,902,903,904])await expect(panel.locator(`[data-evidence-id="${id}"] summary`)).toHaveCount(0);
  await expect(panel.locator('[data-evidence-id="901"]')).toContainText(special);
  for(const id of [902,903,904])await expect(panel.locator(`[data-evidence-id="${id}"]`)).toContainText('원문은 현재 응답에 없습니다.');
  await expect(panel.locator('[data-evidence-id="904"]')).toContainText('HTTP 503 <실패> & 원문 사유');await expect(panel).not.toContainText('공통 최신값 혼입 금지');
  expect(await page.evaluate(()=>window.p19Injected)).toBeUndefined();await expect(panel.locator('img')).toHaveCount(0);
  await layout(page);await page.screenshot({path:info.outputPath('boundaries.png')});expect(es).toEqual([]);
});

test('Group와 한글 해석 상세의 긴 문자열·특수문자 본문은 전체 그대로 읽힌다',async({page},info)=>{
  const es=errors(page),long='<script>window.p19Injected=true</script> & 긴판단'.repeat(40);
  await page.route('**/api/incidents/990/narrative',async route=>{
    const response=await route.fetch(),d=await response.json(),g=d.flow.nodes.find(x=>x.type==='group').card;
    g.decision.final_reason_ko='판단 이유 '+long;g.decision.steps=[{key:'final',source:'policy',title_ko:'판단 단계',detail_ko:'전체 판단 '+long,items_ko:['하위 판단 '+long]}];g.progress_ko=['실제 진행 '+long];g.budget={max_agent_calls:0,max_cost_usd:0,deadline_s:0};
    const interp={...d.flow.nodes[0],id:'p19-interpret',type:'interpret',label:'한글 해석',role:'interpreter',inputs:[],card:null,conclusion_raw:null,conclusion_ko:null,depth:50};d.flow.nodes.push(interp);
    d.raw=[{...d.raw[0],id:'p19-translation',ko_source:'llm',ko:'정확한 한글 '+long,raw:'EXACT RAW '+long}];await route.fulfill({response,json:d});
  });
  await page.goto('/incidents/990');await ready(page);let {panel,trigger}=await open(page,'group');
  for(const text of ['판단 이유 '+long,'전체 판단 '+long,'하위 판단 '+long,'실제 진행 '+long,'Agent 호출 최대 0회','제한 시간 0초'])await expect(panel).toContainText(text);
  await layout(page);await page.screenshot({path:info.outputPath('group-long.png')});await page.keyboard.press('Escape');await expect(trigger).toBeFocused();
  // Interpretation runs are display work, not operator steps (roomWorkflow.operatorFlow): they have no card of their own.
  await expect(page.locator('[id="step-p19-interpret"]')).toHaveCount(0);
  expect(await page.evaluate(()=>window.p19Injected)).toBeUndefined();await layout(page);expect(es).toEqual([]);
});

test('사건 상세 로드 실패는 다시 불러오기로 회복하며 마지막 승인 정보를 성공으로 꾸미지 않는다',async({page})=>{
  let failed=true;
  await page.route('**/api/incidents/990',route=>failed?route.fulfill({status:503,json:{detail:'합성 상세 불가'}}):route.continue());
  await page.goto('/incidents/990');await ready(page);
  await expect(page.locator('.room-error')).toContainText('조치 승인 정보 갱신에 실패했습니다.');
  const {panel}=await open(page);await expect(panel).toContainText('수집');await page.keyboard.press('Escape');
  failed=false;await page.getByRole('button',{name:'다시 불러오기',exact:true}).click();await expect(page.locator('.room-error')).toHaveCount(0);
});
