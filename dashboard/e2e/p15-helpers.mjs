import { expect } from '@playwright/test';
import { mkdirSync, appendFileSync, readFileSync, readdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { createHash } from 'node:crypto';
const sourceFiles = ['index.html','vite.config.ts',...readdirSync('src').filter(f => /\.(ts|tsx|css)$/.test(f)).sort().map(f => `src/${f}`)];
export const sourceHash = createHash('sha256').update(sourceFiles.map(f => `${f}\n${readFileSync(f,'utf8')}`).join('\n')).digest('hex');
export const mock = process.env.LIVEPROBE_MOCK === '1';
// 각 개선 단계가 이전 배포의 캡처·검사 증거를 덮어쓰지 않도록 분리한다.
const artifactRoot = process.env.LIVEPROBE_EVIDENCE_ROOT ?? 'e2e/artifacts';
export async function resetMock(request) { if (mock) await request.post('/api/mock/reset', { headers: { 'X-LiveProbe-Client':'dashboard' } }); }
export async function tab(page, name) {
  const room=page.locator('.incident-room'), history=['대화','전체 대화·이력'].includes(name);
  await room.waitFor();
  const current=history?'tab-chat':'tab-flow';
  if (await room.evaluate((e, cls)=>e.classList.contains(cls),current)) return;
  const nav=room.getByRole('navigation',{name:'관제실 화면 전환'});
  if (await nav.isVisible()) await nav.getByRole('button',{name:history?'전체 대화·이력':'현재 단계',exact:true}).click();
  else await room.locator('.conversation-pane .pane-heading').getByRole('button',{name:history?'전체 대화·이력':'단계 관제로',exact:true}).click();
  await expect(room).toHaveClass(new RegExp(current));
}
/** P18 W6: 근거는 별도 탭이 아니라 02 대화의 단계 카드 안에 있다. 좁은 화면에서는 대화 탭으로 전환한다. */
export async function evidenceTab(page) { await tab(page,'대화'); }
/** 데스크톱 무스크롤 검사: 페이지(.main) 자체가 세로로 스크롤되지 않는다. */
export async function noPageScroll(page) { const m=await page.locator('.main').evaluate(e=>({sh:e.scrollHeight,ch:e.clientHeight})); expect(m.sh,JSON.stringify(m)).toBeLessThanOrEqual(m.ch+1); }
/** fixedRoot: 단계 전용 증거 위치(예: e2e/artifacts/mock/w6). 다른 단계의 기존 캡처·목록을 덮어쓰지 않는다. */
export async function capture(page, testInfo, name, fixedRoot) {
  if (fixedRoot) {
    mkdirSync(fixedRoot,{recursive:true}); const path=`${fixedRoot}/${name}-${testInfo.project.name}.png`;
    await page.screenshot({path,fullPage:true});
    appendFileSync(`${fixedRoot}/captures.jsonl`, JSON.stringify({ path, state:name, scope:'mock', project:testInfo.project.name, viewport:page.viewportSize(), url:page.url(), at:new Date().toISOString(), source_sha256:sourceHash })+'\n');
    return path;
  }
  const dir = `${artifactRoot}/${mock ? 'mock' : 'live'}`;
  mkdirSync(dir,{recursive:true});
  const path=`${dir}/${name}-${testInfo.project.name}.png`;
  mkdirSync(dirname(path),{recursive:true});
  await page.screenshot({path,fullPage:true});
  appendFileSync(`${artifactRoot}/captures.jsonl`, JSON.stringify({ path, state:name, project:testInfo.project.name, viewport:page.viewportSize(), url:page.url(), at:new Date().toISOString(), source_sha256:sourceHash })+'\n');
}
export async function layout(page) {
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  const sizes=await page.locator('.main,.room-pane,.chat-feed,.composer,.step-card,.detail-panel,.detail-scroll,.resource-detail,.command-card,.decision-card,.ops-side,.side-scroll,.alert-panel').evaluateAll(es=>es.filter(e=>e.getBoundingClientRect().width>0).map(e=>({name:e.className,client:e.clientWidth,scroll:e.scrollWidth})));
  for(const s of sizes)expect(s.scroll,JSON.stringify(s)).toBeLessThanOrEqual(s.client+1);
  const overlaps=await page.evaluate(()=>{
    const out=[];
    for(const group of document.querySelectorAll('.composer form,.step-chat-row,.step-actions,.detail-head,.confirm-gate .inline,.room-tabs,.filters,.map-toolbar,.nav,.graph-toolbar,.bar-right,.side-tabs,.ops-side-head')){
      const es=[...group.querySelectorAll('button,input,select,textarea,a')].filter(e=>e.getBoundingClientRect().width>0);
      for(let i=0;i<es.length;i++)for(let j=i+1;j<es.length;j++){
        const a=es[i].getBoundingClientRect(),b=es[j].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)out.push([es[i].textContent,es[j].textContent]);
      }
    }
    for(const root of document.querySelectorAll('.topology-canvas,.flow-canvas')){
      const frame=root.querySelector('.react-flow')?.getBoundingClientRect();if(!frame||!frame.width)continue;
      for(const control of root.querySelectorAll('.graph-toolbar button'))for(const node of root.querySelectorAll('.resource-button,.flow-step button')){
        const a=control.getBoundingClientRect(),b=node.getBoundingClientRect();
        const left=Math.max(b.left,frame.left),right=Math.min(b.right,frame.right),top=Math.max(b.top,frame.top),bottom=Math.min(b.bottom,frame.bottom);
        if(Math.min(a.right,right)-Math.max(a.left,left)>1&&Math.min(a.bottom,bottom)-Math.max(a.top,top)>1)out.push(['그래프 제어와 노드',control.getAttribute('aria-label'),node.getAttribute('aria-label')]);
      }
    }
    // 지도 연결선 문구는 자원 카드 뒤에 가려지면 안 된다(카드가 SVG 연결선 위에 그려진다).
    for(const label of document.querySelectorAll('.topology-canvas .react-flow__edge-textwrapper'))for(const node of document.querySelectorAll('.topology-canvas .resource-button')){
      const a=label.getBoundingClientRect(),b=node.getBoundingClientRect();
      if(a.width&&Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)out.push(['연결선 문구가 자원에 가려짐',label.textContent,node.getAttribute('aria-label')]);
    }
    return out;
  });expect(overlaps).toEqual([]);
  const clipped=await page.locator('.btn,.filter,.room-tabs button,.side-tabs button,.alert-button,.cited-by button,.graph-toolbar button').evaluateAll(es=>es.filter(e=>e.getBoundingClientRect().width>0 && (e.scrollWidth>e.clientWidth+2||e.scrollHeight>e.clientHeight+2)).map(e=>({text:e.textContent,w:e.clientWidth,sw:e.scrollWidth,h:e.clientHeight,sh:e.scrollHeight})));
  expect(clipped).toEqual([]);
  mkdirSync(artifactRoot,{recursive:true});
  appendFileSync(`${artifactRoot}/layout.jsonl`, JSON.stringify({ url:page.url(), viewport:page.viewportSize(), source_sha256:sourceHash, document:await page.evaluate(()=>({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})), panels:sizes, overlaps, clipped })+'\n');
}
export function errors(page) { const es=[];page.on('pageerror',e=>es.push(e.message));return es; }

