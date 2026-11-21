import type {Action, FlowEdge, FlowNode, Narrative} from './api.ts';
import {isAppStep, type AppStep} from './applicationFlow.ts';
import type {RoomPhase} from './roomWorkflow.ts';

const definitions = {
  detection: ['감지', 'observe', 'signal'],
  evidence: ['근거 수집', 'observe', 'evidence'],
  routing: ['대응 경로 결정', 'analyze', 'router'],
  investigation: ['원인 조사', 'analyze', 'agent'],
  verification: ['독립 검증', 'analyze', 'verify'],
  action: ['조치 실행', 'repair', 'action'],
  test_build: ['시험·빌드', 'repair', 'action'],
  dev_validation: ['dev 검증', 'qa', 'verify'],
  approval: ['운영 승인', 'approve', 'action'],
  recovery: ['복구 확인', 'recover', 'verify'],
  result: ['결과·소명', 'result', 'outcome'],
  handoff: ['운영자 인계', 'result', 'outcome'],
} as const;

export type SemanticKind = keyof typeof definitions;
export type SemanticNode = FlowNode & {
  semantic: {kind: SemanticKind; phase: RoomPhase; members: FlowNode[]};
};
type Stage = {kind: SemanticKind; action?: string; label?: string};
type Options = {incidentStatus?: string; recoveryObserved?: boolean; actions?: Action[]};
export const isSemanticNode = (node:FlowNode):node is SemanticNode => 'semantic' in node;
export function semanticForSource(nodes:SemanticNode[], id?:string|null):SemanticNode|undefined {
  return id ? nodes.find(n=>n.id===id || n.aliases?.includes(id)) : undefined;
}

