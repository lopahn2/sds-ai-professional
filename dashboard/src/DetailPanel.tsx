import { useEffect, useRef } from 'react';
import { clock, money, type FlowNode, type HandoffView, type Narrative, type RawItem } from './api';
import { flowStatusKo, flowTitle, FLOW_TYPE_KO, matchesRef, resolveStepId } from './narrative';
import { numberKo } from './overviewModel';
import { effortKo, KIND_KO } from './labels';
import type { DetailTarget } from './StepCards';
import { ExecutionStream } from './ExecutionStream';
import { stepExecution, type StreamState } from './execution';
import type { RunEvent } from './feed';
import { Disclosure } from './Disclosure';

function Raw({ label, text }: { label: string; text: string | null | undefined }) {
  if (!text) return null;
  return <div className="detail-raw"><h4>{label}</h4><pre className="raw-text">{text}</pre></div>;
}
function RawRow({ r }: { r: RawItem }) {
  return <div className="raw-item"><div className="raw-ref"><code>{r.id}</code><time>{clock(r.at)}</time></div><pre className="raw-text">{r.raw}</pre></div>;
}
/** Verbatim records and technical metadata for one step. Opens only from 상세보기; Esc/닫기 closes and returns focus. */
export function DetailPanel({ target, data, handoff, onClose, events = [], streamState = 'connecting', streamError = false, receivedAt = null, snapshot = false }: { target: DetailTarget; data: Narrative; handoff: HandoffView | null; onClose: () => void; events?: RunEvent[]; streamState?: StreamState; streamError?: boolean; receivedAt?: string | null; snapshot?: boolean }) {
  const close = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  // 고정 시트일 때만 배경을 inert로 만든다. 기존 속성은 소유하지 않으므로 종료/폭 변경 시 그대로 복원한다.
  useEffect(() => {
    const sheet = panel.current;
    if (!sheet) return;
    const media = window.matchMedia('(max-width: 1100px)');
    let restore: (() => void)[] = [];
    const release = () => { restore.forEach(f => f()); restore = []; };
    const sync = () => {
      release();
      if (!media.matches) return;
      let branch: HTMLElement = sheet;
      while (branch.parentElement) {
        for (const sibling of Array.from(branch.parentElement.children)) {
          if (sibling === branch || !(sibling instanceof HTMLElement) || sibling.classList.contains('detail-backdrop')) continue;
          const previous = sibling.getAttribute('inert');
          sibling.setAttribute('inert', '');
          restore.push(() => { if (previous === null) sibling.removeAttribute('inert'); else sibling.setAttribute('inert', previous); });
        }
        branch = branch.parentElement;
        if (branch === document.body) break;
      }
      if (!sheet.contains(document.activeElement)) close.current?.focus({ preventScroll: true });
    };
    const trap = (event: KeyboardEvent) => {
      if (!media.matches || event.key !== 'Tab') return;
      const controls = Array.from(sheet.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, [tabindex="0"]'))
        .filter(e => e.getClientRects().length > 0 && !e.closest('[inert]'));
      const first = controls[0] ?? close.current, last = controls.at(-1) ?? close.current;
      if (!sheet.contains(document.activeElement) || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
        event.preventDefault(); (event.shiftKey ? last : first)?.focus({ preventScroll: true });
      }
    };
    sync(); media.addEventListener('change', sync); document.addEventListener('keydown', trap);
    return () => { release(); media.removeEventListener('change', sync); document.removeEventListener('keydown', trap); };
  }, []);
  const n: FlowNode | undefined = data.flow.nodes.find(x => x.id === resolveStepId(target.nodeId, data.flow.nodes));
  useEffect(() => { close.current?.focus({ preventScroll: true }); }, [target]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); onClose(); } };
    document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key);
  }, [onClose]);
  const c = n?.card ?? null;
  let title = n ? flowTitle(n) : '단계'; let body: React.ReactNode;
  if (!n) body = <p className="muted">이 단계는 최신 기록에 없습니다.</p>;
  else if (target.kind === 'evidence') {
    const item = c?.kind === 'evidence' ? c.items[target.index] : null;
    title = item ? `${item.source_ko} · 원문` : '수집 항목 원문';
    const uniqueRef = item?.ref && c?.kind === 'evidence' && c.items.filter(x => x.ref === item.ref).length === 1;
    // 행 원문이 비어 있어도 최신 공통 묶음으로 과거 스냅샷을 대신하지 않는다.
    const rows = item?.raw_items ?? (uniqueRef ? data.raw.filter(r => matchesRef(r, item!.ref!)) : []);
    body = <>
      {item && <dl className="kv"><dt>출처</dt><dd className="mono">{item.source}</dd><dt>대상</dt><dd className="mono">{item.target}</dd><dt>수집 상태</dt><dd>{item.ok ? '성공' : '실패'}</dd><dt>수집 시각</dt><dd>{clock(item.collected_at)}</dd><dt>근거 ID</dt><dd className="mono">{item.ref || '연결된 원문 없음'}</dd></dl>}
      {item?.error && <Raw label="실패 사유 원문" text={item.error} />}
      <h4>원문 {rows.length}줄</h4>
      {rows.length ? rows.map(r => <RawRow r={r} key={r.id} />) : <p className="muted">이 항목의 원문은 현재 응답에 없습니다.</p>}
    </>;
  } else {
    const inputs = [...new Set(n.inputs ?? [])].map(ref => ({ ref, rows: data.raw.filter(r => matchesRef(r, ref)) }));
    // 근거 수집: 단계 전체 상세는 수집 항목마다 요약·실패 사유·원문을 펼쳐 볼 수 있게 한다(행 원문 우선, 단일 출처만 공통 묶음 사용).
    const evidence = c?.kind === 'evidence' ? c.items.map(it => {
      const unique = it.ref && c.items.filter(x => x.ref === it.ref).length === 1;
      return { it, rows: it.raw_items ?? (unique ? data.raw.filter(r => matchesRef(r, it.ref!)) : []) };
    }) : [];
    // 한글 해석: 표시용 해석 결과(원문 → 한글)를 그대로 보여 준다. 분석 판단에는 쓰지 않는다.
    const translated = n.type === 'interpret' ? data.raw.filter(r => r.ko_source === 'llm' && r.ko) : [];
    const shown = Boolean(n.conclusion_raw || inputs.length || evidence.length || translated.length
      || c?.kind === 'routing' || c?.kind === 'group'
      || (c?.kind === 'agent' && (c.summary_raw || c.candidates.length || c.error))
      || (c?.kind === 'signal' && c.reason) || (n.type === 'outcome' && handoff));
    body = <>
      {['agent', 'verify', 'router'].includes(n.type) && <ExecutionStream key={n.call_id || n.id} node={n} view={stepExecution(n, events)} state={streamState} error={streamError} receivedAt={receivedAt} />}
      <dl className="kv">
        <dt>단계</dt><dd>{FLOW_TYPE_KO[n.type] ?? '단계'} · {snapshot ? '저장 당시 ' : ''}{flowStatusKo(n.status)}</dd>
        <dt>시각</dt><dd>{n.at ? clock(n.at) : '미기록'}</dd>
        {(n.role || n.model) && <><dt>모델 · 추론 강도</dt><dd><span className="mono">{n.model || '미기록'}</span> · {effortKo(n.effort)}</dd>
          <dt>입력 / 출력 토큰</dt><dd>{numberKo(n.tokens?.in)} / {numberKo(n.tokens?.out)}</dd>
          <dt>비용 · 소요 시간</dt><dd>{money(n.cost_usd)} · {n.duration_ms == null ? '미기록' : `${numberKo(n.duration_ms / 1000, 1)}초`}</dd></>}
      </dl>
      {n.job_ko && <p className="muted">{n.job_ko}</p>}
      <Raw label="단계 제목 원문" text={n.label} />
      <Disclosure key={n.id} label="단계 응답 전체 원문 보기">{() => <pre className="raw-text">{JSON.stringify(n, null, 2)}</pre>}</Disclosure>
      <Raw label="결론 원문" text={n.conclusion_raw} />
      {c?.kind === 'evidence' && <><h4>수집 {c.total}건 · 성공 {c.ok} · 실패 {c.failed}</h4>
        {evidence.map(({ it, rows }) => <details key={it.id} className="detail-input" data-evidence-id={it.id}>
          <summary><b>{it.source_ko}</b> · <code>{it.target}</code> · {it.ok ? `원문 ${rows.length}줄` : '수집 실패'}</summary>
          {it.summary_ko && <p>{it.summary_ko}</p>}
          {it.error && <Raw label="실패 사유 원문" text={it.error} />}
          {rows.length ? rows.map(r => <RawRow r={r} key={r.id} />) : <p className="muted">이 항목의 원문은 현재 응답에 없습니다.</p>}
        </details>)}</>}
      {c?.kind === 'routing' && <><Raw label="판단 사유 원문" text={c.reason_raw} /><Raw label="AI 분류 의견 원문" text={c.llm?.rationale_raw} />{c.model_policy && <><h4>분석 모델 후보</h4><ul className="reason-list">{c.model_policy.candidates.map(m => <li key={m.model}><span className="mono">{m.model}</span> · {m.eligible ? '적격' : '부적격'} · 점수 {m.score ?? '미기록'} · {m.reason_ko}</li>)}</ul></>}</>}
      {c?.kind === 'group' && <>
        <dl className="kv"><dt>Group</dt><dd>{c.group_ko}</dd><dt>판단</dt><dd>{c.decision.level_ko} · {c.decision.decided_by_ko}{c.decision.confidence == null ? '' : ` · 신뢰도 ${c.decision.confidence.toFixed(2)}`}</dd><dt>상태</dt><dd>{c.status_ko}</dd></dl>
        {c.decision.final_reason_ko && <p>{c.decision.final_reason_ko}</p>}
        {c.decision.steps.length > 0 && <><h4>판단 과정 전체</h4><ul className="reason-list">{c.decision.steps.map((s, i) => <li key={i}><b>{s.title_ko}</b> {s.detail_ko}{s.items_ko?.length ? <ul>{s.items_ko.map((x, j) => <li key={j}>{x}</li>)}</ul> : null}</li>)}</ul></>}
        <h4>역할 구성</h4><ul className="reason-list">{c.roles.map(r => <li key={r.role}><b>{r.role_ko || r.role}</b> · <span className="mono">{r.model || '모델 미기록'}</span> · 추론 {effortKo(r.effort)} · {flowStatusKo(r.status)}{r.job_ko ? ` — ${r.job_ko}` : ''}</li>)}</ul>
        {c.budget && <><h4>예산</h4><p className="muted">Agent 호출 최대 {c.budget.max_agent_calls ?? '미기록'}회 · 비용 상한 {c.budget.max_cost_usd == null ? '미기록' : `${c.budget.max_cost_usd} USD`} · 제한 시간 {c.budget.deadline_s == null ? '미기록' : `${c.budget.deadline_s}초`}</p></>}
        {c.progress_ko.length > 0 && <><h4>진행 기록</h4><ul className="reason-list">{c.progress_ko.map((p, i) => <li key={i}>{p}</li>)}</ul></>}
      </>}
      {c?.kind === 'agent' && <><Raw label="요약 원문" text={c.summary_raw} />{c.candidates.length > 0 && <><h4>원인 후보 원문</h4><ol className="reason-list">{c.candidates.map((x, i) => <li key={i}>{x.cause_ko && <p>{x.cause_ko}</p>}<span className="raw-inline">{x.cause_raw}</span> · {x.confidence == null ? '신뢰도 미기록' : `신뢰도 ${x.confidence.toFixed(2)}`}{x.verifier ? ` · 검증 ${({ supported: '근거로 지지', unconfirmed: '미확인', contradicted: '반박됨', insufficient: '근거 부족' } as Record<string, string>)[x.verifier] ?? x.verifier}` : ''}</li>)}</ol></>}<Raw label="오류 원문" text={c.error} /></>}
      {c?.kind === 'signal' && <Raw label="감지 사유 원문" text={c.reason} />}
      {translated.length > 0 && <><h4>해석 {translated.length}건</h4>
        {translated.map(r => <details key={r.id} className="detail-input"><summary>{KIND_KO[r.kind] ?? '기록'} · <code>{r.id}</code></summary><p>{r.ko}</p><pre className="raw-text">{r.raw}</pre></details>)}</>}
      {n.type === 'outcome' && handoff && <><Raw label="인계 요약 원문" text={handoff.summary_raw} />{handoff.next_steps_raw.length > 0 && <><h4>권장 조치(원문)</h4><ul className="reason-list">{handoff.next_steps_raw.map((s, i) => <li key={i} className="mono">{s}</li>)}</ul></>}</>}
      {inputs.length > 0 && <><h4>입력 근거 원문</h4>{inputs.map(x => <details key={x.ref} className="detail-input"><summary><code>{x.ref}</code> · {x.rows.length ? `${x.rows.length}줄` : '원문 없음'}</summary>{x.rows.map(r => <pre className="raw-text" key={r.id}>{r.raw}</pre>)}</details>)}</>}
      {!shown && <p className="muted">{snapshot ? '저장된 시점에 확정 결론 원문이 없었습니다.' : n.status === 'planned' || n.status === 'running' ? '확정 결론 원문은 단계가 완료된 뒤 표시됩니다.' : '이 단계에 연결된 원문 기록이 없습니다.'}</p>}
    </>;
  }
  return <aside ref={panel} className="detail-panel" role="complementary" aria-label="단계 상세" data-node-id={target.nodeId}>
    <header className="detail-head"><div><span className="eyebrow">원문 보기</span><h3>{title}</h3></div><button ref={close} type="button" className="btn sm" onClick={onClose}>닫기</button></header>
    <div className="detail-scroll">{body}</div>
  </aside>;
}
