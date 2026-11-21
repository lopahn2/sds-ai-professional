// A fresh Chromium context can only read the exact registered dev resources for this workflow.
import {createHash} from 'node:crypto';
import {writeFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import {join} from 'node:path';

let input='';
for await (const chunk of process.stdin) {input+=chunk;if(input.length>64000)throw Error('plan_too_large');}
const plan=JSON.parse(input);
const sha=x=>createHash('sha256').update(Buffer.isBuffer(x)?x:JSON.stringify(x)).digest('hex');
const same=(value,expected)=>typeof value===typeof expected && value===expected;
class Stop extends Error { constructor(code,outcome='unverifiable'){super(code);this.outcome=outcome;} }
const check=(condition,code)=>{if(!condition)throw new Stop(code,'failed');};
const steps=[];
const evidence=[];

function responsePath(flow) {
  if (flow.scenario==='planned') return flow.path;
  return '/api/products?q='+encodeURIComponent(flow.value);
}
async function readRegistered(url) {
  const response=await fetch(url,{redirect:'manual',signal:AbortSignal.timeout(8500),headers:{accept:'*/*','cache-control':'no-cache'}});
  if(response.status>=300&&response.status<400)throw new Stop('qa_redirect_blocked');
  if(Number(response.headers.get('content-length'))>1024*1024)throw new Stop('qa_response_too_large');
  const chunks=[];let size=0;
  for await (const chunk of response.body||[]){size+=chunk.length;if(size>1024*1024)throw new Stop('qa_response_too_large');chunks.push(chunk);}
  return {status:response.status,body:Buffer.concat(chunks),type:response.headers.get('content-type')||'application/octet-stream'};
}
async function contextFor(browser,flow) {
  const context=await browser.newContext({viewport:{width:1280,height:800},serviceWorkers:'block',acceptDownloads:false,reducedMotion:'reduce',permissions:[]});
  // One page can poll the very same GET in the background. Mark requests synchronously dispatched
  // by an actual registered control so a timer cannot stand in for the operator's click.
  const controls=plan.service==='catalog'?'#submit-business,#records button':flow.submit;
  await context.addInitScript(selector=>{
    const original=window.fetch.bind(window);
    window.fetch=(input,init)=>{
      const event=window.event,control=event?.submitter||event?.target;
      if(event?.isTrusted&&control instanceof Element&&control.closest(selector)){
        const request=new Request(input,init);
        if(request.method==='GET'&&new URL(request.url).origin===window.location.origin){
          const headers=new Headers(request.headers);headers.set('x-liveprobe-qa-control','1');
          return original(new Request(request,{headers}));
        }
      }
      return original(input,init);
    };
  },controls);
  const paths=new Set(['/','/app.js','/app.css','/favicon.ico',responsePath(flow).replaceAll('+','%20')]);
  if(plan.service==='order')paths.add('/actuator/info');
  if(plan.service==='delivery')for(const p of ['/api/info','/api/deliveries?search=&delayed=false'])paths.add(p);
  if(['catalog','payment','notification'].includes(plan.service))for(const p of ['/api/config','/static/app.js','/static/styles.css'])paths.add(p);
  if(plan.service==='catalog'){
    paths.add('/api/products?q=');
    if(/^\d{1,8}$/.test(flow.id))paths.add('/api/products/'+flow.id);
  }
  let blocked=null;
  await context.routeWebSocket('**/*',ws=>{blocked='qa_unregistered_network_request';ws.close();});
  await context.route('**/*',async route=>{
    const r=route.request(),u=new URL(r.url()),path=u.pathname+u.search;
    if(r.method()!=='GET'||u.origin!==plan.origin||!paths.has(path)){
      // The optional fault panel on the catalog landing page performs this read on an interval.
      if(!(r.method()==='GET'&&u.origin===plan.origin&&path==='/api/chaos'&&['catalog','payment','notification','delivery'].includes(plan.service)))blocked='qa_unregistered_network_request';
      await route.abort();return;
    }
    try{
      const observed=await readRegistered(plan.origin+path);
      const csp="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; form-action 'none'; base-uri 'none'; frame-src 'none'; object-src 'none'";
      await route.fulfill({status:observed.status,body:observed.body,headers:{'Content-Type':observed.type,'Cache-Control':'no-store','Content-Security-Policy':csp,'X-Content-Type-Options':'nosniff'}});
    }catch(err){blocked=err instanceof Stop?err.message:'qa_dev_origin_unreachable';await route.abort().catch(()=>{});}
  });
  return {context,blocked:()=>blocked};
}
function controlResponse(page,path) {
  return page.waitForResponse(resp=>{
    const req=resp.request();
    return req.method()==='GET'&&resp.url()===plan.origin+path&&req.headers()['x-liveprobe-qa-control']==='1';
  },{timeout:9000}).catch(()=>{throw new Stop('qa_no_request_from_registered_control');});
}
async function catalog(page,flow,response,report,done) {
  const result=await response.json(), first=result.products?.[0];
  report.network={method:'GET',path:responsePath(flow),status:response.status(),body_sha256:sha(result)};
  check(result.total===flow.total&&first?.id===flow.id&&first.price===flow.price,'qa_product_search_response_mismatch');
  done({http_status:response.status(),network_sha256:report.network.body_sha256});
  const rows=page.locator('#records .record');
  await rows.first().waitFor({state:'visible'});
  check(await rows.count()===flow.total&&await page.locator('#stat-one').innerText()===flow.total+'개','qa_product_list_dom_mismatch');
  const product=rows.first();
  check((await product.innerText()).includes(first.name),'qa_product_name_mismatch');
  done({dom_sha256:sha(await product.innerText())});
  const detailPath='/api/products/'+flow.id;
  const [detail]=await Promise.all([controlResponse(page,detailPath),product.getByRole('button',{name:first.name+' 상세 보기',exact:true}).click()]);
  done();
  const item=(await detail.json()).product;
  check(detail.status()===200&&item?.id===flow.id&&item.price===flow.price&&item.stock===flow.stock
    &&item.description?.includes(flow.description),'qa_product_detail_response_mismatch');
  done({http_status:detail.status(),path:detailPath,network_sha256:sha(item)});
  const shown=page.locator('#product-detail');await shown.waitFor({state:'visible'});
  const visible=await shown.innerText();
  check(visible.includes(item.name)&&visible.includes(item.description)&&visible.includes('재고 '+item.stock+'개')
    &&visible.includes(item.price.toLocaleString('ko-KR')+'원'),'qa_product_detail_dom_mismatch');
  report.dom={text_sha256:sha(visible),values:{가격:item.price,재고:item.stock}};
  done({dom_sha256:report.dom.text_sha256});
  const independent=await readRegistered(plan.origin+detailPath);
  check(independent.status===200,'qa_product_independent_http_failed');
  const actual=JSON.parse(independent.body).product;
  report.independent_read={status:independent.status,body_sha256:sha(actual),matched:false};
  check(actual?.id===item.id&&actual.price===item.price&&actual.stock===item.stock,'qa_product_independent_read_mismatch');
  report.independent_read.matched=true;
}
const plannedResults=[];
const phaseNames={open:'등록된 dev 업무 화면 열기',input:'개발자 테스트의 입력값 전달',submit:'조회 동작 실행',
  network:'업무 요청과 응답 확인', dom:'최종 화면의 업무 결과 대조',independent_read:'별도 조회 결과와 대조'};
function field(doc,keys) {
  for(const k of keys){if(Array.isArray(doc)&&k==='length'){doc=doc.length;continue;}if(doc===null||typeof doc!=='object'||!Object.hasOwn(doc,k))throw new Stop('qa_comparison_field_missing');doc=doc[k];}
  return doc;
}
async function planned(browser,flow,index) {
  const phases=['open',...(flow.input?['input']:[]),'submit','network','dom','independent_read'];
  const report={id:flow.id,steps:phases.map(id=>({id,status:'not_run',summary_ko:phaseNames[id]})),network:null,dom:null,screenshot_sha256:null,independent_read:null};
  plannedResults.push(report);
  let current=0,failure;
  const done=()=>{report.steps[current].status='passed';current++;};
  const {context,blocked}=await contextFor(browser,flow),page=await context.newPage();
  page.setDefaultTimeout(10000);
  try{
    const doc=await page.goto(plan.origin+'/',{waitUntil:'domcontentloaded'});if(!doc?.ok())throw new Stop('qa_dev_page_unavailable');done();
    if(flow.input){
      const input=page.locator(flow.input);
      await input.fill(flow.value);
      check(await input.inputValue()===flow.value,'qa_input_value_mismatch');done();
    }
    else await page.locator(plan.service==='delivery'?'#count':'#stat-one').filter({hasText:/[0-9]/}).waitFor({state:'visible'});
    const request=controlResponse(page,flow.path.replaceAll('+','%20'));
    const button=plan.service==='order'?page.locator('#order').getByRole('button',{name:'주문 가능 여부 확인',exact:true}):page.locator(flow.submit);
    const [resp]=await Promise.all([request,button.click()]);done();
    const json=await resp.json();
    report.network={method:'GET',path:flow.path,status:resp.status(),body_sha256:sha(json)};
    Object.assign(report.steps[current],{http_status:resp.status(),network_sha256:report.network.body_sha256,method:'GET',path:flow.path});
    check(resp.status()===flow.status,'qa_business_http_status_mismatch');done();
    const box=page.locator(flow.result);await box.waitFor({state:'visible'});
    if(plan.service==='order'){
      await page.waitForFunction(()=>/^응답 /.test(document.getElementById('status')?.innerText||''));
      check(await page.locator('#status').innerText()==='응답 '+flow.status,'qa_ui_http_status_mismatch');
      check(json.item===flow.value,'qa_order_item_mismatch');
      if(flow.status===200){
        check(typeof json.orderable==='boolean'&&Number.isInteger(json.inventory?.quantity)
          &&json.inventory.item===flow.value &&json.inventory.available===json.orderable,'qa_order_response_contract_mismatch');
      }else check(json.error==='unknown item','qa_unknown_item_response_mismatch');
    }
    else if(plan.service==='delivery'){
      await page.waitForFunction(()=>document.querySelector('#count')?.innerText==='조회 '+document.querySelectorAll('#deliveries .list-row.late,#deliveries .pill.late').length+'건');
      check(await button.isChecked(),'qa_delivery_filter_not_selected');
    }
    else await page.waitForFunction(()=>/검색을 완료|현황을 새로/.test(document.querySelector('#notice')?.innerText||''));
    const text=await box.innerText();
    const document=plan.service==='order'?JSON.parse(text):null;
    const values={};
    report.dom={text_sha256:sha(text),values};
    report.steps[current].dom_sha256=report.dom.text_sha256;
    for(const c of flow.checks){
      const raw=field(json,c.path),lookup=String(raw);
      const expected=c.map && Object.hasOwn(c.map,lookup)?c.map[lookup]:raw;
      const label=c.label.join('.');
      if(plan.service==='order'){
        const rendered=field(document,c.label);
        check(same(rendered,expected),'qa_dom_field_mismatch');
        values[label]=String(rendered);
      }else{
        // Only the exact count or a rendered product record represents the selected search.
        if(flow.result==='#stat-one'&&c.path.join('.')==='total')check(text===String(expected)+'개','qa_product_count_mismatch');
        else if(flow.result==='#records'&&c.path.join('.')==='products.0.price')check((await box.locator('.record').first().locator('.amount').innerText())===Number(expected).toLocaleString('ko-KR')+'원','qa_product_price_mismatch');
        else if(plan.service==='notification'&&['queue_depth','sent'].includes(c.path.join('.'))&&flow.result===(c.path[0]==='sent'?'#stat-two':'#stat-one'))check(text===String(expected)+'건','qa_notification_count_mismatch');
        else if(plan.service==='payment'&&c.path.join('.')==='payments.length'&&flow.result==='#records'){
          check(Array.isArray(json.payments)&&json.payments.length<=100&&new Set(json.payments.map(p=>p.id)).size===json.payments.length,'qa_payment_history_contract_mismatch');
          const rows=box.locator('.record'),visible=json.payments.slice(0,8);
          check(await rows.count()===visible.length,'qa_payment_list_mismatch');
          for(const [i,payment] of visible.entries()){
            const row=await rows.nth(i).innerText();
            check(row.includes('결제번호 '+payment.id+' ·')&&row.includes(payment.status==='cancelled'?'취소 완료':'결제 완료'),'qa_payment_record_mismatch');
          }
        }
        else if(plan.service==='delivery'&&c.path.join('.')==='deliveries.length'&&flow.result==='#count'){
          check(text==='조회 '+expected+'건'&&Array.isArray(json.deliveries)&&json.deliveries.every(d=>d.delayed===true)
            &&await page.locator('#deliveries .list-row').count()===expected
            &&await page.locator('#deliveries .pill.late').count()===expected,'qa_delivery_count_mismatch');
        }
        else if(plan.service==='payment'&&c.path.join('.')==='processed'&&flow.result==='#stat-one')check(text===String(expected)+'건','qa_payment_count_mismatch');
        else if(plan.service==='payment'&&c.path.join('.')==='charged_amount'&&flow.result==='#stat-two')check(text===Number(expected).toLocaleString('ko-KR')+'원','qa_payment_amount_mismatch');
        else throw new Stop('qa_business_dom_binding_unavailable');
        values[label]=String(expected);
      }
    }
    done();
    report.independent_read={status:flow.independent_status,body_sha256:flow.independent_sha256,matched:false};
    report.steps[current].independent_sha256=flow.independent_sha256;
    for(const c of flow.bindings)check(same(field(json,c.path),c.expected),'qa_independent_value_mismatch');
    report.independent_read.matched=true;
    if(blocked())throw new Stop(blocked());
    const screen=await (plan.service==='payment'?page.locator('#stat-one'):box).screenshot({type:'png',timeout:5000});const file='flow-'+index+'.png';
    await writeFile(join(plan.evidenceDir,file),screen,{mode:0o600});report.screenshot_sha256=sha(screen);
    report.steps.find(s=>s.id==='dom').screenshot_sha256=report.screenshot_sha256;done();
    report.outcome='passed';
  }catch(err){
    failure=blocked()?new Stop(blocked()):err instanceof Stop?err:new Stop('qa_browser_flow_unverifiable');
    report.outcome=failure.outcome;
    const at=report.steps[Math.min(current,report.steps.length-1)];
    at.status=failure.outcome==='failed'?'failed':'unverifiable';at.code=failure.message;
  }finally{await context.close();}
  steps.push(...report.steps);
  return failure;
}
async function exercise(browser,flow,index) {
  const {context,blocked}=await contextFor(browser,flow),page=await context.newPage();
  page.setDefaultTimeout(9000);
  const key=flow.test_id;
  const phases=[
    ['open',phaseNames.open],['input',phaseNames.input],['submit','상품 검색'],
    ['network','검색 응답 대조'],['search_dom','검색 목록과 가격 확인'],
    ['detail_submit','상품 상세 열기'],['detail_network','상품 상세 응답 대조'],
    ['dom','화면의 상품 가격·설명·재고 확인'],['independent_read',phaseNames.independent_read],
  ];
  const report={id:key,steps:phases.map(([id,summary_ko])=>({id,status:'not_run',summary_ko}))};
  plannedResults.push(report);
  let failure,current=0;
  const done=(extra={})=>Object.assign(report.steps[current++],extra,{status:'passed'});
  try {
    const doc=await page.goto(plan.origin+'/',{waitUntil:'domcontentloaded'});
    if(!doc?.ok())throw new Stop('qa_dev_page_unavailable');
    done();
    const input=page.locator('#query');await input.fill(flow.value);
    check(await input.inputValue()===flow.value,'qa_input_value_mismatch');done();
    const button=page.locator('#business-form #submit-business');
    const [response]=await Promise.all([controlResponse(page,responsePath(flow)),button.click()]);
    done();
    check(response.status()===flow.status,'qa_business_http_status_mismatch');
    await catalog(page,flow,response,report,done);
    if(blocked())throw new Stop(blocked());
    const screen=await page.locator('#product-detail').screenshot({type:'png',timeout:5000});
    const file='flow-'+index+'.png';await writeFile(join(plan.evidenceDir,file),screen,{mode:0o600});
    report.screenshot_sha256=sha(screen);
    report.steps.find(s=>s.id==='dom').screenshot_sha256=report.screenshot_sha256;
    done({independent_sha256:report.independent_read.body_sha256});
    evidence.push({test_id:key,file,screenshot_sha256:report.screenshot_sha256});
    report.outcome='passed';
  }catch(err){
    failure=blocked()?new Stop(blocked()):err instanceof Stop?err:new Stop('qa_browser_flow_unverifiable');
    report.outcome=failure.outcome;
    Object.assign(report.steps[Math.min(current,report.steps.length-1)],{status:failure.outcome,code:failure.message});
  }finally{await context.close();}
  steps.push(...report.steps.map(s=>({...s,id:key+'/'+s.id})));
  return failure;
}
let browser,reason;
try{
  browser=await chromium.launch({args:['--disable-background-networking','--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp','--host-resolver-rules=MAP * ~NOTFOUND']});
  for(const [i,flow] of plan.flows.entries()){
    const err=flow.scenario==='planned'?await planned(browser,flow,i):await exercise(browser,flow,i);
    if(err && (!reason || err.outcome==='failed'))reason=err;
  }
}catch{reason=new Stop('qa_browser_unavailable');}
finally{if(browser)await browser.close();}
process.stdout.write(JSON.stringify({steps,evidence,flows:plannedResults,outcome:reason?.outcome||'passed',reason:reason?.message||null}));
