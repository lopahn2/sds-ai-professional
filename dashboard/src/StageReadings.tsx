import { useState } from 'react';
import type { AgentCard, EvidenceCard, FlowNode, GroupCard, Narrative, RawItem, RoutingCard, SignalCard } from './api';
import { ApiError, request } from './api';
import { Disclosure } from './Disclosure';
import { CitationLink } from './CitationLink';
import { GroupProgress, ModelChoice, VerifyResult } from './Explain';
import { effortKo } from './labels';
import { flowStatusKo, flowTitle, rawForRef } from './narrative';
import { summaryKo } from './presentation';
import { chronological, citedReading, evidenceQuestion, latestServiceReadings } from './stageEvidence';
import { groupSteps } from './roomWorkflow';
import { readingStatus } from './readingStatus';
import { whenKo } from './history';
import type { Accountability } from './accountability';

/** Stored model conclusions may append a quoted log or diff to a readable explanation. */
export function reading(value:unknown,fallback='해석 미관측') {
  if (typeof value!=='string') return fallback;
  const head=value.split(/(?:\s근거\s*[:：]\s*(?:source:|[\x22`{])|\n(?:Traceback|at |\s*[{]|diff --git|--- a\/))/u,1)[0];
  const safe=summaryKo(head,'',1800);
  if(safe) return safe;
  const beginning=[];
  for (const sentence of head.split(/(?<=[.!?])\s+|\n+/).slice(0,3)) {
    const text=summaryKo(sentence,'',700);
    if (!text) break;
    beginning.push(text);
  }
  return beginning.join(' ') || fallback;
}
export const timeText = (at?: string | null) => at && Number.isFinite(Date.parse(at)) ? whenKo(at) : '시각 미관측';

function Original({text, label='상세보기'}:{text?:string | null; label?:string}) {
  return text ? <Disclosure label={label}>{() => <pre className="raw-text">{text}</pre>}</Disclosure> : null;
}

export function SignalReading({card,data}:{card:SignalCard;data:Narrative}) {
  const items = data.flow.nodes.flatMap(n => n.card?.kind === 'evidence' ? n.card.items : []);
  const latest = latestServiceReadings(data.incident, items);
  return <><p className="step-lead">{reading(card.what_ko, '감지 설명 미관측')}</p>
    <dl className="stage-facts"><dt>오류 유형</dt><dd>{reading(card.reason_ko, reading(data.incident.title_ko, '유형 미관측'))}</dd>
      <dt>최초 감지</dt><dd>{timeText(card.first_at)} · {card.place_ko}</dd>
      <dt>신호 출처</dt><dd>{reading(card.source_ko,'출처 미관측')} · {card.signal_count ?? '미관측'}건</dd></dl>
    <h3>최근 관측</h3>{latest.length ? <ul className="stage-observations">{latest.map(i => <li key={i.id}><strong>{evidenceQuestion(i)}</strong>
      <span>{reading(i.summary_ko)}</span><time>{timeText(i.collected_at)}</time></li>)}</ul> : <p>{reading(data.headline.impact,'후속 관측 미기록')}</p>}
    {data.incident.recovery_kind === 'observed' && <p className="stage-note">정상 상태를 관측했습니다. 복구 당시의 근거는 결과 단계에서 확인할 수 있습니다.</p>}
    <p>사용자 영향 · {reading(data.headline.impact,'영향 미관측')}</p><Original text={card.reason} label="감지 신호 상세보기"/></>;
}

function RawLines({rows,error}:{rows:RawItem[];error:string|null}) {
  return rows.length || error ? <Disclosure label="관측 원문 상세보기">{() => <>{error && <pre className="raw-text">{error}</pre>}
    {rows.map(r => <pre key={r.id} className="raw-text">{r.raw}</pre>)}</>}</Disclosure> : null;
}

export function EvidenceTimeline({card,data}:{card:EvidenceCard;data:Narrative}) {
  const [question,setQuestion] = useState<string | null>(null);
  const all = chronological(card.items);
  const tags = Array.from(new Set(all.map(evidenceQuestion)));
  const rows = question ? all.filter(i => evidenceQuestion(i) === question) : all;
  return <><p className="step-lead">수집 {card.total}건 · 성공 {card.ok}건 · 실패 {card.failed}건</p>
    <ul className="question-changes" aria-label="조사 질문별 변화">{tags.map(tag=>{
      const observations=all.filter(item=>evidenceQuestion(item)===tag);
      const first=observations[0], last=observations[observations.length-1];
      const describe=(item:typeof first)=>item.ok?reading(item.summary_ko,'수집 성공 · 설명 미관측'):'수집 실패';
      return <li key={tag}><button type="button" className="link-btn" aria-label={tag} aria-pressed={question===tag} onClick={()=>setQuestion(tag)}>{tag} · {observations.length}건</button>
        <span>최초 {describe(first)} <time>{timeText(first.collected_at)}</time></span>
        {last.id!==first.id && <span>최근 {describe(last)} <time>{timeText(last.collected_at)}</time></span>}</li>;
    })}</ul>
    <h3>시간순 관측 {rows.length}건</h3>
    {question && <button type="button" className="link-btn" onClick={() => setQuestion(null)}>전체 관측 보기</button>}
    <ol className="stage-timeline" aria-label="시간순 관측 기록">{rows.map(it => {
      const unique = it.ref && all.filter(i => i.ref === it.ref).length === 1;
      const originals = it.raw_items ?? (unique && it.ref ? rawForRef(data.raw,it.ref) : []);
      const ko = [...new Set([it.summary_ko,...originals.map(r=>r.ko)].map(v=>reading(v,'')).filter(Boolean))];
      return <li key={it.id}><div className="timeline-meta"><time>{timeText(it.collected_at)}</time><span>{evidenceQuestion(it)}</span></div>
        <b>{it.target || it.source_ko}</b>{it.ok ? ko.length ? ko.map(t=><p key={t}>{t}</p>) : <p>수집 성공 · 설명 미관측</p> : <p className="warning-text">이 출처를 수집하지 못했습니다.</p>}
        {[...new Set(originals.filter(r=>r.reading_state && r.reading_state!=='ready').map(r=>readingStatus(r.reading_state)))].map(label=><small key={label}>{label}</small>)}
        <RawLines rows={originals} error={it.error}/></li>;
    })}</ol></>;
}

export function RoutingReading({card}:{card:RoutingCard}) {
  return <><p className="step-lead">{reading(card.final_reason_ko, card.interpreting ? '판단 이유를 해석하고 있습니다.' : '판단 이유 미관측')}</p>
    <p>{card.level_ko} · {card.decided_by_ko} {typeof card.confidence === 'number' && <>· 분류 신뢰도 {Math.round(card.confidence*100)}%</>}</p>
    <ol className="stage-timeline">{card.steps.filter(s => s.key !== 'final' && s.key !== 'model').map((s,i) => <li key={i}>
      <strong>{s.title_ko}</strong><p>{reading(s.detail_ko)}</p>{s.items_ko?.map((v,k) => <p key={k}>{reading(v)}</p>)}</li>)}</ol>
    <ModelChoice policy={card.model_policy} full/><Original label="판단 근거 상세보기" text={card.reason_raw}/></>;
}

export function GroupReading({node,card,data,choose}:{node:FlowNode;card:GroupCard;data:Narrative;choose?:(id:string)=>void}) {
  const steps = groupSteps(data.flow,node);
  return <><p className="step-lead">{reading(card.decision.final_reason_ko, '팀 구성 이유 미기록')}</p>
    <ul className="group-team" aria-label="담당 작업과 실행 순서">{card.roles.map(r => {
      const matches=steps.filter(n=>n.role===r.role);
      const predecessors=new Set(data.flow.edges.filter(e=>matches.some(n=>n.id===e.target)).map(e=>e.source));
      const before=steps.filter(n=>predecessors.has(n.id));
      const parallel=steps.filter(n=>n.role!==r.role && matches.some(m=>m.depth===n.depth));
      return <li key={r.role}><strong>{r.role_ko}</strong><span>{reading(r.job_ko,'역할 설명 미기록')}</span>
        <small>{r.model || '모델 미관측'} · {effortKo(r.effort)} · {flowStatusKo(r.status)}</small>
        {before.length>0 && <small>선행 작업 · {[...new Set(before.map(flowTitle))].join(' · ')}</small>}
        {parallel.length>0 && <small>같은 단계의 병렬 역할 · {[...new Set(parallel.map(flowTitle))].join(' · ')}</small>}
        {choose && matches.map(n=><button type="button" key={n.id} className="link-btn" onClick={()=>choose(n.id)}>{flowTitle(n)} · 작업 보기</button>)}</li>;
    })}</ul><GroupProgress items={card.progress_ko}/></>;
}

export function AgentReading({node,card,data,hideLead=false}:{node:FlowNode;card:AgentCard;data:Narrative;hideLead?:boolean}) {
  const items = data.flow.nodes.flatMap(n=>n.card?.kind==='evidence'?n.card.items:[]);
  const verify = node.type === 'verify';
  return <>
    {verify && <VerifyResult card={card}/>}
    {!hideLead && <p className="step-lead">{reading(card.summary_ko ?? node.conclusion_ko, node.status === 'running' ? '분석 요청을 수행하고 있습니다.' : '판단 요약 미관측')}</p>}
    {card.acceptance?.rejected && <p className="stage-note">{reading(card.acceptance.reason_ko,'근거 검사를 통과하지 못했습니다.')}</p>}
    {card.error && <><p className="warning-text">{reading(card.error_ko,'분석 실행에 실패했습니다.')}</p><Original text={card.error} label="실행 오류 상세보기"/></>}
    {!!card.counter_evidence?.length && <section><h3>{node.role === 'cause_reasoner' ? '확정하지 못한 이유' : '반대 근거와 남은 의문'}</h3>
      {card.counter_evidence.map((c,i)=><div key={i}><p>{reading(c.text_ko,'해석 미관측')}</p><Original text={c.text_raw} label="반대 근거 상세보기"/></div>)}</section>}
    {card.candidates.length>0 && <section><h3>{node.role==='recovery_planner'?'조치가 다루는 원인':'원인 후보'}</h3>
      {card.candidates.map((c,i)=><div key={i}><p>{reading(c.cause_ko,'원인 후보 해석 미관측')}</p><Original text={c.cause_raw} label="후보 원문 상세보기"/></div>)}</section>}
    {!!card.quotes?.length && <section><h3>판단에 사용한 관측</h3><ul className="stage-observations">{card.quotes.map((q,i)=>{
      const ko = citedReading(q,items,data.raw);
      return <li key={i}><strong>{q.ref_ko || q.ref}</strong><span>{reading(ko ?? q.quote,q.ref_known ? '인용 구절의 한글 해석 미관측' : '수집 근거와 연결되지 않은 인용')}</span>
        {q.candidate_index!=null && <small>후보 {q.candidate_index + 1}의 근거</small>}
        <Original text={q.quote} label="인용 상세보기"/></li>;
    })}</ul></section>}
    {!!card.citations?.length && <section><h3>판단에 참고한 자료</h3>{card.citations.map(c=><div key={c.chunk_id}>
      <CitationLink citation={{...c,score:c.score??0}}/> {c.section || c.locator}
      <p>{reading(c.content,'발췌 해석 미관측')}</p><Original text={c.content} label="문서 발췌 상세보기"/></div>)}</section>}
    {card.proposed_action && <section><h3>제안한 변경</h3><dl className="stage-facts">
      <dt>대상과 조치</dt><dd>{card.proposed_action.target} · {card.proposed_action.type_ko}</dd>
      <dt>영향 범위</dt><dd>{reading(card.proposed_action.impact_ko,'영향 범위 미기록')}</dd>
      <dt>성공 확인 조건</dt><dd>{reading(card.proposed_action.success_condition_ko,'성공 조건 미기록')}</dd>
      <dt>원복 조건</dt><dd>{reading(card.proposed_action.rollback_condition_ko,'원복 조건 미기록')}</dd>
    </dl></section>}
    {!!card.recommended_steps_ko?.length && <section><h3>제안한 조치와 확인 순서</h3>
      <ol>{card.recommended_steps_ko.map((step,i)=><li key={i}>{reading(step,'조치 설명 해석 미관측')}</li>)}</ol></section>}

  </>;
}

export function AccountabilityReading({id,value,canWrite,onChange,basePath}:{id:number|string;basePath?:string;value:Accountability;canWrite:boolean;onChange:()=>void}) {
  const [busy,setBusy]=useState(false), [message,setMessage]=useState(''),[note,setNote]=useState('');
  const confirm=value.status==='pending_operator';
  const action=confirm?'confirm':'reinvestigate';
  const submit=async()=>{
    if (busy) return;
    setBusy(true);setMessage('');
    try {
      await request((basePath || '/incidents/'+id+'/accountability')+'/'+action,{method:'POST',body:JSON.stringify({revision:value.revision,...(confirm&&note.trim()?{note:note.trim()}:{})})});
      setMessage(confirm?'운영자 결정을 기록했습니다.':'후속 조사를 접수했습니다.');setNote('');onChange();
    } catch(e) { setMessage(e instanceof ApiError && e.status===409?'소명 기록이 갱신되었습니다. 최신 내용을 확인하세요.':'소명 요청에 실패했습니다.');onChange(); } finally{setBusy(false);}
  };
  return <section className="accountability-reading"><h3>결과 소명</h3><dl className="stage-facts">
    <dt>감지한 이유</dt><dd>{reading(value.detection_ko,'감지 근거 미관측')}</dd>
    <dt>정상화된 이유</dt><dd>{reading(value.recovery_ko,'복구 원인 미확정')}</dd>
    {value.recovery_observed_at && <><dt>복구 관측</dt><dd>{timeText(value.recovery_observed_at)}</dd></>}
    {value.limits_ko && <><dt>근거의 한계</dt><dd>{(Array.isArray(value.limits_ko)?value.limits_ko:[value.limits_ko]).map((v,i)=><p key={i}>{reading(v)}</p>)}</dd></>}
    {value.evidence_refs?.length>0 && <><dt>소명 근거</dt><dd>{value.evidence_refs.join(' · ')}</dd></>}
    {value.decision_at && <><dt>최종 결정</dt><dd>{timeText(value.decision_at)} · {value.decision_by || '운영자 미관측'}</dd></>}
    {value.decision_note && <><dt>운영자 판단</dt><dd>{reading(value.decision_note,'운영자 기록 상세에서 확인')}<Original text={value.decision_note}/></dd></>}
    </dl>{confirm && <p className="stage-note">{value.recovery_observed_at ? '서비스 복구 관측 · 운영자 소명 확인 대기' : '원래 실패 지점의 복구 관측 미기록 · 운영자 소명 확인 대기'}</p>}
    {canWrite && (confirm || value.status==='unreviewed') && <form onSubmit={e=>{e.preventDefault();void submit();}}>
      {confirm && <label>운영자 판단 근거 (선택)<textarea maxLength={1000} value={note} onChange={e=>setNote(e.target.value)}/></label>}
      <button type="submit" className="btn sm" disabled={busy}>{confirm?(value.awaiting_closure===false?'과거 소명 확인 기록':'소명 확인·정상 확정'):'후속 재조사 요청'}</button></form>}
    {message && <p role="status">{message}</p>}</section>;
}
