import { useState, type FormEvent, type ReactNode } from 'react';
import { clock, money, type FlowNode, type HandoffView, type Narrative } from './api';
import type { RunEvent } from './feed';
import { confidenceKo, eventMessageIds, flowStatusKo, flowTitle, FLOW_TYPE_KO, steerStateKo, STEERABLE_TYPES, stepDomId } from './narrative';
import { ACTION_KO, effortKo, INTENT_KO, levelKo, roleKo } from './labels';
import { ExecutionStream } from './ExecutionStream';
import type { ExecutionView, StreamState } from './execution';
import { summaryKo } from './presentation';

export type LocalSent = { message_id: number | null; body: string; ack_ko: string | null; target_role: string | null; hold: boolean | null; at: string; via: 'messages' | 'commands' };
export type DetailTarget = { kind: 'node'; nodeId: string } | { kind: 'evidence'; nodeId: string; ref: string | null; index: number };
const tone = (s: string) => s === 'failed' ? 'danger' : s === 'done' ? 'ok' : s === 'escalated' ? 'warn' : s === 'skipped' || s === 'planned' ? '' : 'info';
const NOT_YET = (n: FlowNode) => n.status === 'planned' ? '아직 실행 전입니다.' : n.status === 'running' ? '분석 중입니다. 공개 답변을 아래에서 확인하세요.' : n.status === 'skipped' ? '이 단계는 건너뛰었습니다.' : n.conclusion_raw ? '한글 해석 확인 중 · 원문은 별도로 볼 수 있습니다.' : '단계 요약 미기록';

