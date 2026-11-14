// P18 W6 2영역 관제실 모의 브라우저 시험: 계약 2장(U1·U2·판단 과정·Group·근거 N행·단계 채팅·인계·상세 패널·초안 보존).
import { test, expect } from '@playwright/test';
import { capture as baseCapture, errors, layout, mock, resetMock, tab } from './p15-helpers.mjs';
const W6 = process.env.LIVEPROBE_W6_EVIDENCE_ROOT ?? 'e2e/artifacts/mock/w6';
const capture = (page, info, name) => baseCapture(page, info, name.replace(/^w6\//, ''), W6);
test.skip(!mock, '모의 API에서만 실행한다(실배포 장애 기록 보호).');
test.beforeEach(async ({ request }) => resetMock(request));
const H = { headers: { 'X-LiveProbe-Client': 'dashboard' } };
const narrative = async (request, id) => (await request.get(`/api/incidents/${id}/narrative`)).json();
const ready = async page => { await expect(page.locator('.incident-room')).toBeVisible({ timeout: 10000 }); await expect(page.locator('.step-card').first()).toBeAttached({ timeout: 10000 }); };
const wide = info => info.project.name === 'width-1440';
/** 카드가 02 대화 스크롤 영역 안에 실제로 보인다. */
const inFeed = card => card.evaluate(e => { const f = e.closest('.chat-feed').getBoundingClientRect(), a = e.getBoundingClientRect(); return a.top >= f.top - 2 && a.top < f.bottom - 20; });
/** 흐름 노드를 키보드로 선택한다(좁은 화면은 흐름 탭으로 전환, 포커스가 캔버스를 노드로 옮긴다). */
async function pick(page, nodeId) { await tab(page, '흐름'); const b = page.locator(`.react-flow__node[data-id="${nodeId}"] button`); await b.focus(); await page.keyboard.press('Enter'); const card = page.locator(`[id="step-${nodeId}"]`); await expect.poll(() => inFeed(card), { timeout: 4000 }).toBe(true); return card; }

test('U1: 기본 화면은 01 흐름과 02 분석·운영자 대화 두 영역이며 상시 근거 열·03 열·상세 패널이 없다', async ({ page }, info) => {
  const es = errors(page); await page.goto('/incidents/971'); await ready(page);
  await expect(page.locator('.evidence-pane')).toHaveCount(0);
  await expect(page.locator('.detail-panel')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '근거 · 한글 해석', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '운영자 대화', exact: true })).toHaveCount(0);
  await expect(page.locator('.pane-index')).toHaveText(['01', '02']);
  const nav = page.getByRole('navigation', { name: '관제실 화면 전환' });
  if (await nav.isVisible()) await expect(nav.getByRole('button')).toHaveText(['흐름', '대화']);
  else { for (const name of ['AI 분석 흐름', '분석 · 운영자 대화']) await expect(page.getByRole('region', { name, exact: true })).toBeVisible();
    const w = await page.locator('.room-pane').evaluateAll(es => es.map(e => e.getBoundingClientRect().width)); expect(w.length).toBe(2); expect(w[1]).toBeGreaterThan(500); }
  await tab(page, '대화'); await expect(page.getByRole('textbox', { name: '운영자 메시지' })).toBeVisible();
  // 실행 기록 이벤트(agent_started 등)는 카드에 흡수되어 대화에 따로 늘어놓지 않는다.
  await expect(page.locator('.chat-event')).toHaveCount(0);
  await layout(page); await capture(page, info, 'w6/default'); expect(es).toEqual([]);
});

