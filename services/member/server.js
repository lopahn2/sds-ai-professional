'use strict';
const http=require('node:http');
const fs=require('node:fs');
const path=require('node:path');
const {createBusiness}=require('./business.js');
const {createMetrics}=require('./metrics.js');
const {createChaos}=require('./chaos.js');
function fail(status,message) { const error=new Error(message); error.status=status; throw error; }
async function readJson(req) {
  let size=0; const chunks=[];
  for await(const chunk of req) {
    size+=chunk.length;
    if(size>8192) fail(413,'요청 본문이 너무 큽니다.');
    chunks.push(chunk);
  }
  let body;
  try {body=JSON.parse(Buffer.concat(chunks).toString('utf8'));} catch {fail(400,'올바른 JSON 본문을 보내 주세요.');}
  if(!body||typeof body!=='object'||Array.isArray(body)) fail(400,'JSON 객체가 필요합니다.');
  return body;
}
function createApplication(options={}) {
  const env=options.env||process.env;
  const safeTag=value=>String(value).replace(/[^a-zA-Z0-9_.-]/g,'_').slice(0,64);
  const info={system:safeTag(env.SYSTEM_NAME||'member'),service:safeTag(env.SERVICE_NAME||'member'),env:['dev','stg','prd'].includes(env.APP_ENV)?env.APP_ENV:'dev',version:String(env.APP_VERSION||'0.1.0').slice(0,80),language:'node'};
  const accessLog=options.accessLog||((line)=>process.stdout.write(line+'\n'));
  const errorLog=options.errorLog||((line)=>process.stdout.write(line+'\n'));
  const metrics=createMetrics(info), business=createBusiness(fail,env,reportException);
  function reportException(error) {
    metrics.error(error.name||'Error');
    let stack=error.stack||String(error);for(let cause=error.cause;cause;cause=cause.cause)stack+='\nCaused by: '+(cause.stack||String(cause));
    errorLog(`level=ERROR system=${info.system} service=${info.service}\n${stack}`);
  }
  let trafficTimer,trafficRequest,inFlight=false,closing=false,trafficIndex=0;
  const configuredInterval=Number(env.SELF_TRAFFIC_INTERVAL_SECONDS??4);
  const trafficInterval=Number.isFinite(configuredInterval)&&configuredInterval>0?configuredInterval*1000:0;
  function selfTraffic() {
    if(env.SELF_TRAFFIC==='0'||!trafficInterval||inFlight||closing||!server.listening) return;
    inFlight=true;
    const call=business.selfCalls[trafficIndex++%business.selfCalls.length], payload=call.body===undefined?'':JSON.stringify(call.body);
    const request=http.request({hostname:'127.0.0.1',port:server.address().port,path:call.path,method:call.method,headers:{'content-type':'application/json','content-length':Buffer.byteLength(payload)},timeout:35000},response=>{
      response.resume(); response.on('end',()=>{inFlight=false;});
    });
    request.on('error',error=>{inFlight=false;if(!closing) accessLog(JSON.stringify({level:'WARN',system:info.system,service:info.service,event:'self_traffic_failed',error:error.code||'REQUEST_FAILED'}));});
    trafficRequest=request;request.on('close',()=>{if(trafficRequest===request)trafficRequest=undefined;inFlight=false;});
    request.on('timeout',()=>request.destroy()); request.end(payload);
  }
  const chaos=createChaos(fail,env,options.exit||((code)=>process.exit(code)),reportException,()=>setImmediate(selfTraffic),'member'==='delivery',{event:accessLog,backgroundJob:business.backgroundJob,validateConfiguration:business.validateConfiguration});
  const staticFiles=new Map([['/',['index.html','text/html; charset=utf-8']],['/app.css',['app.css','text/css; charset=utf-8']],['/app.js',['app.js','text/javascript; charset=utf-8']]].map(([route,[file,type]])=>[route,{body:fs.readFileSync(path.join(__dirname,'static',file)),type}]));
  const knownPaths=new Set(['/healthz/live','/healthz/ready','/api/info','/api/chaos','/metrics',...staticFiles.keys()]);
  const server=http.createServer(async(req,res)=>{
    const started=process.hrtime.bigint();
    let uri='NOT_FOUND';
    const method=['GET','POST','PATCH','DELETE','PUT','OPTIONS','HEAD'].includes(req.method)?req.method:'OTHER';
    res.on('finish',()=>{
      const seconds=Number(process.hrtime.bigint()-started)/1e9;
      metrics.observe(method,uri,res.statusCode,seconds);
      accessLog(JSON.stringify({time:new Date().toISOString(),level:res.statusCode>=500?'ERROR':'INFO',system:info.system,service:info.service,environment:info.env,method,uri,status:res.statusCode,duration_ms:Number((seconds*1000).toFixed(3))}));
    });
    res.setHeader('x-content-type-options','nosniff');
    res.setHeader('content-security-policy',"default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
    res.setHeader('cache-control','no-store');
    const send=(status,body,type='application/json; charset=utf-8')=>{res.writeHead(status,{'content-type':type});res.end(typeof body==='string'||Buffer.isBuffer(body)?body:JSON.stringify(body));};
    let releaseSlot;
    try {
      const url=new URL(req.url,'http://localhost');
      uri=business.template(url.pathname)||(knownPaths.has(url.pathname)?url.pathname:'NOT_FOUND');
      if(req.method==='GET'&&staticFiles.has(url.pathname)) {const file=staticFiles.get(url.pathname);return send(200,file.body,file.type);}
      if(req.method==='GET'&&url.pathname==='/healthz/live') return send(chaos.enabled('liveness_fail')?503:200,{status:chaos.enabled('liveness_fail')?'생존 확인 실패':'정상'});
      if(req.method==='GET'&&url.pathname==='/healthz/ready') return send(chaos.faults.not_ready.on?503:200,{status:chaos.faults.not_ready.on?'준비되지 않음':'정상'});
      if(req.method==='GET'&&url.pathname==='/api/info') return send(200,info);
      if(req.method==='GET'&&url.pathname==='/metrics') return send(200,metrics.render(business.metrics(),chaos.state().allocatedBytes),'text/plain; version=0.0.4; charset=utf-8');
      if(url.pathname==='/api/chaos') {
        if(req.method==='GET') return send(200,chaos.state());
        if(req.method==='POST') return send(200,chaos.update(await readJson(req)));
        if(req.method==='DELETE') return send(200,chaos.reset());
        return send(405,{error:'허용하지 않는 요청 방식입니다.'});
      }
      if(business.template(url.pathname)) {
        releaseSlot=await chaos.acquire();
        await chaos.beforeBusiness();
        const body=['POST','PATCH'].includes(req.method)?await readJson(req):{};
        const response=await business.handle(req.method,url,body,chaos.faults);
        if(response) return send(response.status,response.body);
        return send(405,{error:'허용하지 않는 요청 방식입니다.'});
      }
      send(404,{error:'요청 경로를 찾을 수 없습니다.'});
    } catch(error) {
      const status=error.status||500;
      if(status>=500&&!error.noStack) reportException(error);
      if(!res.destroyed)send(status,{error:error.status?error.message:'업무 처리 중 오류가 발생했습니다. 운영 로그를 확인해 주세요.'});
    } finally {releaseSlot?.();}
  });
  server.requestTimeout=40000; server.headersTimeout=10000;
  server.on('listening',()=>{if(env.SELF_TRAFFIC!=='0'&&trafficInterval) {trafficTimer=setInterval(selfTraffic,trafficInterval);trafficTimer.unref();}});
  function cleanup(){if(closing)return;closing=true;clearInterval(trafficTimer);trafficRequest?.destroy();chaos.close();}
  const close=server.close.bind(server);server.close=(...args)=>{cleanup();return close(...args);};
  server.on('close',cleanup);
  return {server,reportException};
}
if(require.main===module) {
  const app=createApplication();
  app.server.listen(Number(process.env.PORT||8080),'0.0.0.0',()=>process.stdout.write(JSON.stringify({level:'INFO',event:'started',system:process.env.SYSTEM_NAME||'member',service:process.env.SERVICE_NAME||'member',port:app.server.address().port})+'\n'));
  for(const signal of ['SIGTERM','SIGINT']) process.on(signal,()=>{app.server.close(()=>process.exit(0));app.server.closeAllConnections();});
}
module.exports={createApplication};
