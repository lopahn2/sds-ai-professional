import type {FlowNode, Narrative} from './api.ts';
import type {AppActivity,WorkEvent} from './ApplicationWorkTrace.tsx';
import type {RoomPhase} from './roomWorkflow.ts';
import {applicationStateKo} from './unified.ts';

const roleLabels:Record<string,string>={classifier:'사건 수준 판단',analyst:'애플리케이션 분석',specialist:'원인 추론',critic:'반론 검토',verifier:'결과 확인',patch:'코드 수정'};
const jobLabels:Record<string,string>={source:'배포 소스 확인',collect:'애플리케이션 근거 수집',investigate:'소스 근거 탐색',test_build:'재현·수정·회귀 시험',qa:'사용자 여정 시험',current_version_check:'현재 버전 업무 재검증',recovery:'원래 요청 복구 관측',deploy:'버전 배포',verify:'배포 검증',reconcile:'변경 결과 재조회'};
export type AppStep=FlowNode & {app:{phase:RoomPhase;state:string;stageIndex:number; eventIds:number[]; workKey?:string; jobKind?:string; jobEnvironment?:'dev'|'prd'; round?:number; lastAt?:number; transitionEventId?:number; fileEventIds?:number[]}};
export interface AppSummary {id:string; state?:string|null; reason?:string|null; reason_ko?:string|null; environment?:string|null; system?:string|null; service?:string|null; created_at?:number|null; next_ko?:string|null; protocol?:string|null; round?:number|null;root_incident_id?:string|number|null}
export function isAppStep(n:FlowNode): n is AppStep {return 'app' in n;}
export function appPhase(state:string):RoomPhase {
  if (['created','detected','collecting'].includes(state)) return 'observe';
  if (['source_queued','source_pinning','analysis_queued','analyzing'].includes(state)) return 'analyze';
  if (['awaiting_prd_approval','awaiting_merge'].includes(state)) return 'approve';
  if (state.startsWith('dev_') || state.startsWith('qa_') || state==='awaiting_user_test') return 'qa';
  if (state.startsWith('prd_') || state==='awaiting_recovery') return 'recover';
  if (['completed','failed','handoff','recovery_review'].includes(state)) return 'result';
  return 'repair';
}
const typeFor:Record<RoomPhase,string>={observe:'signal',analyze:'group',repair:'action',qa:'verify',approve:'action',recover:'verify',result:'outcome'};
function timestamp(at:number|null|undefined) {
  return typeof at==='number' && Number.isFinite(at) ? new Date(at*1000).toISOString() : null;
}
function stateNode(i:number,state:string,at:number|null|undefined,status:string):AppStep {
  const phase=appPhase(state),label=applicationStateKo(state);
  return {id:'app-stage:'+i,type:state==='collecting'?'evidence':typeFor[phase],label,role:null,role_ko:label,
    status,depth:i*2,lane:0,group:null,at:timestamp(at),job_ko:'',model:null,effort:null,tokens:null,
    cost_usd:null,duration_ms:null,inputs:[],conclusion_ko:null,conclusion_raw:null,
    app:{phase,state,stageIndex:i,eventIds:[]}};
}
export function appFinished(state?:string|null){return ['completed','failed','handoff'].includes(state??'');}

/** Stages are saved in transition order. Jobs and model calls are joined only
 * on recorded ids. Earlier completions without a call id remain distinct. */