test('U2: 모든 노드 선택은 같은 실행의 카드로 이동·펼침·포커스하고 같은 역할의 두 실행도 각자 카드로 간다', async ({ page, request }, info) => {
  test.setTimeout(90000);
  const n = await narrative(request, 971); await page.goto('/incidents/971'); await ready(page);
  expect(n.flow.nodes.filter(x => x.role === 'k8s_log_analyst').length).toBe(2);
  for (const node of n.flow.nodes) {
    const card = await pick(page, node.id);
    await expect(card).toHaveClass(/open/); await expect(card).toBeFocused(); await expect(card).toHaveAttribute('data-node-id', node.id);
    await expect.poll(() => inFeed(card), { timeout: 4000 }).toBe(true);
  }
  await expect(page.locator('.detail-panel')).toHaveCount(0); // 노드 선택만으로 상세 패널을 열지 않는다
  await tab(page, '흐름'); await expect(page.locator(`.react-flow__node[data-id="${n.flow.nodes.at(-1).id}"] button`)).toHaveAttribute('aria-pressed', 'true');
  await tab(page, '대화'); await layout(page); await capture(page, info, 'w6/node-jump');
});

test('사건 수준 판단은 판단 과정 5단계를, Group 선택은 선택 이유·역할 구성·진행 기록을 보여 준다', async ({ page, request }, info) => {
  const n = await narrative(request, 971); await page.goto('/incidents/971'); await ready(page);
  const router = await pick(page, 'router');
  await expect(router.locator('.decision-steps > li')).toHaveCount(5);
  await expect(router.locator('.decision-steps > li .decision-step-head b')).toHaveText(['관측 특징 확인', '규칙 판정', 'AI 분류 의견', '최종 결정', '분석 모델 선택']);
  await expect(router).toContainText('AI가 보통→복잡으로 올렸고');
  await expect(router).not.toContainText('LLM raised level'); // 영어 원문은 상세보기에서만
  await layout(page); await capture(page, info, 'w6/router-steps');
  const group = await pick(page, 'group'); const g = n.flow.nodes.find(x => x.id === 'group').card;
  await expect(group.locator('.role-list > li')).toHaveCount(g.roles.length);
  for (const t of ['선택 이유', '역할 구성', '진행 기록', 'Group 종료: 검증 통과']) await expect(group).toContainText(t);
  await expect(group.getByRole('textbox', { name: /단계에 보낼 지시/ })).toHaveCount(1); // 분석이 끝난 사건도 카드 채팅은 있다(명령 경로)
  await expect(group).toContainText('분석이 끝나 실행 계획 확인 절차로 접수됩니다.');
  await layout(page); await capture(page, info, 'w6/group');
});

test('근거 수집 카드는 N건 전체 행과 실패 사유를 보이고 행의 원문 보기는 서버 원문을 연다', async ({ page, request }, info) => {
  const n = await narrative(request, 990); const ev = n.flow.nodes.find(x => x.type === 'evidence');
  await page.goto('/incidents/990'); await ready(page);
  await tab(page, '흐름'); await expect(page.locator(`.react-flow__node[data-id="evidence"]`)).toContainText('근거 수집');
  const card = await pick(page, 'evidence');
  await expect(card.locator('[data-evidence-id]')).toHaveCount(ev.card.total);
  await expect(card).toContainText(`수집 ${ev.card.total}건 · 성공 ${ev.card.ok} · 실패 ${ev.card.failed}`);
  await expect(card.locator('.evidence-rows li.failed')).toHaveCount(ev.card.failed); await expect(card.locator('.evidence-rows li.failed')).toContainText('실패 사유');
  await layout(page); await capture(page, info, 'w6/evidence');
  const k = ev.card.items.findIndex(it => it.ok && it.ref === 'E5_recent_events');
  await card.locator('[data-evidence-id]').nth(k).getByRole('button', { name: /원문 보기/ }).click();
  const panel = page.getByRole('complementary', { name: '단계 상세' }); const rows = n.raw.filter(r => r.ref === 'E5_recent_events');
  await expect(panel.locator('.raw-item')).toHaveCount(rows.length); await expect(panel.locator('.raw-item .raw-text').first()).toHaveText(rows[0].raw);
  await page.keyboard.press('Escape'); await expect(panel).toHaveCount(0);
});

