import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { mock } from './p15-helpers.mjs';
import { mockUnifiedRoom } from './fixtures/unified-room.mjs';

test.skip(!mock, '격리된 관제실 모의 서버 전용');

const rail = page => page.locator('.flow-canvas[data-mode="stage"]');
const cards = page => rail(page).locator('button[data-stage-id]');
const detail = page => page.locator('.semantic-stage');
const card = (page, id) => cards(page).and(page.locator('[data-stage-id="' + id + '"]'));
const browseButtons = page => page.getByRole('button', {name:/카드 둘러보기/});

test('단계 흐름과 전체 흐름이 같은 두 줄 카드, 화살표, 병렬 조치, 합류를 그린다', async ({page}) => {
  const source = JSON.parse(readFileSync(new URL('./fixtures/narrative-971.json',import.meta.url)));
  const action = source.flow.nodes.find(node=>node.id === 'action:90');
  const second = {...action, id:'action:91', label:'레플리카 변경', lane:1,
    card:{...action.card,action_id:91,type:'scale_replicas',type_ko:'레플리카 변경'}};
  source.flow.nodes.push(second);
  source.flow.edges.push(
    {id:'run:4101>action:91:flow',source:'run:4101',target:second.id,kind:'flow',label:null},
    {id:'action:91>outcome:flow',source:second.id,target:'outcome',kind:'flow',label:null});
  await page.route('**/api/incidents/971/narrative',route=>route.fulfill({json:source}));
  await page.goto('/incidents/971');
  const canvas = page.locator('.flow-canvas');
  const graphNodes = canvas.locator('.react-flow__node');
  const edgeList = canvas.locator('.react-flow__edge');
  const arrows = () => edgeList.evaluateAll(edges=>edges.map(edge=>{
    const path = edge.querySelector('path.react-flow__edge-path');
    const marker = path?.getAttribute('marker-end')?.match(/#([^'")]+)['"]?\)/)?.[1];
    return {id:edge.dataset.id, arrow:!!(marker && document.getElementById(marker)?.children.length && path.getTotalLength()>0)};
  }).sort((a,b)=>a.id.localeCompare(b.id)));
  const stages = graphNodes.locator('.flow-step');
  const nodeIds = () => graphNodes.evaluateAll(nodes=>nodes.map(node=>node.dataset.id).sort());
  await expect(rail(page)).toBeVisible();
  await expect(canvas.locator('.react-flow__node[data-id="stage:action:action:91"]')).toHaveCount(1);
  const verify='stage:verification', result='stage:result';
  const a='stage:action:action:90', b='stage:action:action:91';
  const forkAndJoin=[verify+'>'+a+':flow',verify+'>'+b+':flow',a+'>'+result+':flow',b+'>'+result+':flow'];
  // If an observed recovery is recorded it follows the action before the final explanation.
  const recovery='stage:recovery';
  if ((await nodeIds()).includes(recovery)) forkAndJoin.splice(2,2,a+'>'+recovery+':flow',b+'>'+recovery+':flow');
  await expect.poll(async ()=>(await arrows()).filter(edge=>edge.arrow).map(edge=>edge.id)).toEqual(expect.arrayContaining(forkAndJoin));
  const observedEdges=await arrows(), observedNodes=await nodeIds();
  expect(observedEdges.every(edge=>edge.arrow)).toBe(true);
  expect(observedEdges.map(edge=>edge.id)).not.toContain(a+'>'+b+':flow');
  async function geometryOfCards() {
    return stages.evaluateAll(cards=>cards.map(card=>{
      const title=card.querySelector('.flow-step-top strong'), state=card.querySelector('.flow-step-top .flow-state');
      const meta=card.querySelector('.flow-step-meta');
      const titleBox=title.getBoundingClientRect(), stateBox=state.getBoundingClientRect();
      return {height:card.offsetHeight, shape:card.offsetWidth/card.offsetHeight>2.6, icon:!!card.querySelector('.flow-role-icon svg'),
        title:title.textContent, status:state.textContent, subtitle:meta.textContent,
        statusBesideTitle:stateBox.left>=titleBox.left && stateBox.top<titleBox.bottom,
        subtitleBelow:meta.getBoundingClientRect().top>=titleBox.top+3};
    }));
  }
  const compact=await geometryOfCards();
  expect(compact.every(c=>c.height<=66 && c.shape && c.title && c.status && c.subtitle &&
    c.icon && c.statusBesideTitle && c.subtitleBelow)).toBe(true);
  const rows = await graphNodes.evaluateAll(nodes=>new Set(nodes.map(node=>Math.round(node.getBoundingClientRect().y))).size);
  expect(rows).toBeGreaterThan(1);
  await page.getByRole('button',{name:'전체 흐름',exact:true}).click();
  await expect.poll(nodeIds).toEqual(observedNodes);
  await expect.poll(arrows).toEqual(observedEdges);
  expect(await geometryOfCards()).toEqual(compact);
});

async function openRoom(page, kind, {summaryFirst = false} = {}) {
  if (kind === 'Kubernetes') {
    await page.goto('/incidents/971');
  } else {
    const { application } = await mockUnifiedRoom(page, {state:'awaiting_prd_approval'});
    await page.route('**/api/application/v1/incidents/native-a/summary', route => route.fulfill({
      json:{id:application.id, state:application.state, environment:application.environment,
        service:application.service, created_at:application.created_at, reason:'재고 요청을 조사합니다.'},
    }));
    let showActivity;
    if (summaryFirst) {
      const gate = new Promise(resolve => { showActivity = resolve; });
      await page.route('**/api/application/v1/incidents/native-a/activity?*',async route => {
        await gate;
        return route.fallback();
      });
    }
    await page.goto('/incidents/application/' + application.id);
    if (summaryFirst) {
      // A summary can already select approval before its earlier activity records arrive.
      await expect(detail(page)).toHaveAttribute('data-selected-step','stage:approval');
      await expect(cards(page)).toHaveCount(2);
      showActivity();
    }
  }
  await expect(rail(page)).toBeVisible();
  await expect.poll(() => cards(page).count()).toBeGreaterThanOrEqual(7);
}

async function geometry(page) {
  return rail(page).evaluate(viewport => {
    const bounds = viewport.getBoundingClientRect();
    const center = (bounds.left + bounds.right)/2;
    const stages = Object.fromEntries([...viewport.querySelectorAll('button[data-stage-id]')].map(button => {
      const r = button.getBoundingClientRect();
      const visible = Math.max(0, Math.min(r.right,bounds.right) - Math.max(r.left,bounds.left));
      return [button.dataset.stageId, {fraction:visible/r.width, offset:(r.left+r.right)/2 - center}];
    }));
    return {stages, width:bounds.width};
  });
}

async function expectCentered(page,id) {
  await expect.poll(async () => Math.abs((await geometry(page)).stages[id].offset)).toBeLessThan(2);
  await expect(card(page,id)).toHaveAttribute('aria-pressed','true');
  await expect(detail(page)).toHaveAttribute('data-selected-step',id);
}

async function expectFades(page, left, right) {
  await expect.poll(() => page.locator('.flow-canvas[data-mode="stage"]').evaluate(element =>
    ['::before','::after'].map(pseudo => {
      const style = getComputedStyle(element,pseudo);
      return style.backgroundImage.includes('linear-gradient') && parseFloat(style.width) > 8 ? (Number(style.opacity) >= .9 ? 1 : Number(style.opacity) === 0 ? 0 : null) : null;
    }))).toEqual([left,right]);
}

async function expectGraphBounds(page) {
  // A serpentine graph can extend past a selected terminal node on either side.
  const sides = await rail(page).evaluate(el=>{
    const bounds=el.getBoundingClientRect();
    const cards=[...el.querySelectorAll('.react-flow__node')].map(node=>node.getBoundingClientRect());
    return [Number(cards.some(r=>r.left < bounds.left-1)),Number(cards.some(r=>r.right > bounds.right+1))];
  });
  await expectFades(page,sides[0],sides[1]);
}

async function pressCardAtVisiblePoint(page,id) {
  const point = await card(page,id).evaluate(button => {
    const c = button.getBoundingClientRect(), v = button.closest('.flow-canvas').getBoundingClientRect();
    return {x:(Math.max(v.left,c.left) + Math.min(v.right,c.right))/2, y:(c.top+c.bottom)/2};
  });
  await page.mouse.click(point.x,point.y);
}

async function watchFrames(page,id) {
  await card(page,id).evaluate(button => {
    const viewport = button.closest('.flow-canvas');
    const frames = [];
    function capture() {
      const card = button.getBoundingClientRect(), view = viewport.getBoundingClientRect();
      frames.push((card.left + card.right - view.left - view.right)/2);
      if (frames.length < 120) requestAnimationFrame(capture);
    }
    window.travelOffsets = frames;
    capture();
  });
}

for (const kind of ['Kubernetes','Application']) {
  test(kind + ' 선택 카드가 가운데로 이동할 때 이웃 카드와 상세가 함께 바뀐다', async ({page}) => {
    await page.emulateMedia({reducedMotion:'no-preference'});
    await openRoom(page,kind);
    await expect(browseButtons(page)).toHaveCount(0);
    const [from,to,beyond] = kind === 'Kubernetes'
      ? ['stage:routing','stage:investigation','stage:verification']
      : ['stage:investigation','stage:action:code','stage:test_build'];
    await cards(page).and(page.locator('[aria-pressed="true"]')).press('Home');
    await expectCentered(page,'stage:detection');
    await card(page,'stage:detection').press('ArrowRight');
    await expectCentered(page,'stage:evidence');
    await card(page,'stage:evidence').press('ArrowRight');
    await expectCentered(page,from);
    await expect(rail(page)).toBeVisible();
    await expect(detail(page)).toBeVisible();
    const initial = await geometry(page);
    const hint = initial.width >= 640 ? beyond : to;
    expect(initial.stages[hint].fraction).toBeGreaterThan(0.35);
    expect(initial.stages[hint].fraction).toBeLessThan(0.65);
    if (hint === beyond) expect(initial.stages[to].fraction).toBeGreaterThan(0.99);
    await expectFades(page,1,1);

    // Coordinate clicks use the painted card rather than scrolling the DOM to a locator.
    await watchFrames(page,to);
    await pressCardAtVisiblePoint(page,to);
    await expectCentered(page,to);
    await expect(rail(page)).toBeVisible();
    await expect(detail(page)).toBeVisible();
    const middle = await geometry(page);
    if (hint === beyond) expect(middle.stages[beyond].fraction).toBeGreaterThan(0.99);
    else expect(middle.stages[to].fraction).toBeGreaterThan(0.99);
    const offsets = await page.evaluate(() => window.travelOffsets);
    const distance = initial.stages[to].offset;
    expect(new Set(offsets.filter(x => x > distance*0.03 && x < distance*0.97)).size).toBeGreaterThanOrEqual(2);

    await watchFrames(page,from);
    await pressCardAtVisiblePoint(page,from);
    await expectCentered(page,from);
    const reverse = await page.evaluate(() => window.travelOffsets);
    expect(new Set(reverse.filter(x => x < -distance*0.03 && x > -distance*0.97)).size).toBeGreaterThanOrEqual(2);

    await page.getByRole('button',{name:'전체 흐름',exact:true}).click();
    await expect(page.locator('.react-flow__node[data-id="' + from + '"] button')).toHaveAttribute('aria-pressed','true');
    await page.getByRole('button',{name:'단계 흐름',exact:true}).click();
    await expectCentered(page,from);
  });
}

test('수동 스크롤과 포커스는 위치를 유지하고 키보드 선택·초기 기록 수신만 가운데를 바꾼다', async ({page}) => {
  await page.emulateMedia({reducedMotion:'reduce'});
  let reads = 0;
  page.on('response',response => {
    if (new URL(response.url()).pathname === '/api/application/v1/incidents/native-a/activity' && response.ok()) reads++;
  });
  await openRoom(page,'Application',{summaryFirst:true});
  await expectCentered(page,'stage:approval');
  await expectGraphBounds(page);
  const last = cards(page).last(), first = cards(page).first();
  await last.focus();
  await last.press('Home');
  await expectCentered(page,'stage:detection');
  await expectFades(page,0,1);
  await first.press('ArrowRight');
  await expectCentered(page,'stage:evidence');
  await card(page,'stage:evidence').press('ArrowRight');
  await expectCentered(page,'stage:investigation');
  await rail(page).evaluate(viewport => {
    const camera = viewport.querySelector('.react-flow__viewport');
    const record = () => {
      // The container may resize when the detail arrives. Compare the camera to its actual frame.
      const m = new DOMMatrixReadOnly(getComputedStyle(camera).transform);
      const frame = viewport.querySelector('.react-flow').getBoundingClientRect();
      window.manualTravel.push([m.e-frame.width/2, m.f-frame.height/2, m.a]);
    };
    window.manualTravel = [];
    record();
    new MutationObserver(record).observe(camera, {attributes:true, attributeFilter:['style']});
  });
  const adjacent = card(page,'stage:action:code');
  await adjacent.focus();
  await expectCentered(page,'stage:investigation');
  await rail(page).hover();
  await page.mouse.wheel(300,0);
  await page.mouse.wheel(0,360);
  const {x,y,width,height} = await rail(page).boundingBox();
  const client = await page.context().newCDPSession(page);
  await client.send('Emulation.setTouchEmulationEnabled',{enabled:true});
  for (const [index,portion] of [.72,.63,.54,.45,.36,.27].entries()) {
    await client.send('Input.dispatchTouchEvent',{type:index === 0 ? 'touchStart' : 'touchMove',
      touchPoints:[{x:x+width*portion,y:y+height/2}]});
  }
  await client.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await client.detach();
  const initialReads = reads;
  await expect.poll(() => reads,{timeout:12000}).toBeGreaterThan(initialReads);
  await expectCentered(page,'stage:investigation');
  const cameraSamples = await page.evaluate(() => window.manualTravel);
  for (const [x,y,zoom] of cameraSamples) {
    expect(Math.abs(x-cameraSamples[0][0])).toBeLessThan(1);
    expect(Math.abs(y-cameraSamples[0][1])).toBeLessThan(1);
    expect(zoom).toBe(cameraSamples[0][2]);
  }

  await watchFrames(page,'stage:action:code');
  await adjacent.press('Enter');
  await expectCentered(page,'stage:action:code');
  const samples = await page.evaluate(() => window.travelOffsets);
  expect(samples.filter(x => x > 2 && x < samples[0] - 2)).toEqual([]);
  await adjacent.press('End');
  await expectCentered(page,'stage:approval');
  await expectGraphBounds(page);
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
  await expectCentered(page,'stage:detection');
  await expect(rail(page)).not.toHaveAttribute('data-before','true');
  await expect(rail(page)).not.toHaveAttribute('data-after','true');
  await expectFades(page,0,0);
  await expect(browseButtons(page)).toHaveCount(0);
});

// A transient arrival highlight does not identify an agent that keeps working.
async function expectWorkingMotion(step, active) {
  await expect.poll(() => step.evaluate(node => {
    const ring = getComputedStyle(node,'::after');
    return ring.animationName !== 'none' && ring.animationIterationCount.includes('infinite') &&
      ring.animationPlayState === 'running' && parseFloat(ring.animationDuration) > 0;
  })).toBe(active);
  if (active) {
    const paintedRing = () => step.evaluate(node => getComputedStyle(node,'::after').boxShadow);
    const first = await paintedRing();
    await expect.poll(paintedRing,{intervals:[90,120,200]}).not.toBe(first);
  }
}

const work = page => page.getByRole('navigation',{name:'현재 작업 위치'});
const returnToDiagram = async page => {
  const back = page.getByRole('button',{name:/흐름으로 돌아가기/});
  if (await back.isVisible()) await back.click();
};

test('Kubernetes에서 선택한 카드와 일하는 역할은 구분되고 보관된 사건에는 현재 작업 표시가 없다', async ({page}) => {
  const narrative = JSON.parse(readFileSync(new URL('./fixtures/narrative-1976.json',import.meta.url)));
  narrative.view_mode = 'live';
  await page.route('**/api/incidents/1976/narrative',route => route.fulfill({json:narrative}));
  await page.route('**/api/incidents/1976/events?*',route => route.fulfill({
    json:{events:[], control:'running', streaming:false, execution_mode:'live'},
  }));
  await page.goto('/incidents/1976');
  const jump = work(page).getByRole('button',{name:/원인 조사 현재 작업 보기.*쿠버네티스 분석/});
  await expect(jump).toBeVisible();
  await expect(work(page).getByRole('button')).toHaveCount(1);
  await cards(page).and(page.locator('[aria-pressed=true]')).press('Home');
  await expect(detail(page)).toHaveAttribute('data-selected-step','stage:detection');
  await returnToDiagram(page);
  const flowStep = id => page.locator('.react-flow__node[data-id="' + id + '"] .flow-step');
  await expect(flowStep('stage:detection')).not.toHaveAttribute('data-working','true');
  await expect(flowStep('stage:investigation')).toHaveAttribute('data-working','true');
  await expectWorkingMotion(flowStep('stage:investigation'),true);
  await expectWorkingMotion(flowStep('stage:detection'),false);
  await jump.click();
  await expect(detail(page)).toHaveAttribute('data-selected-step','stage:investigation');
  await returnToDiagram(page);
  await page.getByRole('button',{name:'전체 흐름',exact:true}).click();
  const agent = page.locator('.react-flow__node[data-id="stage:investigation"] .flow-step');
  await expect(agent).toHaveAttribute('data-working','true');
  await expectWorkingMotion(agent,true);
  await page.emulateMedia({reducedMotion:'reduce'});
  await expectWorkingMotion(agent,false);
  await expect(agent.locator('.flow-state')).toContainText('작업 중');
  await expect(page.locator('.flow-step[data-working=true]')).toHaveCount(1);
  await expect(jump).toBeVisible();

  narrative.view_mode = 'snapshot';
  await page.reload();
  await expect(rail(page)).toBeVisible();
  await expect(work(page)).toHaveCount(0);
  await expect(rail(page).locator('[data-working=true]')).toHaveCount(0);
  await page.getByRole('button',{name:'전체 흐름',exact:true}).click();
  await expect(page.locator('.stage-work-idle')).toContainText('저장 당시');
  await expect(page.locator('.flow-step[data-working=true]')).toHaveCount(0);
});

test('Application 역할 호출과 완료 이벤트가 오면 현재 작업 위치가 옮겨가고 마지막 호출 후에는 사라진다', async ({page}) => {
  const {application} = await mockUnifiedRoom(page,{state:'analyzing'});
  const path = '/api/application/v1/incidents/' + application.id;
  await page.route('**' + path + '/summary',route => route.fulfill({json:{
    id:application.id, state:'analyzing', created_at:1000, environment:'dev', service:'inventory',
  }}));
  const stages = ['detected','collecting','analyzing'];
  const events = stages.map((state,i) => ({id:i+1, event:state, at:1000+i, data:{}}));
  function role(event,role,call_id) {
    const id = events.length+1;
    events.push({id, event, at:1000+id, data:{role,call_id,model:'gpt-6.2-sol'}});
  }
  await page.route('**' + path + '/activity?*',route => {
    const after = Number(new URL(route.request().url()).searchParams.get('after'));
    return route.fulfill({json:{id:application.id, state:'analyzing', cursor:events.length, has_more:false,
      stages:stages.map((state,i) => ({state,occurred_at:1000+i})), events:events.filter(e => e.id > after), active_jobs:[]}});
  });
  role('role_started','analyst','call-1');
  await page.goto('/incidents/application/' + application.id);
  await expect(work(page).getByRole('button',{name:/원인 조사 현재 작업 보기.*애플리케이션 분석/})).toBeVisible();
  await expect(rail(page).locator('.react-flow__node[data-id="stage:investigation"] .flow-step')).toHaveAttribute('data-working','true');
  await card(page,'stage:evidence').click();
  await returnToDiagram(page);
  await page.getByRole('button',{name:'전체 흐름',exact:true}).click();
  role('role_completed','analyst','call-1');
  role('role_started','verifier','call-2');
  const jumping = work(page).getByRole('button',{name:/독립 검증 현재 작업 보기.*결과 확인/});
  await expect(jumping).toBeVisible();
  await expect(work(page).getByRole('button')).toHaveCount(1);
  const verifying = page.locator('.react-flow__node[data-id="stage:verification"] .flow-step');
  await expect(verifying).toHaveAttribute('data-working','true');
  await expectWorkingMotion(verifying,true);
  await expect(page.locator('.flow-step[data-working=true]')).toHaveCount(1);
  await expect(detail(page)).toHaveAttribute('data-selected-step','stage:evidence');
  await jumping.click();
  await expect(detail(page)).toHaveAttribute('data-selected-step','stage:verification');
  await returnToDiagram(page);
  role('role_completed','verifier','call-2');
  await expect(work(page)).toHaveCount(0);
  await expect(page.locator('.stage-work-idle')).toContainText('미관측');
  await expect(page.locator('.flow-step[data-working=true]')).toHaveCount(0);
});
