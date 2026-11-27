import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { mock } from './p15-helpers.mjs';
test.skip(!mock, '격리 API로 사건 전이와 권한 실패를 확인합니다.');
const overview = JSON.parse(readFileSync(new URL('./fixtures/samsung-overview.mock.json', import.meta.url),'utf8'));
const make = (id, service, state) => ({id,workload:service,status:state,title:'서비스 처리 지연',namespace:'dev',system:'samsung-trade',environment:'dev',created_at:'2026-10-01T00:00:00Z',severity_level:'NORMAL'});
async function data(page,{roots=[],apps=[],appStatus=200}={}) {
  const o=structuredClone(overview); o.incidents.open=[]; o.incidents.counts={unclaimed_handoff:1249};
  await page.route('**/api/overview',r=>r.fulfill({json:o}));
  await page.route('**/api/incidents?*',r=>{
    const q=new URL(r.request().url()).searchParams,items=q.has('status')?roots.filter(i=>i.status===q.get('status')):roots;
    return r.fulfill({json:{items,total:items.length}});
  });
  await page.route('**/api/unified/summaries?*',r=>r.fulfill({status:appStatus,json:{incidents:apps,total:apps.length,as_of:10}}));
}

test('관제 홈은 일반 글꼴만 먼저 받고 희귀 한글을 표시할 때 전체 글꼴을 받는다', async ({page}) => {
  await data(page);
  await page.goto('/');
  await expect(page.getByRole('article', {name: 'payment', exact: true})).toBeVisible();
  const initial = await page.evaluate(async () => {
    await document.fonts.ready;
    return performance.getEntriesByType('resource').map(entry => new URL(entry.name).pathname)
      .filter(path => path.includes('/wanted-sans-') && path.endsWith('.woff2'));
  });
  expect(initial.length).toBeGreaterThan(0);
  expect(initial.every(path => path.includes('-core-'))).toBe(true);

  const requestsFullFont = page.waitForRequest(request => {
    const path = new URL(request.url()).pathname;
    return path.includes('/wanted-sans-') && path.endsWith('.woff2') && !path.includes('-core-');
  });
  await page.evaluate(async () => {
    await document.fonts.load('400 16px "Wanted Sans"', '뷁');
  });
  await requestsFullFont;
});

