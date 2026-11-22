import test from 'node:test';
import assert from 'node:assert/strict';
import type {AgentCard, FlowNode} from '../src/api.ts';
import type {WorkEvent} from '../src/ApplicationWorkTrace.tsx';
import {applicationFlow} from '../src/applicationFlow.ts';
import {semanticFlow} from '../src/semanticFlow.ts';
import {LEGACY_KO, PENDING_KO, readingNote, readingOf, salvage} from '../src/readingText.ts';
import {appEvidence, appSource, composition, k8sEvidence, k8sSource, uniqueQuotes, type AppObservation} from '../src/evidenceGroups.ts';
import {appInvestigationGoals, k8sInvestigationGoals, type AppRoleResult} from '../src/stageGoals.ts';
import {approvalHold, deriveStageBrief, flowStatusTone, type AppSource} from '../src/stageBrief.ts';

// ---------------------------------------------------------------- reading rule
test('a displayable Korean passage is shown as it is', () => {
  assert.deepEqual(readingOf('재고 예약 요청에서 0으로 나누기 예외가 발생했습니다.'), {text: '재고 예약 요청에서 0으로 나누기 예외가 발생했습니다.'});
  assert.equal(salvage('분기가 원인입니다. 근거: source:src/App.java'), '분기가 원인입니다.');
  assert.equal(salvage('found.trim() 호출로 NullPointerException이 발생했습니다. 입력 전용 분기 두 곳이 원인입니다.'), '입력 전용 분기 두 곳이 원인입니다.', 'a sentence naming an exception class stays hidden; the rest remains')
});
test('a passage being summarised reads 정리 중', () => {
  assert.deepEqual(readingOf(null, {state: 'pending'}), {note: PENDING_KO});
  assert.deepEqual(readingOf(null, {state: 'running'}), {note: PENDING_KO});
  assert.deepEqual(readingOf(null, {era: 'current', preparing: true}), {note: PENDING_KO});
});
test('records that predate the summary pipeline read 과거 데이터 없음', () => {
  assert.deepEqual(readingOf(null, {state: 'legacy'}), {note: LEGACY_KO});
  assert.deepEqual(readingOf(null, {era: 'legacy'}), {note: LEGACY_KO});
  assert.deepEqual(readingOf(null), {note: LEGACY_KO}, 'an older server sends neither state nor era');
});
test('a failed or filtered summary of current data is omitted, never shown as a failure', () => {
  for (const state of ['failed', 'missing', 'unobserved']) assert.equal(readingOf(null, {state}), null, state);
  assert.equal(readingOf('java.lang.IllegalStateException: stock', {state: 'ready'}), null);
  assert.equal(readingOf(null, {era: 'current'}), null);
  for (const state of ['pending', 'running', 'failed', 'missing', 'unobserved', 'legacy', undefined, null, 'other']) {
    const note = readingNote(state);
    assert.ok(!note || !/실패|해석 미관측|미기록/.test(note), String(state));
  }
});

// ---------------------------------------------------------------- evidence grouping
test('Kubernetes ids of one source merge; code references and unknown ids stay apart', () => {
  assert.equal(k8sSource('E3').key, 'E3');
  assert.equal(k8sSource('E3_service').key, 'E3');
  assert.equal(k8sSource('E3_service#0').key, 'E3');
  assert.equal(k8sSource('E8_code_refs').group, '코드 위치');
  assert.equal(k8sSource('E8_node').group, '노드 상태');
  assert.equal(k8sSource('E10_investigation').group, '추가 근거 탐색');
  assert.equal(k8sSource('R2').group, '참고 문서');
  assert.equal(k8sSource('made-up').group, '수집되지 않은 근거');
});
test('repeated and contained excerpts are dropped', () => {
  assert.deepEqual(uniqueQuotes(['a b', 'a  b', 'x a b y', 'z', '']), ['x a b y', 'z']);
});
const observations: AppObservation[] = [{id: 'E1-inventory', source: 'pod_log', service: 'inventory', at: 1000, ko: '재고 예약 중 0으로 나누기 예외', state: 'ready'},
  {id: 'E2-inventory', source: 'rollout', service: 'inventory', at: 2000, ko: null, state: 'legacy'}];