test('원문 보기 상세는 대화 카드를 반쯤 가리지 않는다(좁은 화면은 가림막이 있는 시트, 넓은 화면은 옆 열)', async ({ page }, info) => {
  await page.goto('/incidents/990'); await ready(page);
  const card = await pick(page, 'evidence'); await tab(page, '대화');
  await card.locator('[data-evidence-id]').first().getByRole('button', { name: /원문 보기/ }).click();
  const panel = page.getByRole('complementary', { name: '단계 상세' }); await expect(panel).toBeVisible();
  const box = await panel.boundingBox(); const feed = await page.locator('.chat-feed').boundingBox();
  const backdrop = page.locator('.detail-backdrop');
  if (box.x < feed.x + feed.width - 1) {
    // 패널이 대화 영역 위에 떠 있으면 뒤 화면 전체를 덮는 가림막이 있어야 잘린 카드가 그대로 노출되지 않는다.
    await expect(backdrop).toBeVisible();
    const b = await backdrop.boundingBox(); const vp = page.viewportSize();
    expect(b.x <= 0 && b.y <= 0 && b.width >= vp.width - 1 && b.height >= vp.height - 1, JSON.stringify(b)).toBe(true);
    // 패널이 화면 전체 폭이면 가림막을 누를 자리가 없으므로 Esc로 닫는다.
    if (box.x > 1) { await backdrop.click({ position: { x: 5, y: vp.height - 5 } }); } else { await page.keyboard.press('Escape'); }
    await expect(panel).toHaveCount(0);
  } else {
    await expect(backdrop).toBeHidden();
    await page.keyboard.press('Escape'); await expect(panel).toHaveCount(0);
  }
  await capture(page, info, 'w6/evidence-raw-overlay');
});

test('단계 채팅: 대상 역할·대상 노드를 담아 보내고 ack를 즉시, 반영 기록 뒤 반영 상태를 갱신한다', async ({ page, request }, info) => {
  await page.goto('/incidents/1976'); await ready(page);
  const card = await pick(page, 'planned:coordinator');
  await expect(card.locator('.step-chat-target')).toHaveText('대상: 진행 조정 · 이 단계에 지시합니다.');
  const box = card.getByRole('textbox', { name: '진행 조정 단계에 보낼 지시' });
  await box.fill('연결 풀 대기 스레드 수를 먼저 비교하세요.');
  const sent = page.waitForRequest(r => r.method() === 'POST' && /\/incidents\/1976\/messages$/.test(r.url()));
  await card.getByRole('button', { name: '지시 보내기', exact: true }).click();
  const body = (await sent).postDataJSON(); expect(body).toEqual({ body: '연결 풀 대기 스레드 수를 먼저 비교하세요.', target_node: 'planned:coordinator', target_role: 'coordinator' });
  const thread = card.getByLabel('이 단계 대화');
  await expect(thread.locator('.ack-bubble').first()).toContainText('진행 조정 시작 전에 이 지시를 전달합니다.', { timeout: 1500 });
  await expect(thread).toContainText('진행 조정');
  await expect(thread.locator('.operator-bubble')).toHaveCount(1); await expect(box).toHaveValue('');
  await expect(thread.locator('.steer-state')).toContainText(/반영 예정/);
  // DOM에 존재하는 것뿐 아니라 새 steering 카드가 들어온 뒤에도 응답 전체가 실제 대화 영역 안에 보여야 한다.
  await expect(page.locator('.step-card.type-steer')).toHaveCount(1, { timeout: 6000 });
  await expect.poll(() => thread.locator('.ack-bubble').first().evaluate(e => {
    const f = e.closest('.chat-feed').getBoundingClientRect(), a = e.getBoundingClientRect();
    return a.top >= f.top + 2 && a.bottom <= f.bottom - 2;
  }), { timeout: 4000 }).toBe(true);
  // 일반 대화 목록에는 같은 메시지를 중복 표시하지 않는다.
  await expect(page.locator('.chat-feed > .operator-bubble')).toHaveCount(0);
  await layout(page); await capture(page, info, 'w6/step-chat-ack');
  await request.post('/api/mock/apply', { ...H, data: { incident_id: 1976 } });
  await expect(thread.locator('.steer-state')).toHaveText('진행 조정 시작 전에 반영됨', { timeout: 8000 });
  // 그룹 카드는 다음 미실행 역할로, 사건 수준 판단 카드는 대상 없이 보낸다.
  const group = await pick(page, 'group'); await expect(group.locator('.step-chat-target')).toContainText('대상: 진행 조정');
  const router = await pick(page, 'router'); await expect(router.locator('.step-chat-target')).toHaveText('대상 역할 없이 다음 Agent 경계에서 반영합니다.');
  const r2 = page.waitForRequest(r => r.method() === 'POST' && /\/messages$/.test(r.url()));
  await router.getByRole('textbox', { name: /단계에 보낼 지시/ }).fill('판단 근거를 다시 확인하세요.'); await router.getByRole('button', { name: '지시 보내기' }).click();
  expect((await r2).postDataJSON().target_role).toBeUndefined();
});

