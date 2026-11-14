'use strict';
const http=require('node:http');
const https=require('node:https');
class DependencyError extends Error {constructor(cause){super('같은 환경의 업무 연동에 실패했습니다.',{cause});this.name='DependencyError';this.status=502;}}
// URL은 배포가 같은 환경 주소로 주입한다. HTTP redirect는 따라가지 않는다.
async function callDependency(base,route,{method='GET',body,fault=false}={}) {
 if(!base&&!fault)return null;
 try {
  const url=fault?new URL('http://127.0.0.1:1'):new URL(route,base);
  if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw new Error('허용하지 않는 의존 URL');
  return await new Promise((resolve,reject)=>{
   const payload=body===undefined?undefined:JSON.stringify(body);let total=0;
   const request=(url.protocol==='https:'?https:http).request(url,{method,headers:payload?{'content-type':'application/json','content-length':Buffer.byteLength(payload)}:{}},response=>{
    response.on('data',chunk=>{total+=chunk.length;if(total>1048576)request.destroy(new Error('의존 응답 크기 초과'));});
    response.on('error',reject);
    response.on('end',()=>{if(response.statusCode>=500)reject(new Error(`의존 HTTP ${response.statusCode}`));else resolve({status:response.statusCode});});
   });
   const timeout=setTimeout(()=>request.destroy(Object.assign(new Error('의존 호출 2초 제한 초과'),{code:'ETIMEDOUT'})),2000);timeout.unref();
   request.on('close',()=>clearTimeout(timeout));request.on('error',reject);request.end(payload);
  });
 } catch(cause) {throw new DependencyError(cause);}
}
module.exports={callDependency,DependencyError};
