import type { FlowNode, Narrative } from './api';
import type { RunEvent } from './feed';
import { dispatchFor } from './stepPanel';
import { stepExecution, type StreamState } from './execution';
import { ExecutionStream } from './ExecutionStream';
import { effortKo } from './labels';
import { flowStatusKo, flowTitle } from './narrative';
import { executionStageKo } from './presentation';
import { Disclosure } from './Disclosure';
import { EscalationNote } from './Explain';
import { AccountabilityReading, AgentReading, EvidenceTimeline, GroupReading, RoutingReading, SignalReading, reading, timeText } from './StageReadings';

type Props = {
  data:Narrative; node: FlowNode | null; events: RunEvent[]; historical: boolean; current: boolean;
  summary: string; state: StreamState; error: boolean; receivedAt: string | null; readOnly:boolean;
  onSelect:(id:string)=>void; onChange:()=>void;
};
export function StepPanel({data,node,events,historical,current,summary,state,error,receivedAt,readOnly,onSelect,onChange}:Props) {
  if (!node) return <section className="step-panel"><h2>단계 미관측</h2><p>아직 이 사건에 기록된 단계가 없습니다.</p></section>;
  const c=node.card;
  const agent=['agent','verify','router'].includes(node.type);
  const dispatch=dispatchFor(node,events);
  const view=stepExecution(node,events);
  const live=!historical && node.status==='running' && !view.final;
  const last=view.records.at(-1);
  const nextIds=new Set(data.flow.edges.filter(e=>e.source===node.id).map(e=>e.target));
  const next=data.flow.nodes.filter(n=>nextIds.has(n.id) && n.type!=='interpret');
  const task=reading(dispatch?.task_ko ?? node.job_ko,'요청 내용 미기록');
  const target=c?.kind==='action'?c.target:data.incident.namespace+'/'+data.incident.workload;
  const decision=c?.kind==='outcome' ? data.accountability : undefined;
  return <section className="step-panel" aria-label="선택 단계 입력과 결과" tabIndex={-1} data-selected-step={node.id}>
    <div className="step-status"><span>{historical?'저장된 단계':current?'현재 작업':'선택한 단계'}</span><span>{flowStatusKo(node.status)}</span></div>
    <h2>{flowTitle(node)}</h2>
    {node.model && <p className="step-model">{node.model} · {effortKo(node.effort)}</p>}
    {c?.kind==='signal' ? <SignalReading card={c} data={data}/>
      : c?.kind==='evidence' ? <EvidenceTimeline card={c} data={data}/>
      : c?.kind==='routing' ? <RoutingReading card={c}/>
      : c?.kind==='group' ? <GroupReading card={c} node={node} data={data} choose={onSelect}/>
      : c?.kind==='agent' ? <AgentReading card={c} node={node} data={data}/>
      : c?.kind==='action' ? <><p className="step-lead">{c.type_ko} · {c.status_ko}</p><p>대상 {c.target}</p>{c.changes.map((s,i)=><p key={i}>{reading(s)}</p>)}</>
      : <p className="step-lead">{summary}</p>}
    {c?.kind==='outcome' && c.handoff && <dl className="stage-facts">{c.handoff.decisions.map(d=><div key={d.key}><dt>{d.label_ko}</dt><dd>{reading(d.value_ko)}</dd></div>)}</dl>}
    {c?.kind==='outcome' && !!c.handoff?.next_steps_ko.length && <section><h3>다음 조치</h3><ol>{c.handoff.next_steps_ko.map((step,i)=><li key={i}>{reading(step)}</li>)}</ol></section>}
    {decision && decision.status!=='not_applicable' && <AccountabilityReading id={data.incident.id} value={decision} canWrite={!readOnly} onChange={onChange}/>}
    {node.type==='investigation' && <dl className="stage-facts"><dt>조사 질문</dt><dd>{reading(node.job_ko,'질문 미기록')}</dd>
      <dt>조사 결과</dt><dd>{reading(node.conclusion_ko,node.status==='running'?'근거를 수집하고 있습니다.':'결론 미기록')}</dd>
      {!!node.inputs.length && <><dt>찾은 근거</dt><dd>{node.inputs.join(' · ')}</dd></>}</dl>}
    {node.type==='investigation' && !!node.source_activity?.length && <section><h3>외부 실행기의 소스 조사</h3>
      <ol className="stage-timeline">{node.source_activity.map(event=><li key={event.job_id+':'+event.attempt+':'+event.seq}>
        <div className="timeline-meta"><time>{typeof event.at==='number'?timeText(new Date(event.at*1000).toISOString()):'시각 미관측'}</time>
          <span>시도 {event.attempt}</span></div><strong>{event.phase==='read_file'?'소스 파일 확인':'배포 이미지와 소스 대조'}</strong>
        {event.file && <p>{event.file}</p>}</li>)}</ol></section>}
    <EscalationNote n={node}/>
    {(agent || c?.kind==='action' || node.type==='investigation') && <section className="step-activity" aria-label="에이전트 작업 흐름">
      <h3>{live?'현재 작업':'작업 기록'}</h3><ol className="work-path">
        <li><small>담당</small><b>{node.role_ko || flowTitle(node)}</b></li>
        <li><small>대상</small><b>{target}</b></li>
        <li key={last?.seq ?? node.status} className={live && last ? 'activity-arrived' : ''}><small>작업</small><b>{agent ? (last?executionStageKo(last.kind,last.detail?.phase,last.detail?.status):flowStatusKo(node.status)) : c?.kind==='action'?c.status_ko:flowStatusKo(node.status)}</b></li>
      </ol>{agent && <p><strong>전달한 요청</strong> {task}</p>}
      {dispatch && <p className="stage-input-count">전달 근거 {dispatch.evidence_refs.length} · 참고 자료 {dispatch.rag_refs.length} · 운영 지시 {dispatch.operator_message_ids.length}</p>}
      {(view.records.length>0 || (agent && live)) && <ExecutionStream node={node} view={view} state={state} error={error} receivedAt={receivedAt}/>}
      {!agent && node.at && <time>{timeText(node.at)}</time>}</section>}
    {!!next.length && <section className="step-following"><h3>연결된 다음 작업</h3>{next.map(n=><button className="btn sm" type="button" key={n.id} onClick={()=>onSelect(n.id)}>{flowTitle(n)} · {flowStatusKo(n.status)} →</button>)}</section>}
    {node.conclusion_raw && !c && <Disclosure label="결론 원문 상세보기">{()=> <pre className="raw-text">{node.conclusion_raw}</pre>}</Disclosure>}
  </section>;
}