test('서비스 하나에 dev·prd, 사건 수에 따른 진입과 개입·이관 이력 분리',async({page})=>{
  const roots=[make(1,'payment','analyzing'),make(2,'payment','action_pending'),make(3,'inventory','analyzing')];
  const apps=[{id:'child',state:'awaiting_prd_approval',service:'payment',system:'samsung-trade',environment:'dev',reason:'결제 수정본 검토',root_incident_id:2}];
  await data(page,{roots,apps});
  await page.goto('/');
  const payment=page.getByRole('article',{name:'payment',exact:true});
  await expect(payment).toHaveCount(1);
  await expect(payment.getByRole('button',{name:'payment prd · 준비 · 상세 보기'})).toBeVisible();
  await expect(page.locator('.atlas-rail-title')).toContainText('운영자 개입1');
  await expect(page.locator('.atlas-history')).toContainText('1,249건');
  await expect(page.getByRole('link',{name:/운영 승인/})).toHaveAttribute('href','/incidents?work=approval');
  await payment.getByRole('button',{name:'payment dev · 장애 2건 · 상세 보기'}).click();
  const selected=page.getByRole('complementary',{name:'운영자 작업'});
  await expect(selected.getByRole('heading',{name:'payment'})).toBeVisible();
  await expect(selected.locator('.atlas-cases a')).toHaveCount(2);
  await expect(selected.locator('.atlas-cases a').first()).toHaveAttribute('href','/incidents/2');
  await page.getByRole('button',{name:'inventory dev · 장애 1건 · 상세 보기'}).click();
  await expect(selected.getByText('Pod 준비 / 목표')).toBeVisible();
  await selected.locator('.atlas-cases a').click();
  await expect(page).toHaveURL(/\/incidents\/3$/);
});
test('새 장애는 종에만 추가하고 선택과 현재 화면을 유지한다',async({page})=>{
  const roots=[make(1,'payment','analyzing')];await data(page,{roots});
  await page.goto('/');
  const selected=page.getByRole('button',{name:'payment prd · 준비 · 상세 보기'});
  const zoom=page.getByRole('group',{name:'서비스 지도 배율'});
  await zoom.getByRole('button',{name:'확대',exact:true}).click();
  await expect(zoom.locator('output')).toHaveText('120%');
  await selected.click();
  await expect(page.locator('.atlas-interventions h2')).toHaveText('payment');
  await expect(page.locator('.alert-button')).toHaveAttribute('aria-label',/알림 0건/);
  roots.push(make(23,'order','open'));
  await expect(page.locator('.alert-button')).toHaveAttribute('aria-label',/알림 1건/,{timeout:10000});
  await expect(page).toHaveURL(/\/$/);await expect(selected).toHaveAttribute('aria-pressed','true');await expect(zoom.locator('output')).toHaveText('120%');
  await expect(page.locator('.toast,.toasts,.alert-panel')).toHaveCount(0);
  await page.locator('.alert-button').click();
  await expect(page.locator('.bell-updates a')).toHaveText(/새 사건.*order/);
  await expect(page.locator('.alert-button')).toHaveAttribute('aria-label',/알림 0건/);
});
test('Application 읽기 권한 누락은 0건으로 집계하지 않고 인프라 관제도 열 수 있다',async({page})=>{
  await data(page,{appStatus:403});await page.goto('/');
  await expect(page.getByText('Application 개입 대기 미관측',{exact:true})).toBeVisible();
  await expect(page.locator('.atlas-rail-title')).toContainText('—');
  await page.getByRole('button',{name:/^Kubernetes ·/}).click();
  await expect(page.getByRole('heading',{name:'수집 진단',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'지도로 돌아가기'}).click();
  await expect(page.getByRole('heading',{name:'서비스 지도',exact:true})).toBeVisible();
});
test('서비스 상세와 인프라 지도는 각각 열 때 필요한 화면만 불러온다', async ({page}) => {
  await data(page);
  const requested = [];
  page.on('request', request => requested.push(new URL(request.url()).pathname));
  const graphLoaded = () => requested.some(path => /\/(TopologyMap|graphShared)-[^/]+\.js$/.test(path));
  await page.goto('/');
  await page.getByRole('button', { name: 'payment prd · 준비 · 상세 보기' }).click();
  await page.getByRole('button', { name: '인프라 자원 상세', exact: true }).click();
  await expect(page.getByRole('complementary', { name: '자원 상세', exact: true })).toContainText('payment');
  expect(graphLoaded()).toBe(false);
  await page.getByRole('navigation', { name: '수집원 상태' }).getByRole('button').first().click();
  await page.getByRole('button', { name: '인프라 연결 지도 열기', exact: true }).click();
  await expect(page.getByRole('region', { name: '클러스터 연결 지도' })).toBeVisible();
  expect(graphLoaded()).toBe(true);
});
test('노트북과 큰 화면은 지도 옆 작업 목록과 읽을 수 있는 서비스 이름을 유지한다', async({page},info)=>{
  test.skip(info.project.name!=='width-1440','두 데스크톱 크기를 한 번씩 확인합니다.');
  await data(page);await page.emulateMedia({reducedMotion:'reduce'});
  for(const [width,height] of [[1440,900],[1920,1080]]) {
    await page.setViewportSize({width,height});await page.goto('/');
    await expect(page.locator('.atlas-node').first()).toBeVisible();
    const map=await page.locator('.atlas-map').boundingBox(), rail=await page.locator('.atlas-interventions').boundingBox();
    expect(map.y).toBeLessThan(125);expect(rail.x).toBeGreaterThan(map.x+map.width-1);
    expect(await page.locator('.atlas-node h4').first().evaluate(e=>parseInt(getComputedStyle(e).fontSize))).toBeGreaterThanOrEqual(14);
    expect(await page.locator('.atlas-env').first().evaluate(e=>getComputedStyle(e).transitionDuration)).toBe('0s');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  }
});
test('dev 브라우저 QA 실패가 도착하면 운영 승인은 그대로이고 종에 보완이 들어온다',async({page})=>{
  const apps=[];await data(page,{apps});await page.goto('/');
  await expect(page.locator('.atlas-rail-title')).toContainText('운영자 개입0');
  apps.push({id:'native-qa',state:'qa_failed',environment:'dev',system:'samsung-trade',service:'payment',reason:'결제 처리 검증 실패'});
  await expect(page.locator('.alert-button')).toHaveAttribute('aria-label',/알림 1건/,{timeout:10000});
  await expect(page.locator('.atlas-interventions .atlas-action')).toContainText('0건');
  await expect(page.locator('.atlas-cases a').first()).toHaveAttribute('href','/incidents/application/native-qa');
  await page.locator('.alert-button').click();
  await expect(page.locator('.bell-updates a')).toHaveText('QA 보완 필요 · payment');
});
