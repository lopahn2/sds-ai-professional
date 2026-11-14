'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {createChaos}=require('../chaos.js');
const NAMES=['code_bug','code_bug_validation','code_bug_serialization','intermittent_exception','error_rate','latency','latency_spike','hang','crash','startup_config_error','memory_leak','not_ready','liveness_fail','cpu_spin','worker_exhaustion','background_job_failure','business_invariant','dependency_failure'];
function setup(t,env={}) {
  const logs=[];
  const chaos=createChaos((status,message)=>{throw Object.assign(new Error(message),{status});},env,()=>{},()=>{},()=>{},true,{event:line=>logs.push(line)});
  t.after(()=>chaos.close()); return {chaos,logs};
}
test('18개 공통 프리미티브와 기존 faults map·매개변수 호환',t=>{
  const {chaos}=setup(t);
  for(const name of NAMES) {assert.ok(chaos.state().faults[name],name);assert.ok(chaos.state().faults[name].label);}
  chaos.update({fault:'latency',params:{seconds:0.02}});
  assert.equal(chaos.state().faults.latency.params.delay_ms,20);
  chaos.update({fault:'error_rate',params:{ratio:1}});
  assert.equal(chaos.state().faults.error_rate.params.rate,1);
  chaos.update({fault:'event_loop_block',params:{block_ms:1}});
  assert.deepEqual(chaos.state().active.sort(),['error_rate','event_loop_block','latency']);
  for(const ttl_seconds of [0,3601,NaN,'1']) assert.throws(()=>chaos.update({fault:'not_ready',ttl_seconds}),{status:400});
});
test('runtime TTL 자동 만료·기본900·최대3600·원자 영속·close 보존',async t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'p18-node-state-')); t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const {chaos,logs}=setup(t,{FAULT_STATE_DIR:dir});
  const before=Date.now(); chaos.update({fault:'not_ready'});
  assert.ok(Date.parse(chaos.state().faults.not_ready.expires_at)-before>=899000);
  chaos.update({fault:'liveness_fail',ttl_seconds:3600});
  chaos.update({fault:'not_ready',ttl_seconds:1});
  const saved=JSON.parse(fs.readFileSync(path.join(dir,'faults.json'))); assert.ok(saved.not_ready.expires_at);
  chaos.close();
  const resumed=setup(t,{FAULT_STATE_DIR:dir}).chaos;
  assert.equal(resumed.enabled('not_ready'),true);
  await new Promise(resolve=>setTimeout(resolve,1100));
  assert.equal(resumed.enabled('not_ready'),false); assert.equal(resumed.enabled('liveness_fail'),true);
  resumed.reset(); assert.equal(fs.existsSync(path.join(dir,'faults.json')),false);
  assert.ok(logs.some(x=>JSON.parse(x).event==='chaos_changed'));
});
test('배포 ENV JSON은 runtime clear·재시작에도 유지되고 이미지 rollback만 제거',t=>{
  const env={RELEASE_FAULTS:JSON.stringify({faults:{code_bug:{params:{}}}})};
  const {chaos}=setup(t,env);
  chaos.update({fault:'not_ready'});chaos.update({fault:'code_bug',on:false});chaos.reset();
  assert.equal(chaos.enabled('code_bug'),true);
  assert.equal(chaos.state().faults.code_bug.source,'release');
  assert.deepEqual(chaos.state().release_active,['code_bug']);
  assert.equal(setup(t,env).chaos.enabled('code_bug'),true);
  assert.equal(setup(t,{}).chaos.enabled('code_bug'),false);
});
test('손상된 runtime 및 release JSON은 안전하게 무시하며 경고',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'p18-node-bad-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  fs.writeFileSync(path.join(dir,'faults.json'),'{bad');
  const {chaos,logs}=setup(t,{FAULT_STATE_DIR:dir,RELEASE_FAULTS:'{bad'});
  assert.deepEqual(chaos.state().active,[]);assert.ok(logs.length>=2);
});