const codePatch:Stage = {kind:'action',action:'code',label:'코드 수정'};
const productionDeploy:Stage = {kind:'action',action:'prd-deploy',label:'prd 배포'};
function appStage(n:AppStep):Stage|null {
  const {state, jobKind} = n.app;
  if (jobKind==='current_version_check' || jobKind==='recovery') return {kind:'recovery'};
  if (n.role==='classifier') return {kind:'routing'};
  if (n.role==='verifier') return {kind:'verification'};
  if (n.role==='patch') return codePatch;
  if (n.role || jobKind==='investigate') return {kind:'investigation'};
  if (jobKind==='test_build') return {kind:'test_build'};
  if (jobKind==='qa') return {kind:'dev_validation'};
  if (n.app.jobEnvironment && ['deploy','verify','reconcile'].includes(jobKind??'')) {
    return n.app.jobEnvironment==='prd' ? productionDeploy : {kind:'dev_validation'};
  }
  if (state==='detected' || state==='created') return {kind:'detection'};
  if (state==='collecting' || state==='source_queued' || state==='source_pinning') return {kind:'evidence'};
  if (state==='analysis_queued') return {kind:'routing'};
  if (state==='analyzing') return {kind:'investigation'};
  if (state==='patching' || state==='patch_proposed') return codePatch;
  if (state.startsWith('test_build_')) return {kind:'test_build'};
  if (state.startsWith('dev_') || state.startsWith('qa_') || state==='awaiting_user_test') return {kind:'dev_validation'};
  // Target snapshots pin the comparison version before the v2 manual check or v3 dev journey.
  // No production approval or write has occurred when this collection starts.
  if (state==='prd_targets_collecting') return {kind:'dev_validation'};
  if (state==='awaiting_prd_approval' || state==='awaiting_merge') return {kind:'approval'};
  if (state.startsWith('prd_')) return productionDeploy;
  if (state==='awaiting_recovery') return {kind:'recovery'};
  if (state==='reconciliation_required') return {kind:'action',action:'reconciliation',label:'변경 결과 확인'};
  if (state==='handoff') return {kind:'handoff'};
  if (['recovery_review','failed','completed'].includes(state)) return {kind:'result'};
  return null;
}
function coreStage(n:FlowNode, opts:Options):Stage|null {
  // The core graph also contains planned placeholders (including pending outcome) with no execution.
  if (n.status==='planned' || (n.id.startsWith('plan:') && n.status==='skipped') || n.type==='interpret') return null;
  switch(n.type) {
    case 'signal': return {kind:'detection'};
    case 'evidence': return n.card?.kind==='evidence' && n.card.total===0 && !n.at ? null : {kind:'evidence'};
    case 'router': case 'group': return n.status==='skipped' ? null : {kind:'routing'};
    case 'agent': case 'investigation': return {kind:'investigation'};
    case 'verify': return {kind:'verification'};
    case 'command': case 'action':
      if (n.card?.kind==='action' && ['pending_approval','rejected'].includes(n.card.status)) return {kind:'approval'};
      return {kind:'action',action:n.id,label:n.card?.kind==='action' ? n.card.type_ko : n.label};
    case 'outcome':
      return {kind: (n.card?.kind==='outcome' ? n.card.status : opts.incidentStatus)==='handed_off' ? 'handoff' : 'result'};
    case 'code_action': return {kind:'investigation'};
    default: return null;
  }
}
function lifecycle(n:FlowNode):string {
  if (!isAppStep(n)) {
    if (n.card?.kind==='action') {
      if (n.card.status==='pending_approval') return 'awaiting_approval';
      if (n.card.status==='rejected') return 'failed';
      if (n.card.status==='approved' && n.status==='running') return 'queued';
    }
    if (n.type==='group') return 'done'; // The team was selected. Its roles execute separately.
    return n.status;
  }
  if (n.app.workKey || !['running','done'].includes(n.status)) return n.status;
  if (n.app.state==='failed' || n.app.state==='qa_failed') return 'failed';
  if (n.status==='running') {
    if (['awaiting_prd_approval','awaiting_merge'].includes(n.app.state)) return 'awaiting_approval';
    if (n.app.state==='awaiting_user_test') return 'awaiting_user_test';
    if (n.app.state==='recovery_review') return 'awaiting_review';
    if (n.app.state.endsWith('_queued')) return 'queued';
  }
  return n.status;
}
function observedAt(n:FlowNode):number {
  if (isAppStep(n) && n.app.lastAt!=null) return n.app.lastAt*1000;
  return n.at ? Date.parse(n.at) || 0 : 0;
}
function makeNode(id:string, stage:Stage, members:FlowNode[], aliases:string[], opts:Options, completed:Set<string>):SemanticNode {
  const [defaultLabel,regularPhase,type] = definitions[stage.kind];
  const phase=stage.action==='prd-deploy' ? 'recover' : regularPhase;
  const sequence = (n:FlowNode) => isAppStep(n) ? n.app.transitionEventId ?? n.app.eventIds.at(-1) ?? 0 : 0;
  const chronological = [...members].sort((a,b)=>observedAt(a)-observedAt(b) || sequence(a)-sequence(b));
  // Generic containing states may remain 'running' while a separate concrete role is doing the work.
  const statusOf = (n:FlowNode) => completed.has(n.id) ? 'done' : lifecycle(n);
  const active = chronological.filter(n=>['running','queued','awaiting_approval','awaiting_user_test','awaiting_review'].includes(statusOf(n)));
  const current = active.at(-1) ?? chronological.at(-1)!;
  let status = statusOf(current);
  const state = chronological.filter(isAppStep).filter(m=>!m.app.workKey).at(-1);
  // Workers can post the transport-level job_reported event just after the controller accepts the receipt.
  // The verified controller outcome is authoritative for that attempt.
  if (stage.kind==='test_build' && state?.app.state==='test_build_passed') status='done';
  if (stage.kind==='dev_validation' && state?.app.state==='awaiting_user_test') {
    status=state.status==='done' ? 'manual_record' : 'awaiting_user_test';
  }
  if (stage.kind==='dev_validation' && state?.app.state==='qa_passed') status='done';
  if (stage.kind==='dev_validation' && state?.app.state==='qa_failed') status='failed';
  if (stage.kind==='handoff') status=['handed_off','handoff'].includes(opts.incidentStatus??'') && !members.some(n=>n.card?.kind==='outcome' && n.card.handoff_claimed) ? 'awaiting_operator' : 'done';
  if (stage.kind==='result' && opts.incidentStatus==='recovery_review') status='awaiting_review';
  const label=stage.label || defaultLabel;
  return {
    id,type,label,role:null,role_ko:label,model:null,effort:null,tokens:null,cost_usd:null,duration_ms:null,
    job_ko:current.job_ko,at:current.at,inputs:[],conclusion_ko:current.conclusion_ko,conclusion_raw:null,
    group:current.group,status,aliases,depth:0,lane:0,
    semantic:{kind:stage.kind,phase,members:chronological},
  };
}