test('예정 단계가 실제 실행으로 바뀌어도 대화·실제 반영·초안·상세와 새로고침 후 기록을 보존한다', async ({ page, request }, info) => {
  let completed = false;
  await page.route('**/api/incidents/1976/narrative', async route => {
    const response = await route.fetch(); const data = await response.json();
    if (completed) {
      const old = 'planned:coordinator', actual = 'run:9001';
      data.flow.nodes = data.flow.nodes.map(n => n.id === old ? { ...n, id: actual, aliases: [old], status: 'done', conclusion_raw: '실제 실행 9001의 완료 기록' } : n);
      data.flow.edges = data.flow.edges.map(e => ({ ...e, source: e.source === old ? actual : e.source, target: e.target === old ? actual : e.target }));
    }
    await route.fulfill({ response, json: data });
  });
  const es = errors(page); await page.goto('/incidents/1976'); await ready(page);
  const planned = await pick(page, 'planned:coordinator');
  await planned.getByRole('textbox', { name: /단계에 보낼 지시/ }).fill('실행 전 연결 풀을 먼저 확인하세요.');
  await planned.getByRole('button', { name: '지시 보내기' }).click();
  await expect(planned.locator('.ack-bubble')).toHaveCount(1);
  await planned.getByRole('textbox', { name: /단계에 보낼 지시/ }).fill('아직 전송하지 않은 후속 초안');
  await planned.getByRole('button', { name: '상세보기' }).click();
  completed = true;
  const applied = await request.post('/api/mock/apply', { ...H, data: { incident_id: 1976 } }); expect(applied.status()).toBe(201);
  const actual = page.locator('[id="step-run:9001"]');
  await expect(actual).toHaveClass(/open/, { timeout: 8000 });
  await expect(page.locator('[id="step-planned:coordinator"]')).toHaveCount(0);
  await expect(actual.locator('.operator-bubble')).toHaveCount(1);
  await expect(actual.locator('.operator-bubble')).toContainText('실행 전 연결 풀을 먼저 확인하세요.');
  await expect(actual.locator('.steer-state')).toHaveText('진행 조정 시작 전에 반영됨');
  await expect(actual.getByRole('textbox', { name: /단계에 보낼 지시/ })).toHaveValue('아직 전송하지 않은 후속 초안');
  await expect(page.locator('.detail-panel')).toContainText('실제 실행 9001의 완료 기록');
  await page.keyboard.press('Escape'); await expect(page.locator('.detail-panel')).toHaveCount(0);
  await expect(actual.getByRole('button', { name: '상세보기' })).toBeFocused();
  await layout(page); await capture(page, info, 'w6/step-identity-completed');
  await page.reload(); await ready(page);
  const restored = await pick(page, 'run:9001');
  await expect(restored.locator('.operator-bubble')).toHaveCount(1);
  await expect(restored.locator('.steer-state')).toHaveText('진행 조정 시작 전에 반영됨');
  expect(es).toEqual([]);
});

