// 단계 카드 브리핑: 머리(톤·모델), 이어받음, 진행 중, 첫 줄, 사실, 목표, 남은 것, 판단 기록, 근거를 한 곳에서 계산한다.
// pure, no React — 화면은 이 결과만 그린다.
import type { AgentCard, EvidenceItem, FlowEdge, FlowNode, Narrative } from './api.ts';
import type { WorkEvent } from './ApplicationWorkTrace.tsx';
import { isAppStep } from './applicationFlow.ts';
import { appEvidence, composition, k8sEvidence, type AppObservation, type EvidenceSummary } from './evidenceGroups.ts';
import { effortKo } from './labels.ts';
import { readingOf, type ReadingEra, type ReadingView } from './readingText.ts';
import type { SemanticKind, SemanticNode } from './semanticFlow.ts';
import { appInvestigationGoals, appVerificationGoals, k8sInvestigationGoals, k8sVerificationGoals, type AppRoleResult, type Goal } from './stageGoals.ts';
import { evidenceQuestion, chronological } from './stageEvidence.ts';
import { testComparison } from './stageTests.ts';
import { matchingFingerprint, object, observedTime, releaseProof, word, type Data } from './unified.ts';

export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'muted';
export type Fact = { label: string; value: string };
export type CallRow = { id: string; origin: 'core' | 'app'; ref: string; label: string; model: string | null; at: string | null; status: string; summary: ReadingView; running: boolean };
export type Lineage = { source: string; label: string; text: string };
export type StageBrief = {
  tone: Tone; models: string[]; lineage: Lineage[]; lineageMore: number; working: string | null;
  lead: string; facts: Fact[]; goals: Goal[]; remaining: string[]; calls: CallRow[]; evidence: EvidenceSummary | null; composition: string | null;
};

/** The application half of a stage, resolved by the caller from `?view=stage` and the activity stream. */
export type AppSource = {
  doc: { id: string; state: string; environment: string; service: string; created_at: number; data: Data };
  /** the selected round's saved state (current data or an archived round) */
  saved: Data;
  /** events of the selected round that belong to this stage */
  events: WorkEvent[];
  /** every event of the selected round (routing reads the whole team) */
  roundEvents: WorkEvent[];
  observations: AppObservation[];
  summary: { reason?: string | null; reason_ko?: string | null; created_at?: number | null };
};
export type CoreSource = { data: Narrative; preparing: boolean };
export type BriefInput = { node: SemanticNode; nodes: SemanticNode[]; edges: FlowEdge[]; core?: CoreSource; app?: AppSource; actors?: string[] };

const TONES: Record<string, Tone> = {
  running: 'info', queued: 'info', done: 'ok', reported: 'ok', manual_record: 'ok', failed: 'danger', escalated: 'warn',
};
export const flowStatusTone = (status: string): Tone => TONES[status] ?? (status.startsWith('awaiting_') ? 'warn' : 'muted');

export const APP_ROLES: Record<string, string> = { classifier: '사건 수준 판단', analyst: '원인 분석', specialist: '심층 원인 추론', critic: '반론 검토', verifier: '독립 검증', patch: '코드 수정' };
const PHASES: Record<string, string> = { started: '모델 호출 시작', waiting: '응답 대기', streaming: '공개 답변 수신', succeeded: '응답 수신 완료', failed: '호출 실패',
  inspect_resources: '대상 리소스 확인', read_logs: '장애 로그 조회', pin_source: '배포 이미지와 소스 대조', read_file: '소스 위치 확인', reproduce: '변경 전 장애 재현',
  edit_file: '파일 변경 적용', regression: '변경 후 회귀 시험', build: '시험한 소스 빌드', test_journey: 'dev 사용자 여정 시험' };