export function applicationFlow(view:AppActivity|null,summary:AppSummary|null) {
  const events=view?.events??[], original=view?.stages??[];
  const stages=original.filter(s=>typeof s.state==='string' && s.state);
  // Older servers can still report the current status but have no transition timestamp.
  const detected=events.find(e=>e.event==='detected');
  if((detected || summary?.created_at!=null) && stages[0]?.state!=='detected' && (stages.length || (view?.state || summary?.state))) {
    stages.unshift({state:'detected',occurred_at:detected?.at ?? summary!.created_at!});
  }
  const current=view?.state || summary?.state;
  const stateRows:{state:string;occurred_at:number|null;round?:number}[]=[...stages];
  if(current && stateRows.at(-1)?.state!==current) stateRows.push({state:current,occurred_at:null});
  const nodes:AppStep[]=stateRows.map((s,i)=>stateNode(i,s.state,s.occurred_at,i===stateRows.length-1&&!appFinished(current)?'running':s.state==='failed'||s.state==='qa_failed'?'failed':'done'));
  let round = 1;
  for (const [index, step] of nodes.entries()) {
    round = stateRows[index].round ?? round;
    step.app.round = round;
  }
  const stateNodes=nodes.slice();
  const edges:Narrative['flow']['edges']=(original.length ? stateNodes.slice(1) : []).map((n,i)=>({id:'app-edge:'+i,source:stateNodes[i].id,target:n.id,kind:'flow',label:null}));
  const byKey=new Map<string,AppStep>();
  let stageIndex=-1;
  for(const event of events) {
    if(stageIndex<0 && stateNodes[0]?.app.state==="detected" && !detected)stageIndex=0;
    const next=stateNodes[stageIndex+1];
    if(next && next.app.state===event.event) {
      stageIndex++;
      next.app.transitionEventId=event.id;
    }
    const owningStage=stateNodes[stageIndex];
    const d=event.data;
    const job=!!d.job_id,role=(!!d.role && !!d.call_id) || event.event==='role_completed';
    if(!job && !role){owningStage?.app.eventIds.push(event.id);continue;}
    const key=job?'job:'+d.job_id:'call:'+(d.call_id || 'record:'+event.id);
    let step=byKey.get(key);
    if(!step){
      const label=job?jobLabels[d.kind??'']||'실행기 작업':roleLabels[d.role??'']||'분석 에이전트';
      const observedPhase=job&&d.kind==='recovery'?'recover':job&&d.kind==='qa'?'qa':job&&d.kind==='test_build'?'repair':owningStage?.app.phase||'analyze';
      const owner=owningStage?.app.stageIndex ?? -1;
      const row=stateNode(owner,owningStage?.app.state??'analyzing',event.at,'running');
      step={...row,id:'app-'+key,role:d.role??null,role_ko:label,label,type:job?d.kind==='collect'?'evidence':'action':d.role==='classifier'?'router':d.role==='verifier'?'verify':'agent',
        model:d.model??null,depth:owner<0?0:row.depth+1,lane:1+nodes.filter(n=>n.app.workKey && n.app.stageIndex===owner).length,
        app:{...row.app,phase:observedPhase,workKey:key,round:owningStage?.app.round,jobKind:d.kind}};
      nodes.push(step);byKey.set(key,step);
      if(owningStage)edges.push({id:'app-edge:'+step.id,source:owningStage.id,target:step.id,kind:'flow',label:'이 단계의 작업'});
    }
    step.app.eventIds.push(event.id);
    step.app.lastAt=event.at;
    if(d.kind)step.app.jobKind=d.kind;
    if(d.job_environment)step.app.jobEnvironment=d.job_environment;
    if(event.event==='runner_progress' && d.phase==='edit_file' && d.file) {
      (step.app.fileEventIds ??= []).push(event.id);
    }
    if(event.event==='investigation_started' || event.event==='investigation_finished'){
      step.label='추가 근거 탐색';
      step.role_ko='추가 근거 탐색';
      step.type='investigation';
      step.app.phase='analyze';
      step.job_ko=d.request_ko || step.job_ko;
      if(event.event==='investigation_finished'){
        step.status='reported';
        step.conclusion_ko=d.reason_ko || null;
      }
    }
    if(event.event==='job_queued') step.status='queued';
    if(event.event==='runner_progress' || event.event==='job_claimed') step.status='running';
    if(event.event==='role_completed' || event.event==='job_reported') {step.status=(d as {status?:string}).status==='failed'?'failed':event.event==='job_reported'?'reported':'done';if(d.summary_ko)step.conclusion_ko=d.summary_ko;}
    if(event.event==='job_cancelled') step.status='skipped';
    if(event.event==='job_report_rejected')step.status='failed';
    if(d.model)step.model=d.model;
    if('effort' in d && typeof d.effort==='string')step.effort=d.effort;
  }
  // Not receiving a completion receipt is different from observing success.
  for(const step of byKey.values())if(['running','queued'].includes(step.status) && appFinished(current))step.status='unobserved';
  return {nodes,edges};
}

export function stepEvents(step:AppStep, nodes:AppStep[] = []) {
  const ids=new Set(step.app.eventIds);
  if(!step.app.workKey) for (const n of nodes.filter(n=>n.app.stageIndex===step.app.stageIndex)) for (const id of n.app.eventIds) ids.add(id);
  return (event:WorkEvent)=>ids.has(event.id);
}
export function activeAppStep(nodes:AppStep[]) {
  const activeState=[...nodes].reverse().find(n=>!n.app.workKey);
  const active=[...nodes].reverse().find(n=>n.app.workKey && ['running','queued'].includes(n.status) && n.app.stageIndex===activeState?.app.stageIndex);
  return active || activeState || nodes.at(-1) || null;
}
export function attachApplicationFlow(core:Narrative['flow'],anchor:FlowNode|undefined|null,app:ReturnType<typeof applicationFlow>):Narrative['flow']{
  if(!anchor || !app.nodes.length)return core;
  const first=app.nodes.find(n=>!n.app.workKey);
  return {nodes:[...core.nodes,...app.nodes.map(n=>({...n,depth:anchor.depth+1+n.depth}))],
    edges:[...core.edges,...(first?[{id:'app-continuation',source:anchor.id,target:first.id,kind:'flow',label:'코드 조사 연결'}]:[]),...app.edges]};
}
