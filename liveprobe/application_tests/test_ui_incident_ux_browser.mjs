// Application 사건 UX 회귀: localhost 정적 파일 + 명시적인 합성 API 응답만 사용한다.
// PLAYWRIGHT_MODULE=<기존 playwright 경로> node test_ui_incident_ux_browser.mjs [스크린샷 경로]
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url);
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE || '@playwright/test');
const STATIC=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../app/application/static');
const OUT=process.argv[2];
if(OUT) fs.mkdirSync(OUT,{recursive:true});
const server=http.createServer((req,res)=>{
  const name=req.url==='/'?'index.html':path.basename(req.url.split('?')[0]);
  if(!['index.html','app.js','styles.css'].includes(name)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');
  res.end(fs.readFileSync(path.join(STATIC,name)));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const sample='RAW_STACK <img src=x onerror="window.__raw_xss=true">\nTraceback: worker.run()';
const image='registry/order@sha256:'+'d'.repeat(64);
const rows=[['automatic','analyzing'],['failed','failed'],['done','completed'],['legacy','awaiting_user_test'],['user','awaiting_user_test'],['approve','awaiting_prd_approval'],['unknown','RAW_UNKNOWN_STATE']].map(([id,state])=>({id,state,service:'order',system:'samsung-trade',environment:id==='legacy'?'stg':'dev',created_at:1794528000,reason:'RAW_REASON '+sample}));
const state={artifact:'a'.repeat(64),version:0,listCalls:0,detailCalls:0,readyCalls:0,failCatalog:false,failReady:false,badList:false,posts:[]};
function release(id){
  const legacy=id==='legacy',applicable=['user','approve'].includes(id);
  return {release_id:'release-'+id,...(legacy?{}:{protocol:'dev-prd/v2'}),valid:true,
    artifact_sha256:state.artifact,plan_sha256:'b'.repeat(64),artifact:{image,source_tree_sha256:'RAW_SOURCE_DIGEST'},
    summary_ko:applicable?'주문 처리 오류 수정':'RAW_RELEASE_SUMMARY',risk_ko:applicable?'주문 처리 결과를 확인해야 합니다.':'RAW_RELEASE_RISK',rollback_ko:applicable?'직전 검증 이미지로 원복합니다.':'RAW_ROLLBACK',
    invalidation_reason:'RAW_INVALIDATION',
    targets:{prd:[{service:'order',environment:'prd',uid:'exact-prd-uid',resource_version:'42'}]},
    tests:{before_failed:true,after_passed:true,regression_executed:true,total:3,failed:0,report_sha256:'RAW_REPORT_DIGEST',executed_tests:[{id:'order-pool-regression',phase:'after',status:'passed'}]},
    stage_results:{dev:{deployed:true,verified:true,write_outcome:'RAW_WRITE',error:{code:'RAW_ENV_CODE',message_ko:'RAW_ENV_MESSAGE'},verification:{image,checks:[{name:'order',passed:true,status_code:200,body_sha256:'f'.repeat(64)}]}},...(legacy?{stg:{deployed:true,verified:true,error:'RAW_LEGACY_ERROR'}}:{})},
    user_test:id==='approve'?{id:'exact-user-test',passed:true,invalidated:false,occurred_at:1794528000,expires_at:1893456000,notes_ko:'RAW_HUMAN_NOTES'}:null};
}
function detail(row){return {...row,error:row.id==='failed'?{code:'after_not_passed',message_ko:'RAW_FAILURE_MESSAGE'}:{code:'RAW_ERROR_CODE',message_ko:'RAW_ERROR_MESSAGE'},handoff_classification:{class:'RAW_CLASS',label_ko:'RAW_CLASS_LABEL'},stages:[{state:'collecting',occurred_at:1794528000,details:{message:'RAW_EVENT'}}],data:{
  collection:{observed_at:1794528000,target:{image:'RAW_COLLECTION_IMAGE'},logs:[{service:'order',text:sample+' RAW_LOG_'+state.version}],evidence:[{id:'evidence-1',service:'order',environment:'dev',source:'pod_log',quality:'observed',text:'RAW_EVIDENCE'}]},
  analysis:{roles:[{role:'analyst',model:'RAW_MODEL',result:{summary_ko:'연결이 반환되지 않아 주문 처리용 연결 풀이 고갈되었습니다.',fixable:true,citations:[{ref:'evidence-1',quote:sample}],unresolved:['높은 부하에서 재발 여부를 확인해야 합니다.']}}]},
  patch:{summary_ko:'주문 완료 시 연결을 반환하도록 수정합니다.',risk_ko:'처리 중인 주문의 재시도 영향을 확인해야 합니다.',diff:'--- a/main.py\n+++ b/main.py\n-RAW_BEFORE\n+RAW_AFTER\n',diff_sha256:'RAW_DIFF_DIGEST',tests_added:['order-pool-regression']},
  test_build:{error_code:row.id==='failed'?'after_not_passed':'RAW_TEST_ERROR',tests:release(row.id).tests},release:release(row.id)}};}
const browser=await chromium.launch();
const checks=[];
try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[],requests=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>requests.push(r.url()));
  await page.clock.install({time:new Date('2026-11-14T00:00:00Z')});
  await page.route('**/api/application/v1/**',async route=>{
    const request=route.request(),url=new URL(request.url()),p=url.pathname.replace('/api/application/v1','');
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(p==='/readyz'){
      state.readyCalls++;
      if(state.failReady)return route.abort('failed');
      return json({ready:false,message_ko:'RAW_READINESS_MESSAGE',checks:{database:true,human_identity:true,machine_identity:true,profiles:true,model:true,runner:false},model_status:{state:'configured_unverified',last_failure_code:'RAW_READY_CODE'},waiting_people:{awaiting_user_test:2,awaiting_prd_approval:1}},503);
    }
    if(p==='/catalog')return state.failCatalog==='text'?route.fulfill({status:502,contentType:'text/plain',body:'RAW_NON_JSON'}):state.failCatalog?json({code:'RAW_API_CODE',message_ko:'RAW_API_MESSAGE'},500):json({profiles_configured:true,environments:['dev','stg','prd'],services:[{service:'order',system:'samsung-trade'}]});
    if(p==='/incidents'){
      state.listCalls++;
      return json(state.badList?{}:{incidents:rows,total:rows.length,as_of:1794528000});
    }
    const m=p.match(/^\/incidents\/([^/]+)(?:\/(release|user-test|approve-prd))?$/);
    if(m){
      const row=rows.find(row=>row.id===m[1]);
      if(request.method()==='POST'){
        state.posts.push({path:p,headers:request.headers(),body:request.postDataJSON()});
        row.state='completed';return json({ok:true});
      }
      state.detailCalls++;
      return json(m[2]==='release'?{incident_id:row.id,release:release(row.id),test_build:detail(row).data.test_build}:detail(row));
    }
    return json({},404);
  });
  const noRaw=async()=>{
    assert.doesNotMatch(await page.textContent('body'),/RAW_/,'raw text is not mounted in the DOM');
    assert.doesNotMatch(await page.locator('body').ariaSnapshot(),/RAW_/,'raw text is not in the accessibility tree');
  };
  const select=async id=>{
    await page.click(`[data-id="${id}"]`);
    await expect(page.locator('#detail-sections')).toBeVisible();
  };
  const tick=async()=>{
    const before=state.detailCalls;
    await page.clock.runFor(16000);
    await expect.poll(()=>state.detailCalls).toBeGreaterThan(before);
    await page.waitForTimeout(100);
  };
  await page.goto(base);
  await expect(page.locator('#readiness-body')).toContainText('실행기 최근 연결');
  assert.equal(await page.isVisible('#workspace'),false);
  assert.equal(await page.isVisible('#auth-form'),true);
  assert.equal(await page.isVisible('#readiness-checks'),true);
  await noRaw();
  await page.click('#raw-readiness > summary');
  await expect(page.locator('#readiness-raw')).toContainText('RAW_READINESS_MESSAGE');
  await page.click('#raw-readiness > summary');
  await expect(page.locator('#readiness-raw')).toBeEmpty();
  await page.fill('#auth-bearer','synthetic-read');
  await page.click('#auth-form button[type=submit]');
  await expect(page.locator('#incident-list button')).toHaveCount(rows.length);
  assert.equal(await page.isVisible('#auth-form'),false);
  assert.equal(await page.isVisible('#create-form'),false);
  await select('automatic');await noRaw();
  await expect(page.locator('#incident-summary')).toContainText('연결이 반환되지 않아');
  await expect(page.locator('#analysis-roles')).toContainText('연결 풀이 고갈');
  await expect(page.locator('#patch-overview')).toContainText('연결을 반환');
  await expect(page.locator('#patch-summary')).toContainText('재시도 영향');
  for (const id of ['stage-timeline','evidence-table','analysis-roles','patch-files','patch-tests','test-details','release-explain']) {
    await expect(page.locator('#'+id)).toBeVisible();
    assert.equal(await page.locator('#'+id).evaluate(n=>!!n.closest('details')),false);
  }
  assert.equal(await page.isVisible('#user-test-form'),false);
  assert.equal(await page.isVisible('#approve-form'),false);
  checks.push('access_readiness_summary_incident_first_and_on_demand_create');

  for(const [id,target,marker] of [['raw-collection','collection-logs','RAW_LOG_0'],['raw-analysis','analysis-raw','RAW_MODEL'],['raw-patch','patch-diff','RAW_AFTER'],['raw-tests','tests-raw','RAW_REPORT_DIGEST'],['raw-stages','stages-raw','RAW_EVENT'],['raw-artifacts','artifacts-raw','RAW_SOURCE_DIGEST'],['raw-environments','env-raw','RAW_ENV_MESSAGE'],['raw-incident','incident-raw','RAW_REASON']]){
    const summary=page.locator('#'+id+' > summary');
    await summary.focus();await summary.press('Enter');
    await expect(page.locator('#'+target)).toContainText(marker);
    assert.ok((await page.locator('body').ariaSnapshot()).includes(marker));
    assert.equal(await page.locator('#incident-detail img').count(),0,'server markup remains plain text');
    await summary.press('Space');
    await expect(page.locator('#'+target)).toBeEmpty();
    await noRaw();
  }
  checks.push('eight_keyboard_disclosures_lazy_mount_unmount_and_safe_text');

  await page.locator('#raw-collection > summary').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#collection-logs')).toContainText('RAW_LOG_0');
  await page.evaluate(()=>window.__rawNode=document.querySelector('#collection-logs').firstChild);
  await tick();
  assert.equal(await page.evaluate(()=>window.__rawNode===document.querySelector('#collection-logs').firstChild),true);
  assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('#raw-collection > summary')),true);
  state.version++;
  await tick();
  await expect(page.locator('#collection-logs')).toContainText('RAW_LOG_1');
  assert.equal(await page.locator('#raw-collection').getAttribute('open'),'');
  checks.push('poll_preserves_open_state_focus_unchanged_dom_and_updates_open_content');

  await select('failed');await noRaw();
  await expect(page.locator('#next-action-body')).toContainText('수정 후 시험이 통과하지 못했습니다');
  assert.equal(await page.locator('details[data-raw][open]').count(),0);
  await expect(page.getByRole('heading',{name:'처리 결과'})).toBeVisible();
  await expect(page.locator('#response-records')).toBeVisible();
  await expect(page.locator('#test-results')).toBeVisible();
  await expect(page.locator('#test-results')).toContainText('수정 후 시험이 통과하지 못했습니다');
  await page.locator('#raw-tests > summary').click();
  await expect(page.locator('#test-details')).not.toBeEmpty();
  await page.locator('#raw-tests > summary').click();
  await expect(page.locator('#tests-raw')).toBeEmpty();
  await expect(page.locator('#test-details')).not.toBeEmpty();
  await select('done');
  await expect(page.locator('#incident-summary')).toContainText('종료 시각');
  await expect(page.locator('#next-action-body')).toContainText('prd 업무 확인 결과는 미기록');
  await expect(page.locator('#response-records')).toBeVisible();
  await select('legacy');await noRaw();
  await expect(page.locator('#next-action-body')).toContainText('읽기 전용');
  assert.equal(await page.isVisible('#user-test-form'),false);
  assert.equal(await page.isDisabled('#user-test-submit'),true);
  assert.equal(await page.isDisabled('#approve-submit'),true);
  await select('unknown');await noRaw();
  await expect(page.locator('#next-action-body')).toContainText('현재 상태를 확인하지 못했습니다');
  assert.doesNotMatch(await page.textContent('#next-action-body'),/자동으로 진행 중/);
  checks.push('failed_legacy_unknown_paths_use_structured_labels_not_raw_fallbacks');

  await select('user');
  assert.equal(await page.isVisible('#approval-target'),false,'user test form precedes technical approval detail');
  if(OUT)await page.locator('#human-section').screenshot({path:path.join(OUT,'application-user-test.png')});
  await page.fill('#test-notes','작성 중인 시험 기록');
  await page.fill('#tester-bearer','synthetic-test-draft');
  await page.selectOption('#test-passed','true');
  await page.click('#create-panel > summary');
  await page.selectOption('#create-environment','dev');
  await page.selectOption('#create-service','order');
  await page.fill('#create-reason','새 사건 작성 중');
  await page.focus('#test-notes');
  await tick();
  assert.equal(await page.inputValue('#test-notes'),'작성 중인 시험 기록');
  assert.equal(await page.inputValue('#tester-bearer'),'synthetic-test-draft');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'test-notes');
  await page.click('#refresh');
  await expect(page.locator('#create-service')).toHaveValue('order');
  await expect(page.locator('#create-environment')).toHaveValue('dev');
  await expect(page.locator('#create-reason')).toHaveValue('새 사건 작성 중');
  await page.click('#user-test-submit');
  await expect.poll(()=>state.posts.length).toBe(1);
  await expect(page.locator('#detail-title')).toContainText('완료');
  assert.equal(state.posts[0].headers.authorization,'Bearer synthetic-test-draft');
  assert.deepEqual(state.posts[0].body,{release_id:'release-user',artifact_sha256:state.artifact,plan_sha256:'b'.repeat(64),image,passed:true,notes_ko:'작성 중인 시험 기록'});
  checks.push('applicable_user_test_drafts_focus_create_selections_and_exact_submission');

  await select('approve');
  assert.equal(await page.inputValue('#tester-bearer'),'');
  assert.equal(await page.inputValue('#test-notes'),'');
  assert.equal(await page.inputValue('#test-passed'),'');
  await expect(page.locator('#approval-target')).toBeVisible();
  await expect(page.locator('#approve-digest')).toBeVisible();
  await expect(page.locator('#approve-confirm')).not.toBeChecked();
  await expect(page.locator('#approve-submit')).toBeDisabled();
  await expect(page.locator('#approve-digest')).toContainText(image);
  await expect(page.locator('#approve-digest')).toContainText(state.artifact);
  await expect(page.locator('#approve-digest')).toContainText('exact-prd-uid');
  await expect(page.locator('#approval-verification')).toContainText('f'.repeat(64));
  await expect(page.locator('#approval-verification')).toContainText('직전 검증 이미지로 원복합니다.');
  await expect(page.locator('#approval-verification')).not.toContainText('RAW_HUMAN_NOTES');
  assert.equal(await page.isVisible('#user-test-form'),false);
  if(OUT)await page.locator('#human-section').screenshot({path:path.join(OUT,'application-approval.png')});
  await page.fill('#approver-bearer','synthetic-approver');
  await page.check('#approve-confirm');
  await page.focus('#approver-bearer');
  await tick();
  assert.equal(await page.isChecked('#approve-confirm'),true);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'approver-bearer');
  state.artifact='c'.repeat(64);
  await tick();
  assert.equal(await page.isChecked('#approve-confirm'),false);
  assert.equal(await page.inputValue('#approver-bearer'),'synthetic-approver');
  assert.equal(await page.isDisabled('#approve-submit'),true);
  await page.check('#approve-confirm');
  await page.click('#approve-submit');
  await expect.poll(()=>state.posts.length).toBe(2);
  await expect(page.locator('#detail-title')).toContainText('완료');
  assert.equal(state.posts[1].headers.authorization,'Bearer synthetic-approver');
  assert.deepEqual(state.posts[1].body,{release_id:'release-approve',artifact_sha256:state.artifact,plan_sha256:'b'.repeat(64),image,user_test_id:'exact-user-test'});
  checks.push('approval_exact_artifact_verification_safety_confirmation_and_fingerprint_bound_payload');

  // 다른 사건으로 이동할 때 사용자 시험 초안·권한과 열린 원문은 전부 제거된다.
  rows.find(r=>r.id==='user').state='awaiting_user_test';
  await select('user');
  await page.fill('#test-notes','다른 사건으로 가져가면 안 되는 초안');
  await page.fill('#tester-bearer','synthetic-not-retained');
  await page.click('#raw-incident > summary');
  await expect(page.locator('#incident-raw')).toContainText('RAW_REASON');
  await select('automatic');
  assert.equal(await page.inputValue('#test-notes'),'');
  assert.equal(await page.inputValue('#tester-bearer'),'');
  assert.equal(await page.inputValue('#approver-bearer'),'');
  assert.equal(await page.locator('details[data-raw][open]').count(),0);
  await noRaw();
  checks.push('different_incident_resets_raw_dom_and_sensitive_form_drafts');

  state.failCatalog=true;state.failReady=true;
  await page.click('#refresh');
  await expect(page.locator('#error-banner')).toBeVisible();
  await expect(page.locator('#readiness-body')).toContainText('대기 건수도 미관측');
  await noRaw();
  await page.click('#raw-error > summary');
  await expect(page.locator('#error-raw')).toContainText('RAW_API_MESSAGE');
  await page.click('#raw-error > summary');
  state.failCatalog='text';
  await page.click('#refresh');
  await expect(page.locator('#error-message')).toContainText('서버 응답 형식');
  await noRaw();
  await page.click('#raw-error > summary');
  await expect(page.locator('#error-raw')).toContainText('RAW_NON_JSON');
  await page.click('#raw-error > summary');
  state.failCatalog=false;state.failReady=false;state.badList=true;
  await page.click('#refresh');
  await expect(page.locator('#error-message')).toContainText('건수는 미관측');
  assert.doesNotMatch(await page.textContent('#list-count'),/전체 0건/);
  state.badList=false;await page.click('#refresh');
  await expect(page.locator('#error-banner')).toBeHidden();
  await noRaw();
  checks.push('request_readiness_and_malformed_list_failures_never_imply_zero_or_expose_raw');

  for(const width of [1440,768,375]){
    await page.setViewportSize({width,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth+1),`no overflow at ${width}`);
    if(OUT){await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(OUT,`application-ux-${width}.png`)});}
  }
  await page.click('#access-panel > summary');
  await page.click('#forget-auth');
  assert.equal(await page.isVisible('#workspace'),false);
  assert.equal(await page.inputValue('#create-reason'),'');
  assert.equal(await page.textContent('#incident-raw'),'');
  await noRaw();
  assert.deepEqual(errors,[]);
  assert.equal(await page.evaluate(()=>window.__raw_xss),undefined);
  assert.ok(requests.every(url=>new URL(url).origin===base),'no external requests');
  assert.equal(state.posts.length,2,'only the explicitly tested synthetic attestations were sent');
  checks.push('responsive_1440_768_375_and_forget_clears_data_no_page_errors_or_external_requests');
} finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
console.log(JSON.stringify({ok:true,checks_count:checks.length,checks}));
