import test from 'node:test';
import assert from 'node:assert/strict';
import { chronological, citedReading, evidenceQuestion, latestServiceReadings } from '../src/stageEvidence.ts';
import type {AgentQuote,EvidenceItem,Incident} from '../src/api.ts';

const item=(id:number,source:string,target:string,at:string|null,text='관측 내용'): EvidenceItem=>({
  id,source,source_ko:source,target,collected_at:at,summary_ko:text,ok:true,error:null,ref:'E3_service',
  raw_items:[{id:'row'+id,kind:'status',source,at,ref:'E3_service',raw:'raw '+id,ko:text,ko_source:'rules',severity:'info',cited_by:[]}]
});
test('최근 상태는 같은 네임스페이스·대상의 성공한 관측만 연결한다',()=>{
  const list=[item(4,'service','dev/Service/payment',null),item(3,'service','dev/Service/other','2026-11-19T01:00:00Z'),
    item(2,'service','dev/Service/payment','2026-11-18T01:00:00Z'),item(1,'deployment','dev/Deployment/payment','2026-11-17T01:00:00Z'),
    {...item(5,'service','dev/Service/payment','2026-11-20T01:00:00Z'),ok:false}];
  assert.deepEqual(chronological(list).map(i=>i.id),[1,2,3,5,4]);
  assert.deepEqual(latestServiceReadings({namespace:'dev',workload:'payment'} as Incident,list).map(i=>i.id),[2,1]);
  assert.equal(evidenceQuestion(item(9,'endpoints','dev/Service/payment',null)),'연결 대상');
});
test('반복 수집한 출처는 실제 인용문이 일치하는 해석만 붙인다',()=>{
  const list=[item(1,'service','service',null,'첫 번째 관측'),item(2,'service','service',null,'두 번째 관측')];
  const q={ref:'E3_service',ref_known:true,quote:'raw 2'} as AgentQuote;
  assert.equal(citedReading(q,list,[]),'두 번째 관측');
  assert.equal(citedReading({...q,quote:'different'},list,[]),null);
});
