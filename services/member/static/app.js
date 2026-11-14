'use strict';
const $=id=>document.getElementById(id);
const notice=(text,error=false)=>{ $('notice').textContent=text; $('notice').className='notice'+(error?' error':''); };
async function request(path,method='GET',body) {
  const started=performance.now();
  let res;
  try {res=await fetch(path,{method,headers:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(35000)});} catch {throw new Error('서버에 연결할 수 없거나 응답 시간이 초과되었습니다. 실행 상태를 확인해 주세요.');}
  const data=await res.json();
  if(!res.ok) throw new Error(`${data.error||'요청을 처리하지 못했습니다.'} (응답 ${res.status}, ${Math.round(performance.now()-started)}밀리초)`);
  return data;
}
async function act(button,fn) {
  button.disabled=true;
  try {await fn();} catch(error) {notice(error.message,true);} finally {button.disabled=false;}
}
function text(tag,value,className) {const node=document.createElement(tag);node.textContent=value;if(className)node.className=className;return node;}
const faultNames={code_bug:['코드 결함','특정 업무 경로에서 실제 예외와 오류 응답이 발생합니다.'],latency:['응답 지연','업무 요청 응답이 기본 2초 늦어집니다.'],error_rate:['일부 요청 실패','업무 요청의 절반을 일시적 실패로 반환합니다.'],crash:['프로세스 종료','응답 직후 종료합니다. 다시 실행해야 복구됩니다.'],memory_leak:['메모리 누수','초당 8메비바이트씩 계속 증가합니다. 메모리 제한에 도달하면 종료됩니다.'],not_ready:['준비 상태 실패','준비 확인은 실패하지만 생존 확인은 유지됩니다.'],event_loop_block:['이벤트 루프 정지','업무 요청마다 1.5초 동안 모든 요청 처리가 멈춥니다.']};
let faultState={};
async function refreshChaos() {
  const state=await request('/api/chaos'); faultState=state.faults;
  for(const [fault,config] of Object.entries(faultState)) {
    let button=document.querySelector(`[data-fault="${fault}"]`);
    if(!button) {
      const box=text('div','','fault');button=document.createElement('button');button.type='button';button.dataset.fault=fault;
      button.append(text('strong',config.label||faultNames[fault]?.[0]||fault),text('span','꺼짐'));box.append(button,text('p',config.signal||faultNames[fault]?.[1]||'업무 장애 실험'));$('faults').append(box);
      button.addEventListener('click',()=>act(button,async()=>{
        const on=!faultState[fault].on;
        if(on && ['crash','startup_config_error','memory_leak','event_loop_block','cpu_spin','worker_exhaustion'].includes(fault) && !confirm('실제 중단이나 재시작이 발생할 수 있습니다. 장애 실험을 켜시겠습니까?')) return;
        await request('/api/chaos','POST',{fault,on});await refreshChaos();notice(`${faultState[fault].label||fault} 실험을 ${on?'켰습니다':'껐습니다'}.`);
      }));
    }
    button.disabled=config.source==='release';
    button.setAttribute('aria-pressed',String(config.on));button.querySelector('span').textContent=config.source==='release'?'배포 결함(이미지)':config.on?'켜짐':'꺼짐';
  }
  const active=Object.values(faultState).filter(x=>x.on).length;
  $('chaos-note').textContent=active?`현재 ${active}개 장애가 켜져 있습니다. 메모리 증가분 ${Math.round(state.allocatedBytes/1048576)}메비바이트.`:'켜진 장애가 없습니다.';
}
$('reset').addEventListener('click',()=>act($('reset'),async()=>{await request('/api/chaos','DELETE');await refreshChaos();notice('런타임 장애를 해제했습니다. 배포 결함은 이미지 rollback으로 제거합니다. 해제한 메모리는 수거 시점에 따라 늦게 줄어들 수 있습니다.');}));
request('/api/info').then(info=>{$('environment').textContent=({dev:'개발',stg:'검증',prd:'운영'}[info.env]||'알 수 없는')+' 환경';$('version').textContent='버전 '+info.version;}).catch(error=>notice(error.message,true));
refreshChaos().catch(error=>{$('chaos-note').textContent=error.message;});
setInterval(()=>refreshChaos().catch(error=>{$('chaos-note').textContent=error.message;}),5000);

async function loadMembers() {
  const data=await request('/api/members?search='+encodeURIComponent($('search-name').value));
  $('count').textContent=`조회 ${data.members.length}명 · 전체 ${data.total}명`;
  $('members').replaceChildren();
  for(const member of data.members) {
    const row=text('div','','list-row'),detail=document.createElement('div');
    detail.append(text('strong',member.name),text('p','가입 '+new Date(member.createdAt).toLocaleString('ko-KR'),'help'));
    const button=text('button','로그인');button.type='button';button.className='secondary';button.addEventListener('click',()=>{$('login-name').value=member.name;$('login-name').focus();});
    row.append(detail,button);$('members').append(row);
  }
  if(!data.members.length) $('members').append(text('p','일치하는 회원이 없습니다. 이름을 바꾸어 조회하거나 새로 가입해 주세요.','empty'));
}
$('signup').addEventListener('submit',event=>{event.preventDefault();act(event.submitter,async()=>{const data=await request('/api/members','POST',{name:$('signup-name').value});notice(`${data.name} 회원을 등록했습니다.`);$('signup-name').value='';await loadMembers();});});
$('login').addEventListener('submit',event=>{event.preventDefault();act(event.submitter,async()=>{const data=await request('/api/login','POST',{name:$('login-name').value});notice(`${data.displayName}님, ${data.message}`);});});
$('search').addEventListener('submit',event=>{event.preventDefault();act(event.submitter,loadMembers);});
$('replay').addEventListener('click',()=>act($('replay'),async()=>{await request('/api/login','POST',{name:'장애회원'});notice('장애회원의 로그인 요청이 정상 처리되었습니다.');}));
loadMembers().catch(error=>{notice(error.message,true);$('members').replaceChildren(text('p','목록을 불러오지 못했습니다. 조회 버튼으로 다시 시도해 주세요.','empty'));});