/** No raw labels, error messages or provider output are promoted into summaries. */
export function stepSummary(n: FlowNode, data: Narrative, snapshot = false): string {
  if (snapshot && n.status === 'running') return '저장 당시 진행 중이었던 단계입니다. 현재 실행 중인 작업은 없습니다.';
  if (snapshot && n.status === 'planned') return '저장 당시 실행 전이었던 단계입니다.';
  const c = n.card;
  if (c?.kind === 'signal') return summaryKo(c.what_ko, '감지 신호 확인 중');
  if (c?.kind === 'evidence') return `수집 ${c.total ?? '미관측'}건 · 성공 ${c.ok ?? '미관측'} · 실패 ${c.failed ?? '미관측'}`;
  if (c?.kind === 'routing') return `${levelKo(c.level)} 판정 · ${confidenceKo(c.confidence)}`;
  if (c?.kind === 'group') return `${levelKo(c.level)} 그룹 · 역할 ${c.roles.length}개`;
  if (c?.kind === 'agent') return summaryKo(c.summary_ko, c.error ? '실행 오류가 기록되었습니다.' : NOT_YET(n));
  if (c?.kind === 'action') return ACTION_KO[c.type] ?? '조치 기록';
  if (c?.kind === 'outcome') return summaryKo(c.handoff?.summary_ko || n.conclusion_ko, '결과 확인이 필요합니다.');
  if (n.type === 'signal') return summaryKo(data.headline.what, '감지 신호 확인 중');
  return summaryKo(n.conclusion_ko, NOT_YET(n));
}
export function HandoffSummary({ view }: { view: HandoffView }) {
  return <div className="handoff-summary"><p className="step-lead">{summaryKo(view.summary_ko, '한글 요약을 준비 중입니다.')}</p>
    {view.decisions.length > 0 && <dl className="kv decision-table" aria-label="결정사항">{view.decisions.map(d => <div className="decision-row" key={d.key} data-key={d.key}><dt>{summaryKo(d.label_ko, '결정')}</dt><dd>{summaryKo(d.value_ko, '판단 확인 중')}</dd></div>)}</dl>}
    {view.next_steps_ko.length > 0 && <><h4>다음 단계</h4><ol className="next-steps">{view.next_steps_ko.map((s, i) => <li key={i}>{summaryKo(s, '후속 조치 확인 필요')}</li>)}</ol></>}
  </div>;
}
function ThreadItem({ e, name }: { e: RunEvent; name: (r: string) => string }) {
  const d = e.detail ?? {};
  if (e.kind === 'operator_message') return <div className="operator-bubble"><small>운영자{d.target_role ? ` → ${name(d.target_role)}` : ''} · {clock(e.at)}</small><p>{e.summary}</p></div>;
  if (e.kind === 'steering_interpreted') return <div className="ack-bubble" data-message-id={d.message_id}><small>AI 지시 해석 · {clock(e.at)}</small><p>{summaryKo(d.ack_ko, '지시를 접수했습니다. 다음 단계부터 반영합니다.')}</p><span className="badge">{INTENT_KO[d.intent] ?? '지시 반영'}{d.target_role ? ` · ${name(d.target_role)}` : ''}</span></div>;
  return null;
}
/** Recorded threads and pending local sends keep their original ownership. */
export function StepThread({ events, local, node, name }: { events: RunEvent[]; local: LocalSent[]; node: FlowNode | null; name: (r: string) => string }) {
  const seen = new Set(events.filter(e => e.kind === 'operator_message').map(e => e.detail?.message_id));
  const pending = local.filter(l => l.message_id == null || !seen.has(l.message_id));
  if (!events.length && !pending.length) return null;
  const messages = events.filter(e => e.kind === 'operator_message');
  return <div className="step-thread" aria-label="이 단계 대화" aria-live="polite">
    {events.filter(e => e.kind !== 'operator_applied').map(e => <ThreadItem e={e} key={e.seq} name={name} />)}
    {messages.map(m => { const applied = events.find(e => e.kind === 'operator_applied' && eventMessageIds(e).includes(String(m.detail?.message_id))); const hold = events.find(e => e.kind === 'steering_interpreted' && e.detail?.message_id === m.detail?.message_id)?.detail?.hold ?? null;
      if (!m.detail?.target_role) return applied?.detail?.applied_to_role ? <p className="steer-state" key={`state-${m.seq}`}>{name(applied.detail.applied_to_role)} 시작 전에 반영됨</p> : null;
      return <p className="steer-state" key={`state-${m.seq}`}>{steerStateKo({ applied: applied?.detail ?? null, hold, targetStatus: node?.status ?? null, roleName: name(m.detail.target_role), targetRole: m.detail.target_role, appliedRoleName: applied?.detail?.applied_to_role ? name(applied.detail.applied_to_role) : null })}</p>; })}
    {pending.map((l, i) => <div className="local-sent" key={`local-${i}`}><div className="operator-bubble"><small>운영자{l.target_role ? ` → ${name(l.target_role)}` : ''} · {clock(l.at)}</small><p>{l.body}</p></div>
      <div className="ack-bubble" data-message-id={l.message_id ?? undefined}><small>AI 지시 해석 · 방금</small><p>{summaryKo(l.ack_ko, l.via === 'commands' ? '실행 계획 확인 절차로 접수했습니다. 아래 운영자 지시 카드에서 계획과 부작용을 확인하세요.' : '지시를 접수했습니다. 다음 단계부터 반영합니다.')}</p>{l.target_role && <span className="badge">대상 · {name(l.target_role)}</span>}</div>
      {l.target_role && l.via === 'messages' && <p className="steer-state">{l.hold ? `${name(l.target_role)} 시작 전 반영 예정` : '반영 예정'}</p>}
    </div>)}
  </div>;
}
export function StepChat({ node, targetRole, closed, finished, draft, onDraft, onSend, name }: { node: FlowNode; targetRole: string | null; closed: boolean; finished: boolean; draft: string; onDraft: (v: string) => void; onSend: (body: string) => Promise<void>; name: (r: string) => string }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  if (closed) return <p className="step-chat-closed">종료된 장애라 이 단계에 지시를 보낼 수 없습니다.</p>;
  async function submit(e: FormEvent) {
    e.preventDefault(); const body = draft.trim(); if (!body || busy) return;
    setBusy(true); setError('');
    try { await onSend(body); onDraft(''); } catch { setError('지시를 보내지 못했습니다. 입력 내용은 유지했습니다. 다시 시도하세요.'); } finally { setBusy(false); }
  }
  const title = flowTitle(node);
  return <form className="step-chat" onSubmit={submit} aria-label={`${title} 단계 지시`}><p className="step-chat-target">{finished ? '분석이 끝나 실행 계획 확인 절차로 접수됩니다.' : targetRole ? `대상: ${name(targetRole)} · 이 단계에 지시합니다.` : '대상 역할 없이 다음 Agent 경계에서 반영합니다.'}</p>
    <div className="step-chat-row"><textarea aria-label={`${title} 단계에 보낼 지시`} rows={2} maxLength={finished ? 8000 : 2000} value={draft} placeholder={finished ? 'AI에게 후속 작업을 지시하세요' : '이 단계에 지시하세요'} onChange={e => onDraft(e.target.value)} /><button className="btn primary" type="submit" disabled={busy || !draft.trim()}>{busy ? '전송 중' : '지시 보내기'}</button></div>{error && <div className="error" role="alert">{error}</div>}
  </form>;
}

