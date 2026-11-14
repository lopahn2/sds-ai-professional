"use strict";
const $ = id => document.getElementById(id);
const make = (tag, text, className) => {const node=document.createElement(tag); if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;};
const money = value => new Intl.NumberFormat('ko-KR').format(value)+'원';
const channelNames={email:'이메일',sms:'문자',push:'앱 알림'};
let config;
let busy=false;
async function api(path,method='GET',body){
  const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||'요청을 처리하지 못했습니다');
  return data;
}
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error);$('notice').setAttribute('role',error?'alert':'status');}
async function action(button,fn){if(busy)return;busy=true;const original=button.textContent;button.disabled=true;button.textContent='처리 중…';try{await fn();}catch(error){notice(error.message,true);}finally{button.disabled=false;button.textContent=original;busy=false;}}
function row(title,description,amount){const item=make('li',undefined,'record');const main=make('div',undefined,'record-main');main.append(make('strong',title),make('p',description));if(amount!==undefined)main.append(make('span',money(amount),'amount'));item.append(main);return item;}
function empty(list,text){list.append(make('li',text,'empty'));}
const parameterSpec={latency:['seconds','지연 시간(초)',2,0,30,0.1],error_rate:['ratio','오류 비율(0~1)',0.5,0,1,0.1],memory_leak:['mib_per_second','초당 증가량(MiB)',8,1,64,1]};
async function refreshChaos(){
 const state=await api('/api/chaos');$('active-count').textContent='활성 장애 '+state.active.length+'개';
 // Preserve focused parameter fields while polling.
 if($('faults').contains(document.activeElement))return;
 $('faults').replaceChildren();
 for(const fault of state.faults){
   const release = fault.source === 'release';
   const card=make('article',undefined,'fault'+(fault.on?' active':''));card.append(make('h3',fault.label),make('span',fault.on?'켜짐':'꺼짐','state-label'),make('p',fault.signal));
   let input;const spec=parameterSpec[fault.name];
   if(spec){const field=make('div',undefined,'field');input=make('input');input.type='number';input.id='param-'+fault.name;input.value=fault.params[spec[0]]??spec[2];input.min=spec[3];input.max=spec[4];input.step=spec[5];const label=make('label',spec[1]);label.htmlFor=input.id;field.append(label,input);card.append(field);}
   const button=make('button',fault.label+(fault.on?' 끄기':' 켜기'));button.type='button';button.dataset.fault=fault.name;button.setAttribute('aria-pressed',String(fault.on));
   button.disabled=release; if(release){button.textContent='배포 결함(이미지)';card.append(make('p','이미지 원복으로만 해제됩니다.'));if(input)input.disabled=true;}
   button.addEventListener('click',()=>action(button,async()=>{
     if(!fault.on&&['crash','memory_leak'].includes(fault.name)&&!confirm(fault.name==='crash'?'프로세스가 종료됩니다. 배포 환경에서는 재시작됩니다. 진행하시겠습니까?':'메모리가 제한 없이 증가하며 컨테이너가 강제 종료될 수 있습니다. 진행하시겠습니까?'))return;
     const params={};if(spec){if(!input.reportValidity())return;params[spec[0]]=Number(input.value);}
     await api('/api/chaos','POST',{fault:fault.name,on:!fault.on,params});
     // Remove focus before re-rendering a changed fault state.
     button.blur();await refreshChaos();notice(fault.label+(fault.on?' 장애를 해제했습니다.':' 장애를 활성화했습니다.'));
   }));card.append(button);$('faults').append(card);
 }
}
$('clear-faults').addEventListener('click',event=>action(event.currentTarget,async()=>{await api('/api/chaos','DELETE');await refreshChaos();notice('런타임 장애를 해제했습니다. 배포 결함과 업무 데이터 손상은 별도 원복이 필요합니다.');await refreshBusiness();}));
$('refresh').addEventListener('click',event=>action(event.currentTarget,async()=>{await refreshBusiness();notice('업무 현황을 새로 불러왔습니다.');}));

async function showProduct(id){const data=await api('/api/products/'+id);const p=data.product;const detail=$('product-detail');detail.replaceChildren(make('h3',p.name),make('p',p.description),make('p','분류: '+p.category+' · 재고 '+p.stock+'개'),make('strong',money(p.price),'amount'));detail.hidden=false;}
async function refreshBusiness(){
 const data=await api('/api/products?q='+encodeURIComponent($('query').value));$('stat-one').textContent=data.total+'개';$('stat-two').textContent=data.searches+'회';
 const list=$('records');list.replaceChildren();if(!data.products.length)empty(list,'일치하는 상품이 없습니다. 다른 검색어를 입력하세요.');
 for(const product of data.products){const item=row(product.name,product.category+' · 재고 '+product.stock+'개',product.price);const button=make('button','상세 보기');button.setAttribute('aria-label',product.name+' 상세 보기');button.addEventListener('click',()=>action(button,async()=>{await showProduct(product.id);notice(product.name+' 상세 정보를 표시했습니다.');}));item.append(button);list.append(item);}
}
$('business-form').addEventListener('submit',event=>{event.preventDefault();action($('submit-business'),async()=>{await refreshBusiness();$('product-detail').hidden=true;notice('상품 검색을 완료했습니다.');});});

(async()=>{
 try{config=await api('/api/config');$('environment').textContent={dev:'개발 환경',stg:'검증 환경',prd:'운영 환경'}[config.environment];$('environment').dataset.environment=config.environment;$('version').textContent='버전 '+config.version;$('traffic-state').textContent=config.self_traffic?'자체 트래픽: 4초마다 업무 요청':'자체 트래픽: 사용 안 함';await Promise.all([refreshBusiness(),refreshChaos()]);notice('업무 요청과 장애 실험을 사용할 수 있습니다.');}
 catch(error){notice(error.message,true);}
 setInterval(async()=>{if(busy)return;try{await Promise.all([refreshBusiness(),refreshChaos()]);}catch(error){notice(error.message,true);}},5000);
})();
