import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { ApiError, age, clock, costKo, request, type Command, type FlowNode, type IncidentDetail, type InterpretRequestResult, type MessageResponse, type Narrative } from './api';
import type { RunEvent } from './feed';
import { acceptsInstructions, closable, conversationEvents, currentIncident, handoffFallback, isFinished, messageEvents, narrativePollMs, normalizeNarrative, orderSteps, controlEventKo, pendingActions, resolveStepId, stepDomId, stepTargetRole, stepThread } from './narrative';
import { commandControls, commandPollMs } from './commands';
import { INTENT_KO, STAGE_FROM_STATUS, placeKo, roleKo } from './labels';
import { usePolling } from './usePolling';
import { historyResult, impactKo, whenKo } from './history';
import './incidentRecord.css';
import { CommandCard } from './CommandCard';
import { ChangeRequestCard } from './ChangeRequestCard';
import { changePollMs, type ChangeRequest } from './changes';
import { ActionApprovalCard, CloseCard, ClosedCard, HandoffCard } from './ActionApproval';
import { FlowGraph } from './FlowGraph';
import { StepCard, type DetailTarget, type LocalSent } from './StepCards';
import { UsageLine } from './Explain';
import { DetailPanel } from './DetailPanel';
import { useRunEvents } from './useRunEvents';
import { stepExecution } from './execution';
import { ErrorBox, LevelBadge, Spinner, StatusBadge } from './ui';
import { Disclosure } from './Disclosure';
import { incidentSymptom, nextActionKo, summaryKo } from './presentation';
import { CodeActionCard } from './CodeActionCard';
import { continuationActive, type CodeActionSummary } from './unified';

