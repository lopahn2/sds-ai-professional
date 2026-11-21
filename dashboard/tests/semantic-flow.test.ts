import test from 'node:test';
import assert from 'node:assert/strict';
import type {FlowNode,Action} from '../src/api.ts';
import type {WorkEvent} from '../src/ApplicationWorkTrace.tsx';
import {applicationFlow,isAppStep} from '../src/applicationFlow.ts';
import {semanticFlow,semanticForSource} from '../src/semanticFlow.ts';
import {phaseOf} from '../src/roomWorkflow.ts';
import {flowTitle} from '../src/narrative.ts';

const source=(id:string,type:string,status='done',extra:Partial<FlowNode>={}):FlowNode=>({
  id,type,status,label:id,role:null,role_ko:id,job_ko:'',model:null,effort:null,tokens:null,cost_usd:null,duration_ms:null,
  inputs:[],conclusion_ko:null,conclusion_raw:null,lane:0,depth:0,group:null,...extra
});
const edge=(source:string,target:string,kind='flow')=>({id:source+'>'+target,source,target,kind,label:null});
const action=(id:number,status:string)=>source('action:'+id,'action',status==='pending_approval'?'running':'done',
  {card:{kind:'action',action_id:id,type:'scale_replicas',type_ko:'레플리카 변경',target:'dev/inventory',status,status_ko:'',changes:[]}});

function buildApplication(sequence:[string,WorkEvent['data']?][],states:string[]) {
  const events=sequence.map(([event,data],index)=>({id:index+1,at:100,event,data:data??{}}));
  return applicationFlow({id:'inventory',state:states.at(-1)!,stages:states.map(state=>({state,occurred_at:100})),
    cursor:events.length,has_more:false,events},{id:'inventory',created_at:100});
}

test('inventory v2 separates seven stored calls into decisions, edits, tests and one human gate',()=>{
  const states=['detected','collecting','source_pinning','analysis_queued','analyzing','patch_proposed','test_build_queued',
    'test_build_passed','dev_targets_collecting','dev_deploy_queued','dev_verified','prd_targets_collecting','awaiting_user_test','awaiting_prd_approval'];
  const roles=['classifier','classifier','analyst','specialist','critic','verifier','patch'];
  const sequence:[string,WorkEvent['data']?][]=[];
  for (const state of states) {
    sequence.push([state]);
    if(state==='analyzing') for (const role of roles) sequence.push(['role_completed',{role,model:role==='critic'?'gpt-6-astra':'gpt-6.1-sol'}]);
    if(state==='test_build_queued') sequence.push(['job_queued',{job_id:'j',kind:'test_build'}],
      ['runner_progress',{job_id:'j',kind:'test_build',phase:'edit_file',file:'Inventory.java'}],['job_reported',{job_id:'j',kind:'test_build'}]);
  }
  const raw=buildApplication(sequence,states), flow=semanticFlow(raw,{incidentStatus:'awaiting_prd_approval'});
  assert.deepEqual(flow.nodes.map(n=>n.semantic.kind),['detection','evidence','routing','investigation','verification','action','test_build','dev_validation','approval']);
  const byKind=(kind:string)=>flow.nodes.find(n=>n.semantic.kind===kind)!;
  assert.equal(byKind('routing').semantic.members.filter(n=>n.role==='classifier').length,2);
  assert.deepEqual(byKind('investigation').semantic.members.map(n=>n.role).filter(Boolean),['analyst','specialist','critic']);
  assert.equal(byKind('test_build').status,'done');
  assert.equal(byKind('dev_validation').status,'manual_record');
  assert.equal(byKind('approval').status,'awaiting_approval');
  assert.ok(byKind('action').semantic.members.some(n=>n.role==='patch'));
  const edits=byKind('action').semantic.members.find(n=>isAppStep(n) && n.app.workKey==='job:j')!;
  assert.ok(isAppStep(edits) && edits.app.eventIds.length===1);
  assert.equal(semanticForSource(flow.nodes,'app-job:j')!.semantic.kind,'test_build');
  for (const [a,b] of [['routing','investigation'],['investigation','verification'],['verification','action'],['action','test_build']]) {
    assert.ok(flow.edges.some(e=>e.source===byKind(a).id && e.target===byKind(b).id),a+' → '+b);
  }
  assert.equal(phaseOf(byKind('action')),'repair');
  assert.equal(flowTitle(byKind('action')),'코드 수정');
});

