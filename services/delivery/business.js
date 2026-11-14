'use strict';
const {callDependency}=require('./dependency.js');
const STEPS = ['접수','출고','배송 중','완료'];
class StatusRegressionError extends Error {constructor(){super('배송 이벤트보다 현재 단계가 이전입니다.');this.name='StatusRegressionError';}}
class DelayScanError extends Error {constructor(){super('지연 감시 작업의 도착 예정일이 유효하지 않습니다.');this.name='DelayScanError';}}
function validateConfiguration(){throw Object.assign(new Error('배송 설정 검증 실패'),{name:'ConfigurationError'});}
function parseArrivalDate(value,bug) {
  // 정상 수정: 검증된 도착 예정일 value를 사용한다.
  return new Date(bug?'invalid-arrival-date':value).toISOString();
}
function serializeEta(parcel,bug) {
  // 정상 수정: 송장의 dueAt을 날짜 원본으로 사용한다.
  return {tracking:parcel.tracking,dueAt:new Date(bug?'invalid-eta-date':parcel.dueAt).toISOString()};
}
function createBusiness(fail,env={},reportException=()=>{}) {
  const now = Date.now();
  let transitions = 0;
  const deliveries = [
    {tracking:'LP1001',recipient:'김하늘',status:'출고',dueAt:new Date(now+86400000).toISOString()},
    {tracking:'LP1002',recipient:'이준호',status:'배송 중',dueAt:new Date(now-7200000).toISOString()},
    {tracking:'LP-AUTO',recipient:'자체 점검',status:'출고',dueAt:new Date(now+86400000).toISOString()},
  ].map(x=>({...x,events:[{status:x.status,note:'초기 배송 정보',at:new Date().toISOString()}]}));
  const delayed = x=>x.status!=='완료' && Date.parse(x.dueAt)<Date.now();
  function transition(parcel, next, codeBug) {
    const current = STEPS.indexOf(parcel.status), target = STEPS.indexOf(next);
    if (target<0) fail(400,'알 수 없는 배송 단계입니다.');
    if (target!==current && target!==current+1) fail(409,'배송 단계는 다음 단계로만 변경할 수 있습니다.');
    if (codeBug && next==='배송 중') {
      // 정상 수정: 마지막 이벤트 인덱스는 events.length-1이다.
      const previousEvent = parcel.events[parcel.events.length];
      parcel.lastNote = previousEvent.note.trim();
    }
    if (target!==current) {
      parcel.status=next; transitions++;
      parcel.events.push({status:next,note:'단계 변경',at:new Date().toISOString()});
    }
    return {...parcel,delayed:delayed(parcel)};
  }
  function checkIntegrity() {
    const parcel=deliveries[0],previous=parcel.status;parcel.status='접수';
    try {if(parcel.events.some(event=>STEPS.indexOf(event.status)>STEPS.indexOf(parcel.status)))throw new StatusRegressionError();}
    finally {parcel.status=previous;}
  }
  function backgroundJob() {
    const record={dueAt:undefined};
    if(!Number.isFinite(Date.parse(record.dueAt)))throw new DelayScanError();
  }
  return {
    validateConfiguration,backgroundJob,
    template(path) {
      if (path==='/api/deliveries') return path;
      if (/^\/api\/deliveries\/[^/]+\/status$/.test(path)) return '/api/deliveries/{id}/status';
      if (/^\/api\/deliveries\/[^/]+\/eta$/.test(path)) return '/api/deliveries/{id}/eta';
      if (/^\/api\/deliveries\/[^/]+$/.test(path)) return '/api/deliveries/{id}';
      return null;
    },
    async handle(method,url,body,faults) {
      const enabled=name=>typeof faults==='boolean'?name==='code_bug'&&faults:!!faults?.[name]?.on;
      const codeBug=enabled('code_bug');
      if(enabled('business_invariant')){try{checkIntegrity();}catch(error){reportException(error);}}
      const path=url.pathname;
      if (path==='/api/deliveries' && method==='GET') {
        const search=(url.searchParams.get('search')||'').slice(0,80);
        const onlyDelayed=url.searchParams.get('delayed')==='true';
        return {status:200,body:{deliveries:deliveries.filter(x=>(x.tracking.includes(search)||x.recipient.includes(search))&&(!onlyDelayed||delayed(x))).map(x=>({...x,delayed:delayed(x)})),steps:STEPS}};
      }
      if (path==='/api/deliveries' && method==='POST') {
        const tracking=typeof body.tracking==='string'?body.tracking.trim():'';
        const recipient=typeof body.recipient==='string'?body.recipient.trim():'';
        const dueAt=body.dueAt || new Date(Date.now()+86400000).toISOString();
        if (!/^[A-Za-z0-9-]{3,32}$/.test(tracking)||!recipient||recipient.length>40||!Number.isFinite(Date.parse(dueAt))) fail(400,'송장 번호(영문·숫자·붙임표 3~32자), 수령인, 도착 예정일을 확인해 주세요.');
        const arrival=parseArrivalDate(dueAt,enabled('code_bug_validation'));
        if (deliveries.some(x=>x.tracking===tracking)) fail(409,'이미 등록된 송장 번호입니다.');
        if (deliveries.length>=1000) fail(409,'데모 송장 수 한도입니다. 재시작하면 초기화됩니다.');
        const parcel={tracking,recipient,status:'접수',dueAt:arrival,events:[{status:'접수',note:'송장 등록',at:new Date().toISOString()}]};
        deliveries.push(parcel); return {status:201,body:parcel};
      }
      const match=path.match(/^\/api\/deliveries\/([^/]+)(\/(status|eta))?$/);
      if (match && ((method==='GET'&&match[3]!=='status')||(method==='PATCH'&&match[3]!=='eta'))) {
        const parcel=deliveries.find(x=>x.tracking===decodeURIComponent(match[1]));
        if (!parcel) fail(404,'송장을 찾을 수 없습니다.');
        if(method==='PATCH') {
          const current=STEPS.indexOf(parcel.status),target=STEPS.indexOf(body.status);
          if(target<0)fail(400,'알 수 없는 배송 단계입니다.');
          if(target!==current&&target!==current+1)fail(409,'배송 단계는 다음 단계로만 변경할 수 있습니다.');
          await callDependency(env.NOTIFICATION_URL,'/api/notifications',{method:'POST',body:{recipient:parcel.recipient,channel:'sms',message:`${parcel.tracking} ${body.status}`},fault:enabled('dependency_failure')});
          return {status:200,body:transition(parcel,body.status,codeBug)};
        }
        return {status:200,body:match[3]==='eta'?serializeEta(parcel,enabled('code_bug_serialization')):{...parcel,delayed:delayed(parcel)}};
      }
      return null;
    },
    metrics() { return `# TYPE delivery_delayed_total gauge\ndelivery_delayed_total ${deliveries.filter(delayed).length}\n# TYPE delivery_status_changes_total counter\ndelivery_status_changes_total ${transitions}\n`; },
    selfCalls:[{path:'/api/deliveries/LP-AUTO/status',method:'PATCH',body:{status:'배송 중'}},{path:'/api/deliveries',method:'POST',body:{tracking:'LP-AUTO',recipient:'자체 점검'}},{path:'/api/deliveries/LP1001/eta',method:'GET'},{path:'/api/deliveries',method:'GET'},{path:'/api/deliveries/LP1001',method:'GET'}],
  };
}
module.exports = {createBusiness};
