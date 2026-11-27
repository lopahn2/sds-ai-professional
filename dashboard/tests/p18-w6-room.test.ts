import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';
import type { FlowNode, Incident, Narrative } from '../src/api.ts';
import { conversationEvents, handoffFallback, messageEvents, orderSteps, steerStateKo, stepTargetRole, stepThread, STEERABLE_TYPES } from '../src/narrative.ts';

// Render the real TSX cards without a build or new dependency (same loader approach as p16-flow.test.ts).
const src = new URL('../src/', import.meta.url).href;
registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith('.') && context.parentURL?.startsWith(src) && !/\.[a-z]+$/i.test(specifier)) {
      for (const ext of ['.tsx', '.ts']) { const url = new URL(`${specifier}${ext}`, context.parentURL); try { readFileSync(url); return { url: url.href, shortCircuit: true }; } catch { /* next */ } }
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url.endsWith('.css')) return { format: 'module', source: 'export {};', shortCircuit: true };
    if (url.startsWith(src) && /\.tsx?$/.test(url)) {
      const source = ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
      return { format: 'module', source, shortCircuit: true };
    }
    return next(url, context);
  },
});
const { StepCard, StepThread } = await import('../src/StepCards.tsx');
const { DetailPanel } = await import('../src/DetailPanel.tsx');
const { HandoffCard } = await import('../src/ActionApproval.tsx');

const fixture = (id: number) => JSON.parse(readFileSync(new URL(`../e2e/fixtures/narrative-${id}.json`, import.meta.url), 'utf8')) as Narrative;
const strip = (n: FlowNode): FlowNode => { const { at: _a, card: _c, ...rest } = n; return rest as FlowNode; };
const legacy = (d: Narrative): Narrative => ({ ...d, handoff_view: undefined, flow: { ...d.flow, nodes: d.flow.nodes.map(strip) } });
const card = (d: Narrative, nodeId: string, extra: Record<string, unknown> = {}) => renderToStaticMarkup(createElement(StepCard, {
  n: d.flow.nodes.find(x => x.id === nodeId)!, data: d, flash: false, onDetail() {}, handoff: d.handoff_view ?? null,
  thread: [], local: [], targetRole: null, closed: false, finished: false, draft: '', onDraft() {}, onSend: async () => {}, name: (r: string) => r, onJump() {}, ...extra,
}));
const ev = (seq: number, kind: string, detail: Record<string, unknown> = {}) => ({ seq, kind, actor: 'x', summary: `s${seq}`, detail, at: `2026-10-11T12:00:${String(seq).padStart(2, '0')}Z` });

test('판단 과정과 사유는 기본 표시하고 판단 원문만 숨긴다', () => {
  const d = fixture(990); const html = card(d, 'router');
  const keys = [...html.matchAll(/data-step-key="([a-z]+)"/g)].map(m => m[1]);
  assert.deepEqual(keys, (d.flow.nodes.find(n=>n.id==='router')!.card as any).steps.map((s:any)=>s.key));
  assert.ok(html.includes('decision-context'));
  const full = detail(d, 'router');
  assert.ok(full.includes('판단 사유 원문') && full.includes('AI 분류 의견 원문'));
  assert.ok(!html.includes('The rule engine identifies'), '판단 원문은 상세보기에서만 보인다');
});