/** P21: the infrastructure map sits in the collection diagnostics beside the service map (source button → 수집 진단 → 인프라 연결 지도). */
export async function openInfraMap(page) {
  await page.getByRole('navigation', { name: '수집원 상태' }).getByRole('button').first().click();
  await expect(page.getByRole('heading', { name: '수집 진단', exact: true })).toBeVisible();
  const map = page.locator('details.disclosure').filter({ has: page.locator('summary', { hasText: '인프라 연결 지도' }) });
  if (!(await map.evaluate(d => d.open))) await map.locator('summary').first().click();
  await expect(page.locator('.topology-canvas')).toBeVisible({ timeout: 10000 });
}

/** The semantic stage card that shows a raw flow node (dashboard/src/semanticFlow.ts coreStage). */
export function stageOf(node) {
  if (node.status === 'planned' || node.type === 'interpret' || (node.id.startsWith('plan:') && node.status === 'skipped')) return null;
  switch (node.type) {
    case 'signal': return 'stage:detection';
    case 'evidence': return node.card?.kind === 'evidence' && node.card.total === 0 && !node.at ? null : 'stage:evidence';
    case 'router': case 'group': return node.status === 'skipped' ? null : 'stage:routing';
    case 'agent': case 'investigation': case 'code_action': return 'stage:investigation';
    case 'verify': return 'stage:verification';
    case 'outcome': return node.card?.kind === 'outcome' && node.card.status === 'handed_off' ? 'stage:handoff' : 'stage:result';
    case 'command': case 'action': return node.card?.kind === 'action' && ['pending_approval', 'rejected'].includes(node.card.status) ? 'stage:approval' : 'stage:action:' + node.id;
    default: return null;
  }
}
/** Every stage card a narrative must show: one card per stage, never one per raw node. */
export async function expectStages(page, narrative) {
  const want = [...new Set(narrative.flow.nodes.map(stageOf).filter(Boolean))];
  const ids = await page.locator('.flow-step button').evaluateAll(es => es.map(e => e.dataset.stageId));
  expect(new Set(ids).size, JSON.stringify(ids)).toBe(ids.length);
  expect(ids.every(id => id?.startsWith('stage:')), JSON.stringify(ids)).toBe(true);
  for (const id of want) expect(ids, id).toContain(id);
  return ids;
}
/** Open a stage card's judgement rows and the first collapsed call (its request and answer stream). */
export async function openFirstCall(panel) {
  const rows = panel.getByRole('button', { name: /^판단 기록/ });
  if (await rows.count() && (await rows.first().getAttribute('aria-expanded')) === 'false') await rows.first().click();
  const call = panel.locator('.call-toggle[aria-expanded="false"]').first();
  if (await call.count()) await call.click();
}
