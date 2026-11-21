import test from 'node:test';
import assert from 'node:assert/strict';
import { focusStep, dispatchFor } from '../src/stepPanel.ts';
const n = (id, type, status = 'running') => ({ id, type, status });
test('실제 호출을 group보다 우선하고 운영자 선택은 갱신·병렬 중에도 고정한다', () => {
  const nodes = [n('g','group'), n('a','agent'), n('v','verify'), n('r','router')];
  assert.equal(focusStep(nodes,null,false).id,'a');
  assert.equal(focusStep(nodes,'v',false).id,'v');
  assert.equal(focusStep([...nodes,n('a2','agent')],'v',false).id,'v');
  assert.equal(focusStep(nodes,'g',false).id,'g');
  assert.equal(focusStep(nodes,'missing',false),null);
  assert.equal(focusStep([nodes[0],nodes[2]],null,false).id,'v');
  assert.equal(focusStep([nodes[0],nodes[3]],null,false).id,'r');
});
test('저장 기록 기본은 결과이며 과거 호출에 dispatch를 합성하지 않는다', () => {
  assert.equal(focusStep([n('a','agent'),n('o','outcome','done')],null,true).id,'o');
  assert.equal(dispatchFor(n('old','agent'),[]),null);
  const d={version:'dispatch/v1',role_ko:'분석',evidence_refs:['E1'],rag_refs:['R1'],operator_message_ids:[3]};
  const node={...n('a','agent'),call_id:'call-a'};
  assert.equal(dispatchFor(node,[{kind:'agent_started',detail:{call_id:'call-other',dispatch:d}}]),null);
  assert.equal(dispatchFor(node,[{kind:'agent_started',detail:{call_id:'call-a',dispatch:d}}]),d);
});
