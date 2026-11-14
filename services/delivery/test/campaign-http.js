'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {once}=require('node:events');
const http=require('node:http');
const {spawn}=require('node:child_process');
const path=require('node:path');
const {createApplication}=require('../server.js');
const member=require('../package.json').name.endsWith('member');
const list=member?'/api/members':'/api/deliveries';
const cases=member?[
 ['code_bug','/api/login','POST',{name:'장애회원'},'login','TypeError'],
 ['code_bug_validation',list,'POST',{name:'새회원'},'normalizeMemberName','TypeError'],
 ['code_bug_serialization','/api/members/1/profile','GET',undefined,'serializeProfile','RangeError'],
]:[
 ['code_bug','/api/deliveries/LP-AUTO/status','PATCH',{status:'배송 중'},'transition','TypeError'],
 ['code_bug_validation',list,'POST',{tracking:'LP-NEW',recipient:'수령인'},'parseArrivalDate','RangeError'],
 ['code_bug_serialization','/api/deliveries/LP1001/eta','GET',undefined,'serializeEta','RangeError'],
];
async function setup(t,env={}) {
 const logs=[],errors=[];
 const app=createApplication({env:{SELF_TRAFFIC:'0',...env},accessLog:line=>logs.push(line),errorLog:line=>errors.push(line)});
 app.server.listen(0,'127.0.0.1');await once(app.server,'listening');
 t.after(async()=>{app.server.closeAllConnections();await new Promise(resolve=>app.server.close(resolve));});
 const request=(route,method='GET',body)=>fetch(`http://127.0.0.1:${app.server.address().port}`+route,{method,headers:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 return {...app,request,logs,errors};
}
for(const [fault,route,method,body,fn,type] of cases) test(`${fault}: 실제 HTTP 500·고유 업무 함수 스택·해제200`,async t=>{
 const {request,errors}=await setup(t);
 await request('/api/chaos','POST',{fault});
 assert.equal((await request(route,method,body)).status,500);
 assert.match(errors.join('\n'),new RegExp(type+':'));
 assert.match(errors.join('\n'),new RegExp('at '+fn+' .*business\\.js:\\d+:\\d+'));
 await request('/api/chaos','DELETE');
 assert.equal((await request(route,method,body)).status,method==='POST'&&route===list?201:200);
});
test('생존·준비 프로브 독립 및 간헐 예외와 무스택 오류율',async t=>{
 const {request,errors}=await setup(t);
 await request('/api/chaos','POST',{fault:'liveness_fail'});assert.equal((await request('/healthz/live')).status,503);assert.equal((await request('/healthz/ready')).status,200);
 await request('/api/chaos','POST',{fault:'intermittent_exception',params:{ratio:1}});assert.equal((await request(list)).status,500);assert.match(errors.join('\n'),/IntermittentError/);
 await request('/api/chaos','DELETE');errors.length=0;
 await request('/api/chaos','POST',{fault:'error_rate',params:{ratio:1}});assert.equal((await request(list)).status,503);assert.equal(errors.length,0);
});
test('꼬리 지연·hang은 실제 지연, 제어 경로 유지',async t=>{
 const {request}=await setup(t);
 for(const [fault,params] of [['latency_spike',{ratio:1,seconds:0.03}],['hang',{seconds:0.03}]]) {
  await request('/api/chaos','POST',{fault,params});const start=performance.now();assert.equal((await request(list)).status,200);assert.ok(performance.now()-start>=25);await request('/api/chaos','DELETE');
 }
});
test('실제 업무 정합성 및 주기 작업 예외, HTTP 정상·지표 증가',async t=>{
 const {request,errors}=await setup(t);
 await request('/api/chaos','POST',{fault:'business_invariant'});assert.equal((await request(list)).status,200);
 assert.match(errors.join('\n'),member?/SessionIntegrityError/:/StatusRegressionError/);
 await request('/api/chaos','POST',{fault:'background_job_failure',params:{interval_seconds:0.02}});
 await new Promise(resolve=>setTimeout(resolve,65));
 assert.match(errors.join('\n'),member?/SessionCleanupError/:/DelayScanError/);
 const metrics=await (await request('/metrics')).text();assert.match(metrics,/app_errors_total\{exception="(?:SessionCleanupError|DelayScanError)"\} [1-9]/);
 await request('/api/chaos','DELETE');const count=errors.length;await new Promise(resolve=>setTimeout(resolve,60));assert.equal(errors.length,count);
});
test('의존 주소 없으면 건너뛰고 명시적 장애는 실제 연결거부 502·원인 스택',async t=>{
 const {request,errors}=await setup(t);const route=member?'/api/members/1/profile':'/api/deliveries/LP-AUTO/status';const method=member?'GET':'PATCH',body=member?undefined:{status:'배송 중'};
 assert.equal((await request(route,method,body)).status,200);
 await request('/api/chaos','POST',{fault:'dependency_failure'});assert.equal((await request(route,method,body)).status,502);
 assert.match(errors.join('\n'),/DependencyError/);assert.match(errors.join('\n'),/ECONNREFUSED/);
});
test('설정된 같은 환경 의존만 호출: 정상·5xx·2초 timeout을 502로 변환',async t=>{
 let mode='ok',hits=[],bodies=[];
 const upstream=http.createServer((req,res)=>{hits.push([req.method,req.url]);let payload='';req.on('data',chunk=>payload+=chunk);req.on('end',()=>{if(payload)bodies.push(JSON.parse(payload));});req.resume();if(mode==='hang')return;res.writeHead(mode==='error'?503:200,{'content-type':'application/json'});res.end('{}');});
 upstream.listen(0,'127.0.0.1');await once(upstream,'listening');t.after(()=>{upstream.closeAllConnections();upstream.close();});
 const {request,errors}=await setup(t,{[member?'CATALOG_URL':'NOTIFICATION_URL']:`http://127.0.0.1:${upstream.address().port}`});
 const route=member?'/api/members/1/profile':'/api/deliveries/LP-AUTO/status',method=member?'GET':'PATCH',body=member?undefined:{status:'배송 중'};
 assert.equal((await request(route,method,body)).status,200);assert.ok(hits[0][1].startsWith(member?'/api/products?q=':'/api/notifications'));
 if(!member)assert.equal(bodies[0].channel,'sms');
 mode='error';assert.equal((await request(route,method,body)).status,502);
 mode='hang';const started=performance.now();assert.equal((await request(route,method,body)).status,502);assert.ok(performance.now()-started>=1900&&performance.now()-started<3000);assert.match(errors.join('\n'),/DependencyError/);
});
test('자체 HTTP 트래픽이 세 코드 결함 경로를 순환하고 시드만 재사용',async t=>{
 for(const [fault,,, ,fn] of cases) {
  const {request,logs,errors}=await setup(t,{SELF_TRAFFIC:'1',SELF_TRAFFIC_INTERVAL_SECONDS:'0.02'});
  await request('/api/chaos','POST',{fault});await new Promise(resolve=>setTimeout(resolve,300));
  assert.match(errors.join('\n'),new RegExp('at '+fn+' '));
  const paths=logs.map(line=>JSON.parse(line).uri);assert.ok(paths.includes(list));
 }
});
test('배포 결함 API clear 불가 및 UI는 서버 metadata로 새 장애를 표시',async t=>{
 const {request}=await setup(t,{RELEASE_FAULTS:'{"faults":{"not_ready":{"params":{}}}}'});
 await request('/api/chaos','DELETE');assert.equal((await request('/healthz/ready')).status,503);
 const script=await (await request('/app.js')).text();assert.match(script,/config\.label/);assert.match(script,/config\.source==='release'/);assert.match(script,/배포 결함/);
});
test('crash 3초 뒤 실제 exit1 및 설정 검증 함수 실제 시작 실패',async()=>{
 for(const fault of ['crash','startup_config_error']) {
  const start=performance.now();const child=spawn(process.execPath,['server.js'],{cwd:path.dirname(__dirname),env:{...process.env,PORT:'0',SELF_TRAFFIC:'0',RELEASE_FAULTS:JSON.stringify({faults:{[fault]:{params:{}}}})},stdio:['ignore','pipe','pipe']});
  let output='';child.stdout.on('data',chunk=>output+=chunk);child.stderr.on('data',chunk=>output+=chunk);
  const timer=setTimeout(()=>child.kill('SIGKILL'),6000);
  const [code]=await once(child,'exit');clearTimeout(timer);assert.equal(code,1,output);
  if(fault==='crash')assert.ok(performance.now()-start>=2900);else assert.match(output,/at (?:Object\.)?validateConfiguration .*business\.js:\d+:\d+/);
 }
});