test('재수집된 근거는 각 행의 당시 요약과 원문을 열고 최신 정상값으로 덮지 않는다', async ({ page }, info) => {
  await page.route('**/api/incidents/990/narrative', async route => {
    const response = await route.fetch(); const data = await response.json(); const c = data.flow.nodes.find(n => n.type === 'evidence').card;
    const origin = data.raw.find(r => r.ref === 'E1_deployment'); const base = c.items.find(it => it.ref === 'E1_deployment');
    c.items = [0, 1].map(ready => ({ ...base, id: 900 + ready, summary_ko: `당시 준비된 Pod ${ready}개`, raw_items: [{ ...origin, id: `evidence:${900 + ready}:E1_deployment#0`, raw: JSON.stringify({ ready_replicas: ready }), ko: `당시 준비된 Pod ${ready}개` }] }));
    c.total = 2; c.ok = 2; c.failed = 0;
    data.raw = data.raw.map(r => r.ref === 'E1_deployment' ? { ...r, raw: '{"ready_replicas":1}' } : r);
    await route.fulfill({ response, json: data });
  });
  const es = errors(page); await page.goto('/incidents/990'); await ready(page); const card = await pick(page, 'evidence');
  for (const index of [0, 1]) {
    const row = card.locator('[data-evidence-id]').nth(index);
    await expect(row).toContainText(`당시 준비된 Pod ${index}개`);
    await row.getByRole('button', { name: /원문 보기/ }).click();
    const panel = page.locator('.detail-panel');
    await expect(panel.locator('.raw-item .raw-text')).toHaveText(JSON.stringify({ ready_replicas: index }));
    await expect(panel).not.toContainText(JSON.stringify({ ready_replicas: 1 - index }));
    await capture(page, info, `w6/evidence-snapshot-${index}`);
    await page.keyboard.press('Escape');
  }
  await layout(page); expect(es).toEqual([]);
});

test('혼합 배치 반영은 메시지마다 자기 대상 반영과 대체 반영을 구분한다', async ({ page }, info) => {
  // 실제 steering.take 가짜 세션 재현에서 생성된 이벤트 계약을 그대로 사용한다.
  const at = '2026-10-25T03:00:03Z';
  const events = [
    { seq: 1, kind: 'operator_message', actor: 'local-operator', summary: '원인 추론은 DB 풀을 봐라', at, detail: { message_id: 7, target_node: 'plan:10:cause_reasoner', target_role: 'cause_reasoner' } },
    { seq: 2, kind: 'steering_interpreted', actor: 'liveprobe', summary: '', at, detail: { message_id: 7, target_node: 'plan:10:cause_reasoner', hold: true, ack_ko: '원인 추론 시작 전에 이 지시를 전달합니다.' } },
    { seq: 3, kind: 'operator_message', actor: 'local-operator', summary: '로그 분석에 이것도', at, detail: { message_id: 8, target_node: 'run:100', target_role: 'k8s_log_analyst' } },
    { seq: 4, kind: 'steering_interpreted', actor: 'liveprobe', summary: '', at, detail: { message_id: 8, target_node: 'run:100', hold: false, ack_ko: '이미 실행되어 다음 단계 Agent에게 전달합니다.' } },
    { seq: 5, kind: 'operator_applied', actor: 'liveprobe', summary: '지시 2건 반영', at, detail: { message_ids: [7, 8], applied_to_role: 'cause_reasoner', targeted_role: ['cause_reasoner', 'k8s_log_analyst'], fallback: true } },
  ];
  await page.route('**/api/incidents/1976/events*', route => route.fulfill({ json: { events, control: 'running' } }));
  await page.route('**/api/incidents/1976/narrative', async route => {
    const response = await route.fetch(); const data = await response.json();
    const base = data.flow.nodes.find(n => n.role === 'coordinator');
    const cause = { ...base, id: 'run:101', aliases: ['plan:10:cause_reasoner'], role: 'cause_reasoner', role_ko: '원인 추론', status: 'done', depth: 4, lane: 0,
      card: { ...base.card, role: 'cause_reasoner', role_ko: '원인 추론', steer_target_role: 'cause_reasoner' } };
    const log = { ...base, id: 'run:100', role: 'k8s_log_analyst', role_ko: '쿠버네티스·로그 분석', status: 'done', depth: 3, lane: 0,
      card: { ...base.card, role: 'k8s_log_analyst', role_ko: '쿠버네티스·로그 분석', steer_target_role: 'k8s_log_analyst' } };
    data.flow.nodes = data.flow.nodes.filter(n => !['agent', 'verify', 'steer'].includes(n.type)).concat([log, cause]);
    data.flow.edges = [];
    await route.fulfill({ response, json: data });
  });
  const es = errors(page); await page.goto('/incidents/1976'); await ready(page); await tab(page, '대화');
  const cause = page.locator('[id="step-run:101"]'), log = page.locator('[id="step-run:100"]');
  for (const card of [cause, log]) await card.getByRole('button', { name: '펼치기', exact: true }).click();
  await expect(cause.locator('.steer-state')).toHaveText('원인 추론 시작 전에 반영됨');
  await expect(log.locator('.steer-state')).toHaveText('쿠버네티스·로그 분석 대신 원인 추론에 반영됨');
  await expect(cause.locator('.operator-bubble')).toHaveCount(1);
  await expect(log.locator('.operator-bubble')).toHaveCount(1);
  await expect(page.locator('.chat-feed > .operator-bubble')).toHaveCount(0);
  for (const [card, key] of [[cause, 'normal'], [log, 'fallback']]) {
    const state = card.locator('.steer-state'); await state.scrollIntoViewIfNeeded();
    await expect.poll(() => state.evaluate(e => { const f = e.closest('.chat-feed').getBoundingClientRect(), r = e.getBoundingClientRect(); return r.top >= f.top && r.bottom <= f.bottom; })).toBe(true);
    await layout(page); await capture(page, info, `w6/mixed-batch-${key}`);
  }
  expect(es).toEqual([]);
});

