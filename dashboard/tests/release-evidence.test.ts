import test from 'node:test';
import assert from 'node:assert/strict';
import { qaFlows, qaSteps, statusLabel } from '../src/releaseEvidenceModel.ts';

test('QA 기대값과 결과는 동일한 업무 흐름·단계 ID에만 연결한다', () => {
  const plan = { flows:[{id:'order', name_ko:'주문', request:{path:'/order'}, result:{selector:'#status'}}, {id:'stock', independent_read:{service:'inventory'}}] };
  const receipt = { flows:[{id:'stock',steps:[{id:'independent_read',status:'failed'}]}, {id:'order',steps:[{id:'network',status:'passed'}, {id:'dom',status:'not_run'}]}] };
  const [order,stock] = qaFlows(receipt,plan);
  const orderSteps=qaSteps(order.plan,order.actual);
  assert.equal(orderSteps.find(s=>s.id==='open')?.receipt.status,undefined);
  assert.equal(orderSteps.find(s=>s.id==='network')?.receipt.status,'passed');
  assert.equal(orderSteps.find(s=>s.id==='dom')?.receipt.status,'not_run');
  assert.equal(qaSteps(stock.plan,stock.actual).at(-1)?.receipt.status,'failed');
  assert.deepEqual(qaFlows({},{}),[]);
  assert.deepEqual(qaSteps({},{}),[]);
});
test('미관측·미실행·확인 불가·시험 실패는 별개로 표시한다', () => {
  assert.deepEqual([undefined,'not_run','unverifiable','failed','passed','tests_missing'].map(statusLabel),
    ['미관측','미실행','확인 불가','실패','통과','필수 시험 없음']);
});
test('상품 검색과 상세 응답을 포함한 브라우저 실행 순서와 시험 근거를 유지한다', () => {
  const ids=['open','input','submit','network','search_dom','detail_submit','detail_network','dom','independent_read'];
  const steps = ids.map((id,index)=>({id,status:index<6?'passed':'not_run',file:'tests/test_business.py',expected_ko:'검색 결과를 확인합니다.'}));
  const actual=qaSteps({id:'product'}, {steps});
  assert.deepEqual(actual.map(s=>s.id),ids);
  assert.equal(actual[6].label,'상세 응답 확인');
  assert.equal(actual[6].receipt.file,'tests/test_business.py');
});

import {pairedTest,approvedVersion} from '../src/releaseEvidenceModel.ts';
test('같은 파일·시험의 수정 전후만 비교하고 다른 여정이나 이미지로 통과를 만들지 않는다',()=>{
  const ref={test_id:'test_payment',file:'tests/pay.py'};
  const runs=[{file:ref.file,id:'tests/pay.py::test_payment',phase:'before',status:'failed'},
    {file:'tests/other.py',id:ref.test_id,phase:'after',status:'passed'}];
  assert.deepEqual(pairedTest(ref,runs),{before:'failed',after:null});
  assert.deepEqual(pairedTest(ref,[...runs,{...runs[0],phase:'after',status:'passed'}]),{before:'failed',after:'passed'});
  const image='image@sha256:'+'a'.repeat(64);
  assert.equal(approvedVersion({artifact:{image},qa:{observed_image:image,artifact_sha256:'a'},artifact_sha256:'b'}).match,false);
  assert.equal(approvedVersion({artifact:{image}}).match,null);
});