const MODEL_CALL = new Set(['done', 'failed', 'escalated', 'running']);
const pct = (v: number) => Math.round(v * 100) + '%';
const oneLine = (text: string, max = 90) => { const t = text.replace(/\s+/g, ' ').trim(); return t.length > max ? t.slice(0, max - 1) + '…' : t; };
/** Content only: a state note (정리 중 · 과거 데이터 없음) is never a conclusion, fact or remaining item. */
const text = (view: ReadingView) => view && 'text' in view ? view.text : null;
const noteOf = (view: ReadingView) => view && 'note' in view ? view.note : null;
const tag = (model: unknown, effort: unknown) => word(model) ? `${model} · ${effortKo(typeof effort === 'string' ? effort : null)}` : null;
const appRole = (role?: string) => APP_ROLES[role ?? ''] ?? '조사 역할';
const coreMembers = (node: SemanticNode) => node.semantic.members.filter(n => !isAppStep(n));
const agentRuns = (node: SemanticNode) => coreMembers(node).filter((n): n is FlowNode & { card: AgentCard } => n.card?.kind === 'agent');
const eraOf = (card: AgentCard | undefined) => (card as (AgentCard & { reading_era?: ReadingEra }) | undefined)?.reading_era;
/** The events of one stage in the selected round (lineage reads other stages of the same round). */
const nodeEvents = (node: SemanticNode, app: AppSource) => {
  const ids = new Set(node.semantic.members.filter(isAppStep).flatMap(m => m.app.eventIds));
  return app.roundEvents.filter(e => ids.has(e.id));
};
const appReading = (value: unknown, readings: WorkEvent['data']['readings'], key: string) => readingOf(value, { state: readings?.[key]?.state ?? (readings ? 'ready' : undefined) });

function models(node: SemanticNode, app?: AppSource): string[] {
  const out = new Set<string>();
  for (const n of coreMembers(node)) {
    if ((n.agent_run_id != null || n.call_id || n.model) && MODEL_CALL.has(n.status)) { const t = tag(n.model, n.effort); if (t) out.add(t); }
  }
  for (const e of app ? nodeEvents(node, app) : []) if ((e.event === 'role_started' || e.event === 'role_completed') && e.data.model) { const t = tag(e.data.model, e.data.effort); if (t) out.add(t); }
  return [...out];
}

function working(actors: string[] | undefined, app?: AppSource): string | null {
  if (!actors?.length) return null;
  const progress = [...(app?.events ?? [])].reverse().find(e => e.event === 'role_progress' || e.event === 'runner_progress');
  const phase = progress?.data.phase ? PHASES[progress.data.phase] : null;
  return [actors.join(' · '), phase].filter(Boolean).join(' · ');
}

