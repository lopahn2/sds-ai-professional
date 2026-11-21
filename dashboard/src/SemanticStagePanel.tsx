import type { FlowNode, Narrative, AgentCard } from './api';
import {Link} from 'react-router-dom';
import type { RunEvent } from './feed';
import {stepExecution, type StreamState} from './execution';
import {ExecutionStream} from './ExecutionStream';
import {dispatchFor} from './stepPanel';
import {flowStatusKo, flowTitle} from './narrative';
import {effortKo} from './labels';
import {isAppStep, type AppSummary} from './applicationFlow';
import {type useAppActivity} from './ApplicationWorkTrace';
import type {SemanticNode, SemanticKind} from './semanticFlow';
import {AccountabilityReading, AgentReading, EvidenceTimeline, GroupReading, RoutingReading, SignalReading, reading, timeText} from './StageReadings';
import {EscalationNote, VerifyResult} from './Explain';
import {citedReading} from './stageEvidence';
import {Disclosure} from './Disclosure';
import {ApplicationCardReading} from './ApplicationCardReading';
import './semanticStage.css';
import { useStageTravel } from './StageMotion';

export type CoreStepContext={data:Narrative;events:RunEvent[];historical:boolean;state:StreamState;error:boolean;receivedAt:string|null;readOnly:boolean};
export type AppStepContext={summary:AppSummary;activity:ReturnType<typeof useAppActivity>};

const questions:Record<SemanticKind,string>={
  detection:'어떤 장애 신호를 언제 감지했나?', evidence:'시간이 지나면서 무엇이 달라졌나?',
  routing:'왜 이 등급과 조사팀을 선택했나?', investigation:'어떤 원인이 근거를 설명하고 무엇이 남았나?',
  verification:'판단을 수락하거나 반려한 결정적 근거는 무엇인가?', action:'무엇을 바꾸고 어떻게 확인하나?',
  test_build:'원래 실패한 동작이 수정 후 같은 시험을 통과했나?', dev_validation:'시험한 버전이 dev에서 실제로 동작했나?',
  approval:'같은 버전의 prd 배포를 승인할 수 있나?', recovery:'원래 실패한 동작이 어느 환경에서 정상화됐나?',
  result:'왜 장애로 감지됐고 왜 정상화됐나?', handoff:'자동 처리를 멈춘 이유와 필요한 결정은 무엇인가?',
};

function CoreWork({member,core}:{member:FlowNode;core:CoreStepContext}) {
  const dispatch=dispatchFor(member,core.events),view=stepExecution(member,core.events);
  const live=member.status==='running'&&!core.historical;
  if(!dispatch && !view.records.length && !live)return null;
  return <section className="semantic-call"><h4>{member.role_ko || flowTitle(member)}{member.model && <small> · 당시 호출 {member.model} · {effortKo(member.effort)}</small>}</h4>
    {dispatch && <p><b>전달한 요청</b> {reading(dispatch.task_ko,'요청 미관측')}</p>}
    <ExecutionStream node={member} view={view} state={core.state} receivedAt={core.receivedAt} error={core.error}/>
  </section>;
}

function CandidateReading({entries,core}:{entries:(FlowNode & {card:AgentCard})[];core:CoreStepContext}) {
  const evidence=core.data.flow.nodes.flatMap(node=>node.card?.kind==='evidence'?node.card.items:[]);
  const hypotheses=entries.flatMap(n=>n.card.candidates.map((c,i)=>({n,c,i}))).sort((a,b)=>(b.c.confidence??-1)-(a.c.confidence??-1));
  return <>{hypotheses.length>0 && <section><h3>원인 후보별 비교</h3><ol className="stage-timeline">
    {hypotheses.map(({n,c,i})=>{
      const quotes=(n.card.quotes??[]).filter(q=>q.candidate_index===i || (q.candidate_index==null && c.evidence_refs?.includes(q.ref)));
      return <li key={n.id+':'+i}><strong>{reading(c.cause_ko,'후보 해석 미관측')}</strong>
        <p>{n.role_ko || flowTitle(n)}{n.model && ' · '+n.model}{typeof c.confidence==='number' && ' · 후보 신뢰도 '+Math.round(c.confidence*100)+'%'}</p>
        {quotes.map((q,j)=><div key={j}><b>{q.ref_ko || q.ref}</b> {reading(citedReading(q,evidence,core.data.raw),'인용 해석 미관측')}
          {q.quote && <Disclosure label="인용 상세보기">{()=> <pre className="raw-text">{q.quote}</pre>}</Disclosure>}</div>)}
        {c.cause_raw && <Disclosure label="후보 원문 상세보기">{()=> <pre className="raw-text">{c.cause_raw}</pre>}</Disclosure>}</li>;
    })}</ol></section>}
    {entries.map(n=><section key={n.id}><h3>{n.role_ko || flowTitle(n)} <small>{n.model || ''}</small></h3>
      <AgentReading card={{...n.card,candidates:[],quotes:n.card.quotes?.filter(q=>!hypotheses.some(({n:from,c,i})=>n===from&&(q.candidate_index===i||c.evidence_refs?.includes(q.ref))))}} node={n} data={core.data}/></section>)}</>;
}

