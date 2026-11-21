import test from 'node:test';
import assert from 'node:assert/strict';
import { phaseFlow, phaseOf, phasePositions, phaseStatus, serviceSignalImpact, stepNeighborhood } from '../src/roomWorkflow.ts';
import type { FlowNode, Narrative } from '../src/api.ts';
const node = (id: string, type: string, depth: number, lane = 0) => ({ id, type, depth, lane, status: 'done' } as FlowNode);

test('분석 검증과 실제 dev QA를 구분하며 선택한 구간과 연결된 실제 분기를 보존한다', () => {
  const nodes = [node('signal','signal',0), node('router','router',2), node('a','agent',3,0), node('b','verify',3,1), node('out','outcome',5), node('code','code_action',6)];
  const edges = [['signal','router'],['router','a'],['router','b'],['a','out'],['b','out'],['out','code']].map(([source,target]) => ({ id: source + target, source, target, kind: 'flow', label: null }));
  const part = phaseFlow({ nodes, edges }, 'analyze', 'awaiting_user_test');
  assert.deepEqual(part.nodes.map(n => n.id), ['signal','router','a','b','out']);
  assert.deepEqual(part.edges, edges.slice(0,5));
  const focused = stepNeighborhood(part, 'a');
  assert.deepEqual(focused.nodes.map(n => n.id), ['router','a','b','out']);
  assert.deepEqual(focused.edges, edges.slice(1,5));
  assert.equal(phaseOf(nodes[3]), 'analyze');
  assert.equal(phaseOf(nodes[5], 'awaiting_user_test'), 'qa');
  assert.equal(phaseOf(nodes[5], 'awaiting_prd_approval'), 'approve');
  const layout = phasePositions(nodes, 250, 80);
  assert.equal(layout.get('a')!.x, layout.get('b')!.x);
  assert.notEqual(layout.get('a')!.y, layout.get('b')!.y);
  assert.ok(layout.get('router')!.x < layout.get('a')!.x);
});

test('저장 당시 진행과 현재 진행, 아직 기록이 없는 구간을 구분한다', () => {
  const active = { ...node('a','agent',1), status: 'running' };
  assert.equal(phaseStatus([active], true), '기록 당시 진행');
  assert.equal(phaseStatus([active], false), '진행 중');
  assert.equal(phaseStatus([], false), '기록 없음');
});

test('전체 사건은 가로 행의 끝에서 이어지고 같은 깊이의 병렬 역할은 서로 겹치지 않는다', () => {
  const nodes = Array.from({length:9}, (_,d) => node('n'+d,'agent',d));
  nodes.push(node('branch','verify',6,1));
  const p = phasePositions(nodes,260,88,3);
  assert.deepEqual(p.get('n2')!.x,p.get('n3')!.x);
  assert.ok(p.get('n3')!.y>p.get('n2')!.y);
  assert.ok(p.get('n4')!.x<p.get('n3')!.x);
  assert.equal(p.get('n6')!.x,p.get('branch')!.x);
  assert.equal(new Set([...p.values()].map(v=>v.x+','+v.y)).size,p.size);
});

test('Service 무대상 신호는 그 사건 서비스의 최근 수집 수와 관측 복구를 구분한다', () => {
  const entry={namespace:'dev',workload:'payment',title:'NoEndpoints on Service/payment',recovery_kind:'observed'};
  const reading=(name:string,day:number,count:number)=>({ok:true,source:'service',target:'dev/Service/'+name,summary_ko:`Service에 준비된 연결 대상(엔드포인트)이 ${count}개입니다.`,collected_at:`2026-01-0${day}T00:00:00Z`});
  const record={incident:entry,flow:{nodes:[{card:{kind:'evidence',items:[reading('payment',1,0),reading('other',3,9),reading('payment',2,1)]}}]}} as unknown as Narrative;
  assert.match(serviceSignalImpact(record)!,/1개였습니다.+원인은 미확정/);
});

import { continuationHints,groupSteps,operatorFlow } from '../src/roomWorkflow.ts';
test('힌트는 보이는 구간 바로 다음의 실제 실행 분기만 열고 라이브 진행을 예측하지 않는다',()=>{
  const nodes=['g','first','left','right','planned','interpret'].map((id,i)=>({...node(id,i===0?'group':id==='interpret'?'interpret':'agent',i<2?i:2),status:id==='planned'?'planned':'done'}));
  const edges=[['g','first'],['g','interpret'],['first','left'],['first','right'],['first','planned']].map(([source,target])=>({id:source+target,source,target,kind:'flow',label:null}));
  const full=operatorFlow({nodes,edges}),visible={nodes:nodes.slice(0,2),edges:edges.slice(0,1)};
  assert.deepEqual(continuationHints(full,visible,true).map(n=>n.id),['left','right']);
  assert.deepEqual(continuationHints(full,visible,false),[]);
  assert.equal(full.nodes.some(n=>n.type==='interpret'),false);
  assert.deepEqual(groupSteps(full,nodes[0]).map(n=>n.id),['first','left','right','planned']);
});

test('애플리케이션 재시도는 호출 번호·일감 번호별로 한 노드에 남고 종료 미관측을 구분한다', async()=>{
  const {applicationFlow,stepEvents}=await import('../src/applicationFlow.ts');
  const steps=['collecting','analyzing','completed'];
  const events=[
    {event:'collecting',data:{}},
    {event:'job_queued',data:{kind:'collect',job_id:'j1'}},
    {event:'analyzing',data:{}},
    {event:'role_started',data:{call_id:'c1',role:'analyst'}},
    {event:'job_reported',data:{kind:'collect',job_id:'j1'}},
    {event:'role_started',data:{call_id:'c2',role:'analyst'}},
    {event:'role_completed',data:{call_id:'c2',role:'analyst',summary_ko:'검증한 결론입니다.'}},
    {event:'completed',data:{}},
  ].map((e,i)=>({...e,id:i+1,at:i+1}));
  const {nodes,edges}=applicationFlow({id:'a',state:'completed',events,stages:steps.map(state=>({state,occurred_at:events.find(e=>e.event===state)!.at})),has_more:false,cursor:8},{id:'a',created_at:0});
  const collection=nodes.find(n=>n.app.state==='collecting' && !n.app.workKey)!;
  const job=nodes.find(n=>n.app.workKey==='job:j1')!;
  assert.equal(job.status,'reported');
  assert.equal(job.app.stageIndex,collection.app.stageIndex);
  assert.ok(edges.some(edge=>edge.source===collection.id && edge.target===job.id));
  assert.deepEqual(events.filter(stepEvents(job)).map(e=>e.id),[2,5]);
  assert.equal(nodes.find(n=>n.app.workKey==='call:c1')?.status,'unobserved');
  assert.equal(nodes.find(n=>n.app.workKey==='call:c2')?.status,'done');
});
