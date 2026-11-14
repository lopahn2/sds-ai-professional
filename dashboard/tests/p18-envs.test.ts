import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as model from '../src/overviewModel.ts';
import * as labels from '../src/labels.ts';
import type { Overview } from '../src/api.ts';

export const overview = (): Overview => ({ generated_at: null, cluster: { nodes: [], namespaces: [], environments: [{ name: 'prd', order: 3, workloads: 7, pods_ready: 7, pods_total: 7, open_incidents: 1 }, { name: 'dev', order: 1, workloads: 7, pods_ready: 7, pods_total: 7, open_incidents: 0 }, { name: 'stg', order: 2, workloads: 0, pods_ready: 0, pods_total: 0, open_incidents: 0, missing: true }] }, workloads: [], events: [], incidents: { open: [], today: { total: null, resolved: null, handed_off: null, failed: null } }, topology: { generated_at: 100, freshness: { state: 'connected', last_flow_age_s: 1, stale: false, stale_after_s: 60, window_s: 300, received_total: 1, error: null }, nodes: [], structure_edges: [], observed_edges: [], network_alerts: [] }, sources: {} });

// 이 표본은 전환 전 서버처럼 퇴역 stg를 아직 environments에 담아 보낸다(API·대시보드 배포 순서가 어긋난 경우).
// 새 기대: 지도 환경 선택지는 활성 환경(dev, prd)만이다. 예전 기대 ['dev','stg','prd']는 stg가 활성이던 시절의 것이다.
test('환경은 순서대로 정렬하고 기본 prd를 고르며 구 응답은 네임스페이스 선택으로 후퇴한다', () => {
  const o = overview();
  assert.equal(typeof model.environmentOptions, 'function');
  assert.deepEqual(model.environmentOptions(o).map(e => e.name), ['dev', 'prd']);
  assert.ok(!model.environmentOptions(o).some(e => e.name === 'stg'), '퇴역 stg는 지도 환경 선택지에 없다');
  // 새 서버 응답(활성 환경만)도 같은 결과다.
  const current = overview(); current.cluster.environments = current.cluster.environments!.filter(e => e.name !== 'stg');
  assert.deepEqual(model.environmentOptions(current).map(e => e.name), ['dev', 'prd']);
  assert.equal(model.defaultMapScope(current), 'prd');
  assert.equal(model.defaultMapScope(o), 'prd');
  delete o.cluster.environments;
  assert.deepEqual(model.environmentOptions(o), []);
  assert.equal(model.defaultMapScope(o), '');
});

test('장애 위치는 환경·시스템·서비스를 표시하고 누락 필드는 생략하며 구 응답 위치를 보존한다', () => {
  assert.equal(typeof labels.placeKo, 'function');
  assert.equal(labels.placeKo('prd', 'order', 'inventory'), 'prd · order · inventory');
  assert.equal(labels.placeKo('dev', null, 'payment'), 'dev · payment');
  assert.equal(labels.placeKo(null, 'member', null), 'member');
  assert.equal(labels.placeKo(null, null, 'order', 'liveprobe-target'), 'liveprobe-target/order');
  assert.equal(labels.placeKo(null, null, null), '위치 미관측');
});

test('미관측 환경은 0으로 꾸미지 않고 준비 파드와 진행 중 장애를 구분한다', () => {
  assert.equal(typeof model.environmentSummary, 'function');
  const envs = model.environmentOptions(overview());
  assert.equal(model.environmentSummary(envs[0]), '준비 파드 7/7 · 진행 중 장애 0건');
  // 예전에는 미관측 표본이 stg였다. stg가 선택지에서 빠졌으므로 활성 환경이 미관측일 때 같은 단언을 한다.
  assert.equal(model.environmentSummary({ ...envs[1], missing: true, workloads: null, pods_ready: null, pods_total: null }), '미관측');
  assert.equal(model.environmentSummary(envs[1]), '준비 파드 7/7 · 진행 중 장애 1건');
  assert.equal(model.environmentSummary({ ...envs[0], pods_ready: null, open_incidents: null }), '준비 파드 미관측/7 · 진행 중 장애 미관측');
});