test('Application citations of one file merge by path; source code never gets a model summary', () => {
  const s = appEvidence([
    {ref: 'source:src/main/java/App.java', quote: 'if (q == 7) return 1/0;', role: '원인 분석'},
    {ref: 'source:src/main/java/App.java', quote: 'if (q == 7) return 1/0;', role: '심층 원인 추론'},
    {ref: 'file_sha256:src/main/java/App.java', quote: 'return item.trim();', role: '원인 분석'},
    {ref: 'E1-inventory', quote: 'ArithmeticException', role: '원인 분석'},
    {ref: 'E2-inventory', quote: 'rollout 41', role: '원인 분석'},
    {ref: 'original:signal', quote: 'HTTP 500', role: '원인 분석'},
    {ref: 'weird-ref', quote: 'x', role: '원인 분석'},
  ], observations);
  const code = s.groups.find(g => g.label === '소스 코드')!;
  assert.equal(code.rows.length, 1);
  assert.deepEqual([code.rows[0].label, code.rows[0].count, code.rows[0].detail], ['App.java', 3, 'src/main/java/App.java']);
  assert.deepEqual(code.rows[0].roles, ['원인 분석', '심층 원인 추론']);
  assert.equal(code.rows[0].quotes.length, 2);
  assert.ok(code.rows[0].fingerprint);
  assert.equal(code.rows[0].summary, null);
  const log = s.groups.find(g => g.label === '애플리케이션 로그')!;
  assert.deepEqual([log.rows[0].label, log.rows[0].summary], ['inventory · 애플리케이션 로그', {text: '재고 예약 중 0으로 나누기 예외'}]);
  assert.deepEqual(s.groups.find(g => g.label === '배포 변경')!.rows[0].summary, {note: LEGACY_KO});
  assert.equal(s.groups.at(-1)!.label, '출처 미확인');
  assert.equal(s.refs, 5);
  assert.equal(s.citations, 7);
  assert.equal(s.groups[0].label, '소스 코드', 'the most cited group comes first');
  for (const part of ['코드 1', '로그 1', '배포 1', '최초 신호 1']) assert.ok(composition(s).includes(part), part);
  assert.equal(appSource('followup:abc', []).group, '추가 근거 탐색');
});
test('Kubernetes quotes merge by source with their roles, count and Korean summary', () => {
  const card = (quotes: AgentCard['quotes'], extra: Partial<AgentCard> = {}) => ({kind: 'agent', candidates: [], quotes, ...extra}) as unknown as AgentCard;
  const q = (ref: string, quote: string, known = true) => ({ref, ref_ko: null, ref_known: known, quote, supports_raw: '', candidate_index: null});
  const s = k8sEvidence([
    {role: '원인 추론', card: card([q('E7_error_logs', 'OOMKilled'), q('E7_error_logs#0', 'OOMKilled'), q('E3_service', 'no endpoints')], {reading_era: 'current'} as Partial<AgentCard>)},
    {role: '반론 검토', card: card([q('E7', 'exit 137'), q('E99_nope', '?', false)], {reading_era: 'current'} as Partial<AgentCard>)},
  ], [{id: 1, source: 'loki', source_ko: '로그', target: 'x', ok: true, error: null, summary_ko: '메모리 부족으로 종료', ref: 'E7_error_logs', collected_at: '2026-11-21T01:00:00Z'}], []);
  const logs = s.groups.find(g => g.label === '오류 로그')!;
  assert.equal(logs.rows.length, 1);
  assert.deepEqual([logs.rows[0].count, logs.rows[0].roles, logs.rows[0].quotes], [3, ['원인 추론', '반론 검토'], ['OOMKilled', 'exit 137']]);
  assert.deepEqual(logs.rows[0].summary, {text: '메모리 부족으로 종료'});
  assert.equal(s.groups.find(g => g.label === '추가 근거 탐색')!.rows[0].known, false);
});