test('전송 응답을 보인 뒤 운영자가 과거 기록으로 이동하면 늦은 갱신이 읽는 위치를 빼앗지 않는다', async ({ page }) => {
  let release; const resumed = new Promise(resolve => { release = resolve; }); let hold = false;
  await page.route('**/api/incidents/1976/narrative', async route => { if (hold) await resumed; await route.continue(); });
  try {
    await page.goto('/incidents/1976'); await ready(page);
    const card = await pick(page, 'planned:coordinator');
    await card.getByRole('textbox', { name: /단계에 보낼 지시/ }).fill('과거 기록을 먼저 검토하겠습니다.');
    hold = true;
    await card.getByRole('button', { name: '지시 보내기' }).click();
    const ack = card.locator('.ack-bubble').first();
    await expect(ack).toContainText('시작 전에 이 지시를 전달합니다.');
    await expect.poll(() => ack.evaluate(e => { const feed = e.closest('.chat-feed'); if (!feed || !e.isConnected) return false; const a = e.getBoundingClientRect(), f = feed.getBoundingClientRect(); return a.top >= f.top && a.bottom <= f.bottom; })).toBe(true);
    const feed = page.locator('.chat-feed');
    await feed.hover(); await page.mouse.wheel(0, -10000);
    await expect.poll(() => feed.evaluate(e => e.scrollTop)).toBeLessThanOrEqual(2);
    hold = false; release();
    await expect(page.locator('.step-card.type-steer')).toHaveCount(1, { timeout: 6000 });
    await expect.poll(() => feed.evaluate(e => e.scrollTop)).toBeLessThanOrEqual(2);
  } finally { release(); }
});