// P20: 역할별 모델 배치는 이 과제의 핵심이라 그룹 카드에서 기본 표시한다.
// 2026-11-15 표시 원칙: 역할 구성·선택 이유·예산도 펼치기 없이 보이고 원문만 상세에 둔다.
test('그룹 단계는 역할별 모델 배치·진행 기록·역할 구성·예산을 펼치기 없이 표시한다', () => {
  const d = fixture(971); const n = d.flow.nodes.find(x => x.id === 'group')!;
  assert.equal(n.card?.kind, 'group'); if (n.card?.kind !== 'group') return;
  const html = card(d, 'group');
  assert.ok(html.includes('복잡 그룹') && html.includes('역할 구성') && !html.includes('역할 구성·선택 이유 보기'));
  assert.equal((html.match(/data-role="/g) ?? []).length, n.card.roles.length, '역할별 모델 배치는 역할마다 한 줄');
  for (const r of n.card.roles) assert.ok(html.includes(r.model!) && html.includes(r.role_ko), r.role);
  assert.ok(html.includes('aria-label="역할별 모델 배치"') && html.includes('aria-label="Group 진행 기록"'));
  assert.ok(html.includes(n.card.roles[0].job_ko), '담당 업무 설명도 기본 표시한다');
  for (const t of ['예산', '진행 기록']) assert.ok(html.includes(t), t);
  const full = detail(d, 'group');
  for (const t of ['역할 구성', '예산', '진행 기록', '반론 검토', 'Group 종료: 검증 통과']) assert.ok(full.includes(t), t);
});

test('근거 수집 카드는 상단 건수와 같은 N개 행을 보여 주고 실패 항목에 사유를 남긴다', () => {
  const d = fixture(990); const n = d.flow.nodes.find(x => x.id === 'evidence')!;
  assert.equal(n.card?.kind, 'evidence'); if (n.card?.kind !== 'evidence') return;
  const html = card(d, 'evidence');
  assert.equal((html.match(/data-evidence-id="/g) ?? []).length, n.card.total);
  assert.ok(html.includes(`수집 ${n.card.total}건 · 성공 ${n.card.ok} · 실패 ${n.card.failed}`));
  assert.ok(html.includes('실패 사유는 원문에서 확인하세요.'));
  assert.ok(!html.includes('HTTPStatusError'));
  assert.ok(detail(d, 'evidence').includes('HTTPStatusError'));
});

test('분석·검증 카드는 같은 근거를 써도 서로 다른 결과(원인 후보 vs 검증 판정)를 보인다', () => {
  const d = fixture(971);
  const analysis = card(d, 'run:4098'), verify = card(d, 'run:4101');
  assert.ok(analysis.includes('원인 후보') && analysis.includes('추가 조사 필요'));
  assert.ok(verify.includes('근거로 지지') && verify.includes('Kubernetes 조치 제안'));
  assert.ok(verify.includes('실제 서비스 복구 검증과는 다릅니다'));
});

test('펼친 router·group·agent·verify 카드에만 단계 채팅이 있고, 종료 사건은 입력을 막는다', () => {
  const d = fixture(1976);
  for (const n of d.flow.nodes) {
    const html = card(d, n.id);
    assert.equal(html.includes('step-chat"'), STEERABLE_TYPES.includes(n.type), n.id);
  }
  const closed = card(d, 'planned:coordinator', { closed: true });
  assert.ok(closed.includes('종료된 장애라 이 단계에 지시를 보낼 수 없습니다.') && !closed.includes('<textarea'));
  const finished = card(d, 'planned:coordinator', { finished: true });
  assert.ok(finished.includes('분석이 끝나 실행 계획 확인 절차로 접수됩니다.'));
});

test('대상 역할: agent/verify는 그 역할, group은 다음 미실행 역할, router는 생략', () => {
  const d = fixture(1976); const by = (id: string) => d.flow.nodes.find(x => x.id === id)!;
  assert.equal(stepTargetRole(by('planned:coordinator'), d.flow.nodes), 'coordinator');
  assert.equal(stepTargetRole(by('planned:group_verifier'), d.flow.nodes), 'group_verifier');
  assert.equal(stepTargetRole(by('group'), d.flow.nodes), 'coordinator');
  assert.equal(stepTargetRole(by('router'), d.flow.nodes), null);
  // card 없는 이전 서버 응답에서도 흐름의 다음 실행 전 역할을 고른다.
  const old = legacy(d);
  assert.equal(stepTargetRole(old.flow.nodes.find(x => x.id === 'group')!, old.flow.nodes), 'coordinator');
});

test('단계 스레드·대화 목록: 대상 지정 메시지는 그 카드로, 실행 기록 이벤트는 카드에 흡수된다', () => {
  const events = [ev(1, 'agent_started', { role: 'k8s_analyst' }), ev(2, 'operator_message', { message_id: 7, target_node: 'planned:coordinator', target_role: 'coordinator' }),
    ev(3, 'steering_interpreted', { message_id: 7, target_node: 'planned:coordinator', ack_ko: '진행 조정 시작 전에 이 지시를 전달합니다.' }), ev(4, 'operator_message', { message_id: 8 }), ev(5, 'paused')];
  assert.deepEqual(stepThread(events, 'planned:coordinator').map(e => e.seq), [2, 3]);
  assert.deepEqual(conversationEvents(events).map(e => e.seq), [4, 5]);
  assert.deepEqual(conversationEvents(events, new Set([8])).map(e => e.seq), [5]);
  assert.deepEqual(messageEvents(events, '7').map(e => e.seq), [2, 3]);
});

test('대상 없이 보낸 지시도 실제 반영 기록이 있으면 반영한 단계 이름으로 알린다', () => {
  // 실배포 #1081 관측 모양: 대상 없는 메시지 19, 반영 이벤트는 message_ids 배열과 applied_to_role만 있다.
  const events = [ev(8, 'operator_message', { message_id: 19 }), ev(9, 'steering_interpreted', { message_id: 19 }),
    ev(22, 'operator_applied', { message_ids: [19], applied_to_role: 'k8s_log_analyst', escalate: false, reroute: null })];
  const html = renderToStaticMarkup(createElement(StepThread, { events, local: [], node: null, name: (r: string) => r === 'k8s_log_analyst' ? '쿠버네티스·로그 분석' : r }));
  assert.ok(html.includes('쿠버네티스·로그 분석 시작 전에 반영됨'), html);
  const pending = renderToStaticMarkup(createElement(StepThread, { events: events.slice(0, 2), local: [], node: null, name: (r: string) => r }));
  assert.ok(!pending.includes('반영됨'), '반영 기록이 없으면 반영됐다고 말하지 않는다');
});

test('엔진이 사용 한도 대기 후 이어간 기록은 운영자 재개로 표시하지 않는다', async () => {
  const { controlEventKo } = await import('../src/narrative.ts');
  assert.equal(typeof controlEventKo, 'function');
  // 실배포 #1081 관측: actor=liveprobe, detail.after_s 가 있는 resumed 는 엔진의 사용 한도 해제 재개다.
  assert.equal(controlEventKo({ kind: 'resumed', actor: 'liveprobe', detail: { after_s: 7200 } }), '사용 한도가 풀려 분석을 이어갔습니다.');
  assert.equal(controlEventKo({ kind: 'resumed', actor: 'local-operator', detail: {} }), '운영자가 분석을 재개했습니다.');
  assert.equal(controlEventKo({ kind: 'waiting_quota', actor: 'liveprobe', detail: {} }), '두 Codex 계정이 사용 한도라 분석을 대기했습니다.');
  assert.equal(controlEventKo({ kind: 'note', actor: 'liveprobe', detail: { account: 'spare', blocked_for_s: 1800 } }), 'Codex 계정 사용 한도 기록');
  assert.equal(controlEventKo({ kind: 'note', actor: 'liveprobe', detail: {} }), '실행 메모가 기록되었습니다.');
});

test('혼합 배치 반영은 각 메시지의 대상 역할과 실제 반영 역할로 구분한다', () => {
  // 실제 steering.take 재현과 같은 모양: 메시지 7만 자기 대상, 메시지 8은 다음 단계로 대체 반영.
  const events = [ev(1, 'operator_message', { message_id: 7, target_node: 'plan:10:cause_reasoner', target_role: 'cause_reasoner' }),
    ev(2, 'steering_interpreted', { message_id: 7, target_node: 'plan:10:cause_reasoner', hold: true }),
    ev(3, 'operator_message', { message_id: 8, target_node: 'run:100', target_role: 'k8s_log_analyst' }),
    ev(4, 'operator_applied', { message_ids: [7, 8], applied_to_role: 'cause_reasoner', targeted_role: ['cause_reasoner', 'k8s_log_analyst'], fallback: true })];
  const origin = fixture(1976).flow.nodes.find(x => x.role === 'coordinator')!;
  const names = (r: string) => ({ cause_reasoner: '원인 추론', k8s_log_analyst: '로그 분석' })[r] ?? r;
  const cases: [FlowNode, string][] = [
    [{ ...origin, id: 'run:101', role: 'cause_reasoner', aliases: ['plan:10:cause_reasoner'], status: 'done' }, '원인 추론 시작 전에 반영됨'],
    [{ ...origin, id: 'run:100', role: 'k8s_log_analyst', status: 'done' }, '로그 분석 대신 원인 추론에 반영됨'],
  ];
  for (const [node, expected] of cases) {
    const html = renderToStaticMarkup(createElement(StepThread, { events: stepThread(events, node.id, node.aliases ?? []), local: [], node, name: names }));
    assert.ok(html.includes(expected), html);
    assert.ok(!html.includes('원인 추론 대신 다음 단계에 반영됨'));
  }
});

test('배치 fallback 값보다 메시지 자체의 역할 일치가 우선한다', () => {
  assert.equal(steerStateKo({ applied: { applied_to_role: 'cause_reasoner', fallback: true }, targetRole: 'cause_reasoner', appliedRoleName: '원인 추론', hold: true, targetStatus: 'done', roleName: '원인 추론' }), '원인 추론 시작 전에 반영됨');
  assert.equal(steerStateKo({ applied: { applied_to_role: 'cause_reasoner', fallback: false }, targetRole: 'k8s_log_analyst', appliedRoleName: '원인 추론', hold: false, targetStatus: 'done', roleName: '로그 분석' }), '로그 분석 대신 원인 추론에 반영됨');
});

test('반영 상태는 기록된 사실만 말한다', () => {
  assert.equal(steerStateKo({ applied: { applied_to_role: 'coordinator', fallback: false }, hold: true, targetStatus: 'done', roleName: '진행 조정' }), '진행 조정 시작 전에 반영됨');
  assert.equal(steerStateKo({ applied: null, hold: true, targetStatus: 'planned', roleName: '진행 조정' }), '반영 예정');
  assert.equal(steerStateKo({ applied: null, hold: true, targetStatus: 'skipped', roleName: '진행 조정' }), '대상 단계가 실행되지 않아 반영되지 않음');
});

test('단계 순서: 모든 카드에 시각이 있으면 시각순, 하나라도 없으면 흐름 순서(depth·lane)', () => {
  const d = fixture(971);
  assert.deepEqual(orderSteps(d.flow.nodes).map(n => n.id).slice(0, 4), ['signal', 'evidence', 'router', 'group']);
  const partial = fixture(1976);
  assert.deepEqual(orderSteps(partial.flow.nodes).map(n => n.id), [...partial.flow.nodes].sort((a, b) => a.depth - b.depth || a.lane - b.lane).map(n => n.id));
  assert.ok(!orderSteps([...d.flow.nodes, { ...d.flow.nodes[0], id: 'command:1', type: 'command' }]).some(n => n.type === 'command'));
});

test('인계 카드는 인수·판단·다음 조치를 본문에 표시하고 원문은 요청 시 연다', () => {
  const d = fixture(990); const inc = d.incident as Incident;
  const html = renderToStaticMarkup(createElement(HandoffCard, { incident: inc, view: d.handoff_view!, onChange() {} }));
  const order = ['인계 인수', 'handoff-lead', 'decision-table', 'next-steps', 'handoff-raw'].map(k => html.indexOf(k));
  assert.ok(order.every((v, i) => v > 0 && (i === 0 || v > order[i - 1])), JSON.stringify(order));
  assert.equal((html.match(/data-key="/g) ?? []).length, d.handoff_view!.decisions.length);
  assert.ok(!html.includes('kubectl'), '닫힌 원문은 DOM 자체에 없다');
  assert.ok(html.includes('인계 원문 보기'));
  assert.ok(!/<details[^>]* open/.test(html));
});

test('백엔드 새 필드가 없으면 미기록·해석 준비 중을 정직하게 표시하고 영어 목록으로 대신하지 않는다', () => {
  const d = legacy(fixture(990));
  const router = card(d, 'router'); assert.ok(router.includes('현재 미기록') && !router.includes('The rule engine'));
  const group = card(d, 'group'); assert.ok(group.includes('선택 이유와 판단 과정은 서버 기록을 받으면 표시합니다'));
  const evidence = card(d, 'evidence'); assert.ok(evidence.includes('수집 항목별 목록은 아직 받지 못했습니다'));
  const signal = card(d, 'signal'); assert.ok(signal.includes(d.headline.what) && signal.includes('미기록'));
  const steps = { ...d.incident, status: 'handed_off', handoff: { recommended_steps: ['kubectl get pods'] } } as Incident;
  const view = handoffFallback(steps);
  assert.equal(view.summary_ko, null); assert.equal(view.interpreting, true);
  assert.deepEqual(view.decisions.map(x => x.key), ['outcome', 'cause', 'operator']);
  const html = renderToStaticMarkup(createElement(HandoffCard, { incident: steps, view, onChange() {} }));
  assert.ok(html.includes('담당자 확인이 필요합니다.'));
  assert.ok(!html.slice(0, html.indexOf('<details')).includes('kubectl get pods'));
});

test('실제 message_ids 배열 반영 이벤트를 해당 단계 메시지와 연결한다', () => {
  const events = [ev(1, 'operator_message', { message_id: 7, target_node: 'plan:10:cause_reasoner', target_role: 'cause_reasoner' }),
    ev(2, 'steering_interpreted', { message_id: 7, target_node: 'plan:10:cause_reasoner', hold: true }),
    ev(3, 'operator_applied', { message_ids: [7, 11], applied_to_role: 'cause_reasoner', fallback: false })];
  assert.equal(messageEvents(events, 7).length, 3);
  assert.equal(messageEvents(events, 1).length, 0, '1과 11을 부분 일치시키지 않는다');
  const thread = stepThread(events, 'plan:10:cause_reasoner');
  assert.equal(thread.length, 3);
  const n = { ...fixture(1976).flow.nodes.find(x => x.role === 'coordinator')!, status: 'done' };
  const html = renderToStaticMarkup(createElement(StepThread, { events: thread, local: [], node: n, name: () => '원인 추론' }));
  assert.ok(html.includes('원인 추론 시작 전에 반영됨'));
  assert.ok(!html.includes('반영 예정') && !html.includes('실행되지 않아 반영되지 않음'));
});

test('예정 노드의 별칭은 같은 실행 카드로만 연결되고 상세도 유지된다', async () => {
  const { resolveStepId } = await import('../src/narrative.ts');
  assert.equal(typeof resolveStepId, 'function');
  const d = fixture(1976); const planned = d.flow.nodes.find(x => x.role === 'coordinator')!;
  const completed = { ...planned, id: 'run:900', aliases: ['plan:10:coordinator'], status: 'done', conclusion_raw: '이 실행의 완료 결론' };
  const later = { ...completed, id: 'run:901', aliases: ['plan:11:coordinator'] };
  assert.equal(resolveStepId('plan:10:coordinator', [completed, later]), 'run:900');
  assert.equal(resolveStepId('plan:11:coordinator', [completed, later]), 'run:901');
  assert.equal(resolveStepId('plan:12:coordinator', [completed, later]), null, '같은 역할명으로 다른 그룹에 추측 연결하지 않는다');
  const events = [ev(1, 'operator_message', { message_id: 7, target_node: 'plan:10:coordinator' }), ev(2, 'operator_applied', { message_ids: [7], applied_to_role: 'coordinator' })];
  assert.equal(stepThread(events, completed.id, completed.aliases).length, 2);
  assert.equal(stepThread(events, later.id, later.aliases).length, 0);
  const html = renderToStaticMarkup(createElement(DetailPanel, { target: { kind: 'node', nodeId: 'plan:10:coordinator' }, data: { ...d, flow: { nodes: [completed, later], edges: [] } }, handoff: null, onClose() {} }));
  assert.ok(html.includes('이 실행의 완료 결론'));
  assert.ok(!html.includes('이 단계는 최신 기록에 없습니다.'));
});

test('표시할 대상 카드가 없는 지시는 공통 대화에서라도 보존한다', () => {
  const events = [ev(1, 'operator_message', { message_id: 7, target_node: 'plan:10:coordinator' })];
  assert.deepEqual(conversationEvents(events, new Set(), new Set(['run:999'])), events);
});

test('반영 배열의 모든 메시지가 카드에 있을 때만 공통 대화에서 중복을 제외한다', () => {
  const applied = ev(1, 'operator_applied', { message_ids: [7, 8] });
  assert.deepEqual(conversationEvents([applied], new Set([7, 8])), []);
  assert.deepEqual(conversationEvents([applied], new Set([7])), [applied], '공통 대화에 남은 다른 메시지의 적용 기록은 숨기지 않는다');
});

test('재수집된 근거의 첫 행 상세는 자기 원문만 표시한다', () => {
  const d = fixture(990); const n = d.flow.nodes.find(x => x.type === 'evidence')!;
  assert.equal(n.card?.kind, 'evidence'); if (n.card?.kind !== 'evidence') return;
  const origin = d.raw.find(r => r.ref === 'E1_deployment')!;
  const first = { ...n.card.items[0], ref: 'E1_deployment', raw_items: [{ ...origin, id: 'evidence:1:E1_deployment#0', raw: '{"ready_replicas": 0}' }] };
  const second = { ...first, id: first.id + 1, raw_items: [{ ...origin, id: 'evidence:2:E1_deployment#0', raw: '{"ready_replicas": 1}' }] };
  const data = { ...d, raw: [{ ...origin, raw: '{"ready_replicas": 1}' }], flow: { nodes: [{ ...n, card: { ...n.card, items: [first, second] } }], edges: [] } };
  const render = (index: number) => renderToStaticMarkup(createElement(DetailPanel, { target: { kind: 'evidence', nodeId: n.id, ref: 'E1_deployment', index }, data, handoff: null, onClose() {} }));
  assert.ok(render(0).includes('&quot;ready_replicas&quot;: 0'));
  assert.ok(!render(0).includes('&quot;ready_replicas&quot;: 1'));
  assert.ok(render(1).includes('&quot;ready_replicas&quot;: 1'));
  first.raw_items = [];
  assert.ok(!render(0).includes('&quot;ready_replicas&quot;: 1'), '행 원문 없음은 마지막 스냅샷으로 채우지 않는다');
});

test('구형 응답의 동일 출처 두 행은 최신 묶음을 과거 행 원문으로 추측하지 않는다', () => {
  const d = fixture(990); const n = d.flow.nodes.find(x => x.type === 'evidence')!;
  if (n.card?.kind !== 'evidence') throw new Error('fixture');
  const item = { ...n.card.items[0], ref: 'E1_deployment' };
  const origin = d.raw.find(r => r.ref === 'E1_deployment')!;
  const data = { ...d, raw: [{ ...origin, raw: '{"ready_replicas": 1}' }], flow: { nodes: [{ ...n, card: { ...n.card, items: [item, { ...item, id: item.id + 1 }] } }], edges: [] } };
  const html = renderToStaticMarkup(createElement(DetailPanel, { target: { kind: 'evidence', nodeId: n.id, ref: item.ref, index: 0 }, data, handoff: null, onClose() {} }));
  assert.ok(!html.includes('&quot;ready_replicas&quot;: 1'));
  assert.ok(html.includes('원문은 현재 응답에 없습니다'));
});

const detail = (d: Narrative, nodeId: string) => renderToStaticMarkup(createElement(DetailPanel, { target: { kind: 'node', nodeId }, data: d, handoff: null, onClose() {} }));

test('근거 수집 단계 상세는 수집 항목별 원문을 모두 보여 주고 빈 안내를 띄우지 않는다', () => {
  const d = fixture(990); const n = d.flow.nodes.find(x => x.type === 'evidence')!;
  if (n.card?.kind !== 'evidence') throw new Error('fixture');
  const card = n.card;
  const html = detail(d, n.id);
  assert.ok(!html.includes('이 단계에 연결된 원문 기록이 없습니다'), html.slice(0, 400));
  assert.ok(html.includes(`수집 ${card.total}건`));
  for (const it of card.items) assert.ok(html.includes(it.target.replace(/&/g, '&amp;')), `수집 대상 ${it.target} 누락`);
  const firstRaw = d.raw.find(r => r.ref === card.items.find(it => it.ok)!.ref)!;
  assert.ok(html.includes(firstRaw.id), '수집 항목의 원문 줄이 상세에 연결되어야 한다');
});

test('Group 선택 단계 상세는 판단 과정과 역할 구성·예산을 원문 대신 보여 준다', () => {
  const d = fixture(990); const n = d.flow.nodes.find(x => x.type === 'group')!;
  if (n.card?.kind !== 'group') throw new Error('fixture');
  const html = detail(d, n.id);
  assert.ok(!html.includes('이 단계에 연결된 원문 기록이 없습니다'));
  assert.ok(html.includes('역할 구성'));
  for (const r of n.card.roles) assert.ok(html.includes(r.model ?? '모델 미기록'), `역할 ${r.role} 모델 누락`);
});

test('한글 해석 단계 상세는 해석된 원문과 한글 결과 쌍을 보여 준다', () => {
  const d = fixture(990);
  const interp: FlowNode = { ...d.flow.nodes.find(x => x.type === 'agent')!, id: 'run:77', type: 'interpret', role: 'interpreter', card: null, inputs: [], conclusion_raw: null, conclusion_ko: null };
  const data = { ...d, flow: { nodes: [...d.flow.nodes, interp], edges: [] } };
  const translated = d.raw.filter(r => r.ko_source === 'llm' && r.ko);
  assert.ok(translated.length > 0, 'fixture에 LLM 해석 줄이 필요하다');
  const html = detail(data, interp.id);
  assert.ok(!html.includes('이 단계에 연결된 원문 기록이 없습니다'));
  assert.ok(html.includes(`해석 ${translated.length}건`));
  assert.ok(html.includes(translated[0].id));
});
