import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadOpenIncidents, loadRootSnapshot, OPEN_STATES } from '../src/liveSnapshot.ts';
import type { Incident } from '../src/api.ts';
const row = (id: number, status: Incident['status'] = 'action_pending') => ({ id, status }) as Incident;
const unsupportedView = () => Object.assign(new Error('unsupported view'), {status: 422});

test('통합 진행 목록에서 500건 경계를 넘겨도 같은 조회 기준을 사용한다', async () => {
  const rows = Array.from({ length: 503 }, (_, i) => row(i + 1));
  const queried: Array<{view: string | null; compact: string | null; offset: number; anchor: string | null}> = [];
  const out = await loadOpenIncidents(async path => {
    const q = new URL(path, 'http://local').searchParams;
    const offset = Number(q.get('offset'));
    queried.push({view: q.get('view'), compact: q.get('compact'), offset, anchor: q.get('as_of')});
    return { items: rows.slice(offset, offset + 500), total: rows.length, as_of: 503 };
  });
  assert.deepEqual(queried, [
    {view: 'active', compact: 'true', offset: 0, anchor: null},
    {view: 'active', compact: 'true', offset: 500, anchor: '503'},
  ]);
  assert.equal(out.length, 503);assert.equal(out[0].id, 503);assert.equal(out.at(-1)?.id, 1);
});
test('이전 API는 같은 진행 상태를 개별 조회한다', async () => {
  const queried = new Set<string>();
  const out = await loadOpenIncidents(async path => {
    const q = new URL(path, 'http://local').searchParams;
    if (q.get('view') === 'active') throw unsupportedView();
    const status = q.get('status')!;
    queried.add(status);
    return { items: status === 'recovery_review' ? [row(7, status)] : [], total: status === 'recovery_review' ? 1 : 0 };
  });
  assert.deepEqual([...queried].sort(), [...OPEN_STATES].sort());
  assert.deepEqual(out.map(x => x.id), [7]);
});
test('조회 중 상태가 바뀌어 중복된 사건은 한 번만 표시하며 종료 상태는 제외한다', async () => {
  const out = await loadOpenIncidents(async () => ({ items: [row(5),row(5),row(2,'resolved')], total: 3 }));
  assert.deepEqual(out.map(x => x.id), [5]);
});
test('이전 API의 상태 한 개라도 조회 실패하면 부분 목록으로 성공하지 않는다', async () => {
  await assert.rejects(loadOpenIncidents(async path => {
    if (path.includes('view=active')) throw unsupportedView();
    if (path.includes('status=acting')) throw new Error('조회 실패');
    return { items: [row(5)], total: 1 };
  }), /조회 실패/);
});
test('총건수보다 적은 빈 페이지는 장애 없음이 아니라 불완전한 조회 오류다', async () => {
  await assert.rejects(loadOpenIncidents(async () => ({ items: [], total: 1 })), /일부만 조회/);
});

test('첫 진행 페이지에 알림을 받고 다음 페이지는 같은 시점으로 이어 읽는다', async () => {
  const requests: string[] = [];
  const notices: number[] = [];
  const rows = await loadRootSnapshot(async path => {
    requests.push(path);
    return path.includes('view=live') ? { items: [row(12)], total: 2, as_of: 12, alerts: [row(11)] }
      : { items: [row(10)], total: 2, as_of: 12 };
  }, alerts => notices.push(...alerts.map(item => item.id)), () => assert.fail('bundled alerts already received'));
  assert.deepEqual(requests, ['/incidents?view=live&limit=500&offset=0', '/incidents?view=active&compact=true&limit=500&offset=1&as_of=12']);
  assert.deepEqual(notices, [11]);
  assert.deepEqual(rows.map(item => item.id), [12, 10]);
});

test('지원하지 않는 이전 API에서만 기존 알림과 진행 조회를 사용한다', async () => {
  let legacyAlerts = 0;
  const snapshot = await loadRootSnapshot(async path => {
    if (path.includes('view=live')) throw unsupportedView();
    return { items: [row(3)], total: 1 };
  }, () => assert.fail('legacy alerts are separate'), () => { legacyAlerts++; });
  assert.equal(legacyAlerts, 1);
  assert.equal(snapshot[0].id, 3);
  await assert.rejects(loadRootSnapshot(async () => { throw new Error('read unavailable'); }, () => {},
    () => assert.fail('a read failure is not an unsupported view')), /read unavailable/);
});
