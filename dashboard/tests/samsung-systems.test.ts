import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { overviewMap, edgeText, resourceHealth, connectionsOf, incidentFor } from '../src/overviewModel.ts';
import * as model from '../src/overviewModel.ts';
import { buildMapModel } from '../src/mapModel.ts';
import { placeKo } from '../src/labels.ts';
import type { Overview } from '../src/api.ts';

const catalog = JSON.parse(readFileSync(new URL('../../deploy/samsung/system-catalog.json', import.meta.url), 'utf8'));
const fixture = (): Overview => {
  const nodes = catalog.environments.flatMap((env: string) => Object.entries(catalog.service_to_system).map(([name, system]) => ({ id: `${env}/Deployment/${name}`, namespace: env, environment: env, system, name, kind: 'Deployment', role: 'service', status: 'ready' })));
  const endpoint = (env: string, name: string, system: string | null, installed = true) => ({ id: `${env}/Deployment/${name}`, name, namespace: env, environment: catalog.environments.includes(env) ? env : null, system, installed });
  const kafka = { ...endpoint('liveprobe-messaging', 'kafka', null, false), id: 'liveprobe-messaging/Service/kafka' };
  const contracts = catalog.environments.flatMap((env: string) => {
    const business = endpoint(env, 'business-api', 'samsung-business');
    const edge = (a: typeof business, b: typeof business, type: string, operation: string) => ({ id: `${env}:${operation}:${a.id}>${b.id}`, from: a.id, to: b.id, from_ref: a, to_ref: b, type, operation, environment: env, observed: false, deployment_state: a.installed && b.installed ? 'deployed' : 'not_deployed' });
    return [edge(business, kafka, 'declared_event', 'publish'), ...['trade', 'delivery'].flatMap(target => { const consumer = endpoint(env, `${target}-member`, `samsung-${target}`); return [edge(kafka, consumer, 'declared_event', 'consume'), edge(consumer, business, 'declared_snapshot_api', 'snapshot')]; })];
  });
  return { generated_at: null, cluster: { nodes: [], namespaces: [], environments: catalog.environments.map((name: string, i: number) => ({ name, order: i, workloads: 9, pods_ready: 9, pods_total: 9, open_incidents: 0 })) }, workloads: [], incidents: { open: [], today: { total: null, resolved: null, handed_off: null, failed: null } }, events: [], sources: {}, topology: { generated_at: 100, freshness: { state: 'connected', last_flow_age_s: 1, stale: false, stale_after_s: 60, window_s: 300, received_total: 0, error: null }, nodes, structure_edges: [], observed_edges: [], network_alerts: [], contract_edges: contracts } } as unknown as Overview;
};

test('선택 환경은 카탈로그의 정확한 세 한국어 시스템명과 별도 내부 서비스를 보존한다', () => {
  const m = overviewMap(fixture(), 'prd');
  assert.deepEqual(m.groups.filter(g => g.kind === 'system').map(g => g.name).sort(), catalog.systems.map((s: { display_name: string }) => s.display_name).sort());
  for (const system of catalog.systems) {
    const group = m.groups.find(g => g.name === system.display_name)!;
    assert.deepEqual(m.nodes.filter(n => n.groupId === group.id && n.node.kind !== 'ContractEndpoint').map(n => n.node.name).sort(), [...system.services].sort());
  }
  assert.equal(m.nodes.find(n => n.node.name === 'order')!.groupId, m.nodes.find(n => n.node.name === 'inventory')!.groupId);
  assert.notEqual(m.nodes.find(n => n.node.name === 'order')!.id, m.nodes.find(n => n.node.name === 'inventory')!.id);
  assert.equal(new Set(m.nodes.filter(n => n.node.system === 'samsung-trade').map(n => n.x)).size, 2, '서비스가 많은 시스템은 두 열로 배치해 과도한 세로 축소를 피한다');
  assert.ok(m.groups.find(g => g.name === '공통 연계 인프라'));
  assert.equal(placeKo('prd', 'samsung-trade', 'order'), 'prd · 삼성 트레이드 시스템 · order');
  assert.equal(placeKo('prd', 'member', 'member'), 'prd · member · member');
});