// ---------------------------------------------------------------- goal check = the backend gate
const role = (r: Partial<AppRoleResult> & {role: string}): AppRoleResult => ({label: r.role, ...r});
test('Application investigation goals follow the worker gate', () => {
  const ok = appInvestigationGoals({roles: [role({role: 'analyst', confidence: .97, unresolved: []}), role({role: 'critic', accepted: true, unresolved: []})],
    running: [], level: 1, errorCode: null, retries: ['citation_not_grounded']});
  assert.deepEqual(ok.map(g => g.state), ['ok', 'ok', 'ok', 'ok']);
  assert.equal(ok[2].detail, '재시도 후 통과');
  const low = appInvestigationGoals({roles: [role({role: 'analyst', confidence: .79})], running: [], level: 1, errorCode: null, retries: []});
  assert.equal(low[0].state, 'fail');
  const gaps = appInvestigationGoals({roles: [role({role: 'analyst', confidence: .9, unresolved: ['요청 수량']})], running: [], level: 1, errorCode: null, retries: []});
  assert.deepEqual([gaps[1].state, gaps[1].detail], ['fail', '부족한 근거 1건']);
  const cited = appInvestigationGoals({roles: [role({role: 'analyst', confidence: .9})], running: [], level: 1, errorCode: 'citation_not_grounded', retries: []});
  assert.equal(cited[2].state, 'fail');
  const level2 = appInvestigationGoals({roles: [role({role: 'analyst', confidence: .9}), role({role: 'critic', accepted: true})], running: ['specialist'], level: 2, errorCode: null, retries: []});
  assert.equal(level2[3].state, 'working', 'level 2 also needs the specialist');
  assert.equal(level2[3].detail, '심층 원인 추론 진행 중', 'only the role actually running is named');
  const rejected = appInvestigationGoals({roles: [role({role: 'analyst', confidence: .9}), role({role: 'critic', accepted: false})], running: [], level: 1, errorCode: null, retries: []});
  assert.equal(rejected[3].state, 'fail');
});
test('Kubernetes investigation goals read rejections and unlinked quotes', () => {
  const run = (role: string, card: Partial<AgentCard>, status = 'done') => ({role, label: role, status, card: {kind: 'agent', candidates: [], ...card} as AgentCard});
  const goals = k8sInvestigationGoals([run('cause_reasoner', {acceptance: {rejected: true, reason_raw: '', reason_ko: '근거 부족'}, quotes: [{ref: 'E1', ref_ko: null, ref_known: false, quote: 'x', supports_raw: '', candidate_index: null}]}),
    run('challenger', {}, 'running')]);
  assert.deepEqual(goals.map(g => g.state), ['fail', 'fail', 'working']);
});

