// 단계 카드 브리핑 스택의 화면 조각. 계산은 stageBrief.ts가 하고 여기서는 그리기만 한다.
import { useId, useState, type ReactNode } from 'react';
import type { CallRow, Lineage, StageBrief } from './stageBrief';
import type { EvidenceSummary } from './evidenceGroups';
import type { Goal } from './stageGoals';
import { LEGACY_KO, type ReadingView } from './readingText';

const GOAL_MARK: Record<Goal['state'], string> = { ok: '충족', fail: '미충족', working: '진행 중', open: '확인 전' };

export function ModelTags({ models }: { models: string[] }) {
  if (!models.length) return null;
  return <ul className="stage-models" aria-label="호출한 모델">{models.map(m => <li key={m} className="model-tag">{m}</li>)}</ul>;
}

export function Reading({ view, className }: { view: ReadingView; className?: string }) {
  if (!view) return null;
  if ('text' in view) return <p className={className}>{view.text}</p>;
  return <p className={`reading-note${view.note === LEGACY_KO ? ' is-legacy' : ''}${className ? ' ' + className : ''}`}>{view.note}</p>;
}

export function LineageLine({ rows, more, onSelect }: { rows: Lineage[]; more: number; onSelect?: (id: string) => void }) {
  if (!rows.length) return null;
  return <div className="stage-lineage" aria-label="이어받은 판단">{rows.map(r =>
    <button type="button" key={r.source + r.text} className="stage-lineage-row" disabled={!onSelect || r.label === '운영자'} onClick={() => onSelect?.(r.source)}>
      <span className="stage-lineage-kicker">이어받음</span><b>{r.label}</b><span>{r.text}</span>
    </button>)}{more > 0 && <span className="stage-lineage-more">외 {more}</span>}</div>;
}

export function GoalList({ goals }: { goals: Goal[] }) {
  if (!goals.length) return null;
  return <ul className="stage-goals" aria-label="목표 점검">{goals.map(g =>
    <li key={g.key} className={`is-${g.state}`}><span className="goal-mark" aria-hidden="true" /><span><span className="goal-label">{g.label}</span>
      <span className="sr-only"> · {GOAL_MARK[g.state]}</span>{g.detail && <small>{g.detail}</small>}</span></li>)}</ul>;
}

/** The summary panel: what is being done now, the answer so far, the goal check and what remains. */
export function BriefPanel({ brief, extra }: { brief: StageBrief; extra?: ReactNode }) {
  return <section className="stage-brief" aria-label="단계 요약">
    {brief.working && <p className="stage-working" role="status"><span className="working-dot" aria-hidden="true" />진행 중 · {brief.working}</p>}
    <p className="step-lead">{brief.lead}</p>
    {(brief.facts.length > 0 || brief.composition) && <dl className="stage-facts">{brief.facts.map((f, i) => <div key={f.label + i}><dt>{f.label}</dt><dd>{f.value}</dd></div>)}
      {brief.composition && <div><dt>근거 구성</dt><dd>근거 {brief.evidence?.refs ?? 0} ({brief.composition})</dd></div>}</dl>}
    {extra}
    <GoalList goals={brief.goals} />
    {brief.remaining.length > 0 && <div className="stage-remaining"><h3>남은 것</h3><ul>{brief.remaining.map(r => <li key={r}>{r}</li>)}</ul></div>}
  </section>;
}

/** A counted section of the data below the brief. The body renders only while open. */
export function DataSection({ label, count, defaultOpen = false, children }: { label: string; count?: number | null; defaultOpen?: boolean; children: () => ReactNode }) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return <section className="data-section">
    <button type="button" className="data-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(v => !v)}>
      <span>{label}{count != null && <span className="data-count"> {count}</span>}</span><span className="data-chevron" aria-hidden="true" /></button>
    {open && <div className="data-body" id={id}>{children()}</div>}
  </section>;
}

/** One model call per row: role, time, model tag and a one-line summary; the full reasoning opens in place.
 *  A running call starts open so its live answer stays on screen after it completes. */
export function CallRows({ calls, body }: { calls: CallRow[]; body: (call: CallRow) => ReactNode }) {
  return <ol className="call-rows">{calls.map(c => <CallItem key={c.id} call={c} body={body} />)}</ol>;
}
function CallItem({ call, body }: { call: CallRow; body: (call: CallRow) => ReactNode }) {
  const [open, setOpen] = useState(call.running);
  const id = useId();
  return <li className={`call-row${call.running ? ' is-running' : ''}`}>
    <button type="button" className="call-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(v => !v)}>
      <span className="call-head"><b>{call.label}</b>{call.at && <time>{call.at}</time>}{call.model && <span className="model-tag">{call.model}</span>}</span>
      {call.summary && <span className="call-summary">{'text' in call.summary ? call.summary.text : call.summary.note}</span>}
    </button>
    {open && <div className="call-body" id={id}>{body(call)}</div>}
  </li>;
}

/** Evidence grouped by source kind; a repeated source is one row with its citation count. */
export function EvidenceRows({ summary }: { summary: EvidenceSummary }) {
  return <div className="evidence-groups">{summary.groups.map(g => <section key={g.key} className="evidence-group">
    <h4>{g.label}<span className="data-count"> {g.refs}</span></h4>
    <ul>{g.rows.map(r => <EvidenceItemRow key={r.key} row={r} />)}</ul></section>)}</div>;
}
function EvidenceItemRow({ row }: { row: EvidenceSummary['groups'][number]['rows'][number] }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const meta = [row.count > 1 ? '인용 ' + row.count : null, row.roles.join(' · ') || null, row.at != null ? new Date(row.at).toLocaleString('ko-KR') : null].filter(Boolean).join(' · ');
  return <li className="evidence-row">
    <button type="button" className="evidence-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(v => !v)}>
      <span className="evidence-label"><b>{row.label}</b>{row.fingerprint && <span className="evidence-flag">지문 인용</span>}{!row.known && <span className="evidence-flag is-warn">수집 근거와 연결 안 됨</span>}</span>
      {meta && <small>{meta}</small>}
      {row.summary && <span className="evidence-summary">{'text' in row.summary ? row.summary.text : row.summary.note}</span>}
    </button>
    {open && <div className="evidence-body" id={id}>{row.detail && <p className="evidence-path">{row.detail}</p>}
      {row.quotes.length ? row.quotes.map(q => <pre key={q} className="raw-text">{q}</pre>) : <p>발췌 미기록</p>}</div>}
  </li>;
}