test('전체 환경에서도 세 시스템 하위경계를 각 환경 안에 유지한다', () => {
  const m = overviewMap(fixture());
  for (const env of catalog.environments) {
    const parent = m.groups.find(g => g.kind === 'environment' && g.name === env)!;
    assert.ok(parent);
    const children = m.groups.filter(g => g.parentId === parent.id);
    assert.equal(children.length, 3);
    for (const n of m.nodes.filter(n => n.node.namespace === env)) assert.ok(children.some(g => g.id === n.groupId));
  }
  // 예전 기대 9(=3환경×3시스템)는 stg가 활성이던 시절의 것이다. 카탈로그 활성 환경이 dev·prd 둘이므로 3시스템×활성 환경 수다.
  assert.deepEqual(catalog.environments, ['dev', 'prd']);
  assert.equal(m.groups.filter(g => g.kind === 'system').length, 3 * catalog.environments.length);
  assert.equal(m.groups.filter(g => g.kind === 'system').length, 6);
});

test('응답에 퇴역 stg 자원·계약이 남아 있어도 활성 dev·prd만 지도에 그린다', () => {
  assert.deepEqual(catalog.retired_environments, ['stg']);
  const o = fixture();
  // 전환 전 서버(또는 배포 순서 차이)처럼 stg 환경·자원·계약선을 덧붙인다.
  o.cluster.environments!.push({ name: 'stg', order: 1.5, workloads: 9, pods_ready: 9, pods_total: 9, open_incidents: 0 });
  for (const [name, system] of Object.entries(catalog.service_to_system)) o.topology!.nodes.push({ id: `stg/Deployment/${name}`, namespace: 'stg', environment: 'stg', system: system as string, name, kind: 'Deployment', role: 'service', status: 'ready' });
  const prdContracts = o.topology!.contract_edges!.filter(e => e.environment === 'prd');
  const swap = (s: string) => s.replace(/(^|[>:])prd\//g, '$1stg/').replace(/^prd:/, 'stg:');
  for (const e of prdContracts) o.topology!.contract_edges!.push({ ...e, id: swap(e.id), environment: 'stg', from: swap(e.from), to: swap(e.to),
    from_ref: { ...e.from_ref, id: swap(e.from_ref.id), namespace: e.from_ref.namespace === 'prd' ? 'stg' : e.from_ref.namespace, environment: e.from_ref.environment === 'prd' ? 'stg' : e.from_ref.environment },
    to_ref: { ...e.to_ref, id: swap(e.to_ref.id), namespace: e.to_ref.namespace === 'prd' ? 'stg' : e.to_ref.namespace, environment: e.to_ref.environment === 'prd' ? 'stg' : e.to_ref.environment } });
  assert.deepEqual(model.environmentOptions(o).map(e => e.name), ['dev', 'prd']);
  const all = overviewMap(o);
  assert.deepEqual(all.groups.filter(g => g.kind === 'environment').map(g => g.name).sort(), ['dev', 'prd']);
  assert.equal(all.groups.filter(g => g.kind === 'system').length, 6);
  assert.ok(!all.nodes.some(n => n.node.namespace === 'stg' || n.node.environment === 'stg'), 'stg 자원·계약 끝점 없음');
  assert.ok(!all.edges.some(e => e.contract?.environment === 'stg'), 'stg 계약선 없음');
  assert.equal(all.edges.filter(e => e.relation === 'declared_event').length, 3 * catalog.environments.length);
});

test('중첩 그룹의 절대 좌표는 환경과 시스템 위치를 함께 더하며 키보드·연결면의 기준이 된다', () => {
  assert.equal(typeof model.groupOffset, 'function');
  const m = overviewMap(fixture());
  for (const g of m.groups.filter(g => g.parentId)) {
    const parent = m.groups.find(p => p.id === g.parentId)!;
    assert.deepEqual(model.groupOffset(m.groups, g.id), { x: parent.x + g.x, y: parent.y + g.y });
    assert.ok(g.x >= 0 && g.y >= 0 && g.x + g.width <= parent.width && g.y + g.height <= parent.height);
  }
});

test('미배포 Kafka는 계약 전용 끝점이며 플랫폼 숨김 중에도 계약 발행·소비·복구가 남는다', () => {
  const m = overviewMap(fixture(), 'prd');
  const kafka = m.nodes.find(n => n.node.name === 'kafka')!;
  assert.equal(kafka.node.kind, 'ContractEndpoint');
  assert.equal(resourceHealth(kafka.node), '계약 경로 미배포');
  assert.equal(kafka.workload, undefined);
  assert.equal(m.edges.filter(e => e.relation === 'declared_event').length, 3);
  assert.equal(m.edges.filter(e => e.relation === 'declared_snapshot_api').length, 2);
  assert.ok(m.edges.filter(e => e.relation === 'declared_event').every(e => !e.observed && !e.raw && !e.flows));
  assert.match(connectionsOf(m, kafka.id)[0].text, /계약된 이벤트.*연결 미관측.*미배포/);
});

test('실측 소비자→브로커와 논리 브로커→소비자 방향·같은 쌍의 발행을 합치지 않는다', () => {
  const o = fixture();
  o.topology!.nodes.push({ id: 'liveprobe-messaging/Service/kafka', namespace: 'liveprobe-messaging', name: 'kafka', kind: 'Service', system: null, status: 'ready', role: 'system' });
  for (const e of o.topology!.contract_edges!) for (const r of [e.from_ref, e.to_ref]) if (r.name === 'kafka') r.installed = true;
  const broker = 'liveprobe-messaging/Service/kafka', producer = 'prd/Deployment/business-api', consumer = 'prd/Deployment/trade-member';
  for (const source of [producer, consumer]) o.topology!.observed_edges.push({ from: source, to: broker, flows: 17, ok: 17, dropped: 0, errors: 0, state: 'ok', last_seen: 100, ports: {}, via_service: {}, drop_reasons: {}, req_rate: 8, err_rate: 0 });
  const m = overviewMap(o, 'prd');
  assert.ok(m.nodes.some(n => n.id === broker), '계약 끝점인 실브로커는 플랫폼 숨김 예외다');
  assert.ok(m.edges.some(e => e.source === consumer && e.target === broker && e.relation === 'observed_network' && e.observed));
  assert.ok(m.edges.some(e => e.source === broker && e.target === consumer && e.relation === 'declared_event' && !e.observed));
  const samePair = m.edges.filter(e => e.source === producer && e.target === broker);
  assert.equal(samePair.length, 2);
  assert.equal(samePair.find(e => e.relation === 'declared_event')!.raw, undefined);
  assert.match(edgeText(samePair.find(e => e.relation === 'declared_event')!), /계약된 이벤트.*연결 미관측/);
  assert.doesNotMatch(edgeText(samePair.find(e => e.relation === 'declared_event')!), /8\/초|흐름 17|오류 0/);
});

test('은퇴 표시는 숨기되 이름이 같아도 실소속을 바꾸거나 과거 사건 소속을 덮지 않는다', () => {
  const o = fixture();
  o.topology!.nodes.push({ id: 'prd/Deployment/old-member', namespace: 'prd', system: 'member', name: 'old-member', lifecycle: 'retired', kind: 'Deployment', status: 'ready' }, { id: 'prd/Deployment/order-copy', namespace: 'prd', environment: 'prd', system: 'samsung-delivery', name: 'order-copy', kind: 'Deployment', status: 'ready' });
  o.incidents.open.push({ id: 9, namespace: 'prd', workload: 'order', system: 'order', title: '과거 사건', severity_level: 'NORMAL', status: 'analyzing', stage: null, created_at: '' });
  const before = JSON.stringify(o);
  const m = overviewMap(o, 'prd');
  assert.ok(!m.nodes.some(n => n.node.name === 'old-member'));
  assert.equal(m.nodes.find(n => n.node.name === 'order-copy')!.node.system, 'samsung-delivery');
  assert.equal(o.incidents.open[0].system, 'order');
  assert.equal(incidentFor(o, o.topology!.nodes.find(n => n.name === 'order' && n.namespace === 'prd')!)!.system, 'order');
  assert.equal(JSON.stringify(o), before);
  assert.ok(!buildMapModel(o.topology!, { showSystem: true }).nodes.some(n => n.node.name === 'old-member'));
});

test('명시적 실자원 소속 null은 workload의 이전 소속으로 보충하지 않는다', () => {
  const o = fixture();
  const n = o.topology!.nodes.find(n => n.name === 'order' && n.namespace === 'prd')!;
  n.system = null;
  o.workloads.push({ id: n.id, name: n.name, namespace: 'prd', system: 'samsung-trade', kind: 'Deployment', ready: 1, desired: 1, cpu: null, mem: null, restarts_1h: null, req_rate: null, err_rate: null, p95_ms: null, incident: null });
  assert.equal(overviewMap(o, 'prd').nodes.find(x => x.id === n.id)!.node.system, null);
});

test('Kafka Service가 실제 StatefulSet에 접혀도 기본 플랫폼 숨김에서 계약 끝점은 남는다', () => {
  const o = fixture(), serviceId = 'liveprobe-messaging/Service/kafka', workloadId = 'liveprobe-messaging/StatefulSet/kafka';
  o.topology!.nodes.push({ id: serviceId, name: 'kafka', kind: 'Service', namespace: 'liveprobe-messaging', system: null, folded_into: workloadId, status: 'ready' }, { id: workloadId, name: 'kafka', kind: 'StatefulSet', namespace: 'liveprobe-messaging', system: null, role: 'data', status: 'ready' });
  for (const e of o.topology!.contract_edges!) for (const ref of [e.from_ref, e.to_ref]) if (ref.name === 'kafka') ref.installed = true;
  const m = overviewMap(o, 'prd');
  assert.ok(m.nodes.some(n => n.id === workloadId));
  assert.equal(m.edges.filter(e => e.relation === 'declared_event').length, 3);
  assert.ok(m.edges.filter(e => e.relation === 'declared_event').every(e => e.source === workloadId || e.target === workloadId));
});

test('서로 다른 실소속의 folded_into 힌트는 서비스를 다른 시스템 상자 안으로 합치지 않는다', () => {
  const o = fixture(), target = 'prd/Deployment/order';
  o.topology!.nodes.push({ id: 'prd/Service/order-entry', name: 'order-entry', namespace: 'prd', system: 'samsung-delivery', folded_into: target, kind: 'Service', status: 'ready' });
  assert.ok(buildMapModel(o.topology!, { showSystem: true }).nodes.some(n => n.id === 'prd/Service/order-entry' && n.node.system === 'samsung-delivery'));
});


test('서비스 보기에서 같은 소속의 단일 경로만 접고 분기·경고·원본을 보존한다', () => {
  const o = fixture();
  const topo = o.topology!;
  const target = topo.nodes.find(n => n.kind === 'Deployment' && n.namespace === 'prd' && n.system === 'samsung-trade')!;
  const ingress = { ...target, id: 'prd/Ingress/example', kind: 'Ingress', role: 'entry', status: 'ready' };
  topo.nodes.push(ingress);
  topo.structure_edges.push({ from: ingress.id, to: target.id, type: 'routes_to' });
  const compact = () => overviewMap(o, 'prd', { compactServices: true });
  assert.deepEqual(compact().foldedIngresses.get(target.id), [ingress.id]);
  assert.ok(!compact().nodes.some(n => n.id === ingress.id));
  assert.ok(overviewMap(o, 'prd').nodes.some(n => n.id === ingress.id));
  ingress.status = 'down';
  assert.ok(compact().nodes.some(n => n.id === ingress.id));
  ingress.status = 'ready';
  const other = topo.nodes.find(n => n.namespace === target.namespace && n.kind === 'Deployment' && n.id !== target.id)!;
  topo.structure_edges.push({ from: ingress.id, to: other.id, type: 'routes_to' });
  assert.ok(compact().nodes.some(n => n.id === ingress.id));
});