// ---------------------------------------------------------------- brief
const node = (id: string, type: string, status = 'done', extra: Partial<FlowNode> = {}): FlowNode => ({
  id, type, status, label: id, role: null, role_ko: id, job_ko: '', model: null, effort: null, tokens: null, cost_usd: null, duration_ms: null,
  inputs: [], conclusion_ko: null, conclusion_raw: null, lane: 0, depth: 0, group: null, ...extra,
});
test('tones map stage status for the badge', () => {
  assert.deepEqual(['running', 'done', 'failed', 'awaiting_approval', 'skipped'].map(flowStatusTone), ['info', 'ok', 'danger', 'warn', 'muted']);
});
test('an Application investigation brief: conclusion, models, goals, remaining and merged evidence', () => {
  const sequence: [string, WorkEvent['data']][] = [
    ['stage', {}], ['role_started', {role: 'classifier', call_id: 'c0', model: 'gpt-6-luna', effort: 'low'}],
    ['role_completed', {role: 'classifier', call_id: 'c0', model: 'gpt-6-luna', effort: 'low', confidence: .92, level: 1, summary_ko: '코드 결함으로 판단해 분석팀을 배정합니다.'}],
    ['role_started', {role: 'analyst', call_id: 'c1', model: 'gpt-6.1-sol', effort: 'medium'}],
    ['role_completed', {role: 'analyst', call_id: 'c1', model: 'gpt-6.1-sol', effort: 'medium', confidence: .97, unresolved: [], follow_up: ['q'], follow_up_ko: ['수정 후 quantity=7을 다시 시험합니다.'],
      summary_ko: 'AppFaultBusiness의 입력 전용 분기 2곳이 원인입니다.', readings: {summary_ko: {state: 'ready', ko: null}},
      citations: [{ref: 'source:src/App.java', quote: 'return 1/0;'}, {ref: 'source:src/App.java', quote: 'return null.trim();'}, {ref: 'E1-inventory', quote: 'ArithmeticException'}]}],
    ['role_started', {role: 'critic', call_id: 'c2', model: 'gpt-6.2-astra', effort: 'high'}],
    ['role_completed', {role: 'critic', call_id: 'c2', model: 'gpt-6.2-astra', effort: 'high', accepted: true, unresolved: [], confidence: .9, summary_ko: '반대 근거가 없습니다.'}],
  ];
  const events = sequence.map(([event, data], index) => ({id: index + 1, at: 100 + index, event, data}));
  const flow = applicationFlow({id: 'inventory', state: 'patching', stages: [{state: 'analysis_queued', occurred_at: 100}, {state: 'analyzing', occurred_at: 101}, {state: 'patching', occurred_at: 120}],
    cursor: events.length, has_more: false, events}, {id: 'inventory', created_at: 100});
  const {nodes, edges} = semanticFlow(flow);
  const investigation = nodes.find(n => n.semantic.kind === 'investigation')!;
  const ids = new Set(investigation.semantic.members.flatMap(m => 'app' in m ? (m as unknown as {app: {eventIds: number[]}}).app.eventIds : []));
  const app: AppSource = {doc: {id: 'inventory', state: 'patching', environment: 'dev', service: 'inventory', created_at: 100, data: {analysis_routing: {effective_level: 1}}},
    saved: {}, events: events.filter(e => ids.has(e.id)), roundEvents: events, observations, summary: {}};
  const brief = deriveStageBrief({node: investigation, nodes, edges, app});
  assert.equal(brief.lead, 'AppFaultBusiness의 입력 전용 분기 2곳이 원인입니다.');
  assert.deepEqual(brief.models, ['gpt-6.1-sol · 중간', 'gpt-6.2-astra · 높음']);
  assert.deepEqual(brief.goals.map(g => g.state), ['ok', 'ok', 'ok', 'ok']);
  assert.deepEqual(brief.remaining, ['수정 후 quantity=7을 다시 시험합니다.']);
  assert.equal(brief.evidence?.refs, 2);
  assert.equal(brief.evidence?.groups[0].rows[0].count, 2);
  assert.equal(brief.calls.length, 2);
  assert.ok(brief.lineage.some(l => l.label === '대응 경로 결정' && l.text.includes('코드 결함')), JSON.stringify(brief.lineage));
});
test('a Kubernetes investigation brief takes the strongest candidate and counts its evidence', () => {
  const card = {kind: 'agent', agent_run_id: 1, summary_raw: null, summary_ko: '노드 메모리 압박으로 재시작이 반복됩니다.', reading_era: 'current',
    candidates: [{cause_raw: 'memory', cause_ko: '메모리 한도가 낮아 OOM 종료가 반복됩니다.', confidence: .86}, {cause_raw: 'probe', cause_ko: '헬스 체크가 너무 짧습니다.', confidence: .4}],
    quotes: [{ref: 'E7_error_logs', ref_ko: '오류 로그', ref_known: true, quote: 'OOMKilled', supports_raw: '', candidate_index: 0}],
    counter_evidence: [{text_raw: 'x', text_ko: '최근 배포 변경은 없었습니다.'}], verdict: null, verdict_ko: null, next_action: 'act', next_action_ko: '메모리 한도를 올립니다.',
    counter_evidence_count: 1, error: null, accepts_steering: false, steer_target_role: ''} as unknown as AgentCard;
  const {nodes, edges} = semanticFlow({nodes: [node('a1', 'agent', 'done', {role: 'cause_reasoner', role_ko: '원인 추론', model: 'gpt-6.2-sol', effort: 'medium', agent_run_id: 1, card})], edges: []});
  const data = {incident: {id: 1}, flow: {nodes: [], edges: []}, raw: [], headline: {impact: ''}, interpreting: false} as never;
  const brief = deriveStageBrief({node: nodes[0], nodes, edges, core: {data, preparing: false}});
  assert.equal(brief.lead, '메모리 한도가 낮아 OOM 종료가 반복됩니다.');
  assert.deepEqual(brief.models, ['gpt-6.2-sol · 중간']);
  assert.deepEqual(brief.remaining, ['최근 배포 변경은 없었습니다.', '메모리 한도를 올립니다.']);
  assert.equal(brief.composition, '로그 1');
});
test('the approval hold explains why production approval waits', () => {
  assert.equal(approvalHold({state: 'awaiting_prd_approval', data: {}}, {}), '사람의 PR 병합을 검증할 연결 정보가 없어 prd 승인이 보류됐습니다.');
  assert.equal(approvalHold({state: 'x', data: {}}, {approval: {subject: 'kim'}}), 'prd 승인이 기록됐습니다.');
});