type Tab = 'flow' | 'chat';
/** Untargeted conversation entries. Run-record events are absorbed into step cards (contract 2.1). */
function EventEntry({ e }: { e: RunEvent }) {
  const d = e.detail ?? {};
  if (e.kind === 'operator_message') return <div className="operator-bubble"><small>운영자 · {clock(e.at)}</small><p>{e.summary}</p></div>;
  if (e.kind === 'operator_applied') return <p className="chat-status">{d.applied_to_role ? `${roleKo(d.applied_to_role)} 시작 전에 지시를 반영했습니다.` : '지시를 다음 단계에 반영했습니다.'}</p>;
  if (e.kind === 'steering_interpreted') return <div className="ack-bubble"><small>AI 지시 해석 · {clock(e.at)}</small><p>{summaryKo(d.ack_ko, '지시를 접수했습니다. 다음 단계부터 반영합니다.')}</p><span className="badge">{INTENT_KO[d.intent] ?? '지시 반영'}{d.target_role ? ` · ${roleKo(d.target_role)}` : ''}</span></div>;
  const control = controlEventKo(e);
  return <div className="chat-event"><p>{control || '실행 기록이 추가되었습니다.'} · {clock(e.at)}</p><Disclosure label="실행 이벤트 원문 보기">{() => <pre className="raw-text">{e.summary}</pre>}</Disclosure></div>;
}
export function IncidentRoom() { const { id } = useParams(); return <Room key={id} id={id || ''} />; }
/** 정돈-02: reading never starts Korean interpretation. Requesting it is a separate, confirmed action (model call, cost). */
function InterpretRequest({ id, state, pending, onDone }: { id: string; state: string | undefined; pending: number | undefined; onDone: () => void }) {
  const [asking, setAsking] = useState(false); const [busy, setBusy] = useState(false); const [note, setNote] = useState(''); const [error, setError] = useState('');
  if (state !== 'not_requested' && state !== 'unavailable') return null;
  const send = async () => {
    setBusy(true); setError('');
    try {
      const r = await request<InterpretRequestResult>(`/incidents/${id}/interpretations`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
      setNote(r.status === 'accepted' ? '한글 해석을 요청했습니다. 준비되면 화면에 반영됩니다.' : r.status === 'running' || r.status === 'queued' ? '이미 해석이 진행 중입니다. 중복 요청하지 않았습니다.' : '새로 해석할 항목이 없습니다.');
      setAsking(false); onDone();
    } catch { setError('해석 요청을 보내지 못했습니다. 잠시 후 다시 시도하세요.'); }
    finally { setBusy(false); }
  };
  return <div className="interpret-request">
    {!asking ? <button className="btn sm" type="button" onClick={() => setAsking(true)}>한글 해석 요청{pending ? ` (${pending}개 항목)` : ''}</button>
      : <div className="interpret-confirm" role="group" aria-label="한글 해석 요청 확인"><p>모델을 호출하므로 비용이 기록될 수 있습니다. 원문과 기존 기록은 바뀌지 않습니다. 요청할까요?</p>
        <div className="inline"><button className="btn sm primary" type="button" disabled={busy} onClick={() => void send()}>{busy ? '요청 중' : '해석 요청'}</button><button className="btn sm" type="button" disabled={busy} onClick={() => setAsking(false)}>취소</button></div></div>}
    {note && <p className="interpreting-inline" role="status">{note}</p>}<ErrorBox>{error}</ErrorBox>
  </div>;
}
function Room({ id }: { id: string }) {
  const previousSearch = useLocation().state?.incidentListSearch;
  const listPath = typeof previousSearch === 'string' && previousSearch.startsWith('?') ? '/incidents' + previousSearch : '/incidents';
  const codeAction = usePolling<CodeActionSummary>(`/incidents/${id}/code-action`, 5000);
  const continuation = continuationActive(codeAction.data);
  // Missing continuation status cannot authorize an independent cluster write. Linked incidents keep the old core controls historical.
  const coreLocked = !codeAction.data || codeAction.error || continuation || codeAction.data.linked;
  const narrative = usePolling<Narrative>(`/incidents/${id}/narrative`, d => narrativePollMs(d?.incident.status ?? 'open'));
  const commands = usePolling<{ items: Command[] }>(`/incidents/${id}/commands`, d => commandPollMs(d?.items ?? []));
  const changes = usePolling<{items:ChangeRequest[]}>(`/changes?incident_id=${id}`, d => changePollMs(d?.items ?? []));
  // Engine actions and handoff ownership live on the incident detail; poll faster only while an approval is pending.
  const detail = usePolling<IncidentDetail>(`/incidents/${id}`, d => pendingActions(d?.actions).length ? 2000 : 10000);
  const data = useMemo(() => narrative.data ? normalizeNarrative(narrative.data) : null, [narrative.data]);
  const incident = data ? currentIncident(data.incident, detail.data?.incident) : null;
  const [tab, setTab] = useState<Tab>('chat'); const [selected, setSelected] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [pastCommandsOpen, setPastCommandsOpen] = useState(false);
  const followedCommands = useRef(new Set<number>());
  useEffect(() => {
    for (const c of commands.data?.items ?? []) {
      const controls = commandControls(c);
      if (controls.busy || controls.confirm || controls.secret) followedCommands.current.add(c.id);
    }
  }, [commands.data]);

  const [openCards, setOpenCards] = useState<Set<string>>(() => new Set()); const [flash, setFlash] = useState<string | null>(null); const [jumpRev, setJumpRev] = useState(0);
  const [panel, setPanel] = useState<DetailTarget | null>(null);
  const [eventRevision, setEventRevision] = useState(0);
  const [text, setText] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [sent, setSent] = useState('');
  const [drafts, setDrafts] = useState<Record<string, string>>({}); const [local, setLocal] = useState<Record<string, LocalSent[]>>({});
  const feed = useRef<HTMLDivElement>(null); const scrollNext = useRef(false);
  const panelScroll = useRef<number | null>(null);
  const responseScroll = useRef<{ nodeId: string; messageId: number | null; until: number } | null>(null);
  const jump = useRef<string | null>(null); const flashTimer = useRef<ReturnType<typeof setTimeout>>(undefined); const opener = useRef<HTMLElement | null>(null);
  const execution = useRunEvents(id, incident ? isFinished(incident.status) : false, eventRevision, narrative.refresh);
  const { events, control, error: eventError } = execution;
  const snapshot = data?.view_mode === 'snapshot' || execution.state === 'snapshot';
  // Only an operator's own conversation send moves to the latest entry; live updates never steal the reading position.
  useEffect(() => { if (scrollNext.current && feed.current) { feed.current.scrollTop = feed.current.scrollHeight; scrollNext.current = false; } }, [events, commands.data]);
  // Flow node → exact step card: scroll inside the conversation only (page and flow canvas stay put), flash, focus.
  useEffect(() => {
    const nid = jump.current; if (!nid) return;
    const raf = requestAnimationFrame(() => {
      const key = data ? resolveStepId(nid, data.flow.nodes) ?? nid : nid;
      const el = document.getElementById(stepDomId(key)); const box = feed.current; if (!el || !box || !box.getClientRects().length) return;
      jump.current = null;
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const top = box.scrollTop + el.getBoundingClientRect().top - box.getBoundingClientRect().top - 8;
      box.scrollTo({ top: Math.max(0, top), behavior: reduce ? 'auto' : 'smooth' });
      el.focus({ preventScroll: true });
      if (data?.flow.nodes.find(n => n.id === key)?.type === 'code_action') el.focus({ preventScroll: true });
      setFlash(key); clearTimeout(flashTimer.current); flashTimer.current = setTimeout(() => setFlash(null), 1800);
    });
    return () => cancelAnimationFrame(raf);
  }, [jumpRev, tab, data]);
  useEffect(() => () => clearTimeout(flashTimer.current), []);
  // 운영자 본인의 전송만 따라간다. 스크롤·다른 카드 선택 시 즉시 해제한다.
  useLayoutEffect(() => {
    const pending = responseScroll.current;
    if (!pending || panel) return;
    if (Date.now() > pending.until) { responseScroll.current = null; return; }
    const raf = requestAnimationFrame(() => {
      if (responseScroll.current !== pending) return;
      const effectiveId = data ? resolveStepId(pending.nodeId, data.flow.nodes) : null;
      const box = feed.current, card = document.getElementById(stepDomId(effectiveId ?? pending.nodeId));
      const ack = pending.messageId == null ? card?.querySelector('.local-sent:last-child .ack-bubble')
        : card?.querySelector(`.ack-bubble[data-message-id="${pending.messageId}"]`);
      if (!box || !ack) return;
      const f = box.getBoundingClientRect(), a = ack.getBoundingClientRect();
      const delta = a.top < f.top + 8 ? a.top - f.top - 8 : a.bottom > f.bottom - 8 ? a.bottom - f.bottom + 8 : 0;
      if (delta) box.scrollTo({ top: box.scrollTop + delta, behavior: 'instant' });
      // 로컬 접수 표시가 실제 이벤트·흐름 카드로 바뀔 때까지만 위치를 보정한다.
      const mid = pending.messageId;
      if (mid == null || (events.some(e => e.kind === 'steering_interpreted' && e.detail?.message_id === mid)
        && data?.flow.nodes.some(n => n.id === `steer:${mid}`))) responseScroll.current = null;
    });
    return () => cancelAnimationFrame(raf);
  }, [local, events, data, panel]);
  const reload = useCallback(() => { narrative.refresh(); commands.refresh(); detail.refresh(); codeAction.refresh(); setEventRevision(r => r + 1); }, [narrative.refresh, commands.refresh, detail.refresh, codeAction.refresh]);
  const choose = useCallback((nid: string | null) => { if (!nid) return; const key = data ? resolveStepId(nid, data.flow.nodes) ?? nid : nid; responseScroll.current = null; setSelected(key); setHistoryOpen(true); if (key.startsWith('command:')) setPastCommandsOpen(true); setOpenCards(s => s.has(key) ? s : new Set(s).add(key)); setTab('chat'); jump.current = key; setJumpRev(r => r + 1); }, [data]);
  const toggle = useCallback((nid: string) => setOpenCards(s => { const next = new Set(s); const aliases = data?.flow.nodes.find(n => n.id === nid)?.aliases ?? []; const keys = [nid, ...aliases]; if (keys.some(k => next.has(k))) keys.forEach(k => next.delete(k)); else next.add(nid); return next; }), [data]);
  const openDetail = useCallback((t: DetailTarget, el: HTMLElement) => { responseScroll.current = null; opener.current = el; if (feed.current) { const top = feed.current.scrollTop; feed.current.scrollTo({ top, behavior: 'instant' }); panelScroll.current = top; } setPanel(t); }, []);
  useLayoutEffect(() => { if (panelScroll.current != null && feed.current) feed.current.scrollTop = panelScroll.current; }, [panel]);
  const closeDetail = useCallback(() => {
    setPanel(null); const el = opener.current;
    requestAnimationFrame(() => {
      if (panelScroll.current != null && feed.current) feed.current.scrollTop = panelScroll.current;
      const key = panel && data ? resolveStepId(panel.nodeId, data.flow.nodes) : null;
      // 예정 카드가 실행 카드로 바뀌어 원래 버튼이 사라졌으면 같은 단계의 새 버튼에 복귀한다.
      const replacement = key ? document.getElementById(stepDomId(key))?.querySelector<HTMLElement>('.step-actions button:not([aria-expanded])') : null;
      (el?.isConnected ? el : replacement)?.focus({ preventScroll: true });
    });
  }, [panel, data]);
  async function send(e: FormEvent) {
    e.preventDefault(); const body = text.trim(); if (snapshot || coreLocked || !body || busy || !data || !incident || !acceptsInstructions(incident.status)) return;
    setBusy(true); setError(''); setSent(''); scrollNext.current = true;
    try {
      if (isFinished(incident.status)) await request(`/incidents/${id}/commands`, { method: 'POST', body: JSON.stringify({ instruction: body }) });
      else { try { await request(`/incidents/${id}/messages`, { method: 'POST', body: JSON.stringify({ body }) }); } catch (e) { if (e instanceof ApiError && e.status === 409) await request(`/incidents/${id}/commands`, { method: 'POST', body: JSON.stringify({ instruction: body }) }); else throw e; } }
      setText(''); setSent('지시를 접수했습니다. 실행이 필요한 작업은 계획과 부작용을 확인한 뒤 진행합니다.'); reload();
    } catch { setError('지시를 보내지 못했습니다. 입력 내용은 유지했습니다. 다시 시도하세요.'); }
    finally { setBusy(false); }
  }
  /** Step chat: target_role/target_node steering while analysing; after analysis the existing plan→side-effect→confirm command path. */
  async function sendStep(n: FlowNode, body: string) {
    if (snapshot || coreLocked || !data || !incident || !acceptsInstructions(incident.status)) throw new Error('closed');
    const role = stepTargetRole(n, data.flow.nodes); const at = new Date().toISOString();
    const add = (l: LocalSent) => { responseScroll.current = { nodeId: n.id, messageId: l.message_id, until: Date.now() + 8000 }; setLocal(m => ({ ...m, [n.id]: [...(m[n.id] ?? []), l] })); };
    const viaCommand = async () => { await request(`/incidents/${id}/commands`, { method: 'POST', body: JSON.stringify({ instruction: body }) }); add({ message_id: null, body, ack_ko: null, target_role: null, hold: null, at, via: 'commands' }); };
    if (isFinished(incident.status)) await viaCommand();
    else {
      try {
        const r = await request<MessageResponse>(`/incidents/${id}/messages`, { method: 'POST', body: JSON.stringify({ body, target_node: n.id.slice(0, 80), ...(role ? { target_role: role } : {}) }) });
        add({ message_id: r?.message_id ?? null, body, ack_ko: r?.ack_ko ?? null, target_role: r?.target_role ?? role, hold: r?.hold ?? null, at, via: 'messages' });
      } catch (e) { if (e instanceof ApiError && e.status === 409) await viaCommand(); else throw e; }
    }
    reload();
  }
  async function ctl(action: 'pause' | 'resume' | 'stop') {
    if (snapshot || coreLocked) return;
    if (action === 'stop' && !window.confirm('분석을 중단하시겠습니까? 이미 시작된 조치는 검증을 마친 뒤 멈춥니다.')) return;
    setBusy(true); setError(''); try { await request(`/incidents/${id}/control`, { method: 'POST', body: JSON.stringify({ action }) }); reload(); } catch { setError('분석 제어 요청에 실패했습니다. 상태를 확인하고 다시 시도하세요.'); } finally { setBusy(false); }
  }
  if (!data) return <div className="page"><Link to={listPath}>← 장애 목록</Link>{narrative.error ? <><ErrorBox>장애 상황을 불러오지 못했습니다.</ErrorBox><button className="btn" onClick={reload}>다시 불러오기</button></> : <div className="empty" role="status"><Spinner /> 장애 관제실을 준비 중입니다.</div>}</div>;
  const i = incident!, done = isFinished(i.status);
  const sameStatus = i.status === data.incident.status;
  const stage = STAGE_FROM_STATUS[i.status];
  const next = nextActionKo(i);
  const name = (r: string) => roleKo(r, data.flow.nodes.find(n => n.role === r)?.role_ko);
  const result = historyResult(i, data.flow.nodes, snapshot);
  const recordView = done || snapshot || codeAction.data?.linked === true;
  const approvals = done || snapshot || coreLocked ? [] : pendingActions(detail.data?.actions);
  const pending = approvals.length > 0 || (commands.data?.items ?? []).some(c => c.status === 'awaiting_confirmation' || c.status === 'awaiting_secret');
  // 종료는 진행 중인 조치·지시가 없을 때만 연다. 최종 판단은 서버가 다시 한다.
  const liveCommand = (commands.data?.items ?? []).some(c => ['planning', 'awaiting_confirmation', 'awaiting_secret', 'executing', 'rolling_back'].includes(String(c.status)));
  const closeBlocked = pendingActions(detail.data?.actions).length ? '승인 대기 중인 조치가 있습니다. 먼저 승인 또는 거절하세요.' : liveCommand ? '진행 중인 운영자 지시가 있습니다. 지시를 마치거나 취소한 뒤 종료하세요.' : '';
  const open = !snapshot && !coreLocked && acceptsInstructions(i.status);
  // A newer incident status wins over a stale narrative: reuse the server's handoff_view only while the status agrees.
  const handoffView = (sameStatus ? data.handoff_view : null) ?? (['handed_off', 'closed', 'failed'].includes(i.status) ? handoffFallback(i) : null);
  const actionableCommands: Command[] = [], pastCommands: Command[] = [];
  for (const command of commands.data?.items ?? []) {
    const controls = commandControls(command);
    if (!snapshot && !coreLocked && (!done || controls.busy || controls.confirm || controls.secret || controls.rollback || followedCommands.current.has(command.id))) actionableCommands.push(command);
    else pastCommands.push(command);
  }
  const commandCard = (c: Command) => <div className="step-anchor" id={stepDomId(`command:${c.id}`)} tabIndex={-1} key={c.id}><CommandCard command={c} onChange={reload} readOnly={snapshot || coreLocked} /></div>;
  const steps = orderSteps(data.flow.nodes);
  // Message ids rendered inside step cards: this browser's targeted sends and steering nodes (`steer:<message id>`).
  const ownedBy = new Map<string, string>();
  const aliasesFor = (n: FlowNode) => (n.aliases ?? []).filter(key => resolveStepId(key, steps) === n.id);
  const localFor = (n: FlowNode) => Object.entries(local).filter(([nid]) => resolveStepId(nid, steps) === n.id).flatMap(([, xs]) => xs);
  const draftFor = (n: FlowNode) => drafts[n.id] ?? aliasesFor(n).map(key => drafts[key]).find(v => v != null) ?? '';
  for (const [nid, xs] of Object.entries(local)) {
    const owner = resolveStepId(nid, steps);
    if (owner) for (const l of xs) if (l.message_id != null) ownedBy.set(String(l.message_id), owner);
  }
  for (const e of events) if (e.detail?.message_id != null && e.detail?.target_node) {
    const owner = resolveStepId(e.detail.target_node, steps);
    if (owner) ownedBy.set(String(e.detail.message_id), owner);
  }
  for (const n of steps) if (n.type === 'steer') {
    const mid = n.id.replace(/^steer:/, '');
    const targeted = messageEvents(events, mid).some(e => e.detail?.target_node);
    if (!targeted && !ownedBy.has(mid)) ownedBy.set(mid, n.id);
  }
  const owned = new Set<unknown>(ownedBy.keys());
  const threadFor = (n: FlowNode) => {
    const mine = [...ownedBy.entries()].filter(([, nid]) => nid === n.id).flatMap(([mid]) => messageEvents(events, mid));
    const all = new Map([...stepThread(events, n.id, aliasesFor(n)), ...mine].map(e => [e.seq, e]));
    // A steering node only owns untargeted messages; targeted ones live in their step card.
    return [...all.values()].filter(e => n.type !== 'steer' || !e.detail?.target_node).sort((a, b) => a.seq - b.seq);
  };
  const renderedTargets = new Set(steps.flatMap(n => [n.id, ...aliasesFor(n)]));
  const talk = conversationEvents(events, owned, renderedTargets);
  const timed = steps.length > 0 && steps.every(n => n.at);
  const entries: { key: string; t: number; el: ReactNode }[] = [
    ...steps.filter(n => n.type !== 'code_action').map((n, ix) => {
      const aliases = aliasesFor(n);
      const key = aliases[0] ?? n.id;
      return { key: `step-${key}`, t: timed ? Date.parse(n.at!) : ix, el: <StepCard key={key} n={n} data={data} open={[n.id, ...aliases].some(key => openCards.has(key))} flash={flash === n.id} onToggle={() => toggle(n.id)} onDetail={openDetail} handoff={handoffView}
      snapshot={snapshot} execution={stepExecution(n, events)} streamState={snapshot ? 'snapshot' : execution.state} streamError={execution.error} receivedAt={execution.receivedAt} thread={threadFor(n)} local={localFor(n)} targetRole={stepTargetRole(n, data.flow.nodes)} closed={!open} finished={done} draft={draftFor(n)} onDraft={v => setDrafts(d => ({ ...d, [n.id]: v }))} onSend={body => sendStep(n, body)} name={name} onJump={choose} /> };
    }),
    ...talk.map(e => ({ key: `event-${e.seq}`, t: timed ? Date.parse(e.at) : steps.length + e.seq, el: <EventEntry e={e} key={`event-${e.seq}`} /> })),
  ].sort((a, b) => a.t - b.t);
  // 운영자 결정이 없으면 멈춰 있는 상태에서는 결정 카드까지 한 번에 데려간다(좁은 화면에서는 대화 탭으로 전환).
  const decisionNeeded = !snapshot && !coreLocked && (approvals.length > 0 || (i.status === 'handed_off' && !i.handoff?.claimed_by));
  const jumpToDecision = () => {
    setTab('chat');
    window.requestAnimationFrame(() => {
      const card = feed.current?.querySelector<HTMLElement>('.action-card, .handoff-card');
      if (!card) return;
      card.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      card.querySelector<HTMLElement>('button:not(:disabled), summary')?.focus({ preventScroll: true });
    });
  };
  return <div className={`incident-room tab-${tab}${snapshot ? ' snapshot-view' : ''}`}>
    <section className="headline" aria-label="장애 상황 요약"><div className="headline-top"><Link to={listPath}>← 장애 관제</Link><span className="mono">#{i.id}</span><LevelBadge value={i.severity_level} /><>{codeAction.data?.linked ? <span className="badge info">{codeAction.data.state_ko}</span> : <StatusBadge value={i.status} recorded={snapshot} recoveryKind={i.recovery_kind} />}</><h1>장애 관제실</h1><span className="headline-place">{placeKo(i.environment, i.system, null, i.namespace)}</span><span className="headline-facts">{!recordView && <span>{age(i.created_at)} 경과</span>}{i.origin && <span className={`badge ${i.origin === 'evaluation' ? 'info' : ''}`}>{i.origin === 'evaluation' ? '평가 실행' : '출처 미확인'}</span>}</span></div>
      <h2>{i.workload} · {incidentSymptom(i)}</h2>
      <p className="headline-impact">{recordView && <span className="record-label">당시 영향</span>}{impactKo(data.headline.impact, recordView)}</p>
      {result && !codeAction.data?.linked && <section className="record-result" aria-label="처리 결과">
        <p><strong>처리 결과</strong> {result.label}{result.detail && <> · {result.detail}</>}</p>
        <p className="record-times"><span>감지 {result.detected}</span><span>{result.timeLabel} {result.at}</span>{result.duration && <span>{result.duration}</span>}</p>
      </section>}
      {codeAction.data?.linked && <p className="core-history-note"><span>Kubernetes 단계 · 이전 처리 기록</span><StatusBadge value={i.kubernetes_status ?? data.incident.kubernetes_status ?? data.incident.status} recorded /><span>현재 진행은 코드 조치에서 확인하세요.</span></p>}
      {codeAction.data?.linked && <p className="faint">Kubernetes 분석 사용량</p>}<UsageLine incident={i} nodes={data.flow.nodes} />
      {snapshot ? <div className="record-notice" role="note"><strong>저장 당시 · {stage?.label}</strong> {result ? '' : `· 감지 ${whenKo(i.created_at)} `}· 읽기 전용 표본</div>
        : codeAction.data?.linked ? <div className="headline-next"><span className="stage-label">{codeAction.data.state_ko}</span><p><b>다음</b> {codeAction.data.next_ko}</p></div> : (!result || i.status === 'handed_off') && <div className={`headline-next ${decisionNeeded ? 'needs-decision' : ''}`}><span className="stage-label"><i className={`dot ${done ? 'warn' : 'info'}`} />{stage?.label}</span><p><b>{decisionNeeded ? '지금 할 일' : '다음'}</b> {next}</p>{decisionNeeded && <button type="button" className="btn sm primary" onClick={jumpToDecision}>{approvals.length ? '승인 카드로 이동' : '인계 카드로 이동'}</button>}</div>}
    </section>
    {(narrative.error || commands.error || detail.error || eventError) && <div className="room-error"><ErrorBox>{narrative.error ? '상황 정보' : commands.error ? '지시 목록' : detail.error ? '조치 승인 정보' : '실행 기록'} 갱신에 실패했습니다. 마지막으로 받은 내용을 표시합니다.</ErrorBox><button className="btn sm" onClick={reload}>다시 불러오기</button></div>}
    <nav className="room-tabs" aria-label="관제실 화면 전환">{([['flow', '흐름'], ['chat', '대화']] as const).map(([key, label]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}{key === 'chat' && pending && <i className="dot warn" />}</button>)}</nav>
    <div className={`room-columns ${panel ? 'with-detail' : ''}`}>
      <section className="room-pane flow-pane" aria-label="AI 분석 흐름"><header className="pane-heading"><div><span className="pane-index">01</span><h2>AI 분석 흐름</h2></div><span className="badge info">{snapshot ? '저장 흐름' : continuation ? '코드 조치 진행 중' : done ? '실행 기록' : '실시간'}</span></header>
        <FlowGraph snapshot={snapshot} flow={data.flow} selected={selected ? resolveStepId(selected, steps) ?? selected : null} onSelect={choose} /></section>
      <section className="room-pane chat-pane conversation-pane" aria-label="분석 · 운영자 대화"><header className="pane-heading"><div><span className="pane-index">02</span><h2>분석 · 운영자 대화</h2></div><span className="faint">단계 {steps.length}개 · {snapshot ? '저장 기록' : i.status === 'closed' ? '종료 기록' : done ? '후속 작업' : '분석 지시'}</span></header>
        <div className="chat-feed" ref={feed} onWheel={() => { responseScroll.current = null; }} onTouchStart={() => { responseScroll.current = null; }} onPointerDown={() => { responseScroll.current = null; }} onKeyDown={e => { if (['PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown'].includes(e.key)) responseScroll.current = null; }}>
          <CodeActionCard nodeId={data.flow.nodes.find(n => n.type === 'code_action')?.id} flash={flash === data.flow.nodes.find(n => n.type === 'code_action')?.id} id={id} summary={codeAction.data} summaryError={codeAction.error} problem={incidentSymptom(i)} onChange={codeAction.refresh} readOnly={snapshot} />
          {i.status === 'closed' && !codeAction.data?.linked && <ClosedCard incident={i} />}
          {approvals.map(a => <ActionApprovalCard action={a} key={`action-${a.id}`} onChange={reload} />)}
          {!snapshot && !coreLocked && i.status === 'handed_off' && detail.data && <HandoffCard incident={i} view={handoffView ?? handoffFallback(i)} onChange={reload} />}
          {actionableCommands.map(commandCard)}
          {pastCommands.length > 0 && <Disclosure label={`지난 운영자 지시 ${pastCommands.length}건 보기`} open={pastCommandsOpen} onOpen={setPastCommandsOpen}>{() => pastCommands.map(commandCard)}</Disclosure>}
          {!snapshot && <Disclosure label="운영자 지시·한글 해석 안내">{() => <div className="chat-intro"><p>{snapshot ? '저장된 분석 기록입니다. 새 분석은 실행하지 않습니다.' : codeAction.data?.linked ? 'Kubernetes 분석은 이전 기록입니다. 현재 코드 조치는 위 카드에서 확인하세요.' : !open ? '종료된 장애입니다. 기록만 볼 수 있습니다.' : '아래 입력란에서 후속 작업이나 조사 방향을 지시하세요. 변경은 계획과 부작용을 확인한 뒤 진행합니다.'}</p>{data.interpreting && <p role="status">한글 해석 준비 중</p>}{!snapshot && !data.interpreting && <InterpretRequest id={id} state={data.interpret_state?.status} pending={data.interpret_pending_count} onDone={narrative.refresh} />}</div>}</Disclosure>}
          {recordView ? <Disclosure label={`지난 분석 단계 ${steps.length}개 보기`} open={historyOpen} onOpen={setHistoryOpen}>{() => entries.map(x => x.el)}</Disclosure> : entries.map(x => x.el)}
          <Disclosure label="사건 원문·모델 비용 보기">{() => <><dl className="kv"><dt>기록 비용</dt><dd>{costKo(i)}</dd></dl><pre className="raw-text">{JSON.stringify({ title: i.title, status_reason: i.status_reason, headline: data.headline, handoff: i.handoff }, null, 2)}</pre></>}</Disclosure>
          {/* 정돈-03: 기존 코드 변경 요청은 그대로 열람·진행한다. 새 코드 수정·시험·환경 승격은 LiveProbe for Application이 맡는다. */}

          {(changes.data?.items ?? []).map(c => <ChangeRequestCard change={c} key={c.id} onChange={changes.refresh} readOnly={snapshot || coreLocked} />)}
          {!snapshot && !coreLocked && closable(i.status) && detail.data && <CloseCard incident={i} blocked={closeBlocked} onChange={reload} />}

        </div>
        {open && <div className="composer">{!done && <div className="ctrl"><span className="control-status">{control === 'paused' ? '일시정지됨' : control === 'stopping' ? '중단 중' : '자동 분석 중'}</span><button className="btn sm" disabled={busy || control === 'stopping'} onClick={() => void ctl(control === 'paused' ? 'resume' : 'pause')}>{control === 'paused' ? '재개' : '일시정지'}</button><button className="btn sm danger" disabled={busy || control === 'stopping'} onClick={() => void ctl('stop')}>중단</button></div>}<ErrorBox>{error}</ErrorBox>{sent && <p className="composer-notice" role="status">{sent}</p>}<form onSubmit={send}><textarea aria-label="운영자 메시지" rows={2} value={text} maxLength={done ? 8000 : 2000} placeholder={done ? 'AI에게 작업을 지시하세요 (실행 전 부작용을 확인합니다)' : 'AI 분석에 지시하세요 (다음 단계부터 반영)'} onChange={e => setText(e.target.value)} /><button className="btn primary" type="submit" disabled={busy || !text.trim()}>{busy ? '전송 중' : '보내기'}</button></form><small>비밀값은 대화에 쓰지 말고 전용 입력란을 사용하세요.</small></div>}
      </section>
      {/* 좁은 화면에서는 상세가 대화 위에 뜨는 시트이므로 뒤 화면을 가림막으로 덮어 잘린 카드가 그대로 보이지 않게 한다. */}
      {panel && <div className="detail-backdrop" aria-hidden="true" onClick={closeDetail} />}
      {panel && <DetailPanel target={panel} data={data} handoff={handoffView} onClose={closeDetail} snapshot={snapshot} events={events} streamState={snapshot ? 'snapshot' : execution.state} streamError={execution.error} receivedAt={execution.receivedAt} />}
    </div>
  </div>;
}
