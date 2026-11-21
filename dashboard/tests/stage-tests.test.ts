import test from 'node:test';
import assert from 'node:assert/strict';
import {testComparison,testReading} from '../src/stageTests.ts';

test('추가된 재현 시험과 전체 시험 수를 구분하고 같은 시험의 두 실행만 비교한다', () => {
  const reserve='io.liveprobe.target.inventory.InventoryBusinessTest#reservesSevenWhenFaultDisabled';
  const lookup='io.liveprobe.target.inventory.InventoryBusinessTest#looksUpBWhenFaultDisabled';
  const runs=[{id:reserve,phase:'before',status:'failed'},{id:reserve,phase:'after',status:'passed'},
    {id:lookup,phase:'before',status:'failed'},{id:'another.Class#looksUpBWhenFaultDisabled',phase:'after',status:'passed'}];
  const result=testComparison({tests_added:[reserve,lookup]},{tests:{executed_tests:runs,total:11,failed:0}},{},'inventory');
  assert.equal(result.added,2);
  assert.equal(result.total,11);
  assert.equal(result.failed,0);
  assert.deepEqual(result.rows.map(r=>[r.label,r.before,r.after]),[['재고 7개 예약','실패','통과'],['재고 B 조회','실패','미관측']]);
  assert.equal(testReading(lookup,'unrelated'),'looksUpBWhenFaultDisabled');
});

test('상충하는 기록을 통과로 표시하지 않고 시험 수 미관측과 0개를 구분한다', () => {
  const runs=['failed','passed'].map(status=>({id:'tests/test.py::one',phase:'after',status}));
  assert.equal(testComparison({}, {}, {tests:{executed_tests:runs}},'svc').rows[0].after,'미관측');
  assert.equal(testComparison({}, {}, {}, 'svc').added,null);
  assert.equal(testComparison({tests_added:[]}, {}, {}, 'svc').added,0);
});
