'use strict';
const $=id=>document.getElementById(id);
const errorText={ 'inventory unavailable':'재고 서비스에 연결할 수 없습니다.', 'unknown item':'등록되지 않은 상품입니다.', 'order database unavailable':'주문 데이터베이스를 사용할 수 없습니다.' };
function present(data) {
  const result={};
  if(data.item!==undefined) result['상품']=data.item;
  if(data.orderable!==undefined) result['주문 가능 여부']=data.orderable?'주문 가능':'주문 불가';
  if(data.inventory) result['재고']={상품:data.inventory.item??'정보 없음',수량:data.inventory.quantity??'정보 없음',판매가능:data.inventory.available===undefined?'정보 없음':data.inventory.available?'가능':'불가'};
  if(data.error) result['오류']=errorText[data.error]||'요청 처리 오류가 발생했습니다.';
  if(!Object.keys(result).length) result['안내']='예상하지 못한 응답 형식입니다. 운영 로그를 확인해 주세요.';
  return result;
}
$('order').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  $('status').textContent='조회 중';$('notice').className='notice';$('notice').textContent='재고 서비스 응답을 기다리고 있습니다.';
  const started=performance.now();
  try {
    const res=await fetch('/api/orders/check?item='+encodeURIComponent($('item').value.trim()),{signal:AbortSignal.timeout(15000)});
    const data=await res.json();
    $('status').textContent='응답 '+res.status;$('status').className='pill'+(res.ok?'':' late');
    $('response').textContent=JSON.stringify(present(data),null,2);
    $('notice').textContent=res.ok?'주문 가능 여부를 확인했습니다.':(errorText[data.error]||'요청을 처리하지 못했습니다. 운영 로그를 확인해 주세요.');
    $('notice').className='notice'+(res.ok?'':' error');
  } catch {
    $('status').textContent='연결 실패';$('status').className='pill late';$('response').textContent='서버 응답을 읽을 수 없습니다.';
    $('notice').textContent='연결이 끊겼거나 응답 시간이 초과되었습니다. 다시 확인해 주세요.';$('notice').className='notice error';
  } finally {$('elapsed').textContent='소요 시간 '+Math.round(performance.now()-started)+'밀리초';button.disabled=false;}
});
fetch('/actuator/info').then(res=>res.ok?res.json():null).then(info=>{
  if(!info)return;
  const env=info.env||info.environment||info.app?.environment;
  const label={dev:'개발',stg:'검증',prd:'운영'}[env];
  if(label){$('environment').textContent=label+' 환경';$('environment').hidden=false;}
  const version=info.build?.version||info.app?.version;if(version)$('version').textContent='버전 '+version;
}).catch(()=>{});