// ------------------------------------------------------------------ Kubernetes half
function coreBrief(node: SemanticNode, { data, preparing }: CoreSource) {
  const kind = node.semantic.kind, members = coreMembers(node), runs = agentRuns(node);
  const items = data.flow.nodes.flatMap(n => n.card?.kind === 'evidence' ? n.card.items : []) as EvidenceItem[];
  const facts: Fact[] = [], remaining: string[] = [];
  let lead: string | null = null, note: string | null = null, goals: Goal[] = [], evidence: EvidenceSummary | null = null;
  const view = (value: unknown, card?: AgentCard) => readingOf(value, { era: eraOf(card) ?? (card ? undefined : 'current'), preparing });
  if (kind === 'detection') {
    const card = members.find(n => n.card?.kind === 'signal')?.card;
    if (card?.kind === 'signal') {
      lead = text(readingOf(card.what_ko, { era: 'current' })) ?? null;
      facts.push({ label: '최초 감지', value: [card.first_at ? new Date(card.first_at).toLocaleString('ko-KR') : '시각 미관측', card.place_ko].filter(Boolean).join(' · ') });
      facts.push({ label: '신호 출처', value: (text(readingOf(card.source_ko, { era: 'current' })) ?? '출처 미관측') + ' · ' + (card.signal_count ?? '미관측') + '건' });
      const impact = text(readingOf(data.headline.impact, { era: 'current' }));
      facts.push({ label: '사용자 영향', value: impact ?? '영향 미관측' });
      goals = [{ key: 'signal', label: '장애 신호 확인', state: 'ok', detail: null }, { key: 'impact', label: '사용자 영향 범위', state: impact ? 'ok' : 'open', detail: null }];
    }
  } else if (kind === 'evidence') {
    const card = members.find(n => n.card?.kind === 'evidence')?.card;
    if (card?.kind === 'evidence') {
      const all = chronological(card.items);
      const tags = [...new Set(all.map(evidenceQuestion))];
      const changes = tags.map(t => {
        const rows = all.filter(i => evidenceQuestion(i) === t), first = rows[0], last = rows[rows.length - 1];
        const describe = (i: EvidenceItem) => i.ok ? text(readingOf(i.summary_ko, { era: 'current' })) ?? '수집 성공' : '수집 실패';
        return { tag: t, changed: last.id !== first.id && describe(first) !== describe(last), first: describe(first), last: describe(last), count: rows.length };
      }).sort((a, b) => Number(b.changed) - Number(a.changed) || b.count - a.count);
      const top = changes.find(c => c.changed) ?? changes[0];
      lead = top ? (top.changed ? `${top.tag}: ${oneLine(top.first, 40)} → ${oneLine(top.last, 40)}` : `${top.tag}: ${oneLine(top.last, 60)}`) : null;
      for (const c of changes.slice(0, 4)) facts.push({ label: c.tag, value: c.changed ? `${oneLine(c.first, 40)} → ${oneLine(c.last, 40)}` : `${oneLine(c.last, 60)} · ${c.count}건` });
      facts.push({ label: '수집', value: `수집 ${card.total}건 · 성공 ${card.ok}건 · 실패 ${card.failed}건` });
      for (const i of card.items.filter(i => !i.ok)) remaining.push(`${i.source_ko || evidenceQuestion(i)} 수집 실패`);
    }
  } else if (kind === 'routing') {
    const routing = [...members].reverse().find(n => n.card?.kind === 'routing')?.card;
    const group = members.find(n => n.card?.kind === 'group')?.card;
    if (routing?.kind === 'routing') {
      lead = text(readingOf(routing.final_reason_ko, { era: 'current', preparing: routing.interpreting }));
      facts.push({ label: '선택한 수준', value: routing.level_ko + ' · ' + routing.decided_by_ko });
      if (typeof routing.confidence === 'number') facts.push({ label: '분류 신뢰도', value: pct(routing.confidence) });
    } else if (group?.kind === 'group') {
      lead = text(readingOf(group.decision.final_reason_ko, { era: 'current' }));
      facts.push({ label: '선택한 수준', value: group.decision.level_ko + ' · ' + group.decision.decided_by_ko });
    }
  } else if (kind === 'investigation') {
    const candidates = runs.flatMap(n => n.card.candidates.map(c => ({ n, c }))).sort((a, b) => (b.c.confidence ?? -1) - (a.c.confidence ?? -1));
    const best = candidates[0];
    const summary = [...runs].reverse().map(n => text(view(n.card.summary_ko ?? n.conclusion_ko, n.card))).find(Boolean);
    lead = (best && text(view(best.c.cause_ko, best.n.card))) ?? summary ?? null;
    note = best ? noteOf(view(best.c.cause_ko, best.n.card)) : runs.length ? noteOf(view(runs.at(-1)!.card.summary_ko, runs.at(-1)!.card)) : null;
    if (best && typeof best.c.confidence === 'number') facts.push({ label: '후보 신뢰도', value: pct(best.c.confidence) + (candidates.length > 1 ? ` · 후보 ${candidates.length}개` : '') });
    goals = k8sInvestigationGoals(runs.map(n => ({ role: n.role, label: n.role_ko || n.label, status: n.status, card: n.card })));
    for (const n of runs) {
      for (const c of n.card.counter_evidence ?? []) { const t = text(view(c.text_ko, n.card)); if (t && !remaining.includes(t)) remaining.push(t); }
      const next = text(readingOf(n.card.next_action_ko, { era: 'current' }));
      if (next && n.card.next_action && n.card.next_action !== 'none' && !remaining.includes(next)) remaining.push(next);
    }
    evidence = k8sEvidence(runs.map(n => ({ role: n.role_ko || n.label, card: n.card })), items, data.raw, preparing);
  } else if (kind === 'verification') {
    const verifier = [...runs].reverse().find(n => n.type === 'verify' || n.card.verdict) ?? runs.at(-1);
    if (verifier) {
      const reason = text(view(verifier.card.acceptance?.reason_ko ?? verifier.card.summary_ko ?? verifier.conclusion_ko, verifier.card));
      const verdict = verifier.card.verdict_ko ?? (verifier.card.verdict === 'supported' ? '근거로 지지' : null);
      lead = [verdict, reason].filter(Boolean).join(' — ') || null;
      note = noteOf(view(verifier.card.acceptance?.reason_ko ?? verifier.card.summary_ko ?? verifier.conclusion_ko, verifier.card));
      const counts = new Map<string, number>();
      for (const v of verifier.card.candidate_verdicts ?? []) { const k = v.verdict_ko || v.verdict || '판정 미기록'; counts.set(k, (counts.get(k) ?? 0) + 1); }
      if (counts.size) facts.push({ label: '후보별 판정', value: [...counts].map(([k, n]) => `${k} ${n}`).join(' · ') });
      goals = k8sVerificationGoals(verifier.card, verifier.status === 'running');
      evidence = k8sEvidence([{ role: verifier.role_ko || verifier.label, card: verifier.card }], items, data.raw, preparing);
    }
  } else if (kind === 'action') {
    const action = members.find(n => n.card?.kind === 'action')?.card;
    if (action?.kind === 'action') {
      lead = `${action.type_ko} · ${action.target}`;
      facts.push({ label: '진행', value: action.status_ko });
      const changes = action.changes.map(c => text(readingOf(c, { era: 'current' }))).filter((c): c is string => !!c);
      if (changes.length) facts.push({ label: '변경', value: changes.join(' / ') });
    }
    const proposed = runs.map(n => n.card.proposed_action).find(Boolean);
    if (proposed) {
      lead ??= `${proposed.type_ko} · ${proposed.target}`;
      const add = (label: string, v: string | null) => { const t = text(readingOf(v, { era: 'current' })); if (t) facts.push({ label, value: t }); };
      add('영향 범위', proposed.impact_ko); add('성공 조건', proposed.success_condition_ko); add('원복 조건', proposed.rollback_condition_ko);
    }
  } else if (kind === 'recovery') {
    const r = data.accountability;
    lead = data.incident.recovery_kind === 'observed' || r?.recovery_observed_at ? '원래 리소스의 정상화 관측' : '원래 장애 신호의 정상화 미확인';
    facts.push({ label: '대상', value: `${data.incident.namespace} / ${data.incident.workload}` });
    if (r?.recovery_observed_at) facts.push({ label: '관측 시각', value: new Date(r.recovery_observed_at).toLocaleString('ko-KR') });
    const why = text(readingOf(r?.recovery_ko, { era: 'current' })); if (why) facts.push({ label: '정상화된 이유', value: why });
  } else if (kind === 'result') {
    const r = data.accountability;
    const outcome = members.find(n => n.card?.kind === 'outcome')?.card;
    if (r && r.status !== 'not_applicable') {
      lead = r.status === 'pending_operator' ? '운영자 소명 확인 대기' : r.decision_at ? '소명 확인 · 정상 확정' : '결과 소명 기록';
      for (const v of (Array.isArray(r.limits_ko) ? r.limits_ko : r.limits_ko ? [r.limits_ko] : [])) { const t = text(readingOf(v, { era: 'current' })); if (t) remaining.push(t); }
    } else lead = outcome?.kind === 'outcome' ? outcome.status_ko : null;
  } else if (kind === 'handoff') {
    const outcome = members.find(n => n.card?.kind === 'outcome')?.card;
    const handoff = (outcome?.kind === 'outcome' ? outcome.handoff : null) ?? data.handoff_view ?? null;
    lead = text(readingOf(handoff?.decisions.find(d => d.key === 'operator')?.value_ko ?? handoff?.next_steps_ko[0], { era: 'current' })) ?? '운영자 인수와 판단이 필요합니다.';
    const why = text(readingOf(handoff?.summary_ko, { era: 'current', preparing: handoff?.interpreting })); if (why) facts.push({ label: '멈춘 이유', value: why });
    for (const s of handoff?.next_steps_ko.slice(1) ?? []) { const t = text(readingOf(s, { era: 'current' })); if (t) remaining.push(t); }
  } else if (kind === 'approval') {
    const action = members.find(n => n.card?.kind === 'action');
    lead = `${action?.label ?? '조치 승인'} · 운영자 결정 확인`;
  }
  const calls: CallRow[] = members.filter(n => ['agent', 'verify', 'router', 'investigation'].includes(n.type) && (n.agent_run_id != null || n.model || n.status === 'running'))
    .map(n => ({ id: n.call_id ? 'call:' + n.call_id : n.id, origin: 'core' as const, ref: n.id, label: n.role_ko || n.label, model: tag(n.model, n.effort), at: n.at ?? null, status: n.status,
      summary: n.card?.kind === 'agent' ? view(n.card.summary_ko ?? n.conclusion_ko, n.card) : readingOf(n.conclusion_ko, { era: 'current' }), running: n.status === 'running' }));
  return { lead, note, facts, goals, remaining, evidence, calls };
}