test('Kubernetes projections keep actual changes apart and return rejected checks to the same investigation',()=>{
  const signal=source('s','signal'),group=source('g','group','running'), agent=source('a','agent','done',{role:'cause_reasoner'}),
    verifier=source('v','verify'),inquiry=source('inquiry','investigation'),originalOutcome=source('outcome','outcome','done',
      {card:{kind:'outcome',status:'resolved',status_ko:'정상',handoff:null}});
  const nodes=[signal,group,agent,verifier,inquiry,action(11,'succeeded'),action(12,'succeeded'),originalOutcome,source('plan:g:unused','agent','planned')];
  const edges=[edge('s','g'),edge('g','a'),edge('a','v'),edge('v','inquiry','review'),edge('inquiry','v','review'),edge('v','action:11'),
    edge('action:11','action:12'),edge('action:12','outcome')];
  const flow=semanticFlow({nodes,edges},{incidentStatus:'resolved',recoveryObserved:true,
    actions:[{id:12,policy_mode:'approval'} as Action]});
  assert.equal(flow.nodes.filter(n=>n.semantic.kind==='investigation').length,1);
  assert.equal(flow.nodes.filter(n=>n.semantic.kind==='action').length,2);
  assert.equal(semanticForSource(flow.nodes,'plan:g:unused'),undefined);
  assert.equal(semanticForSource(flow.nodes,'g')?.status,'done');
  assert.ok(flow.edges.some(e=>e.source==='stage:verification'&&e.target==='stage:investigation'&&e.kind==='review'));
  assert.ok(flow.edges.some(e=>e.source==='stage:approval'&&e.target==='stage:action:action:12'));
  assert.ok(flow.edges.some(e=>e.source==='stage:recovery'&&e.target==='stage:result'));
  assert.equal(semanticFlow({nodes,edges}).nodes.some(n=>n.semantic.kind==='recovery'),false);
  const rejected=semanticFlow({nodes:[action(21,'rejected')],edges:[]},{actions:[{id:21,policy_mode:'approval'} as Action]});
  assert.deepEqual(rejected.nodes.map(n=>[n.semantic.kind,n.status]),[['approval','failed']]);
});

test('only observed handoff remains and cannot request ownership again after close or a code continuation',()=>{
  const handoff=source('outcome','outcome','done',{card:{kind:'outcome',status:'handed_off',status_ko:'',handoff:null}});
  assert.equal(semanticFlow({nodes:[handoff],edges:[]},{incidentStatus:'handed_off'}).nodes[0].status,'awaiting_operator');
  const closed=source('outcome','outcome','done',{card:{kind:'outcome',status:'closed',status_ko:'',closed_from:'handed_off',handoff:null}});
  const past=semanticFlow({nodes:[closed],edges:[]},{incidentStatus:'closed'});
  assert.deepEqual(past.nodes.map(n=>[n.semantic.kind,n.status]),[['handoff','done'],['result','done']]);
  const mixed=semanticFlow({nodes:[handoff,source('link','code_action','running')],edges:[edge('outcome','link')]},{incidentStatus:'analyzing'});
  assert.equal(mixed.nodes.some(n=>n.semantic.kind==='handoff'),false);
});

test('current-image check is a recovery observation even while an archived approval is on screen',()=>{
  const states=['detected','awaiting_prd_approval'];
  const raw=buildApplication([['detected'],['awaiting_prd_approval'],['job_queued',{job_id:'check',kind:'current_version_check'}]],states);
  const {nodes}=semanticFlow(raw,{incidentStatus:states.at(-1)});
  assert.equal(semanticForSource(nodes,'app-job:check')?.semantic.kind,'recovery');
  assert.equal(nodes.some(n=>n.semantic.kind==='action'),false);
});
