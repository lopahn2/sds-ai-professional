import {test,expect} from '@playwright/test';
import {capture,errors,expectStages,layout,noPageScroll,openFirstCall,resetMock,stageOf,tab} from './p15-helpers.mjs';

test.beforeEach(async({request})=>resetMock(request));
const stage = page => page.getByRole('region',{name:'선택 단계 입력과 결과'});
async function selectNode(page,id) {
  const exact=page.locator(`.react-flow__node[data-id="${id}"] button`);
  await exact.focus(); await page.keyboard.press('Enter');
  await expect(stage(page)).toHaveAttribute('data-selected-step',id);
  await expect(stage(page)).toBeVisible();
}
async function allSteps(page) {
  await tab(page,'흐름');
  const back=page.getByRole('button',{name:/흐름으로 돌아가기/});
  if(await back.isVisible())await back.click();
  const all=page.getByRole('button',{name:'전체 흐름',exact:true});
  if (await all.isVisible()) await all.click();
}
async function inspectComposer(page, state, snapshot) {
  if (snapshot || state==='closed') {
    await expect(page.locator('textarea:visible')).toHaveCount(0);
    return;
  }
  const done=['resolved','handed_off','failed'].includes(state);
  if (done) await page.getByRole('button',{name:'후속 작업 요청',exact:true}).click();
  const composer=page.getByRole('textbox',{name:done?'후속 작업 요청 내용':'운영자 메시지',exact:true});
  await expect(composer).toBeVisible();
  await expect(composer).toHaveAttribute('maxlength',done?'8000':'2000');
  await expect(page.getByRole('button',{name:done?'계획 요청':'보내기',exact:true})).toBeDisabled();
}
test('API의 진행·복구·인계 사건에서 신호, 병렬 단계, 근거 원문과 후속 행동을 구분한다',async({page,request},info)=>{
  test.setTimeout(90000);
  const trouble=errors(page),{items}=await(await request.get('/api/incidents?limit=200')).json();
  const requested=[items.find(i=>['open','analyzing','acting','action_pending'].includes(i.status)),...['COMPLEX','SIMPLE','HIGH_RISK'].map(level=>items.find(i=>i.severity_level===level)),items.find(i=>i.status==='resolved')].filter(Boolean);
  const cases=[...new Map(requested.map(i=>[i.id,i])).values()];
  expect(cases.length).toBeGreaterThan(0);
  for (const i of cases) {
    const response=await request.get('/api/incidents/'+i.id+'/narrative');expect(response.ok()).toBe(true);
    const n=await response.json();
    await page.goto('/incidents/'+i.id);await page.locator('.step-panel').waitFor({state:'attached'});
    await expect(page.getByRole('region',{name:'장애 상황 요약'})).toContainText('#'+i.id);
    await expect(page.locator('.headline h2')).toContainText(i.workload);
    await expect(page.locator('pre.raw-text:visible')).toHaveCount(0);
    await allSteps(page);await expectStages(page,n);
    await layout(page);await capture(page,info,`room-${i.severity_level}-${i.status}-flow`);
    const evidence=n.flow.nodes.find(node=>node.type==='evidence');
    if(evidence){
      await selectNode(page,stageOf(evidence));
      await expect(stage(page)).toContainText('근거');
      const timeline=stage(page).getByRole('button',{name:/시간순 관측 기록/});
      if(await timeline.count())await timeline.first().click();
      const raw=stage(page).locator('summary').filter({hasText:'관측 원문 상세보기'}).first();
      if(await raw.count()){
        await raw.focus();await raw.press('Enter');
        await expect(raw.locator('..').locator('pre.raw-text').first()).toBeVisible();
        await raw.press('Enter');await expect(raw.locator('..').locator('pre.raw-text')).toHaveCount(0);await expect(raw).toBeFocused();
      }
      await tab(page,'대화');
      const past=page.locator('details:not([open]) > summary').filter({hasText:/지난 분석 단계 \d+개 보기/});
      if(await past.isVisible())await past.click();
      const card=page.locator(`[id="step-${evidence.id}"]`);
      const expand=card.getByRole('button',{name:'펼치기',exact:true});
      if(await expand.isVisible())await expand.click();
      if(evidence.card?.kind==='evidence'){
        const rows=evidence.card.items, items=card.locator('[data-evidence-id]');
        await expect(items).toHaveCount(rows.length);await expect(card.locator('.raw-item')).toHaveCount(0);
        const index=rows.findIndex(row=>row.raw_items?.length || (row.ref && rows.filter(a=>a.ref===row.ref).length===1 && n.raw.some(r=>r.ref===row.ref)));
        if(index>=0){
          const row=rows[index], expected=row.raw_items ?? n.raw.filter(r=>r.ref===row.ref);
          await items.nth(index).getByRole('button',{name:/원문 보기/}).click();
          const detail=page.getByRole('complementary',{name:'단계 상세'});await expect(detail).toBeVisible();
          await expect(detail.locator('.raw-item')).toHaveCount(expected.length);
          for(const [j,r] of expected.entries()) await expect(detail.locator('.raw-item .raw-text').nth(j)).toHaveText(r.raw);
          await page.keyboard.press('Escape');await expect(detail).toHaveCount(0);
        }
      }
    }
    const agent=n.flow.nodes.find(node=>node.role&&['agent','router','verify'].includes(node.type));
    if(agent){
      await allSteps(page);await selectNode(page,stageOf(agent));
      await openFirstCall(stage(page));
      // The request is shown only when a dispatch record joins the call by its id (never rebuilt from the result).
      const events=await(await request.get('/api/incidents/'+i.id+'/events')).json();
      const dispatched=(events.events??events).some(e=>e.kind==='agent_started'&&e.detail?.call_id&&e.detail.call_id===agent.call_id&&e.detail.dispatch);
      if(dispatched)await expect(stage(page)).toContainText('전달한 요청');
      else await expect(stage(page).locator('.call-row').filter({hasText:agent.role_ko||agent.label}).first()).toBeVisible();
      const widen=page.getByRole('button',{name:'단계 넓게 보기'});
      if(await widen.isVisible()){
        await widen.click();await expect(stage(page)).toHaveAttribute('data-selected-step',stageOf(agent));
        await page.getByRole('button',{name:'기본 너비로'}).click();
      }

    }
    await tab(page,'대화');await inspectComposer(page,i.status,n.view_mode==='snapshot'||(await page.locator('.snapshot-view').count()>0));
    await layout(page);await capture(page,info,`room-${i.severity_level}-${i.status}-chat`);
  }
  expect(trouble).toEqual([]);
});
test('데스크톱에서 가로 단계·읽을 수 있는 전체 흐름·선택 요약이 한 화면에 보인다',async({page,request},info)=>{
  test.skip(info.project.name!=='width-1440','데스크톱 배치 기준');
  const {items}=await(await request.get('/api/incidents?limit=200')).json();
  const i=items.find(i=>i.severity_level==='COMPLEX')??items[0];test.skip(!i,'장애 기록 없음');
  await page.setViewportSize({width:1440,height:900});await page.goto('/incidents/'+i.id);await page.locator('.step-panel').waitFor({state:'attached'});
  await noPageScroll(page);await allSteps(page);await page.getByRole('button',{name:'흐름 전체 보기',exact:true}).click();
  await expect.poll(()=>page.evaluate(()=>{
    const label=document.querySelector('.flow-step strong'), view=document.querySelector('.flow-canvas .react-flow__viewport');
    return parseFloat(getComputedStyle(label).fontSize)*new DOMMatrix(getComputedStyle(view).transform).a;
  })).toBeGreaterThanOrEqual(11);
  const graph=await page.locator('.flow-pane').boundingBox(), summary=await stage(page).boundingBox();
  expect(graph.x+graph.width).toBeLessThanOrEqual(summary.x+1);
  expect(await stage(page).evaluate(e=>e.scrollHeight-e.clientHeight)).toBeLessThanOrEqual(1);
  await layout(page);await capture(page,info,'room-desktop-1440x900');
});
test('키보드로 지난 사건 단계를 선택하고 상세를 닫은 뒤 동일한 입력으로 복귀한다',async({page,request})=>{
  const {items}=await(await request.get('/api/incidents?limit=200')).json(), i=items.find(i=>i.status==='resolved')??items[0];
  test.skip(!i,'장애 기록 없음');await page.goto('/incidents/'+i.id);await page.locator('.step-panel').waitFor({state:'attached'});
  await allSteps(page);const graph=page.locator('.flow-canvas .react-flow__viewport'),before=await graph.getAttribute('style');
  await page.getByRole('button',{name:'흐름 확대',exact:true}).click();await expect(graph).not.toHaveAttribute('style',before);
  const node=page.locator('.flow-step button').first();await node.focus();await page.keyboard.press('Enter');
  await expect(stage(page)).toHaveAttribute('data-selected-step',await node.locator('xpath=ancestor::*[@data-id][1]').getAttribute('data-id'));
  const input=stage(page).locator('summary').first();
  if(await input.count()){
    await input.focus();await input.press('Enter');
    await expect(input.locator('..')).toHaveAttribute('open','');
    await input.press('Enter');await expect(input).toBeFocused();
  }
  await tab(page,'대화');await tab(page,'흐름');await expect(stage(page)).toBeVisible();await layout(page);
});