test('분석이 끝난 사건의 카드 채팅은 명령 경로로, 종료된 사건은 입력을 막는다', async ({ page, request }) => {
  await page.goto('/incidents/971'); await ready(page);
  const card = await pick(page, 'run:4101');
  const sent = page.waitForRequest(r => r.method() === 'POST' && /\/incidents\/971\/(messages|commands)$/.test(r.url()));
  await card.getByRole('textbox', { name: /단계에 보낼 지시/ }).fill('재고 서비스 파드를 2개로 늘려 주세요.'); await card.getByRole('button', { name: '지시 보내기' }).click();
  expect((await sent).url()).toMatch(/\/commands$/);
  await expect(card.getByLabel('이 단계 대화')).toContainText('실행 계획 확인 절차로 접수했습니다');
  const c = page.locator('.command-card').last(); await expect(c.getByText('정말 진행하시겠습니까?', { exact: true })).toBeVisible({ timeout: 10000 });
  await request.post('/api/incidents/990/close', { ...H, data: { reason: 'W6 시험 종료', confirm: true } });
  await page.goto('/incidents/990'); await ready(page);
  const r = await pick(page, 'router'); await expect(r).toContainText('종료된 장애라 이 단계에 지시를 보낼 수 없습니다.'); await expect(r.locator('textarea')).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '운영자 메시지' })).toHaveCount(0);
});

test('인계 카드는 요약·결정사항·다음 단계 중심이고 원문 권장 조치 목록은 기본 미표시다', async ({ page, request }, info) => {
  const n = await narrative(request, 990); await page.goto('/incidents/990'); await tab(page, '대화');
  const h = page.getByRole('article', { name: '운영자 인계' }); await expect(h).toBeVisible();
  await expect(h.locator('.handoff-lead')).toHaveText(n.handoff_view.summary_ko);
  await expect(h.locator('.decision-table dt')).toHaveText(['결과', '원인 판단', '자동 조치', '운영자 결정']);
  await expect(h.locator('.next-steps li')).toHaveCount(3);
  for (const s of n.handoff_view.next_steps_raw) await expect(h.getByText(s, { exact: true })).toBeHidden();
  await expect(h.getByRole('button', { name: '인계 인수', exact: true })).toBeVisible();
  await h.scrollIntoViewIfNeeded(); await layout(page); await capture(page, info, 'w6/handoff');
  await h.getByText('원문 보기', { exact: true }).click();
  for (const s of n.handoff_view.next_steps_raw) await expect(h.getByText(s, { exact: true })).toBeVisible();
  await h.getByRole('button', { name: '인계 인수', exact: true }).click(); await expect(h).toContainText('인수: operator');
  // 결과 카드도 같은 요약을 쓰고 원문 목록을 펼쳐 두지 않는다.
  const out = await pick(page, 'outcome'); await expect(out.locator('.decision-table')).toBeVisible(); await expect(out).not.toContainText('kubectl');
});

test('상세 패널: 상세보기로만 열리고 교체·닫기·Esc 후 포커스·초안·대화 위치를 보존한다', async ({ page }, info) => {
  await page.goto('/incidents/1976'); await ready(page); await tab(page, '대화');
  const composer = page.getByRole('textbox', { name: '운영자 메시지' }); await composer.fill('작성 중인 초안');
  const card = await pick(page, 'planned:coordinator');
  const stepBox = card.getByRole('textbox', { name: '진행 조정 단계에 보낼 지시' }); await stepBox.fill('단계 초안');
  const feed = page.locator('.chat-feed');
  const opener = card.getByRole('button', { name: '상세보기', exact: true }); await opener.scrollIntoViewIfNeeded(); await page.waitForTimeout(600); const before = await feed.evaluate(e => e.scrollTop); await opener.click();
  const panel = page.getByRole('complementary', { name: '단계 상세' }); await expect(panel).toBeVisible(); await expect(panel).toHaveAttribute('data-node-id', 'planned:coordinator');
  await expect(panel.getByRole('button', { name: '닫기', exact: true })).toBeFocused();
  await expect(page.locator('.detail-panel')).toHaveCount(1);
  if (wide(info)) {
    const w = await panel.evaluate(e => e.getBoundingClientRect().width); expect(w).toBeGreaterThan(320); expect(w).toBeLessThan(360);
    await expect(page.locator('.pane-index')).toHaveCount(2);
  }
  await layout(page); await capture(page, info, 'w6/detail-open');
  if (wide(info)) { // 넓은 화면에서는 대화가 보이므로 다른 카드의 상세보기로 내용을 교체한다(패널을 쌓지 않는다).
    const other = page.locator('[id="step-router"]'); await other.getByRole('button', { name: '펼치기' }).click(); await other.getByRole('button', { name: '상세보기', exact: true }).click();
    await expect(panel).toHaveAttribute('data-node-id', 'router'); await expect(page.locator('.detail-panel')).toHaveCount(1); await expect(panel).toContainText('판단 사유 원문');
    await panel.getByRole('button', { name: '닫기', exact: true }).click(); await expect(panel).toHaveCount(0);
    await expect(other.getByRole('button', { name: '상세보기', exact: true })).toBeFocused();
    await opener.click(); await expect(panel).toBeVisible();
  }
  await page.keyboard.press('Escape'); await expect(panel).toHaveCount(0);
  await expect(opener).toBeFocused();
  await expect(composer).toHaveValue('작성 중인 초안'); await expect(stepBox).toHaveValue('단계 초안');
  if (!wide(info)) expect(Math.abs(await feed.evaluate(e => e.scrollTop) - before)).toBeLessThanOrEqual(4);
  await layout(page);
});

