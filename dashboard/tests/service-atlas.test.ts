import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { atlasLanes, caseHref, caseSummary, chipLabel, laneSummary, linkSummary, loadWaiting, mergeCases, readingLabel, readingStatus, readingTone, serviceAtlas, wirePath, type AtlasService, type Reading } from '../src/serviceAtlasModel.ts';
import type { Incident, Overview } from '../src/api.ts';
import type { ApplicationIncident } from '../src/unified.ts';
const fixture = () => JSON.parse(readFileSync(new URL('../e2e/fixtures/samsung-overview.mock.json', import.meta.url),'utf8')) as Overview;
const root = (id:number,status='action_pending') => ({id,status,environment:'dev',workload:'payment',system:'samsung-trade',title:'결제 처리 지연',namespace:'dev'} as Incident);
const application = (id:string, root_incident_id:number|null=null): ApplicationIncident => ({id,root_incident_id,environment:'dev',state:'awaiting_prd_approval',service:'payment',system:'samsung-trade',reason:'결제 처리 지연',data:{},created_at:1});
test('서비스별 두 환경, 독립 서비스, 태그 미식별·미관측, 실제 연결을 보존', () => {
  const o=fixture();
  o.workloads.push({...o.workloads[0],id:'dev/Deployment/solo',name:'solo',kind:'Deployment',environment:'dev',system:null,ready:null,desired:null});
  const atlas=serviceAtlas(o,[]);
  const items=atlas.groups.flatMap(([,s])=>s), payment=items.filter(s=>s.name==='payment');
  assert.equal(payment.length,1);
  assert.equal(readingStatus(payment[0].env.dev),'healthy'); assert.equal(readingStatus(payment[0].env.prd),'healthy');
  const solo=items.find(s=>s.name==='solo')!;
  assert.equal(solo.system,''); assert.equal(readingStatus(solo.env.prd),'unknown'); assert.equal(readingStatus(solo.env.dev),'unknown');
  assert.ok(atlas.links.some(e=>!e.observed)); assert.ok(!atlas.links.some(e=>e.from===e.to));
});
test('루트와 Application 중복 제거, 과거 독립 원래 ID 유지, 원문 제거', () => {
  const rows=mergeCases([root(12),root(13,'analyzing')],[application('a',12),{...application('legacy a'),reason:'Exception: api_key=PRIVATE'}]);
  assert.equal(rows.length,3); assert.equal(rows.find(s=>s.key==='12')?.label,'prd 승인 대기');
  assert.equal(rows.filter(s=>s.intervention).length,2);
  assert.equal(caseHref(application('legacy a')),'/incidents/application/legacy%20a');
  assert.ok(!JSON.stringify(rows).includes('PRIVATE'));
  const board=serviceAtlas(fixture(), rows);
  const payment=board.groups.flatMap(([,s])=>s).find(s=>s.name==='payment')!;
  assert.equal(payment.env.dev.cases.length,3); assert.equal(readingStatus(payment.env.dev),'risk');
});
test('대기 목록은 고정 기준으로 다음 페이지를 읽고 누락을 0건으로 채우지 않는다', async () => {
  const requests:string[]=[];
  const rows=await loadWaiting(async path=>{
    requests.push(path);return {incidents:[application(requests.length===1?'a':'b')],total:2,as_of:20};
  });
  assert.equal(rows.length,2); assert.match(requests[1],/offset=1&as_of=20/);
  await assert.rejects(loadWaiting(async ()=>({incidents:[],total:1,as_of:20})));
  await assert.rejects(loadWaiting(async ()=>({incidents:[application('same')],total:2,as_of:20})));
});
test('5xx는 정상 Pod보다 우선하며 응답 방향·무트래픽·미관측을 서로 구분한다', async () => {
  const {observationLabel,trafficLabel}=await import('../src/serviceAtlasModel.ts');
  const o=fixture();
  const payment=o.workloads.find(s=>s.name==='payment' && s.environment==='dev')!;
  payment.err_rate=.03;
  const destination=o.workloads.find(s=>s.name==='inventory' && s.environment==='dev')!;
  o.topology!.observed_edges=[{from:payment.id,to:destination.id,req_rate:.2,err_rate:.06} as NonNullable<Overview['topology']>['observed_edges'][number]];
  const model=serviceAtlas(o,[]), all=model.groups.flatMap(([,s])=>s);
  const src=all.find(s=>s.name==='payment')!, target=all.find(s=>s.name==='inventory')!;
  assert.equal(observationLabel(src.env.dev),'HTTP 5xx 3%'); assert.equal(observationLabel(target.env.dev),'HTTP 5xx 6%');
  assert.equal(readingStatus(src.env.dev),'risk');
  assert.equal(trafficLabel({...target.env.prd,requestRate:0,observedResponses:false}),'조회 구간 요청 없음');
  assert.equal(trafficLabel({...target.env.prd,errorRate:null,requestRate:null},'error'),'HTTP 지표 수집 확인 필요');
});
test('기존 dev 시험·prd 승인은 각각 고유 레이블, QA 실패는 보완 개입, prd 회복은 계속 관측', () => {
  const approvals=mergeCases([],[
    {...application('v2'),state:'awaiting_user_test'},application('v3'),{...application('qa'),state:'qa_failed'},
  ]);
  assert.equal(approvals.filter(row=>row.approval).length,2);
  assert.equal(approvals.find(row=>row.key==='app:v2')?.label,'이전 릴리스 수동 확인 대기');
  assert.equal(approvals.find(row=>row.key==='app:v3')?.label,'prd 승인 대기');
  assert.deepEqual(approvals.filter(row=>row.intervention&&!row.approval).map(row=>row.label),['QA 보완 필요']);
  const recovering={...root(6,'analyzing'),code_action:{state:'awaiting_recovery'}};
  assert.deepEqual(mergeCases([recovering],[]).map(r=>[r.intervention,r.label]),[[false,'prd 복구 관측 중']]);
});
const reading = (over: Partial<Reading> = {}): Reading => ({ states: new Set(), resource: null, cases: [], requestRate: null, errorRate: null, observedResponses: false, ...over });
const svc = (system: string, name: string, dev: Partial<Reading> = { states: new Set(['ready']) }, prd: Partial<Reading> = { states: new Set(['ready']) }): AtlasService =>
  ({ key: JSON.stringify([system, name]), name, system, env: { dev: reading(dev), prd: reading(prd) } });