/** Fold source events into operator decisions. Source members and their original identifiers remain available for detail. */
export function semanticFlow(flow:Narrative['flow'], opts:Options={}):{nodes:SemanticNode[];edges:FlowEdge[]} {
  const bins = new Map<string,{stage:Stage; members:FlowNode[];aliases:string[]}>();
  const owners = new Map<string,string>();
  const edges:FlowEdge[]=[];
  const edgeKeys=new Set<string>();
  function link(source:string|undefined,target:string|undefined,kind='flow',label:string|null=null) {
    if (!source || !target || source===target) return;
    const id=source+'>'+target+':'+kind;
    if (!edgeKeys.has(id)) {edgeKeys.add(id);edges.push({id,source,target,kind,label});}
  }
  function add(n:FlowNode,stage:Stage,primary=true) {
    const id='stage:'+stage.kind+(stage.action?':'+stage.action:'');
    const bin=bins.get(id) ?? {stage,members:[],aliases:[]};
    bin.members.push(n);
    if (primary) {
      bin.aliases.push(n.id,...(n.aliases??[]));
      owners.set(n.id,id);
    }
    bins.set(id,bin);
    return id;
  }
  const sourceSteps=flow.nodes.filter(isAppStep);
  const completedSources=new Set<string>();
  const hasContinuation=flow.nodes.some(n=>n.type==='code_action');
  const appWork=new Map<number,AppStep[]>();
  for (const n of sourceSteps) if (n.app.workKey) {
    const list=appWork.get(n.app.stageIndex)??[];
    list.push(n);appWork.set(n.app.stageIndex,list);
  }
  const actionRecords=new Map(opts.actions?.map(a=>[a.id,a]));
  for (const n of flow.nodes) {
    let stage=isAppStep(n) ? appStage(n) : coreStage(n,opts);
    if (isAppStep(n) && !n.app.workKey && n.app.state==='analyzing') {
      // The analyzing transition starts the first recorded role; it is not a parallel agent in its own right.
      const first=appWork.get(n.app.stageIndex)?.find(m=>m.role);
      if (first) stage=appStage(first);
    }
    if (!stage) continue;
    if (n.type==='outcome' && stage.kind==='handoff' && hasContinuation && n.card?.kind==='outcome' && !n.card.handoff_claimed) {
      // A verified code cause can automatically continue while the core DB keeps its earlier handoff state.
      stage={kind:'verification'};
    }
    if (n.type==='code_action' && sourceSteps.length) completedSources.add(n.id);
    if (isAppStep(n) && !n.app.workKey && n.app.state==='analyzing' && appWork.get(n.app.stageIndex)?.some(m=>m.role)) {
      completedSources.add(n.id);
    }
    const owner=add(n,stage);
    if (n.type==='outcome' && stage.kind==='result' && n.card?.kind==='outcome' &&
        (n.card.closed_from==='handed_off' || n.card.handoff_recorded_at)) {
      const handoff=add(n,{kind:'handoff'},false);
      link(handoff,owner);
    }
    // These are two different facts from one record: a human decision and the ensuing change,
    // or a measured recovery and the subsequent explanation.
    if (n.card?.kind==='action' && stage.kind==='action' && actionRecords.get(n.card.action_id)?.policy_mode==='approval'
        && ['approved','executing','succeeded','failed'].includes(n.card.status)) {
      const approval=add({...n,status:'done'},{kind:'approval'},false);
      link(approval,owner);
    }
    if (n.type==='outcome' && !isAppStep(n) && stage.kind==='result' && opts.recoveryObserved) {
      const observed=add({...n,status:'done'},{kind:'recovery'},false);
      link(observed,owner);
    }
    if (isAppStep(n) && n.app.fileEventIds?.length) {
      // A test/build lease also applies the patch. Only its recorded file edits belong to the code-change card.
      const edits:AppStep={...n,status:'reported',app:{...n.app,eventIds:n.app.fileEventIds}};
      add(edits,codePatch,false);
    }
  }
  for (const edge of flow.edges) {
    if (sourceSteps.length && (edge.source.startsWith('app-') || edge.target.startsWith('app-'))) {
      if (edge.id!=='app-continuation') continue;
    }
    const source=owners.get(edge.source), target=owners.get(edge.target);
    if (!source && target && edge.kind==='steer') {
      const note=flow.nodes.find(n=>n.id===edge.source && n.type==='steer');
      if(note)add(note,bins.get(target)!.stage);
    } else {
      const actualTarget=edge.target==='outcome' && target==='stage:result' && opts.recoveryObserved ? 'stage:recovery' : target;
      link(source,actualTarget,edge.kind,edge.label);
    }
  }

  // State-to-role spokes in the source Application graph describe ownership, not seven parallel branches.
  // Follow stored transitions and actual call/job event order. Only overlapping work shares a predecessor.
  const inputs=new Set(flow.edges.filter(e=>e.source.startsWith('app-stage:') && e.target.startsWith('app-stage:')).map(e=>e.target));
  let tails:string[]=[];
  for (const state of sourceSteps.filter(n=>!n.app.workKey)) {
    const entry=owners.get(state.id);
    if (inputs.has(state.id)) for (const previous of tails) link(previous,entry);
    tails=entry?[entry]:[];
    let batch:{id:string;end:number}[]=[];
    let beforeBatch=tails;
    for (const job of appWork.get(state.app.stageIndex)??[]) {
      const id=owners.get(job.id);
      if (!id) continue;
      const first=job.app.eventIds[0] ?? 0;
      if (batch.length && batch.every(previous=>previous.end<first)) {
        beforeBatch=batch.map(previous=>previous.id);
        batch=[];
      }
      for (const prev of beforeBatch) link(prev,id);
      const end=['done','reported','failed','skipped'].includes(job.status) ? job.app.eventIds.at(-1) ?? first : Infinity;
      batch.push({id,end});
    }
    if(batch.length)tails=batch.map(previous=>previous.id);
  }
  const nodes=[...bins].map(([id,{stage,members,aliases}])=>makeNode(id,stage,members,aliases,opts,completedSources));
  const rank=Object.keys(definitions);
  function layoutRank(n:SemanticNode) {
    if(n.id==='stage:action:prd-deploy')return rank.indexOf('approval')+0.5;
    if(n.semantic.kind==='handoff')return rank.indexOf('result')-0.5;
    return rank.indexOf(n.semantic.kind);
  }
  nodes.sort((a,b)=>layoutRank(a)-layoutRank(b) || observedAt(a)-observedAt(b));
  // Each semantic step owns a legible position. Edges can point back to the same investigation after review.
  nodes.forEach((n,index)=>{n.depth=index;});
  return {nodes,edges};
}