test('움직임 감소 설정에서는 카드 이동이 즉시이고 강조 애니메이션이 없다', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' }); await page.goto('/incidents/971'); await ready(page);
  const card = await pick(page, 'outcome');
  await page.waitForTimeout(120); expect(await inFeed(card)).toBe(true);
  expect(await card.evaluate(e => getComputedStyle(e).animationName)).toBe('none');
});

test('한글 요약·판단 과정·결정사항·단계 입력은 실제 렌더링에서 14px 이상으로 읽힌다', async ({ page }) => {
  await page.goto('/incidents/990'); await ready(page); await tab(page, '대화');
  const handoff = page.getByRole('article', { name: '운영자 인계' });
  const check = async locator => {
    const sizes = await locator.evaluateAll(es => es.map(e => ({ text: e.textContent, size: parseFloat(getComputedStyle(e).fontSize) })));
    expect(sizes.length).toBeGreaterThan(0);
    for (const item of sizes) expect(item.size, item.text).toBeGreaterThanOrEqual(14);
  };
  await check(handoff.locator('.handoff-lead,.decision-table dd,.next-steps li'));
  const router = await pick(page, 'router'); await check(router.locator('.decision-steps p,.step-kv dd,.step-lead'));
  const group = await pick(page, 'group'); await check(group.locator('.reason-list li,.role-list p,.step-kv dd'));
  await page.goto('/incidents/1976'); await ready(page);
  const agent = await pick(page, 'planned:coordinator'); await check(agent.locator('.step-lead,textarea'));
});

test('백엔드 새 필드가 없는 응답에서도 깨지지 않고 미기록·해석 준비 중을 표시한다', async ({ page, request }, info) => {
  const n = await narrative(request, 990); delete n.handoff_view; for (const x of n.flow.nodes) { delete x.card; delete x.at; }
  await page.route('**/api/incidents/990/narrative', r => r.fulfill({ json: n }));
  const es = errors(page); await page.goto('/incidents/990'); await ready(page);
  const router = await pick(page, 'router'); await expect(router).toContainText('현재 미기록'); await expect(router).toContainText(n.flow.nodes.find(x => x.id === 'router').conclusion_ko);
  const ev = await pick(page, 'evidence'); await expect(ev).toContainText('수집 항목별 목록은 아직 받지 못했습니다');
  await tab(page, '대화'); const h = page.getByRole('article', { name: '운영자 인계' }); await expect(h).toContainText('한글 요약을 준비 중입니다.'); await expect(h.locator('.decision-table')).toContainText('운영자 인계 · 자동 복구 아님');
  await expect(page.locator('.step-time').first()).toHaveText('시각 미기록');
  await layout(page); await capture(page, info, 'w6/legacy-fields'); expect(es).toEqual([]);
});
