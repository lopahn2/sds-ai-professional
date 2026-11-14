'use strict';
require('./campaign-state.js');
require('./ttl-persistence.js');
require('./campaign-http.js');
require('./campaign-resources.js');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {once} = require('node:events');
const {createApplication} = require('../server.js');

test('공통 히스토그램·업무 지표와 접근 로그',async t=>{
  const {request,logs}=await setup(t);
  await request('/healthz/live');
  const res=await request('/metrics'); assert.equal(res.status,200);
  assert.match(res.headers.get('content-type'),/^text\/plain/);
  const text=await res.text();
  assert.match(text,/app_info\{system="[^"\n]+",service="[^"\n]+",language="node",version="시험판"\} 1/);
  assert.match(text,/http_server_requests_seconds_count\{method="GET",uri="\/healthz\/live",status="200"\} 1/);
  for(const le of ['0.05','0.1','0.25','0.5','1','2.5','5','+Inf']) assert.ok(text.includes(`le="${le}"`));
  assert.ok(text.includes('http_server_requests_seconds_sum'));
  const line=JSON.parse(logs[0]); assert.equal(line.method,'GET'); assert.equal(line.status,200);
  assert.ok(Number.isFinite(line.duration_ms)); assert.ok(line.system); assert.ok(line.service);
});
test('장애 켜짐 표시·준비 실패·지연과 전체 해제',async t=>{
  const {request}=await setup(t);
  assert.equal((await request('/api/chaos','POST',{fault:'없는장애',on:true})).status,400);
  for(const fault of ['code_bug','not_ready','latency']) {
    const res=await request('/api/chaos','POST',{fault,on:true,...(fault==='latency'?{params:{delay_ms:25}}:{})});
    assert.equal(res.status,200); assert.equal((await res.json()).faults[fault].on,true);
  }
  assert.equal((await request('/healthz/ready')).status,503);
  assert.equal((await request('/healthz/live')).status,200);
  assert.equal((await request('/api/chaos','DELETE')).status,200);
  assert.equal((await request('/healthz/ready')).status,200);
  const state=await (await request('/api/chaos')).json();
  assert.ok(Object.values(state.faults).every(x=>!x.on));
});
test('업무 요청만 지연·오류율 적용, 제어 경로로 복구',async t=>{
  const {request}=await setup(t);
  await request('/api/chaos','POST',{fault:'latency',on:true,params:{delay_ms:30}});
  const start=performance.now(); await request('/api/deliveries');
  assert.ok(performance.now()-start>=25);
  await request('/api/chaos','POST',{fault:'latency',on:false});
  await request('/api/chaos','POST',{fault:'error_rate',on:true,params:{rate:1}});
  assert.equal((await request('/api/deliveries')).status,503);
  assert.equal((await request('/api/info')).status,200);
  await request('/api/chaos','POST',{fault:'error_rate',on:false});
  assert.equal((await request('/api/deliveries')).status,200);
  assert.equal((await request('/api/chaos','POST',{fault:'error_rate',params:{rate:2}})).status,400);
});
test('메모리는 매초 실제 할당하고 해제 시 참조를 비운다',async t=>{
  const {request}=await setup(t);
  await request('/api/chaos','POST',{fault:'memory_leak',on:true,params:{mib_per_second:1}});
  await new Promise(resolve=>setTimeout(resolve,1100));
  let state=await (await request('/api/chaos')).json();
  assert.ok(state.allocatedBytes>=1048576);
  await request('/api/chaos','DELETE');
  state=await (await request('/api/chaos')).json(); assert.equal(state.allocatedBytes,0);
});
test('예외는 식별 접두와 실제 노드 다중 행 스택으로 기록한다',async t=>{
  const {reportException,errors}=await setup(t);
  assert.equal(typeof reportException,'function');
  reportException(new TypeError('형식 확인'));
  assert.match(errors[0],/^level=ERROR system=\S+ service=\S+\nTypeError: 형식 확인\n\s+at /);
  assert.match(errors[0],/index\.js:\d+:\d+/);
});
test('한국어 화면·지역 정적 파일과 보안 응답 머리말',async t=>{
  const {request}=await setup(t);
  const res=await request('/'); assert.equal(res.status,200);
  const html=await res.text(); assert.match(html,/<html lang="ko">/); assert.ok(html.includes('장애 실험'));
  assert.equal(res.headers.get('x-content-type-options'),'nosniff');
  assert.ok(!/https?:\/\//.test(html));
  assert.match(html, /id="dueAt"[^>]+type="text"/);
  assert.equal((await request('/app.css')).status,200);
  assert.equal((await request('/app.js')).status,200);
  assert.equal((await request('/not-a-route')).status,404);
});

async function setup(t) {
  const logs = [], errors = [];
  const app = createApplication({env:{SELF_TRAFFIC:'0', APP_ENV:'stg',APP_VERSION:'시험판',SYSTEM_NAME:'custom-system',SERVICE_NAME:'custom-service'}, accessLog:line=>logs.push(line), errorLog:line=>errors.push(line)});
  app.server.listen(0,'127.0.0.1');
  await once(app.server,'listening');
  t.after(async()=>{app.server.closeAllConnections(); await new Promise(resolve=>app.server.close(resolve));});
  const base = `http://127.0.0.1:${app.server.address().port}`;
  const request = (path,method='GET',body)=>fetch(base+path,{method,headers:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  return {...app,base,request,logs,errors};
}
test('생존·준비 상태와 환경 식별 정보',async t=>{
  const {request}=await setup(t);
  assert.equal((await request('/healthz/live')).status,200);
  assert.equal((await request('/healthz/ready')).status,200);
  const info=await (await request('/api/info')).json();
  assert.equal(info.env,'stg'); assert.equal(info.version,'시험판');
  assert.equal(info.system,'custom-system'); assert.equal(info.service,'custom-service');
});

test('송장 등록·조회·순차 단계 변경·지연 목록', async t=>{
  const {request}=await setup(t);
  const res=await request('/api/deliveries','POST',{tracking:'LP-TEST',recipient:'테스트 수령인',dueAt:'2020-01-01T00:00:00Z'});
  assert.equal(res.status,201);
  assert.equal((await request('/api/deliveries/LP-TEST')).status,200);
  assert.equal((await request('/api/deliveries/LP-TEST/status','PATCH',{status:'완료'})).status,409);
  for(const status of ['출고','배송 중','완료']) assert.equal((await request('/api/deliveries/LP-TEST/status','PATCH',{status})).status,200);
  const delayed=await (await request('/api/deliveries?delayed=true')).json();
  assert.ok(!delayed.deliveries.some(x=>x.tracking==='LP-TEST'));
  assert.ok(delayed.deliveries.length>0);
  assert.equal((await request('/api/deliveries','POST',{tracking:'../잘못된번호',recipient:'수령인'})).status,400);
  assert.equal((await request('/api/deliveries','POST',{tracking:'LP-TEST',recipient:'수령인'})).status,409);
});

