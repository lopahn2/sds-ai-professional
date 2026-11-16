import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
const template = JSON.parse(readFileSync(new URL('./fixtures/narrative-971.json', import.meta.url)));
const f = { release_id: 'release-a', artifact_sha256: 'a'.repeat(64), plan_sha256: 'b'.repeat(64), image: `registry/inventory@sha256:${'c'.repeat(64)}` };
async function mock(page, state = 'awaiting_user_test') {
  const summary = { enabled: true, linked: true, incident_id: 971, action_id: 'action-a', application_id: 'native-a', state, phase: 'code_action', state_ko: state === 'awaiting_user_test' ? 'dev 사용자 시험 대기' : 'prd 승인 대기', summary_ko: '수정한 서비스의 동작을 확인합니다.', problem_ko: '주문 처리에 실패합니다.', cause_ko: '연결 반환 처리가 빠졌습니다.', next_ko: 'dev 버전에서 주문 동작을 확인하세요.', runner_observed: null, observed_at: null, eligible: false, blocked_code: null, blocks_cluster: true };
  const detail = { summary, application: { id: 'native-a', state, environment: 'dev', service: 'inventory', system: 'order', reason: '주문 처리에 실패합니다.', created_at: 1000, stages: [], data: { analysis: { root_cause_ko: '연결 반환 처리가 빠졌습니다.', roles: [{ role: 'analyst', model: 'gpt-6.1-sol', effort: 'medium', usage: { input_tokens: 120, output_tokens: null }, occurred_at: 1000, result: { summary_ko: '연결 반환 누락을 확인했습니다.' } }] }, patch: { summary_ko: '연결 반환을 보완했습니다.', risk_ko: '주문 처리 경로에 적용됩니다.' }, raw: 'RAW_ONLY_SENTINEL Traceback hidden', merge_review: null } }, release: { incident_id: 'native-a', state, release: { ...f, artifact: { image: f.image }, valid: true, protocol: 'dev-prd/v2', stage_results: { dev: { verified: true } }, user_test: { ...f, id: 'test-a', kind: 'human', passed: true, invalidated: false, expires_at: Date.now() / 1000 + 600 } } }, events: [] };
  const writes = [], reads = []; let rejectDetail = false, rejectCreate = false, rejectHistory = false;
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    const send = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (request.method() !== 'GET') {
      const body = request.postDataJSON(); writes.push({ path, body, authorization: request.headers().authorization, client: request.headers()['x-liveprobe-client'] });
      if (path === '/api/unified/incidents') return send({ incident_id: 971, code_action: summary }, rejectCreate ? 503 : 201);
      if (path.endsWith('/user-test')) { detail.application.state = 'awaiting_prd_approval'; summary.state = 'awaiting_prd_approval'; summary.state_ko = 'prd 승인 대기'; }
      if (path.endsWith('/review-merge')) detail.application.data.merge_review = { ...f, human_merge_observed: true, expires_at: Date.now() / 1000 + 60, observed_at: Date.now() / 1000 };
      return send({ ok: true });
    }
    reads.push({ path, authorization: request.headers().authorization });
    if (path.endsWith('/code-action/detail')) return send(rejectDetail ? { detail: 'PRIVATE_ERROR_SENTINEL' } : detail, rejectDetail ? 403 : 200);
    if (path.endsWith('/code-action')) return send(summary);
    if (path === '/api/unified/catalog') return send({ enabled: true, profiles_configured: true, environments: ['dev', 'prd'], services: [{ service: 'inventory', system: 'order', profile_id: 'inventory' }] });
    if (path === '/api/unified/history' && rejectHistory) return send({ detail: 'PRIVATE_HISTORY_ERROR' }, 403);
    if (path === '/api/unified/history') return send({ incidents: [{ ...detail.application, id: 'legacy-native-17', root_incident_id: null, environment: 'stg' }], total: 1, as_of: 1000 });
    if (path.endsWith('/narrative')) { const n = structuredClone(template); n.view_mode = 'live'; n.incident.status = 'handed_off'; n.incident.closed_at = null; if (summary.linked) n.flow.nodes.push({ id: 'code-action-action-a', type: 'code_action', label: '코드 수정 조치', role: null, role_ko: '코드 수정 조치', job_ko: '같은 사건에서 코드를 수정하고 검증합니다.', status: 'running', model: null, effort: null, inputs: [], lane: 0, depth: Math.max(...n.flow.nodes.map(n => n.depth)) + 1, at: null, card: null }); return send(n); }
    if (/\/incidents\/\d+$/.test(path)) return send({ incident: { ...template.incident, status: 'handed_off', closed_at: null }, actions: [] });
    if (path.endsWith('/events')) return send({ events: [], control: 'running', streaming: false, execution_mode: 'live' });
    if (path === '/api/stream') return route.fulfill({ contentType: 'text/event-stream', body: ': mock\n\n' });
    if (path === '/api/llm/accounts') return send({ accounts: [], mode: 'mock' });
    return send({ items: [], total: 0 });
  });
  return { writes, reads, summary, detail, rejectDetail: () => { rejectDetail = true; }, rejectHistory: () => { rejectHistory = true; }, rejectCreate: value => { rejectCreate = value; } };
}
async function readDetail(page) {
  const card = page.getByRole('article', { name: '코드 조치', exact: true });
  await card.getByLabel('상세 조회 인증 (incident:read)').fill('READ_TOKEN_SENTINEL');
  await card.getByRole('button', { name: '인증 적용' }).click();
  await expect(card.getByRole('heading', { name: '수정·검증 요약' })).toBeVisible();
  return card;
}
test('public summary stays visible; raw logs and independent core controls stay closed', async ({ page }, info) => {
  const m = await mock(page); await page.goto('/incidents/971');
  const card = page.getByRole('article', { name: '코드 조치', exact: true });
  await expect(card.getByText('주문 처리에 실패합니다.', { exact: true })).toBeVisible();
  await expect(card.getByText('연결 반환 처리가 빠졌습니다.', { exact: true })).toBeVisible();
  await expect(page.getByText('Kubernetes 단계 · 이전 처리 기록')).toBeVisible();
  await expect(page.locator('.handoff-card,.composer,.close-card')).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('unified-room.png') });
  expect(m.writes).toHaveLength(0); expect(m.reads.filter(r => r.path.endsWith('/code-action')).every(r => !r.authorization)).toBeTruthy();
  await readDetail(page); await expect(card.getByRole('region', { name: '코드 조치 모델 실행 기록' })).toContainText('120 / 미관측'); await expect(card.getByRole('region', { name: '코드 조치 모델 실행 기록' }).getByText('연결 반환 누락을 확인했습니다.', { exact: true })).toBeVisible(); await expect(page.getByText(/RAW_ONLY_SENTINEL/)).toHaveCount(0);
  await card.getByText('코드 조치 원문·로그 보기', { exact: true }).click(); await expect(card.locator('pre')).toContainText('RAW_ONLY_SENTINEL');
  await expect(page.getByText('READ_TOKEN_SENTINEL')).toHaveCount(0);
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  await card.getByRole('button', { name: '인증 해제' }).click(); await expect(card.getByRole('heading', { name: '수정·검증 요약' })).toHaveCount(0);
  await expect(card.getByLabel('상세 조회 인증 (incident:read)')).toHaveValue('');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  const flowTab = page.getByRole('button', { name: '흐름', exact: true }); if (await flowTab.isVisible()) await flowTab.click();
  const flowNode = page.locator('[data-id="code-action-action-a"] button'); await flowNode.focus(); await flowNode.click(); await expect(card).toBeFocused();
  await expect(page.locator('#step-code-action-action-a')).toHaveCount(1);
});
test('tester and approver explicitly confirm exact fingerprints; merge gate is separate', async ({ page }) => {
  const m = await mock(page); await page.goto('/incidents/971'); await readDetail(page);
  const testForm = page.getByRole('form', { name: 'dev 사용자 시험 기록', exact: true });
  await testForm.getByLabel('시험 결과').selectOption('true'); await testForm.getByLabel('확인한 동작과 결과').fill('주문 생성과 연결 반환을 확인했습니다.');
  await testForm.getByLabel('시험자 인증 (tester)').fill('TEST_TOKEN_SENTINEL');
  await expect(testForm.getByRole('button', { name: 'dev 사용자 시험 기록' })).toBeDisabled();
  await testForm.getByRole('checkbox').check(); await testForm.getByRole('button', { name: 'dev 사용자 시험 기록' }).click();
  const merge = page.getByRole('form', { name: '사람 병합 확인', exact: true }); await expect(merge).toBeVisible();
  const approval = page.getByRole('form', { name: 'prd 배포 승인', exact: true }); await expect(approval.getByRole('checkbox')).toBeDisabled();
  expect(m.writes[0].body).toEqual({ ...f, passed: true, notes_ko: '주문 생성과 연결 반환을 확인했습니다.' }); expect(m.writes[0].authorization).toBe('Bearer TEST_TOKEN_SENTINEL'); expect(m.writes[0].client).toBe('dashboard');
  await merge.getByLabel('등록 저장소 PR 번호').fill('19'); await merge.getByLabel('승인자 인증 (prd-approver)').fill('MERGE_TOKEN_SENTINEL'); await merge.getByRole('checkbox').check(); await merge.getByRole('button', { name: '사람 병합 확인' }).click();
  await expect(approval.getByRole('checkbox')).toBeEnabled(); await approval.getByLabel('승인자 인증 (prd-approver)').fill('APPROVE_TOKEN_SENTINEL'); await approval.getByRole('checkbox').check(); await approval.getByRole('button', { name: 'prd 배포 승인' }).click();
  await expect.poll(() => m.writes.length).toBe(3); expect(m.writes[1].body).toEqual({ ...f, pr_number: 19 }); expect(m.writes[2].body).toEqual({ ...f, user_test_id: 'test-a' });
  await expect(approval.getByLabel('승인자 인증 (prd-approver)')).toHaveValue(''); expect(m.writes[2].authorization).toBe('Bearer APPROVE_TOKEN_SENTINEL');
});
test('changed release clears confirmation and entered approval credential', async ({ page }) => {
  const m = await mock(page, 'awaiting_prd_approval'); m.detail.application.data.merge_review = { ...f, human_merge_observed: true, expires_at: Date.now() / 1000 + 60 };
  await page.goto('/incidents/971'); await readDetail(page);
  const form = page.getByRole('form', { name: 'prd 배포 승인', exact: true }); await form.getByLabel('승인자 인증 (prd-approver)').fill('STALE_TOKEN_SENTINEL'); await form.getByRole('checkbox').check();
  m.detail.release.release.plan_sha256 = 'd'.repeat(64);
  await expect(form.getByRole('checkbox')).not.toBeChecked({ timeout: 8000 }); await expect(form.getByLabel('승인자 인증 (prd-approver)')).toHaveValue(''); await expect(form.getByRole('button', { name: 'prd 배포 승인' })).toBeDisabled(); expect(m.writes).toHaveLength(0);
});
test('unauthorized detail is explicit and cannot leak the server error', async ({ page }) => {
  const m = await mock(page); m.rejectDetail(); await page.goto('/incidents/971'); const card = page.getByRole('article', { name: '코드 조치', exact: true });
  await card.getByLabel('상세 조회 인증 (incident:read)').fill('UNAUTHORIZED_SENTINEL'); await card.getByRole('button', { name: '인증 적용' }).click();
  await expect(card.getByRole('alert')).toContainText('사람 권한을 확인하지 못했습니다'); await expect(page.getByText('PRIVATE_ERROR_SENTINEL')).toHaveCount(0); await expect(card.getByLabel('상세 조회 인증 (incident:read)')).toHaveValue(''); expect(m.writes).toHaveLength(0);
});
test('internal app intake is idempotent on uncertain response and preserves native historical IDs', async ({ page }) => {
  const m = await mock(page); await page.goto('/application'); await expect(page.getByRole('link', { name: '애플리케이션 장애', exact: true })).toHaveAttribute('href', '/application');
  const history = page.getByRole('region', { name: 'Application 지난 이력' }); await history.getByLabel('이력 조회 인증 (incident:read)').fill('HISTORY_TOKEN_SENTINEL'); await history.getByRole('button', { name: '인증 적용' }).click();
  await expect(history.getByText('legacy-native-17', { exact: true })).toBeVisible(); await expect(history.locator('dd').filter({ hasText: 'stg · 퇴역 이력' })).toBeVisible();
  const intake = page.getByRole('region', { name: '새 애플리케이션 사건' }); await intake.getByRole('combobox', { name: '서비스', exact: true }).selectOption('inventory'); await intake.getByLabel('문제 상황').fill('주문 처리에 실패합니다.');
  await intake.getByLabel('사건 접수 인증 (incident:create)').fill('CREATE_TOKEN_SENTINEL'); await intake.getByRole('checkbox').check(); m.rejectCreate(true); await intake.getByRole('button', { name: '사건 접수·관제실 열기' }).click(); await expect(intake.getByRole('alert')).toBeVisible();
  m.rejectCreate(false); await intake.getByLabel('사건 접수 인증 (incident:create)').fill('CREATE_TOKEN_SENTINEL'); await intake.getByRole('checkbox').check(); await intake.getByRole('button', { name: '사건 접수·관제실 열기' }).click(); await expect(page).toHaveURL(/\/incidents\/971$/); await expect(page.getByRole('article', { name: '코드 조치', exact: true })).toBeVisible();
  expect(m.writes).toHaveLength(2); expect(m.writes[0].body.request_id).toBe(m.writes[1].body.request_id); expect(m.writes[1].authorization).toBe('Bearer CREATE_TOKEN_SENTINEL'); expect(m.writes[1].body.environment).toBe('dev');
  await page.getByRole('link', { name: '애플리케이션 장애', exact: true }).click(); await expect(page.getByLabel('이력 조회 인증 (incident:read)')).toHaveValue(''); await expect(page.getByText('legacy-native-17', { exact: true })).toHaveCount(0);
  m.rejectHistory(); await page.getByLabel('이력 조회 인증 (incident:read)').fill('REJECTED_HISTORY_TOKEN'); await history.getByRole('button', { name: '인증 적용' }).click(); await expect(history.getByRole('alert')).toContainText('사람 권한을 확인하지 못했습니다'); await expect(page.getByLabel('이력 조회 인증 (incident:read)')).toHaveValue(''); await expect(page.getByText('PRIVATE_HISTORY_ERROR')).toHaveCount(0);
});

test('disabled unified feature preserves original Kubernetes handoff controls', async ({ page }) => {
  const m = await mock(page); Object.assign(m.summary, { enabled: false, linked: false, blocks_cluster: false, eligible: false, action_id: null, application_id: null });
  await page.goto('/incidents/971'); await expect(page.getByRole('article', { name: '운영자 인계', exact: true })).toBeVisible();
  await expect(page.getByRole('article', { name: '코드 조치', exact: true })).toHaveCount(0); await expect(page.getByRole('textbox', { name: '운영자 메시지' })).toBeVisible(); expect(m.writes).toHaveLength(0);
});