// ------------------------------------------------------------------ Application half
function appBrief(node: SemanticNode, app: AppSource) {
  const kind = node.semantic.kind, { doc, saved, roundEvents, observations, summary } = app;
  const events = nodeEvents(node, app);
  const facts: Fact[] = [], remaining: string[] = [];
  let lead: string | null = null, note: string | null = null, goals: Goal[] = [], evidence: EvidenceSummary | null = null;
  const roles = events.filter(e => e.event === 'role_completed');
  const done = new Set(roles.map(e => e.data.call_id));
  // A started call without a result is live only while the stage runs and it is the latest call of its role.
  const open = events.filter(e => e.event === 'role_started' && !done.has(e.data.call_id));
  const live = node.status === 'running' ? open.filter(e => !events.some(o => o.event === 'role_started' && o.data.role === e.data.role && o.id > e.id)) : [];
  const runningRoles = live.map(e => e.data.role ?? '');
  const result = (e: WorkEvent): AppRoleResult => ({ role: e.data.role ?? '', label: appRole(e.data.role), confidence: e.data.confidence, unresolved: e.data.unresolved, accepted: e.data.accepted, fixable: e.data.fixable });
  const summaryOf = (e: WorkEvent) => appReading(e.data.reason_ko || e.data.summary_ko, e.data.readings, 'summary_ko');
  const patch = object(saved.patch), release = object(saved.release), build = object(saved.test_build);
  if (kind === 'detection') {
    lead = text(readingOf(summary.reason_ko ?? summary.reason, { state: 'ready' })) ?? null;
    facts.push({ label: '최초 감지', value: `${observedTime(summary.created_at ?? doc.created_at)} · ${doc.environment} / ${doc.service}` });
    const latest = observations.filter(o => o.at != null).at(-1);
    if (latest) facts.push({ label: '최근 관측', value: (text(readingOf(latest.ko, { state: latest.state })) ?? '관측 기록') + ' · ' + new Date(latest.at!).toLocaleString('ko-KR') });
    goals = [{ key: 'signal', label: '장애 신호 확인', state: 'ok', detail: null }, { key: 'impact', label: '사용자 영향 범위', state: 'open', detail: null }];
  } else if (kind === 'evidence') {
    const groups = new Map<string, AppObservation[]>();
    for (const o of observations) groups.set(o.source, [...(groups.get(o.source) ?? []), o]);
    const label: Record<string, string> = { pod_log: '애플리케이션 로그', k8s_event: '리소스 이벤트', deployment: 'Pod 준비', rollout: '배포 변경' };
    const changes = [...groups].map(([source, rows]) => {
      const first = rows[0], last = rows[rows.length - 1];
      const describe = (o: AppObservation) => text(readingOf(o.ko, { state: o.state })) ?? '관측 기록';
      return { tag: label[source] ?? '기타 관측', changed: rows.length > 1 && describe(first) !== describe(last), first: describe(first), last: describe(last), count: rows.length };
    }).sort((a, b) => Number(b.changed) - Number(a.changed) || b.count - a.count);
    const top = changes.find(c => c.changed) ?? changes[0];
    lead = top ? (top.changed ? `${top.tag}: ${oneLine(top.first, 40)} → ${oneLine(top.last, 40)}` : `${top.tag}: ${oneLine(top.last, 60)}`) : null;
    for (const c of changes.slice(0, 4)) facts.push({ label: c.tag, value: c.changed ? `${oneLine(c.first, 40)} → ${oneLine(c.last, 40)}` : `${oneLine(c.last, 60)} · ${c.count}건` });
    facts.push({ label: '수집', value: `수집 결과 ${observations.length}건` });
  } else if (kind === 'routing') {
    const classifier = roles.filter(e => e.data.role === 'classifier').at(-1);
    lead = classifier ? text(summaryOf(classifier)) : null;
    const level = object(doc.data.analysis_routing).effective_level ?? classifier?.data.level;
    if (level != null) facts.push({ label: '선택한 수준', value: String(level) });
    if (typeof classifier?.data.confidence === 'number') facts.push({ label: '분류 신뢰도', value: pct(classifier.data.confidence) });
    const team = [...new Map(roundEvents.filter(e => e.event === 'role_started' && e.data.role && e.data.role !== 'classifier').map(e => [e.data.role, e])).values()];
    if (team.length) facts.push({ label: '배정한 팀', value: team.map(e => appRole(e.data.role) + (e.data.model ? ` (${e.data.model})` : '')).join(' → ') });
  } else if (kind === 'investigation') {
    const analysis = roles.filter(e => e.data.role === 'specialist' || e.data.role === 'analyst');
    const main = analysis.filter(e => e.data.role === 'specialist').at(-1) ?? analysis.at(-1);
    lead = main ? text(summaryOf(main)) : null;
    note = main ? noteOf(summaryOf(main)) : null;
    if (typeof main?.data.confidence === 'number') facts.push({ label: '판단 신뢰도', value: `${pct(main.data.confidence)} · ${appRole(main.data.role)}` });
    const level = Number(object(doc.data.analysis_routing).effective_level ?? NaN);
    const retries = roundEvents.filter(e => e.event === 'analysis_retry').map(e => String((e.data as { reason?: string }).reason ?? ''));
    const errorCode = word(object(saved.error ?? doc.data.error).code);
    goals = appInvestigationGoals({ roles: roles.map(result), running: runningRoles, level: Number.isFinite(level) ? level : null, errorCode, retries });
    for (const e of roles.filter(e => ['analyst', 'specialist', 'critic'].includes(e.data.role ?? ''))) {
      (e.data.follow_up ?? []).forEach((_, i) => { const t = text(appReading(e.data.follow_up_ko?.[i], e.data.readings, 'follow_up/' + i)); if (t && !remaining.includes(t)) remaining.push(t); });
      (e.data.unresolved ?? []).forEach((_, i) => { const t = text(appReading(e.data.unresolved_ko?.[i], e.data.readings, 'unresolved/' + i)); if (t && !remaining.includes(t)) remaining.push('부족한 근거 · ' + t); });
    }
    evidence = appEvidence(roles.flatMap(e => (e.data.citations ?? []).map(c => ({ ref: c.ref, quote: c.quote, role: appRole(e.data.role) }))), observations);
  } else if (kind === 'verification') {
    const verifier = roles.filter(e => e.data.role === 'verifier').at(-1);
    if (verifier) {
      const reason = text(summaryOf(verifier));
      lead = [verifier.data.accepted === true ? '원인 근거 수락' : verifier.data.accepted === false ? '원인 근거 반려' : null, reason].filter(Boolean).join(' — ') || null;
      note = noteOf(summaryOf(verifier));
      evidence = appEvidence((verifier.data.citations ?? []).map(c => ({ ref: c.ref, quote: c.quote, role: appRole('verifier') })), observations);
    }
    goals = appVerificationGoals(verifier ? result(verifier) : undefined, runningRoles.includes('verifier'));
  } else if (kind === 'action') {
    const repair = node.semantic.members.some(m => isAppStep(m) && (m.role === 'patch' || ['patching', 'patch_proposed'].includes(m.app.state) || !!m.app.fileEventIds?.length));
    const files = Array.isArray(patch.changed_files) ? patch.changed_files.filter((p): p is string => typeof p === 'string') : [];
    if (repair) {
      lead = text(readingOf(patch.summary_ko || saved.patch_summary_ko || roles.at(-1)?.data.summary_ko, { state: 'ready' }));
      if (files.length) facts.push({ label: '바뀐 파일', value: files.length + '개' });
      const risk = text(readingOf(patch.risk_ko || release.risk_ko, { state: 'ready' })); if (risk) facts.push({ label: '영향 범위', value: risk });
    } else lead = `${node.semantic.members.filter(isAppStep).at(-1)?.label ?? node.label} · ${doc.service}`;
    const rollback = text(readingOf(release.rollback_ko, { state: 'ready' })); if (rollback) facts.push({ label: '원복 조건', value: rollback });
  } else if (kind === 'test_build') {
    const t = testComparison(patch, release, build, doc.service);
    const fixed = t.rows.filter(r => r.before !== '통과' && r.after === '통과');
    lead = t.rows.length ? (fixed.length ? `수정 전 실패한 시험 ${fixed.length}개가 수정 후 통과` : `동일 시험 ${t.rows.length}개 비교`) : '수정 전후 동일 시험의 결과 미관측';
    for (const r of t.rows.slice(0, 4)) facts.push({ label: r.label, value: `수정 전 ${r.before} → 수정 후 ${r.after}` });
    if (t.total != null && t.failed != null) facts.push({ label: '전체 시험', value: t.passed != null ? `${t.total}개 중 ${t.passed}개 통과` : `${t.total}개 실행 · 실패 ${t.failed}개` });
    if (t.added != null) facts.push({ label: '추가한 재현 시험', value: t.added + '개' });
    facts.push({ label: '빌드', value: build.accepted === true || word(object(release.artifact).build_receipt_sha256) ? '산출물 확인' : build.accepted === false ? '산출물 검증 실패' : '산출물 미관측' });
  } else if (kind === 'dev_validation') {
    const tested = object(release.user_test), qa = object(release.qa);
    const manual = tested.passed === true ? '수동 확인 통과' : tested.passed === false ? '수동 확인 실패' : '수동 확인 미기록';
    const auto = qa.outcome === 'passed' || qa.accepted === true ? '자동 QA 통과' : qa.outcome === 'failed' || qa.accepted === false ? '자동 QA 실패' : '자동 사용자 여정 QA 미관측';
    const deployed = object(object(release.stage_results).dev).verified === true ? 'dev 배포 확인됨' : 'dev 배포 미관측';
    lead = [deployed, manual, auto].join(' · ');
    if (typeof tested.occurred_at === 'number') facts.push({ label: '수동 확인', value: `${observedTime(tested.occurred_at)} · ${word(tested.subject) || '확인자 미관측'}` });
  } else if (kind === 'recovery') {
    const recovery = object(release.recovery);
    lead = recovery.accepted === true && recovery.outcome === 'passed' ? '운영 업무 복구 확인'
      : object(object(release.stage_results).prd).verified === true ? '인프라 정상 · 업무 복구 미확인' : '업무 복구 미확인';
    facts.push({ label: '대상', value: `${doc.environment} / ${doc.service}` });
  } else if (kind === 'approval') {
    const merge = object(doc.data.merge_review), approval = object(release.approval), tested = object(release.user_test);
    const bound = merge.human_merge_observed === true && matchingFingerprint(merge, release);
    facts.push({ label: 'PR 병합 확인', value: (bound ? '확인 기록 있음' : '미관측') + (typeof merge.observed_at === 'number' ? ' · ' + observedTime(merge.observed_at) : '') });
    facts.push({ label: 'prd 승인', value: approval.subject ? String(approval.subject) + ' · ' + observedTime(approval.occurred_at as number) : '미기록' });
    if (tested.kind === 'human') facts.push({ label: '당시 dev 수동 시험', value: (tested.passed === true ? '통과' : tested.passed === false ? '실패' : '결과 미관측') + ' · ' + observedTime(tested.occurred_at as number) });
    lead = [bound ? 'PR 병합 확인됨' : 'PR 병합 확인 미관측', approval.subject ? 'prd 승인 기록됨' : 'prd 승인 미기록'].join(' · ');
  } else if (kind === 'result') {
    lead = appFinishedText(doc.state);
  } else if (kind === 'handoff') {
    const handoff = object((doc as { handoff_classification?: Data }).handoff_classification ?? doc.data.handoff_classification), err = object(doc.data.error);
    lead = text(readingOf(handoff.label_ko, { state: 'ready' })) ?? '운영자가 자동 처리 중단 사유를 확인하고 사건을 인수해야 합니다.';
    const why = text(readingOf(err.message_ko, { state: 'ready' })); if (why) facts.push({ label: '멈춘 이유', value: why });
    facts.push({ label: '인수 대상', value: `${doc.environment} / ${doc.service}` });
    facts.push({ label: '복구 시도', value: handoff.recovery_attempted === true ? '기록됨' : handoff.recovery_attempted === false ? '시도 기록 없음' : '미관측' });
  }
  const order = (row: CallRow) => Number(row.ref) || 0;
  const calls: CallRow[] = [
    ...roles.map(e => ({ id: 'call:' + (e.data.call_id ?? e.id), origin: 'app' as const, ref: String(e.id), label: appRole(e.data.role), model: tag(e.data.model, e.data.effort), at: observedTime(e.at), status: 'done', summary: summaryOf(e), running: false })),
    ...open.map(e => { const running = live.includes(e); return { id: 'call:' + (e.data.call_id ?? e.id), origin: 'app' as const, ref: String(e.id), label: appRole(e.data.role), model: tag(e.data.model, e.data.effort),
      at: observedTime(e.at), status: running ? 'running' : 'stopped', summary: { note: running ? '응답을 기다리고 있습니다' : '결과 없이 끝난 호출' } as ReadingView, running }; }),
  ].sort((a, b) => order(a) - order(b));
  return { lead, note, facts, goals, remaining, evidence, calls };
}
const appFinishedText = (state: string) => state === 'completed' ? '자동 처리 완료 · 소명 기록 확인' : state === 'failed' ? '자동 처리 실패 · 기록 확인 필요' : '결과 소명 기록';