test('선택한 환경만 시스템별로 묶고 장애 시스템을 먼저 배치하며 미지정 시스템을 보존한다', () => {
  const o = overview();
  for (const env of ['dev', 'prd']) for (const [system, name] of [['catalog', 'catalog'], ['order', 'order'], ['order', 'inventory'], [null, 'unassigned']] as const) {
    o.topology!.nodes.push({ id: `${env}/${name}`, name, namespace: env, environment: env, system, kind: 'Deployment', role: 'service', status: 'ready' });
  }
  o.incidents.open.push({ id: 42, namespace: 'prd', workload: 'inventory', system: 'order', environment: 'prd', title: '장애', severity_level: 'NORMAL', status: 'analyzing', stage: null, created_at: '' });
  const m = model.overviewMap(o, 'prd');
  assert.equal(m.groups.length, 3);
  assert.deepEqual(m.groups.map(g => g.name), ['order', 'catalog', '시스템 미지정']);
  assert.equal(m.nodes.length, 4); assert.ok(m.nodes.every(n => n.node.namespace === 'prd'));
  assert.equal(m.nodes.find(n => n.node.name === 'order')!.groupId, m.nodes.find(n => n.node.name === 'inventory')!.groupId);
  assert.equal(m.nodes.find(n => n.node.name === 'inventory')!.incident?.id, 42);
  assert.deepEqual([m.groups[0].x, m.groups[0].y], [0, 0]);
  delete o.cluster.environments;
  assert.equal(model.overviewMap(o, 'prd').groups[0].name, 'prd');
});

test('전체 환경은 환경 순서의 단순 묶음이며 플랫폼 토글과 경고 예외를 보존한다', () => {
  const o = overview();
  for (const env of ['prd', 'dev', 'stg']) o.topology!.nodes.push({ id: env, name: 'payment', namespace: env, environment: env, system: 'payment', kind: 'Deployment', status: 'ready' });
  o.topology!.nodes.push({ id: 'platform', name: 'api', namespace: 'liveprobe-system', kind: 'Deployment', status: 'ready' }, { id: 'legacy', name: 'loadgen', namespace: 'liveprobe-target', kind: 'Deployment', status: 'ready' });
  const m = model.overviewMap(o);
  // 새 기대: 전체 보기 묶음은 활성 환경(dev, prd)만이다. 퇴역 stg 자원이 응답에 남아 있어도 지도에 그리지 않는다.
  assert.deepEqual(m.groups.map(g => g.name), ['dev', 'prd']);
  assert.ok(!m.nodes.some(n => n.node.namespace === 'stg'), '퇴역 stg 자원은 지도에 없다');
  assert.equal(m.hiddenPlatform, 1);
  assert.equal(model.overviewMap(o, 'prd', { showPlatform: true }).nodes.length, 2);
  assert.ok(!m.nodes.some(n => n.node.name === 'loadgen'));
  o.topology!.nodes.find(n => n.id === 'platform')!.status = 'down';
  assert.ok(model.overviewMap(o, 'prd').nodes.some(n => n.id === 'platform'));
});

test('장애 없는 환경에서도 시스템 이름순 읽기 순서를 배치에서 유지한다', () => {
  const o = overview();
  for (const system of ['catalog', 'delivery', 'member', 'notification', 'order', 'payment']) {
    for (const name of system === 'order' ? ['order', 'inventory'] : [system]) o.topology!.nodes.push({ id: `dev/${name}`, name, namespace: 'dev', environment: 'dev', system, kind: 'Deployment', status: 'ready' });
  }
  const m = model.overviewMap(o, 'dev');
  assert.deepEqual([...m.groups].sort((a, b) => a.y - b.y || a.x - b.x).map(g => g.name), m.groups.map(g => g.name));
});
