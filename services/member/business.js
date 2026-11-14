'use strict';
const {callDependency}=require('./dependency.js');
class SessionIntegrityError extends Error {constructor(){super('세션 소유 회원과 실제 회원이 일치하지 않습니다.');this.name='SessionIntegrityError';}}
class SessionCleanupError extends Error {constructor(){super('세션 정리 중 소유 회원을 찾을 수 없습니다.');this.name='SessionCleanupError';}}
function validateConfiguration() {throw Object.assign(new Error('회원 세션 설정 검증 실패'),{name:'ConfigurationError'});}
function normalizeMemberName(body,codeBug) {
 // 정상 수정: 검증된 body.name 문자열을 정규화한다.
 return codeBug?body.profile.name.trim():body.name.trim();
}
function serializeProfile(member,codeBug) {
 // 정상 수정: 회원 생성일을 날짜 원본으로 사용한다.
 return {...member,createdAt:new Date(codeBug?'invalid-member-date':member.createdAt).toISOString()};
}
function createBusiness(fail,env={},reportException=()=>{}) {
 let serial=3,signups=0,logins=0;
 const members=['김하늘','이준호','장애회원'].map((name,i)=>({id:String(i+1),name,createdAt:new Date().toISOString()}));
 const sessions=new Map([['점검','1']]);
 function nameOf(body) {
  if(Object.keys(body).some(k=>k!=='name'))fail(400,'이름만 입력해 주세요. 비밀번호는 받지 않습니다.');
  const name=typeof body.name==='string'?body.name.trim():'';
  if(!name||name.length>40)fail(400,'이름은 1~40자로 입력해 주세요.');return name;
 }
 function login(name,codeBug) {
  const member=members.find(x=>x.name===name);if(!member)fail(404,'가입된 이름이 없습니다. 먼저 회원 가입을 해 주세요.');
  const profiles=[{displayName:member.name}];
  // 정상 수정: 유효한 프로필 인덱스 0을 사용한다.
  const profile=profiles[codeBug&&name.includes('장애')?1:0];
  const displayName=profile.displayName.trim();logins++;sessions.set('점검',member.id);
  return {member,displayName,message:'로그인 시도에 성공했습니다. 실제 인증은 수행하지 않습니다.'};
 }
 function checkIntegrity() {
  const previous=sessions.get('점검');sessions.set('점검','없는회원');
  try {for(const id of sessions.values())if(!members.some(member=>member.id===id))throw new SessionIntegrityError();}
  finally {sessions.set('점검',previous);}
 }
 function backgroundJob() {
  const owner=members.find(member=>member.id==='만료된세션회원');
  if(!owner)throw new SessionCleanupError();
 }
 return {
  validateConfiguration,backgroundJob,
  template(route){if(['/api/members','/api/login'].includes(route))return route;return /^\/api\/members\/[^/]+\/profile$/.test(route)?'/api/members/{id}/profile':null;},
  async handle(method,url,body,faults) {
   const enabled=name=>typeof faults==='boolean'?name==='code_bug'&&faults:!!faults?.[name]?.on;
   if(enabled('business_invariant')){try{checkIntegrity();}catch(error){reportException(error);}}
   if(url.pathname==='/api/members'&&method==='GET') {
    const search=(url.searchParams.get('search')||'').slice(0,80);return {status:200,body:{members:members.filter(x=>x.name.includes(search)),total:members.length}};
   }
   if(url.pathname==='/api/members'&&method==='POST') {
    nameOf(body);const name=normalizeMemberName(body,enabled('code_bug_validation'));
    if(members.some(x=>x.name===name))fail(409,'이미 등록된 이름입니다.');
    if(members.length>=1000)fail(409,'데모 회원 수 한도입니다. 재시작하면 초기화됩니다.');
    const member={id:String(++serial),name,createdAt:new Date().toISOString()};members.push(member);signups++;return {status:201,body:member};
   }
   if(url.pathname==='/api/login'&&method==='POST')return {status:200,body:login(nameOf(body),enabled('code_bug'))};
   const match=url.pathname.match(/^\/api\/members\/([^/]+)\/profile$/);
   if(match&&method==='GET') {
    const member=members.find(x=>x.id===decodeURIComponent(match[1]));if(!member)fail(404,'회원을 찾을 수 없습니다.');
    const profile=serializeProfile(member,enabled('code_bug_serialization'));
    const recommendations=await callDependency(env.CATALOG_URL,'/api/products?q='+encodeURIComponent(member.name),{fault:enabled('dependency_failure')});
    return {status:200,body:{...profile,recommendations}};
   }
   return null;
  },
  metrics(){return `# TYPE member_signups_total counter\nmember_signups_total ${signups}\n# TYPE member_login_attempts_total counter\nmember_login_attempts_total ${logins}\n`;},
  selfCalls:[{path:'/api/login',method:'POST',body:{name:'장애회원'}},{path:'/api/members',method:'POST',body:{name:'장애회원'}},{path:'/api/members/1/profile',method:'GET'},{path:'/api/members',method:'GET'}],
 };
}
module.exports={createBusiness};