function CoreReading({node,core,onChange}:{node:SemanticNode;core:CoreStepContext;onChange:()=>void}) {
  const {kind}=node.semantic, members=node.semantic.members.filter(n=>!isAppStep(n));
  const {data}=core;
  const agentRuns=members.filter((n):n is FlowNode & {card:AgentCard}=>n.card?.kind==='agent');
  const narrativeOutcome=members.find(n=>n.card?.kind==='outcome')?.card;
  const result=data.accountability;
  const handoff=narrativeOutcome?.kind==='outcome'?narrativeOutcome.handoff??data.handoff_view:data.handoff_view;
  const readings=members.map(n=>{
    const card=n.card;
    if(kind==='detection' && card?.kind==='signal')return <SignalReading key={n.id} card={card} data={data}/>;
    if(kind==='evidence' && card?.kind==='evidence')return <EvidenceTimeline key={n.id} card={card} data={data}/>;
    if(kind==='routing' && card?.kind==='group')return <GroupReading key={n.id} node={n} card={card} data={data}/>;
    if(kind==='routing' && card?.kind==='routing')return <RoutingReading key={n.id} card={card}/>;
    if(kind==='investigation' && n.type==='investigation')return <section key={n.id}><h3>추가 조사</h3><p>{reading(n.job_ko,'조사 질문 미관측')}</p>
      <p>{reading(n.conclusion_ko,'조사 결과 미관측')}</p>{n.source_activity?.map(e=><p key={e.job_id+':'+e.attempt+':'+e.seq}>{e.file || '배포 소스 대조'} · 시도 {e.attempt} · {timeText(new Date(e.at*1000).toISOString())}</p>)}</section>;
    if(kind==='verification' && card?.kind==='agent')return <section key={n.id}><p className="step-lead">{reading(card.acceptance?.reason_ko ?? card.summary_ko ?? n.conclusion_ko,'결정적 근거 미관측')}</p>
      <VerifyResult card={card} showNext={false}/><AgentReading node={{...n,type:'agent'}} card={{...card,summary_ko:null,acceptance:null}} data={data} hideLead/></section>;
    if(kind==='action' && card?.kind==='action')return <section key={n.id}><p className="step-lead">{card.target} · {card.type_ko}</p>
      <p>{card.status_ko}</p>{card.changes.map((change,i)=><p key={i}>{reading(change,'변경 내용 해석 미관측')}</p>)}</section>;
    if(kind==='action' && card?.kind==='agent')return <AgentReading key={n.id} node={n} card={card} data={data}/>;
    return null;
  });
  // Render the group decision first. A routing explanation remains attached to its own run.
  const groupAt=members.findIndex(n=>n.card?.kind==='group');
  if(kind==='routing' && groupAt>0)readings.unshift(...readings.splice(groupAt,1));
  const activeWork=members.filter(n=>['agent','verify','router','investigation','action'].includes(n.type));
  return <>
    {kind==='investigation' && <CandidateReading entries={agentRuns} core={core}/>}
    {readings}
    {kind==='routing' && <Link to="/settings">현재 모델 설정 확인</Link>}
    {kind==='recovery' && <><p className="step-lead">{data.incident.recovery_kind==='observed' || result?.recovery_observed_at?'원래 리소스의 정상화 관측':'원래 장애 신호의 정상화 미확인'}</p>
      <p>{data.incident.namespace} / {data.incident.workload} · {timeText(result?.recovery_observed_at)}</p>
      {result?.recovery_ko && <p>{reading(result.recovery_ko)}</p>}</>}
    {kind==='result' && (result && result.status!=='not_applicable' ? <AccountabilityReading id={data.incident.id} value={result} canWrite={!core.readOnly} onChange={onChange}/>
      : <p className="step-lead">{reading(narrativeOutcome?.kind==='outcome'?narrativeOutcome.status_ko:node.conclusion_ko,'최종 소명 미관측')}</p>)}
    {kind==='handoff' && <><p className="step-lead">{reading(handoff?.decisions.find(d=>d.key==='operator')?.value_ko ?? handoff?.next_steps_ko[0], '운영자 인수와 판단이 필요합니다.')}</p>
      <p>{reading(handoff?.summary_ko,'인계 사유 해석 미관측')}</p>{handoff?.decisions.filter(d=>d.key!=='operator').map(d=><p key={d.key}><b>{d.label_ko}</b> {reading(d.value_ko)}</p>)}</>}
    {kind==='approval' && <p className="step-lead">{members.find(n=>n.card?.kind==='action')?.label ?? '조치 승인'} · 운영자 결정 확인</p>}
    {!readings.some(Boolean) && !(kind==='investigation' && agentRuns.length>0) && !['recovery','result','handoff','approval'].includes(kind) &&
      <p>{reading(node.conclusion_ko ?? members.find(n=>n.conclusion_ko)?.conclusion_ko,'저장된 판단 요약 미관측')}</p>}
    {activeWork.map(n=><div key={n.call_id?'call:'+n.call_id:n.id}><EscalationNote n={n}/><CoreWork member={n} core={core}/></div>)}
  </>;
}

export function SemanticStagePanel({node,core,application,onChange}:{node:SemanticNode|null;core?:CoreStepContext;application?:AppStepContext;onChange:()=>void}) {
  const travel = useStageTravel();
  if(!node)return <section className="step-panel"><h2>단계 미관측</h2></section>;
  const hasApp=node.semantic.members.some(isAppStep), hasCore=node.semantic.members.some(n=>!isAppStep(n));
  return <section className="step-panel semantic-stage" aria-label="선택 단계 입력과 결과" tabIndex={-1} data-selected-step={node.id} data-travel={travel?.to === node.id ? travel.direction : undefined}>
    <header className="semantic-stage-title"><h2>{node.label}</h2><span className="badge">{flowStatusKo(node.status)}</span></header>
    <p className="semantic-question">{questions[node.semantic.kind]}</p>
    {core && hasCore && <CoreReading node={node} core={core} onChange={onChange}/>}
    {application && hasApp && <ApplicationCardReading key={application.summary.id} node={node} context={application} onChange={onChange} readOnly={!!core?.readOnly}/>}
  </section>;
}
