// 정돈-03/04/06/07 Application 화면 mock 브라우저 시험. 실제 API·DB·모델에 접속하지 않는다.
// 정적 파일은 로컬 임시 HTTP 서버로, /api/application/v1/* 는 page.route 합성 응답으로 제공한다.
// 사용: PLAYWRIGHT_MODULE=<playwright 패키지 경로> node test_ui_cleanup_browser.mjs [증거 디렉터리]
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../app/application/static');
const OUT = process.argv[2] || null;
const PREFIX = '/api/application/v1';
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

const server = http.createServer((req, res) => {
  const name = req.url === '/' ? 'index.html' : path.basename(req.url.split('?')[0]);
  const file = path.join(STATIC, name);
  if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
  res.end(fs.readFileSync(file));
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

// 2026-11-13 dev/prd 전환: inc0002는 퇴역 stg에서 감지된 기존 v1(dev→stg→prd, protocol 필드 없음) 사용자 시험 대기 기록,
// inc0004는 현행 v2(dev→prd) dev 사용자 시험 대기, inc0005는 퇴역 환경 작업 거부로 이관된 사건이다. prd 필터 총건수(40)는 그대로다.
const incidents = Array.from({ length: 120 }, (_, n) => ({
  id: `inc${String(n).padStart(4, '0')}`,
  state: n === 0 ? 'awaiting_prd_approval' : n === 1 ? 'analyzing' : n === 2 || n === 4 ? 'awaiting_user_test' : 'handoff',
  created_at: 2000 - n, environment: n % 3 === 0 ? 'prd' : n === 2 ? 'stg' : 'dev', service: 'order', system: 'samsung-trade', reason: `r${n}`,
}));
// 예전 표본은 protocol 필드 없이 stage_results.stg만 두었다(stg 사용자 시험 시절). 현행 승인 흐름 표본은 v2(dev 검증 통과)로 바꾸고,
// 기존 v1 기록은 inc0002로 따로 검증한다.
const release = (artifact, id = 'inc0000') => {
  const base = { valid: true, release_id: `rel-${id}`, artifact_sha256: artifact, plan_sha256: 'p'.repeat(64),
    artifact: { image: 'local/order@sha256:' + '2'.repeat(64) }, targets: { prd: [] } };
  if (id === 'inc0002') return { ...base, stage_results: { dev: { deployed: true, verified: true }, stg: { deployed: true, verified: true } }, user_test: null };
  return { ...base, protocol: 'dev-prd/v2', stage_results: { dev: { deployed: true, verified: state.devVerified } },
    user_test: id === 'inc0000' ? { id: 'ut-1', passed: true, invalidated: false, expires_at: Date.now() / 1000 + 3600 } : null };
};
const extra = {
  inc0005: { error: { code: 'environment_retired', class: 'authority_boundary', message_ko: '권한·데이터 안전 경계' },
    handoff_classification: { class: 'authority_boundary', label_ko: '권한·데이터 안전 경계', recovery_attempted: false, attempts: null, reason_code: 'environment_retired' } },
};
const state = { artifact: 'a'.repeat(64), listCalls: [], detailDelay: {}, readyCalls: 0, devVerified: true };

const browser = await chromium.launch();
const results = [];
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.clock.install({ time: new Date('2026-11-10T00:00:00Z') });
  await page.route(`**${PREFIX}/**`, async route => {
    const url = new URL(route.request().url());
    const p = url.pathname.slice(PREFIX.length);
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (p === '/readyz') {
      state.readyCalls += 1;
      return json({ ready: false, mode: 'runtime', code: 'provisioning_required', message_ko: '확인 필요',
        checks: { database: true, human_identity: true, machine_identity: true, profiles: true, runner: false, model: true },
        model_status: { state: 'configured_unverified', configured: true, last_success_at: null, last_failure_at: null, last_failure_code: null },
        waiting_people: { awaiting_user_test: 0, awaiting_prd_approval: 1 } }, 503);
    }
    // 구 서버처럼 퇴역 stg를 environments에 보내도 새 사건 환경 선택지에는 나타나지 않아야 한다.
    if (p === '/catalog') return json({ systems: ['samsung-trade'], services: [{ service: 'order', system: 'samsung-trade' }], environments: ['dev', 'stg', 'prd'], catalog_sha256: 'c'.repeat(64), profiles_configured: true });
    if (p === '/incidents') {
      const q = Object.fromEntries(url.searchParams);
      state.listCalls.push(q);
      let rows = incidents.filter(i => !q.environment || i.environment === q.environment);
      const offset = Number(q.offset || 0), limit = Number(q.limit || 50);
      return json({ incidents: rows.slice(offset, offset + limit), total: rows.length, as_of: 2000, newer_since_as_of: q.as_of ? 2 : 0 });
    }
    const m = p.match(/^\/incidents\/([^/]+)(\/release)?$/);
    if (m) {
      const inc = incidents.find(i => i.id === m[1]);
      const delay = state.detailDelay[m[1]] || 0;
      if (delay) await new Promise(r => setTimeout(r, delay));
      if (m[2]) return json({ incident_id: inc.id, state: inc.state, release: release(state.artifact, inc.id), test_build: null });
      return json({ ...inc, stages: [], error: null, ...(extra[inc.id] || {}), data: { release: release(state.artifact, inc.id) } });
    }
    return json({ code: 'not_found' }, 404);
  });
  await page.goto(base + '/');

  // 정돈-03/07: 제품 목적 안내, 모델 '구성됨'과 '호출 성공'의 분리.
  assert.match(await page.textContent('.purpose'), /별도 제품/);
  assert.equal(await page.isVisible('#workspace'), false);
  await page.waitForSelector('#readiness-checks dl');
  const readiness = await page.textContent('#readiness-checks');
  assert.match(readiness, /모델 실행 구성\(설치·설정\)/);
  assert.match(readiness, /구성됨 · 호출 성공 기록 없음/);
  assert.doesNotMatch(readiness, /모델 연결/);
  results.push('readiness_separates_configured_from_success');

  // dev/prd 전환: 새 사건 환경은 활성 dev·prd만, 이력 필터는 stg를 퇴역·이력으로 표시해 유지한다.
  await page.waitForFunction(() => document.querySelectorAll('#create-environment option').length > 1);
  assert.deepEqual(await page.$$eval('#create-environment option', os => os.map(o => o.value)), ['', 'dev', 'prd']);
  assert.deepEqual(await page.$$eval('#filter-environment option', os => os.map(o => o.value)), ['', 'dev', 'stg', 'prd']);
  assert.equal(await page.textContent('#filter-environment option[value="stg"]'), 'stg (퇴역·이력)');
  results.push('create_uses_active_envs_and_history_filter_marks_retired_stg');

  // 정돈-04: 서버 총건수·필터·페이지(as_of 고정).
  await page.fill('#auth-bearer', 'synthetic-read-only');
  await page.click('#auth-form button[type=submit]');
  await page.waitForFunction(() => document.querySelector('#list-count').textContent.includes('120'));
  assert.equal(await page.locator('#incident-list .incident-select').count(), 50);
  await page.click('#list-next');
  await page.waitForFunction(() => document.querySelector('#list-page').textContent.startsWith('2 /'));
  assert.equal(state.listCalls.at(-1).as_of, '2000');
  assert.equal(state.listCalls.at(-1).offset, '50');
  assert.equal(await page.isVisible('#list-newer'), true);          // 새 사건 유입 안내
  await page.selectOption('#filter-environment', 'prd');
  await page.click('#list-filter button[type=submit]');
  await page.waitForFunction(() => document.querySelector('#list-count').textContent.includes('40'));
  assert.equal(state.listCalls.at(-1).environment, 'prd');
  assert.equal(state.listCalls.at(-1).offset, '0');
  assert.equal(state.listCalls.at(-1).as_of, undefined);
  results.push('server_filter_total_and_pinned_paging');

  // 정돈-06: 늦게 도착한 이전 선택 응답이 현재 선택을 덮지 않는다.
  state.detailDelay = { inc0003: 400 };
  await page.click('[data-id="inc0003"]');
  await page.click('[data-id="inc0000"]');
  await page.waitForFunction(() => document.querySelector('#detail-title').textContent.includes('prd 승인 대기'));
  await page.waitForTimeout(800);                                    // the delayed inc0003 response has now arrived
  assert.match(await page.textContent('#detail-title'), /prd 승인 대기/);
  assert.equal(await page.getAttribute('[data-id="inc0000"]', 'aria-pressed'), 'true');
  assert.match(await page.textContent('#next-action-body'), /prd 승인이 필요합니다/);
  state.detailDelay = {};
  results.push('stale_detail_response_discarded');

  // 정돈-06: 자동 갱신은 작성 중인 시험 기록·포커스·승인 확인(같은 지문)을 유지한다.
  await page.fill('#approver-bearer', 'synthetic-approval-draft');
  await page.check('#approve-confirm');
  await page.focus('#approver-bearer');
  const before = state.listCalls.length;
  await page.clock.runFor(16000);
  await page.waitForTimeout(300);
  assert.ok(state.listCalls.length > before, 'auto refresh fetched the list');
  assert.equal(await page.inputValue('#approver-bearer'), 'synthetic-approval-draft');
  assert.equal(await page.evaluate(() => document.activeElement.id), 'approver-bearer');
  assert.equal(await page.isChecked('#approve-confirm'), true);
  assert.equal(await page.isDisabled('#approve-submit'), false);      // 합성 초안 권한과 같은 지문 확인 유지, 전송은 하지 않는다.
  results.push('auto_refresh_preserves_input_focus_and_confirmation');

  // 정돈-06: 승인 대상 지문이 바뀌면 확인만 해제한다(자동 승인 없음).
  state.artifact = 'b'.repeat(64);
  await page.clock.runFor(16000);
  await page.waitForFunction(() => !document.querySelector('#approve-confirm').checked);
  assert.match(await page.textContent('#live-status'), /승인 확인을 해제/);
  assert.equal(await page.inputValue('#approver-bearer'), 'synthetic-approval-draft');
  results.push('fingerprint_change_clears_confirmation_only');

  // 퇴역 stg 이력 조회: environment=stg를 서버로 그대로 보내고 과거 사건을 찾는다.
  await page.selectOption('#filter-environment', 'stg');
  await page.click('#list-filter button[type=submit]');
  await page.waitForFunction(() => document.querySelector('#list-count').textContent === '전체 1건');
  assert.equal(state.listCalls.at(-1).environment, 'stg');
  assert.match(await page.textContent('[data-id="inc0002"]'), /stg \(퇴역·이력\)/);
  results.push('retired_stg_history_filter_queries_server');

  // 기존 v1(protocol 필드 없음) 기록: stg 사용자 시험 기록으로 표시하되 stg 퇴역으로 읽기 전용, 제출 불가.
  await page.click('[data-id="inc0002"]');
  await page.waitForFunction(() => document.querySelector('#detail-title').textContent.includes('stg 사용자 시험 대기'));
  const legacyNext = await page.textContent('#next-action-body');
  assert.match(legacyNext, /기존 dev→stg→prd\(v1\) 절차의 기록/);
  assert.match(legacyNext, /읽기 전용/);
  assert.match(legacyNext, /protocol 필드 없음/);
  assert.doesNotMatch(legacyNext, /사용자 시험이 필요합니다/);
  assert.equal(await page.textContent('#user-test-form h4'), 'stg 사용자 시험 기록');
  assert.match(await page.textContent('#human-records'), /stg 사용자 시험 기록/);
  assert.equal(await page.isDisabled('#user-test-submit'), true);
  assert.match(await page.textContent('#approval-gate'), /읽기 전용/);
  assert.deepEqual(await page.$$eval('#env-status .env-result h4', hs => hs.map(h => h.textContent)), ['dev', 'stg (퇴역·이력)', 'prd']);
  assert.match(await page.textContent('#incident-summary'), /stg \(퇴역·이력\)/);
  results.push('legacy_v1_release_is_read_only_with_retired_stg_row');

  // 현행 v2 기록: dev 사용자 시험, dev 업무 확인이 통과해야만 제출이 열린다. stg 행은 없다.
  await page.selectOption('#filter-environment', '');
  await page.click('#list-filter button[type=submit]');
  await page.waitForFunction(() => document.querySelector('#list-count').textContent.includes('120'));
  await page.click('[data-id="inc0004"]');
  await page.waitForFunction(() => document.querySelector('#detail-title').textContent.includes('dev 사용자 시험 대기'));
  assert.match(await page.textContent('#next-action-body'), /dev 사용자 시험이 필요합니다/);
  assert.match(await page.textContent('#next-action-body'), /dev-prd\/v2/);
  assert.equal(await page.textContent('#user-test-form h4'), 'dev 사용자 시험 기록');
  assert.match(await page.textContent('label[for="tester-bearer"]'), /^dev 시험자 인증 토큰\(tester\)/);
  assert.equal(await page.textContent('#user-test-submit'), 'dev 시험 기록');
  assert.deepEqual(await page.$$eval('#env-status .env-result h4', hs => hs.map(h => h.textContent)), ['dev', 'prd']);
  assert.equal(await page.isDisabled('#user-test-submit'), false);
  state.devVerified = false;                                         // dev 업무 확인이 아직이면 제출을 막는다.
  await page.clock.runFor(16000);
  await page.waitForFunction(() => document.querySelector('#user-test-submit').disabled);
  state.devVerified = true;
  results.push('v2_user_test_gate_follows_dev_verification');

  // 퇴역 환경 작업 거부 이관: 분류 문구와 세부 원인 코드를 함께 보존해 보여 준다.
  await page.click('[data-id="inc0005"]');
  await page.waitForFunction(() => document.querySelector('#next-action-body').textContent.includes('이관 분류'));
  const handoff = await page.textContent('#next-action-body');
  assert.match(handoff, /퇴역 환경\(stg\)에는 새 작업을 배정하지 않습니다/);
  assert.match(handoff, /이관 분류: 권한·데이터 안전 경계 · 자율 복구 시도 기록 없음/);
  assert.doesNotMatch(handoff, /environment_retired|authority_boundary/);
  await page.click('#raw-incident > summary');
  await page.waitForFunction(() => document.querySelector('#incident-raw').textContent.includes('environment_retired'));
  assert.match(await page.textContent('#incident-raw'), /authority_boundary/);
  await page.click('#raw-incident > summary');
  // 조건부로 빠진 항목이 'null' 글자로 보이지 않는다(이전에는 replaceChildren(null)이 'null'을 그렸다).
  assert.doesNotMatch(handoff, /null/);
  results.push('retired_environment_handoff_keeps_class_and_reason_code');

  for (const width of [1440, 1280, 768, 375]) {
    await page.setViewportSize({ width, height: 900 });
    assert.equal(await page.isVisible('#next-action-body'), true, `next action visible at ${width}`);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `no horizontal overflow at ${width}: ${overflow}`);
    if (OUT) {
      await page.screenshot({ path: path.join(OUT, `application-${width}.png`), fullPage: false });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: path.join(OUT, `application-${width}-top.png`), fullPage: false });
    }
  }
  results.push('responsive_1440_1280_768_375');
} finally {
  await browser.close();
  server.close();
}
console.log(JSON.stringify({ ok: true, checks: results }));
