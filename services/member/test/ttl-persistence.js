'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {createChaos}=require('../chaos.js');
function fixture(t, saved) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'p18-ttl-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const file=path.join(dir,'faults.json'),logs=[];
  fs.writeFileSync(file,JSON.stringify(saved));
  function start() {
    const chaos=createChaos((status,message)=>{throw Object.assign(new Error(message),{status});},{FAULT_STATE_DIR:dir},()=>{},()=>{},()=>{},false,{event:line=>logs.push(JSON.parse(line))});
    t.after(()=>chaos.close());return chaos;
  }
  return {file,logs,start};
}
test('복원 TTL 3600초 초과는 폐기·경고하고 파일에서도 제거',t=>{
  const f=fixture(t,{not_ready:{params:{},expires_at:new Date(Date.now()+7200000).toISOString()}});
  assert.deepEqual(f.start().state().active,[]);
  assert.equal(fs.existsSync(f.file),false);
  assert.ok(f.logs.some(row=>row.level==='WARN'&&row.event==='fault_state_invalid'));
});

test('만료 crash·startup 상태는 세 번 재시작해도 제거되고 실행되지 않음',t=>{
  const past=new Date(Date.now()-1000).toISOString();
  const f=fixture(t,{crash:{params:{},expires_at:past},startup_config_error:{params:{},expires_at:past}});
  for(let i=0;i<3;i++){const state=f.start();assert.deepEqual(state.state().active,[]);state.close();assert.equal(fs.existsSync(f.file),false);}
});
test('혼합맵 만료만 정리하고 정확히 3600초인 유효 항목은 유지',t=>{
  const now=1700000000000;t.mock.method(Date,'now',()=>now);
  const f=fixture(t,{crash:{params:{},expires_at:new Date(now).toISOString()},not_ready:{params:{},expires_at:new Date(now+3600000).toISOString()}});
  assert.deepEqual(f.start().state().active,['not_ready']);
  assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(f.file))),['not_ready']);
});
test('손상 timestamp는 안전 해제·경고·삭제하고 혼합맵 부분 복원을 하지 않음',t=>{
  for(const expires_at of ['invalid',null,123,true,{},'999999999999999999999999999']){
    const f=fixture(t,{not_ready:{params:{},expires_at:new Date(Date.now()+60000).toISOString()},crash:{params:{},expires_at}});
    assert.deepEqual(f.start().state().active,[]);
    assert.equal(fs.existsSync(f.file),false);
    assert.ok(f.logs.some(row=>row.level==='WARN'&&row.event==='fault_state_invalid'));
  }
});
test('혼합맵은 만료 항목의 이름·params도 검증하고 전체 거부',t=>{
  for(const invalid of [{unknown:{params:{},expires_at:new Date(0).toISOString()}},{latency:{params:{bogus:1},expires_at:new Date(0).toISOString()}}]){
    const f=fixture(t,{not_ready:{params:{},expires_at:new Date(Date.now()+60000).toISOString()},...invalid});
    assert.deepEqual(f.start().state().active,[]);assert.equal(fs.existsSync(f.file),false);
  }
});
test('원자 rename 실패는 update 성공·메모리 변경을 위조하지 않음',t=>{
  const f=fixture(t,{}),chaos=f.start();chaos.update({fault:'not_ready'});
  const previous=fs.readFileSync(f.file,'utf8');
  t.mock.method(fs,'renameSync',()=>{throw new Error('injected rename failure');});
  assert.throws(()=>chaos.update({fault:'liveness_fail'}),/rename failure/);
  assert.deepEqual(chaos.state().active,['not_ready']);assert.equal(fs.readFileSync(f.file,'utf8'),previous);
});
test('삭제 실패는 reset 성공·메모리 해제를 위조하지 않음',t=>{
  const f=fixture(t,{}),chaos=f.start();chaos.update({fault:'not_ready'});
  const rm=fs.rmSync;t.mock.method(fs,'rmSync',(target,options)=>{if(target===f.file)throw new Error('injected delete failure');return rm(target,options);});
  assert.throws(()=>chaos.reset(),/delete failure/);assert.equal(chaos.enabled('not_ready'),true);
  assert.ok(JSON.parse(fs.readFileSync(f.file)).not_ready);
});
test('시작 정리 저장 실패는 정상 복원 성공으로 반환하지 않음',t=>{
  const f=fixture(t,{not_ready:{params:{},expires_at:new Date(0).toISOString()}});
  const rm=fs.rmSync;t.mock.method(fs,'rmSync',(target,options)=>{if(target===f.file)throw new Error('injected cleanup failure');return rm(target,options);});
  assert.throws(()=>f.start(),/cleanup failure/);
});
