'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createChaos}=require('../chaos.js');
function setup(t) {
 const errors=[];const chaos=createChaos((status,message)=>{throw Object.assign(new Error(message),{status});},{},()=>{},error=>errors.push(error),()=>{},true);
 t.after(()=>chaos.close());return {chaos,errors};
}
test('CPU는 실제 worker_threads로 점유하고 해제·close시 종료',async t=>{
 const {chaos}=setup(t);chaos.update({fault:'cpu_spin',params:{threads:1}});
 for(let attempt=0;attempt<40&&!(chaos.state().cpuIterations>0);attempt++)await new Promise(resolve=>setTimeout(resolve,50));
 assert.equal(chaos.state().cpuWorkers,1);assert.ok(chaos.state().cpuIterations>0);
 chaos.reset();assert.equal(chaos.state().cpuWorkers,0);
});
test('8개 실제 업무 슬롯, 고갈 시 queue 대기, clear로 대기 업무 재개',async t=>{
 const {chaos}=setup(t),releases=[];
 for(let i=0;i<8;i++) releases.push(await chaos.acquire());
 let acquired=false;const pending=chaos.acquire().then(release=>{acquired=true;release();});
 await new Promise(resolve=>setTimeout(resolve,20));assert.equal(acquired,false);assert.equal(chaos.state().workerPool.waiting,1);
 releases.pop()();await pending;for(const release of releases)release();
 chaos.update({fault:'worker_exhaustion'});let resumed=false;const blocked=chaos.acquire().then(release=>{resumed=true;release();});
 await new Promise(resolve=>setTimeout(resolve,20));assert.equal(resumed,false);assert.equal(chaos.state().workerPool.reserved,8);
 chaos.reset();await blocked;assert.equal(resumed,true);
});
test('슬롯 고갈은 실제 10초 대기 후 WorkerPoolExhausted 503',async t=>{
 const keepAlive=setTimeout(()=>{},11000);t.after(()=>clearTimeout(keepAlive));
 const {chaos}=setup(t);chaos.update({fault:'worker_exhaustion'});const started=performance.now();
 await assert.rejects(chaos.acquire(),error=>error.name==='WorkerPoolExhausted'&&error.status===503);
 assert.ok(performance.now()-started>=9900);assert.equal(chaos.state().workerPool.waiting,0);
});
test('서버 종료는 대기·지연 타이머도 취소하고 실제 메모리를 해제',async t=>{
 const {chaos}=setup(t);chaos.update({fault:'hang',params:{seconds:40}});chaos.update({fault:'memory_leak',params:{mib_per_second:1}});
 const wait=chaos.beforeBusiness();await new Promise(resolve=>setTimeout(resolve,1100));
 assert.ok(chaos.state().allocatedBytes>=1048576);chaos.close();await wait;assert.equal(chaos.state().allocatedBytes,0);
});