const FALLBACK_LEAD: Record<SemanticKind, string> = {
  detection: '감지 신호 기록', evidence: '수집한 근거 기록', routing: '대응 경로 결정 기록', investigation: '원인 판단 기록', verification: '독립 판정 기록',
  action: '조치 기록', test_build: '시험·빌드 기록', dev_validation: 'dev 검증 기록', approval: '운영자 결정 대기', recovery: '복구 관측 기록', result: '결과 소명 기록', handoff: '운영자 인수 필요',
};

/** The single first line of a stage, used for its own brief and as the lineage of the next stage. */
export function stageLead(node: SemanticNode, input: Omit<BriefInput, 'node'>): string {
  const core = input.core && coreMembers(node).length ? coreBrief(node, input.core) : null;
  const app = input.app && node.semantic.members.some(isAppStep) ? appBrief(node, input.app) : null;
  return app?.lead || core?.lead || FALLBACK_LEAD[node.semantic.kind];
}
const leadOf = (kind: SemanticKind, parts: { lead: string | null; note: string | null }[]) =>
  parts.map(p => p.lead).find(Boolean) || [FALLBACK_LEAD[kind], parts.map(p => p.note).find(Boolean)].filter(Boolean).join(' · ');

const EDGE_PHRASE: Record<string, string> = { reroute: '경로 재지정', steer: '운영자 지시 반영', escalate: '상위 모델 승격' };
function lineage(node: SemanticNode, input: BriefInput): { rows: Lineage[]; more: number } {
  const incoming = input.edges.filter(e => e.target === node.id && e.source !== node.id);
  const rows: Lineage[] = [];
  for (const e of incoming) {
    const source = input.nodes.find(n => n.id === e.source);
    if (!source || rows.some(r => r.source === source.id)) continue;
    const rejected = source.semantic.kind === 'verification' && (source.status === 'failed' ||
      agentRuns(source).some(n => n.card.acceptance?.rejected || (n.card.verdict && n.card.verdict !== 'supported')) ||
      (input.app?.roundEvents ?? []).some(ev => ev.event === 'role_completed' && ev.data.role === 'verifier' && ev.data.accepted === false));
    // A later stage pointing back is lineage only when it sent the work back (rejection, reroute, steering).
    if (source.depth > node.depth && !rejected && !EDGE_PHRASE[e.kind]) continue;
    const phrase = rejected && node.semantic.kind === 'investigation' ? '독립 검증 반려 → 재조사'
      : EDGE_PHRASE[e.kind] ? EDGE_PHRASE[e.kind] + (e.label ? ` · ${oneLine(e.label, 40)}` : '') : null;
    rows.push({ source: source.id, label: source.label, text: phrase ?? oneLine(stageLead(source, input)) });
  }
  if (node.semantic.members.some(m => m.type === 'steer')) rows.unshift({ source: node.id, label: '운영자', text: '운영자 지시 반영' });
  return { rows: rows.slice(0, 2), more: Math.max(0, rows.length - 2) };
}