test('P21 상태 톤은 사건·5xx·중단을 장애, 일부 비정상·연결 대상 없음을 주의, 미관측을 정상과 구분한다', () => {
  const incident = { key: '1' } as Parameters<typeof readingLabel>[0]['cases'][number];
  assert.equal(readingTone(reading({ cases: [incident], states: new Set(['ready']) })), 'danger');
  assert.equal(readingTone(reading({ errorRate: .02, states: new Set(['ready']) })), 'danger');
  assert.equal(readingTone(reading({ states: new Set(['down']) })), 'danger');
  assert.equal(readingTone(reading({ states: new Set(['degraded']) })), 'warning');
  assert.equal(readingTone(reading({ states: new Set(['no_endpoints']) })), 'warning');
  assert.equal(readingTone(reading({ states: new Set(['ready']) })), 'ok');
  assert.equal(readingTone(reading()), 'unknown');
  assert.equal(readingLabel(reading({ cases: [incident, incident] })), '장애 2건');
  assert.equal(readingLabel(reading()), '미관측');
});
test('P21 레인은 연결된 시스템을 붙이고 가장 많이 호출되는 시스템을 가운데 둔다 · 상태로 순서를 바꾸지 않는다', () => {
  const business = svc('samsung-business', 'business-api'), member = svc('samsung-delivery', 'delivery-member'), trade = svc('samsung-trade', 'trade-member');
  const groups: [string, AtlasService[]][] = [
    ['liveprobe-verification', [svc('liveprobe-verification', 'lp-config', undefined, {})]],
    ['samsung-business', [business]],
    ['samsung-delivery', [svc('samsung-delivery', 'delivery'), member]],
    ['samsung-trade', [svc('samsung-trade', 'catalog'), svc('samsung-trade', 'inventory'), trade]],
  ];
  const links = [{ from: member.key, to: business.key, observed: false }, { from: trade.key, to: business.key, observed: false }];
  const lanes = atlasLanes(groups, links);
  assert.deepEqual(lanes.map(l => l.system), ['samsung-delivery', 'samsung-business', 'samsung-trade', 'liveprobe-verification']);
  assert.deepEqual(lanes[0].services.map(s => s.name), ['delivery-member', 'delivery']);
  assert.deepEqual(lanes[2].services.map(s => s.name), ['trade-member', 'catalog', 'inventory']);
  assert.equal(laneSummary(lanes[3]), '서비스 1 · prd 미관측 1');
  assert.equal(laneSummary(lanes[1]), '서비스 1 · 모두 준비');
  const sick = groups.map(([system, items]) => [system, items.map(s => s.name === 'inventory' ? { ...s, env: { ...s.env, dev: reading({ errorRate: .1, states: new Set(['ready']) }) } } : s)] as [string, AtlasService[]]);
  const again = atlasLanes(sick, links);
  assert.deepEqual(again.map(l => l.system), lanes.map(l => l.system));
  assert.equal(laneSummary(again[2]), '서비스 3 · 장애·오류 1');
  const names = new Map(groups.flatMap(([, s]) => s).map(s => [s.key, s.name]));
  assert.equal(linkSummary(member, links, names), '→ business-api · 선언');
  assert.equal(linkSummary(business, [...links.slice(0, 1), { ...links[1], observed: true }], names), '← 2개 서비스 · 관측·선언');
  assert.equal(linkSummary(svc('x', 'solo'), links, names), '');
});
test('P21 사건 요약은 두 환경 합계, 칩은 짧은 건수, 읽기 이름은 전체 문구를 쓴다', () => {
  const item = { key: '9' } as Parameters<typeof readingLabel>[0]['cases'][number];
  assert.equal(caseSummary(svc('t', 'inventory', { cases: [item, item], states: new Set(['ready']) }, { cases: [item], states: new Set(['ready']) })), '장애 3건');
  assert.equal(caseSummary(svc('t', 'order')), '');
  assert.equal(chipLabel(reading({ cases: [item, item, item], states: new Set(['ready']) })), '장애 3');
  assert.equal(readingLabel(reading({ cases: [item, item, item] })), '장애 3건');
  assert.equal(chipLabel(reading()), '미관측');
});
test('P21 연결선은 같은 열·같은 행이면 곧게, 아니면 노드 사이 빈칸으로만 꺾인다', () => {
  const a = { left: 0, top: 0, right: 200, bottom: 80 }, below = { left: 0, top: 120, right: 200, bottom: 200 };
  assert.equal(wirePath(a, below), 'M100,80 V120');
  assert.equal(wirePath(below, a), 'M100,120 V80');
  assert.equal(wirePath(a, { left: 240, top: 0, right: 440, bottom: 80 }), 'M200,40 H240');
  const far = { left: 480, top: 220, right: 680, bottom: 300 };
  assert.equal(wirePath(a, far), 'M100,80 V84 H472 V260 H480');
});
test('P21 연결선은 사이에 다른 노드가 있으면 옆 열 간격으로 돌아가고, 사건이 있어도 준비된 Pod는 준비로 읽는다', async () => {
  const a = { left: 0, top: 0, right: 200, bottom: 80 }, middle = { left: 0, top: 100, right: 200, bottom: 180 };
  const b = { left: 0, top: 220, right: 200, bottom: 300 }, beside = { left: 216, top: 0, right: 416, bottom: 80 };
  assert.equal(wirePath(a, b, [a, middle, b, beside]), 'M200,40 H208 V260 H200');
  assert.equal(wirePath(a, b, [a, b, beside]), 'M100,80 V220');
  const { observationLabel } = await import('../src/serviceAtlasModel.ts');
  const item = { key: '3' } as Parameters<typeof readingLabel>[0]['cases'][number];
  assert.equal(observationLabel(reading({ cases: [item], states: new Set(['ready']) })), '준비');
  assert.equal(observationLabel(reading({ cases: [item] })), '미관측');
});
