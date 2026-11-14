'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {Worker}=require('node:worker_threads');
// 서비스별 독립 이미지이므로 이 파일은 member/delivery에 동일하게 유지한다.
function createChaos(fail,env,exit,reportException,onCodeBug,withEventLoop,hooks={}) {
  const definitions={
    code_bug:['코드 결함','500·업무 스택',{}],
    code_bug_validation:['입력 처리 결함','500·입력 스택',{}],
    code_bug_serialization:['응답 변환 결함','500·변환 스택',{}],
    intermittent_exception:['간헐 예외','500·예외 스택',{ratio:0.15}],
    error_rate:['일부 요청 실패','503 비율',{rate:0.5}],
    latency:['응답 지연','고정 지연',{delay_ms:2000}],
    latency_spike:['꼬리 지연','일부 요청 지연',{ratio:0.3,seconds:3}],
    hang:['업무 요청 정지','호출자 시간 초과',{seconds:40}],
    crash:['프로세스 종료','3초 뒤 종료 코드 1',{}],
    startup_config_error:['시작 설정 오류','설정 검증 스택·종료 코드 1',{}],
    memory_leak:['메모리 누수','상한 없는 실제 메모리 증가',{mib_per_second:8}],
    not_ready:['준비 상태 실패','준비 프로브 503',{}],
    liveness_fail:['생존 상태 실패','생존 프로브 503',{}],
    cpu_spin:['CPU 포화','작업 스레드 CPU 점유',{threads:2}],
    worker_exhaustion:['업무 슬롯 고갈','8개 슬롯 점유·10초 대기 후 503',{}],
    background_job_failure:['주기 작업 실패','배치 예외 스택',{interval_seconds:3}],
    business_invariant:['업무 정합성 위반','정합성 예외 스택',{}],
    dependency_failure:['외부 연동 실패','연결 거부·502 스택',{}],
  };
  if(withEventLoop) definitions.event_loop_block=['이벤트 루프 정지','전체 요청 지연',{block_ms:1500}];
  const runtime={},release={},faults={};
  const event=value=>hooks.event?.(JSON.stringify(value));
  const file=env.FAULT_STATE_DIR?path.join(env.FAULT_STATE_DIR,'faults.json'):null;
  let closed=false,expiryTimer,buffers=[],memoryTimer,crashTimer,jobTimer,startupTimer;
  let workers=[],cpuCounters,usedSlots=0,reservedSlots=0;
  const waiters=[],delays=new Map(),signatures={};
  function sleep(ms) {return new Promise(resolve=>{if(closed)return resolve();const timer=setTimeout(()=>{delays.delete(timer);resolve();},ms);timer.unref();delays.set(timer,resolve);});}
  function releaseSlot(){let released=false;return ()=>{if(released)return;released=true;usedSlots--;drain();};}
  function drain(){while(!closed&&usedSlots+reservedSlots<8&&waiters.length){const waiter=waiters.shift();clearTimeout(waiter.timer);usedSlots++;waiter.resolve(releaseSlot());}}
  function acquire(){
    if(closed)return Promise.reject(Object.assign(new Error('업무 슬롯이 종료되었습니다.'),{status:503}));
    if(usedSlots+reservedSlots<8){usedSlots++;return Promise.resolve(releaseSlot());}
    return new Promise((resolve,reject)=>{
      const waiter={resolve,reject};waiter.timer=setTimeout(()=>{
        const index=waiters.indexOf(waiter);if(index>=0)waiters.splice(index,1);
        const error=new Error('업무 처리 슬롯 8개가 모두 점유되어 10초 대기를 초과했습니다.');error.name='WorkerPoolExhausted';error.status=503;reject(error);
      },10000);waiter.timer.unref();waiters.push(waiter);
    });
  }
  function paramsFor(name,incoming={},base) {
    if(!Object.hasOwn(definitions,name)) fail(400,'알 수 없는 장애 유형입니다.');
    const defaults=definitions[name][2];
    if(!incoming||typeof incoming!=='object'||Array.isArray(incoming)) fail(400,'장애 매개변수가 올바르지 않습니다.');
    const normalized={...incoming};
    if(name==='latency'&&Object.hasOwn(normalized,'seconds')) {normalized.delay_ms=normalized.seconds*1000;delete normalized.seconds;}
    if(name==='error_rate'&&Object.hasOwn(normalized,'ratio')) {normalized.rate=normalized.ratio;delete normalized.ratio;}
    if(Object.keys(normalized).some(key=>!Object.hasOwn(defaults,key))) fail(400,'장애 매개변수가 올바르지 않습니다.');
    const params={...defaults,...base,...normalized};
    for(const [key,value] of Object.entries(params)) {
      const ratio=['ratio','rate'].includes(key);
      const upper=ratio?1:key==='threads'?32:key==='seconds'?120:key==='interval_seconds'?3600:key==='mib_per_second'?64:key==='block_ms'?10000:120000;
      const lower=ratio?0:key==='seconds'||key==='interval_seconds'?0.001:1;
      if(typeof value!=='number'||!Number.isFinite(value)||value<lower||value>upper||(!ratio&&!['seconds','interval_seconds'].includes(key)&&!Number.isInteger(value))) fail(400,'장애 매개변수 범위를 확인해 주세요.');
    }
    return params;
  }
  function persist(next=runtime) {
    if(!file) return;
    fs.mkdirSync(env.FAULT_STATE_DIR,{recursive:true});
    if(!Object.keys(next).length) {fs.rmSync(file,{force:true});return;}
    const temporary=file+'.tmp';fs.writeFileSync(temporary,JSON.stringify(next),{mode:0o600});fs.renameSync(temporary,file);
  }
  function enabled(name) {return Boolean(release[name]||runtime[name]);}
  function refresh() {
    for(const [name,[label,signal,defaults]] of Object.entries(definitions)) {
      const config=release[name]||runtime[name];
      faults[name]={name,label,signal,on:!!config,params:{...(config?.params||defaults)},expires_at:release[name]?null:runtime[name]?.expires_at||null,source:release[name]?'release':runtime[name]?'runtime':null};
    }
  }
  function state() {return {faults,active:Object.keys(faults).filter(name=>faults[name].on),release_active:Object.keys(release),allocatedBytes:buffers.reduce((sum,buffer)=>sum+buffer.length,0),cpuWorkers:workers.length,cpuIterations:cpuCounters?Array.from(cpuCounters,(_,i)=>Atomics.load(cpuCounters,i)).reduce((a,b)=>a+b,0):0,workerPool:{capacity:8,used:usedSlots,reserved:reservedSlots,waiting:waiters.length}};}
  function stopCpu(){for(const worker of workers)worker.terminate().catch(reportException);workers=[];cpuCounters=undefined;}
  function stopResources(){
    clearInterval(memoryTimer);clearTimeout(crashTimer);clearInterval(jobTimer);clearTimeout(startupTimer);
    buffers=[];stopCpu();reservedSlots=0;
    for(const [timer,resolve] of delays){clearTimeout(timer);resolve();}delays.clear();
    for(const waiter of waiters.splice(0)){clearTimeout(waiter.timer);waiter.reject(Object.assign(new Error('업무 슬롯 종료'),{status:503}));}
  }
  function syncResources(){
    if(closed)return;
    for(const name of ['memory_leak','crash','cpu_spin','worker_exhaustion','background_job_failure','startup_config_error']) {
      const signature=enabled(name)?JSON.stringify(faults[name].params):'off';
      if(signatures[name]===signature)continue;signatures[name]=signature;
      if(name==='memory_leak') {
        clearInterval(memoryTimer);buffers=[];
        if(enabled(name)){memoryTimer=setInterval(()=>buffers.push(Buffer.alloc(faults[name].params.mib_per_second*1024*1024,0xa5)),1000);memoryTimer.unref();}
      }
      if(name==='crash') {
        clearTimeout(crashTimer);
        if(enabled(name)){crashTimer=setTimeout(()=>{reportException(new Error('장애 실험으로 프로세스를 종료합니다.'));exit(1);},3000);crashTimer.unref();}
      }
      if(name==='cpu_spin') {
        stopCpu();
        if(enabled(name)) {
          const count=faults[name].params.threads;cpuCounters=new Int32Array(new SharedArrayBuffer(count*4));
          for(let index=0;index<count;index++) {
            const worker=new Worker(`const {workerData}=require('node:worker_threads');const counts=new Int32Array(workerData.buffer);let value=1;while(true){for(let i=0;i<65536;i++)value=Math.imul(value,31)+17|0;Atomics.add(counts,workerData.index,1);}`,{eval:true,workerData:{buffer:cpuCounters.buffer,index}});
            worker.on('error',reportException);worker.unref();workers.push(worker);
          }
        }
      }
      if(name==='worker_exhaustion'){reservedSlots=enabled(name)?8:0;drain();}
      if(name==='background_job_failure') {
        clearInterval(jobTimer);
        if(enabled(name)&&hooks.backgroundJob){jobTimer=setInterval(()=>{try{hooks.backgroundJob();}catch(error){reportException(error);}},faults[name].params.interval_seconds*1000);jobTimer.unref();}
      }
      if(name==='startup_config_error') {
        clearTimeout(startupTimer);
        if(enabled(name)&&hooks.validateConfiguration){startupTimer=setTimeout(()=>{try{hooks.validateConfiguration();}catch(error){reportException(error);exit(1);}},0);startupTimer.unref();}
      }
    }
  }
  function scheduleExpiry() {
    clearTimeout(expiryTimer);
    const expires=Object.values(runtime).map(config=>Date.parse(config.expires_at));
    if(closed||!expires.length) return;
    expiryTimer=setTimeout(()=>{
      for(const [name,config] of Object.entries(runtime)) if(Date.parse(config.expires_at)<=Date.now()) {delete runtime[name];event({event:'chaos_changed',fault:name,on:false,params:config.params,reason:'expired'});}
      persist();refresh();syncResources();scheduleExpiry();
    },Math.max(1,Math.min(...expires)-Date.now()));expiryTimer.unref();
  }
  function update(body) {
    if(body.on!==undefined&&typeof body.on!=='boolean') fail(400,'on 값은 참 또는 거짓이어야 합니다.');
    const name=body.fault,params=paramsFor(name,body.params,runtime[name]?.params),on=body.on!==false;
    const ttl=body.ttl_seconds??900;
    if(!Number.isInteger(ttl)||ttl<1||ttl>3600) fail(400,'TTL은 1~3600초 정수여야 합니다.');
    const previous=runtime[name];
    if(on) runtime[name]={params,expires_at:new Date(Date.now()+ttl*1000).toISOString()};else delete runtime[name];
    try {persist();} catch(error) {if(previous) runtime[name]=previous;else delete runtime[name];throw error;}
    refresh();syncResources();scheduleExpiry();event({event:'chaos_changed',fault:name,on,params});
    if(on&&name==='code_bug') onCodeBug();
    return state();
  }
  function reset() {
    persist({});
    for(const name of Object.keys(runtime)) {event({event:'chaos_changed',fault:name,on:false,params:runtime[name].params});delete runtime[name];}
    refresh();syncResources();scheduleExpiry();return state();
  }
  async function beforeBusiness() {
    if(enabled('latency'))await sleep(faults.latency.params.delay_ms);
    if(enabled('latency_spike')&&Math.random()<faults.latency_spike.params.ratio)await sleep(faults.latency_spike.params.seconds*1000);
    if(enabled('hang'))await sleep(faults.hang.params.seconds*1000);
    if(enabled('intermittent_exception')&&Math.random()<faults.intermittent_exception.params.ratio){const error=new Error('간헐 업무 처리 예외');error.name='IntermittentError';throw error;}
    if(enabled('event_loop_block')) {const until=performance.now()+faults.event_loop_block.params.block_ms;while(performance.now()<until) { /* 고의 이벤트 루프 점유 */ }}
    if(enabled('error_rate')&&Math.random()<faults.error_rate.params.rate) {const error=new Error('장애 실험: 업무 요청을 일시적으로 처리할 수 없습니다.');error.status=503;error.noStack=true;throw error;}
  }
  function loadRuntime() {
    if(!file||!fs.existsSync(file)) return;
    let loaded={},cleanup=false;
    const now=Date.now();
    try {
      const parsed=JSON.parse(fs.readFileSync(file,'utf8'));
      if(!parsed||typeof parsed!=='object'||Array.isArray(parsed)) throw new Error('상태 객체가 필요합니다.');
      cleanup=Object.keys(parsed).length===0;
      for(const [name,config] of Object.entries(parsed)) {
        // Validate every entry, even expired ones, before publishing any runtime state.
        const params=paramsFor(name,config?.params),expires=Date.parse(config?.expires_at);
        if(typeof config?.expires_at!=='string'||!Number.isFinite(expires)||expires>now+3600000) throw new Error('저장 만료시각 또는 TTL이 올바르지 않습니다.');
        if(expires>now) loaded[name]={params,expires_at:config.expires_at};else cleanup=true;
      }
    } catch(error) {
      event({level:'WARN',event:'fault_state_invalid',error:error.message});
      loaded={};cleanup=true;
    }
    if(cleanup) try {persist(loaded);} catch(error) {
      event({level:'WARN',event:'fault_state_write_failed',error:error.message});
      throw error;
    }
    Object.assign(runtime,loaded);
  }
  loadRuntime();
  if(env.FAULT_CODE_BUG==='1'&&!runtime.code_bug) runtime.code_bug={params:{},expires_at:new Date(Date.now()+900000).toISOString()};
  if(env.RELEASE_FAULTS) {
    try {
      const parsed=JSON.parse(env.RELEASE_FAULTS);
      if(!parsed?.faults||typeof parsed.faults!=='object'||Array.isArray(parsed.faults)) throw new Error('faults 객체가 필요합니다.');
      const loaded={};for(const [name,config] of Object.entries(parsed.faults)) loaded[name]={params:paramsFor(name,config?.params)};
      Object.assign(release,loaded);event({event:'release_faults_changed',active:Object.keys(release)});
    } catch(error) {event({level:'WARN',event:'release_faults_invalid',error:error.message});}
  }
  refresh();syncResources();scheduleExpiry();
  function close() {closed=true;clearTimeout(expiryTimer);stopResources();}
  return {faults,state,update,reset,enabled,beforeBusiness,acquire,close};
}
module.exports={createChaos};