export function deriveStageBrief(input: BriefInput): StageBrief {
  const { node } = input;
  const core = input.core && coreMembers(node).length ? coreBrief(node, input.core) : null;
  const app = input.app && node.semantic.members.some(isAppStep) ? appBrief(node, input.app) : null;
  const parts = [app, core].filter((p): p is NonNullable<typeof core> => !!p);
  const evidence = app?.evidence ?? core?.evidence ?? null;
  const { rows, more } = lineage(node, input);
  return {
    tone: flowStatusTone(node.status), models: models(node, input.app), lineage: rows, lineageMore: more,
    working: working(input.actors, input.app),
    lead: leadOf(node.semantic.kind, parts),
    facts: parts.flatMap(p => p.facts), goals: parts.flatMap(p => p.goals),
    remaining: [...new Set(parts.flatMap(p => p.remaining))].slice(0, 6),
    calls: parts.flatMap(p => p.calls), evidence, composition: evidence && evidence.refs ? composition(evidence) : null,
  };
}

/** Why production approval is held, from the same facts the release gate checks (the decision slot's first line). */
export function approvalHold(app: { state: string; data: Data }, release: Data, now = Date.now()): string {
  const merge = object(app.data.merge_review), approval = object(release.approval);
  const linked = !!object(app.data.unified_origin).action_id;
  const bound = merge.human_merge_observed === true && matchingFingerprint(merge, release);
  const valid = bound && typeof merge.expires_at === 'number' && merge.expires_at * 1000 > now;
  if (approval.subject) return 'prd 승인이 기록됐습니다.';
  if (release.promotion_disposition === 'unnecessary') return '현재 버전의 복구를 확인해 이전 릴리스 승격을 종료했습니다.';
  if (!linked) return '사람의 PR 병합을 검증할 연결 정보가 없어 prd 승인이 보류됐습니다.';
  if (!releaseProof(release)) return '유효한 시험·버전·변경 근거가 부족해 prd 승인이 보류됐습니다.';
  if (!valid) return '같은 버전의 PR 병합 확인이 필요합니다.';
  if (app.state === 'awaiting_prd_approval') return '검증된 버전의 prd 승인이 필요합니다.';
  return '승인 결과 미관측';
}