type CardProps = { snapshot?: boolean; recorded?: boolean; n: FlowNode; data: Narrative; flash: boolean; onDetail: (t: DetailTarget, opener: HTMLElement) => void; handoff: HandoffView | null;
  execution?: ExecutionView; streamState?: StreamState; streamError?: boolean; receivedAt?: string | null;
  thread: RunEvent[]; local: LocalSent[]; targetRole: string | null; closed: boolean; finished: boolean; draft: string; onDraft: (v: string) => void; onSend: (body: string) => Promise<void>; name: (r: string) => string; onJump: (nodeId: string) => void };
export function StepCard(p: CardProps) {
  const { n, data } = p; const c = n.card ?? null; const title = flowTitle(n);
  const steerable = STEERABLE_TYPES.includes(n.type), index = FLOW_TYPE_KO[n.type] ?? '단계';
  const steerMid = n.type === 'steer' ? n.id.replace(/^steer:/, '') : null;
  const steerTarget = n.type === 'steer' ? data.flow.edges.find(e => e.source === n.id)?.target ?? null : null;
  let body: ReactNode = <p className="step-lead">{stepSummary(n, data, p.snapshot || p.recorded)}</p>;
  if (c?.kind === 'signal') body = <>{body}<dl className="kv step-kv"><dt>위치</dt><dd>{c.place_ko}</dd><dt>출처 · 사유</dt><dd>{summaryKo(c.source_ko, '감지 신호')} · {summaryKo(c.reason_ko, '사유 확인 중')}</dd><dt>신호 수</dt><dd>{c.signal_count ?? '미관측'}</dd><dt>처음 감지</dt><dd>{clock(c.first_at)}</dd></dl></>;
  else if (n.type === 'signal') body = <>{body}<p>{data.incident.workload} · 신호 {data.incident.signal_count ?? '미관측'}</p><p className="muted">출처 · 사유 미기록</p></>;
  else if (c?.kind === 'evidence') body = <>{body}<ol className="evidence-rows" aria-label={`수집 항목 ${c.items.length}건`}>{c.items.map((it, i) => <li key={it.id} className={it.ok ? '' : 'failed'} data-evidence-id={it.id}><div className="evidence-row-head"><b>{summaryKo(it.source_ko, '수집 항목')}</b><span>{it.target}</span><span className={`badge ${it.ok ? 'ok' : 'danger'}`}>{it.ok ? '수집 성공' : '수집 실패'}</span></div><p>{it.ok ? summaryKo(it.summary_ko, '수집 결과 요약 미기록') : '수집하지 못했습니다. 실패 사유는 원문에서 확인하세요.'}</p><button type="button" className="link-btn" aria-label={`${summaryKo(it.source_ko, '수집 항목')} 원문 보기`} onClick={e => p.onDetail({ kind: 'evidence', nodeId: n.id, ref: it.ref, index: i }, e.currentTarget)}>원문 보기</button></li>)}</ol></>;
  else if (n.type === 'evidence') body = <p>수집 항목별 목록은 아직 받지 못했습니다. 원문 보기에서 수집 기록을 확인하세요.</p>;
  else if (c?.kind === 'routing') body = <>{body}<p>{summaryKo(c.final_reason_ko, '판단 사유 확인 중')}</p><div className="decision-context"><ol className="decision-steps">{c.steps.map((s, i) => <li key={`${s.key}-${i}`} data-step-key={s.key}><b>{summaryKo(s.title_ko, '판단 단계')}</b><p>{summaryKo(s.detail_ko, '상세 해석 확인 중')}</p>{s.items_ko?.length > 0 && <ul>{s.items_ko.map((it, j) => <li key={j}>{summaryKo(it, '관측 항목 확인 중')}</li>)}</ul>}</li>)}</ol></div></>;
  else if (n.type === 'router') body = <>{body}<p className="muted">판단 과정은 현재 미기록입니다.</p></>;
  else if (c?.kind === 'group') body = <>{body}<p>{summaryKo(c.decision.final_reason_ko, '선택 이유 미기록')}</p><div className="decision-context"><h4>역할 구성</h4><ul className="role-list">{c.roles.map(r => <li key={r.role} data-role={r.role}><b>{roleKo(r.role, r.role_ko)}</b><p>{summaryKo(r.job_ko, '담당 업무 미기록')}</p><span>{flowStatusKo(r.status)} · {r.model || '모델 미기록'} · {effortKo(r.effort)}</span></li>)}</ul><h4>선택 이유</h4><ul>{c.decision.steps.map((s, i) => <li key={i}>{summaryKo(s.detail_ko, '판단 확인 중')}</li>)}</ul>{c.budget && <><h4>예산</h4><p>호출 최대 {c.budget.max_agent_calls ?? '미기록'}회 · 비용 상한 {c.budget.max_cost_usd ?? '미기록'} USD · 제한 시간 {c.budget.deadline_s ?? '미기록'}초</p></>}{c.progress_ko.length > 0 && <><h4>진행 기록</h4><ul>{c.progress_ko.map((text, i) => <li key={i}>{summaryKo(text, '진행 요약 미기록')}</li>)}</ul></>}</div></>;
  else if (n.type === 'group') body = <p>선택 이유와 판단 과정은 서버 기록을 받으면 표시합니다(현재 미기록).</p>;
  else if (c?.kind === 'agent') body = <>{body}<div className="cause-summary"><h4>원인 후보</h4>{c.candidates.length ? <ol>{c.candidates.map((candidate, ix) => <li key={ix}>{summaryKo(candidate.cause_ko, '원인 설명 미기록')} <small className="muted">· {confidenceKo(candidate.confidence)}</small></li>)}</ol> : <p className="muted">아직 원인이 확인되지 않았습니다.</p>}</div><dl className="kv step-kv"><dt>검증 판정</dt><dd>{summaryKo(c.verdict_ko, '검증 결과 미기록')}</dd><dt>다음 행동</dt><dd>{summaryKo(c.next_action_ko, n.status === 'running' ? '분석 결과를 기다리고 있습니다.' : '후속 행동 미기록')}</dd>{c.counter_evidence_count > 0 && <><dt>반대 근거</dt><dd>{c.counter_evidence_count}건</dd></>}{c.error && <><dt>실행 오류</dt><dd>{summaryKo(c.error_ko, '분석 실행에 실패했습니다. 오류 원문을 확인하세요.')}</dd></>}</dl></>;
  else if (c?.kind === 'action') body = <>{body}<p>{c.target} · {summaryKo(c.status_ko, '상태 확인 중')}</p></>;
  else if (n.type === 'outcome' && ((c?.kind === 'outcome' && c.handoff) || p.handoff)) body = <HandoffSummary view={(c?.kind === 'outcome' && c.handoff) || p.handoff!} />;
  else if (n.type === 'interpret') body = <>{body}{data.raw.filter(r => r.ko_source === 'llm' && r.ko).map(r => <p key={r.id}>{summaryKo(r.ko, '해석 확인 중')}</p>)}</>;
  else if (n.type === 'steer') body = <><p>운영자 지시 기록</p>{steerTarget && <button type="button" className="link-btn" onClick={() => p.onJump(steerTarget)}>대상 단계 카드로 이동</button>}</>;
  return <section id={stepDomId(n.id)} className={`step-card type-${n.type} status-${n.status} open ${p.flash ? 'flash' : ''}`} tabIndex={-1} aria-label={`단계 카드: ${index} · ${title}`} data-node-id={n.id} data-steer-mid={steerMid ?? undefined}>
    <header className="step-head"><span className="step-kind">{index}</span><h3>{title}</h3><span className={`badge ${tone(n.status)}`}>{p.snapshot || p.recorded ? '당시 ' : ''}{flowStatusKo(n.status)}</span><time className="step-time">{n.at ? clock(n.at) : '시각 미기록'}</time></header>
    {<div className="step-body">{body}{n.type === 'verify' && <p className="faint">분석 결과에 대한 검증이며, 실제 서비스 복구 검증과는 다릅니다.</p>}</div>}
    {['agent', 'verify', 'router'].includes(n.type) && <ExecutionStream recorded={p.recorded} node={n} view={p.execution ?? { records: [], attempts: [], final: null }} state={p.streamState ?? 'connecting'} error={p.streamError ?? false} receivedAt={p.receivedAt ?? null} />}
    {n.type !== 'steer' && <StepThread events={p.thread} local={p.local} node={n} name={p.name} />}
    {n.type === 'steer' && <StepThread events={p.thread} local={[]} node={null} name={p.name} />}
    {steerable && p.snapshot && <p className="muted">저장 표본은 열람만 가능합니다.</p>}
    {steerable && !p.snapshot && <StepChat node={n} targetRole={p.targetRole} closed={p.closed} finished={p.finished} draft={p.draft} onDraft={p.onDraft} onSend={p.onSend} name={p.name} />}
    {(n.model || n.cost_usd != null || n.duration_ms != null) && <div className="step-metrics"><span>{n.model || '모델 미기록'} · {effortKo(n.effort)}</span><span>비용 {money(n.cost_usd)}</span><span>소요 {n.duration_ms == null ? '미기록' : `${(n.duration_ms / 1000).toFixed(1)}초`}</span><span>입력 / 출력 토큰 {n.tokens?.in ?? '미관측'} / {n.tokens?.out ?? '미관측'}</span></div>}
    <div className="step-actions"><button type="button" className="btn sm" onClick={e => p.onDetail({ kind: 'node', nodeId: n.id }, e.currentTarget)}>원문 보기</button></div>
  </section>;
}
