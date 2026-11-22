import { test, expect } from '@playwright/test';
import {mockLocalOperator} from './fixtures/local-operator.mjs';
import {mockUnifiedRoom} from './fixtures/unified-room.mjs';

test.beforeEach(async ({page})=>mockLocalOperator(page));
async function chooseStage(page, id) {
  const back=page.getByRole('button',{name:/흐름으로 돌아가기/});
  if(await back.isVisible()) await back.click();
  const full=page.getByRole('button',{name:'전체 흐름',exact:true});
  if(await full.isVisible()) await full.click();
  const node=page.locator('.react-flow__node[data-id="stage:'+id+'"] button').first();
  await node.focus();
  await node.press('Enter');
  await expect(page.locator('.semantic-stage')).toHaveAttribute('data-selected-step','stage:'+id);
}

async function selectPhase(page,phase) {
  await page.getByRole('navigation',{name:'사건 처리 단계'}).getByRole('button',{name:new RegExp(phase)}).click();
}

test('기존 애플리케이션 주소는 같은 관제실로 열리고 역할과 원문을 개별로 조회한다',async({page})=>{
  const id='original-a',base='/api/application/v1/incidents/'+id;
  const source={id,state:'completed',service:'order',environment:'stg',system:'trade',reason:'주문 오류를 감지했습니다.',created_at:1700000000};
  const rows=['detected','collecting','analyzing','role_started','role_completed','completed'].map((event,i)=>({id:i+1,at:source.created_at+i,event,data:event.startsWith('role_')?{role:'analyst',call_id:'call-7',model:'fixture-model',summary_ko:'중복 주문 원인을 확인했습니다.'}:{}}));
  const requested=[];
  await page.route('**/api/**',r=>{
    const u=new URL(r.request().url()),p=u.pathname;
    requested.push(p+u.search);
    const send=json=>r.fulfill({json});
    if(p===base+'/summary')return send(source);
    if(p===base+'/activity'){const events=rows.filter(e=>e.id>Number(u.searchParams.get('after')||0));return send({id,state:source.state,events,stages:rows.filter(e=>['collecting','analyzing','completed'].includes(e.event)).map(e=>({state:e.event,occurred_at:e.at})),has_more:false,cursor:events.at(-1)?.id??6});}
    if(p===base+'/accountability')return send({status:'unreviewed',revision:0,evidence_refs:[],detection_ko:'주문 오류를 감지했습니다.',limits_ko:'당시 복구 근거가 없습니다.'});
    if(p===base){
      const row={id:'log1',source:'pod_log',service:'order',observed_at:1700000001};
      return send({...source,data:{collection:{evidence:[u.searchParams.get('view')==='stage'?{...row,has_original:true}:{...row,text:'QA_FIXTURE_ORIGINAL'}]},
        display_readings:{'evidence/0':{ko:'같은 주문이 두 번 처리됐습니다.',state:'ready'}}}});
    }
    return send({items:[],total:0,accounts:[]});
  });
  await page.goto('/application?incident='+id);
  await expect(page).toHaveURL(/\/incidents\/application\/original-a$/);
  await chooseStage(page,'investigation');
  const panel=page.getByRole('region',{name:'선택 단계 입력과 결과'});
  await expect(panel).toContainText('중복 주문 원인을 확인했습니다.');
  await chooseStage(page,'evidence');
  await expect(panel).toContainText('같은 주문이 두 번 처리됐습니다.');
  expect(requested).not.toContain(base);
  // The time-ordered observations sit in a counted data section under the brief.
  await panel.getByRole('button',{name:/시간순 관측 기록/}).click();
  await page.getByText('관측 원문 상세보기',{exact:true}).click();
  await expect(page.locator('pre.raw-text')).toContainText('QA_FIXTURE_ORIGINAL');
  await page.getByText('관측 원문 상세보기',{exact:true}).click();
  await expect(page.locator('pre.raw-text')).toHaveCount(0);
  expect(requested.some(p=>p.includes('human-sessions'))).toBe(false);
});

test('이전 dev 수동 시험 대기는 당시 자료로 읽고 현재 버전 재검증 가능 여부를 구분한다', async ({page})=>{
  const id='legacy-test', path='/api/application/v1/incidents/'+id;
  const release={protocol:'dev-prd/v2',release_id:'first',valid:true,stage_results:{dev:{verified:true}}};
  const summary={id,state:'awaiting_user_test',protocol:release.protocol,service:'order',environment:'dev',reason:'주문 확인 대기'};
  const writes=[];
  await page.route('**/api/**',r=>{
    const u=new URL(r.request().url()), send=json=>r.fulfill({json});
    if(r.request().method()!=='GET'){writes.push(u.pathname);return r.fulfill({status:409,json:{detail:'unexpected_write'}});}
    if(u.pathname===path+'/summary')return send(summary);
    if(u.pathname===path)return send({...summary,data:{release}});
    if(u.pathname===path+'/current-version-check')return send({eligible:false,release_id:'first',environment:'dev',service:'order',reason_ko:'원래 요청을 확인할 수 없습니다.',latest:null});
    if(u.pathname.endsWith('/stream'))return r.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
    return send({items:[],incidents:[],total:0,accounts:[]});
  });
  await page.goto('/application?incident='+id);
  await selectPhase(page,'dev QA');
  const panel=page.locator('.semantic-stage');
  await expect(panel).toContainText('자동 사용자 여정 QA 미관측');
  await expect(panel).toContainText('원래 요청을 확인할 수 없습니다.');
  await expect(page.getByRole('form',{name:'dev 사용자 시험 기록'})).toHaveCount(0);
  expect(writes).toEqual([]);
});
test('QA 보완 재시도는 같은 요청 번호를 사용하고 성공 직후 권한이 끝나도 접수 결과가 남는다', async ({page})=>{
  const id='qa-case', base='/api/application/v1/incidents/'+id;
  const summary={id,state:'qa_failed',protocol:'dev-qa-prd/v3',service:'order',environment:'dev',system:'trade',reason:'주문 QA 실패'};
  const writes=[];
  let authorized=true;
  const session={state:'active',subject:'qa-operator',scopes:['incident:read','incident:create'],expires_at:Date.now()/1000+600,csrf_token:'fixture-only'};
  await page.route('**/api/**',r=>{
    const u=new URL(r.request().url()), send=(json,status=200)=>r.fulfill({json,status});
    if(r.request().method()==='POST' && u.pathname===base+'/supplements'){
      writes.push(r.request().postDataJSON());
      if(writes.length===1)return send({},503);
      authorized=false;
      return send({incident_id:id,state:'qa_queued',supplement:{request_id:writes[1].request_id,status:'applied'}});
    }
    if(u.pathname===base+'/summary')return send(summary);
    if(u.pathname===base)return send({...summary,data:{round:1,release:{valid:true,protocol:summary.protocol},supplements:[]}});
    if(u.pathname.endsWith('human-sessions/current'))return send(authorized?session:{state:'none'});
    if(u.pathname.endsWith('/stream'))return r.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
    return send({items:[],incidents:[],total:0,accounts:[]});
  });
  await page.goto('/application?incident='+id);
  await selectPhase(page,'dev QA');
  await page.getByText('사건 보완 열기',{exact:true}).click();
  const form=page.getByRole('form',{name:'보완 요청'});
  await form.getByRole('combobox').selectOption('rerun_qa');
  await form.getByRole('textbox').fill('재고를 대조해 주세요.');
  await form.getByRole('button',{name:'QA 재실행 요청',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('요청 결과를 확인하지 못했습니다.');
  await form.getByRole('button',{name:'QA 재실행 요청',exact:true}).click();
  await expect(page.getByText('보완 결과를 기록했습니다.',{exact:true})).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1]).toEqual(writes[0]);
  expect(writes[0]).toEqual({request_id:expect.stringMatching(/^[a-f0-9-]{36}$/),kind:'rerun_qa',note_ko:'재고를 대조해 주세요.'});
});

for(const [work,state,label] of [['qa_fix','qa_failed','QA 보완'],['recovery','awaiting_recovery','복구 관측']]){
  test(label+' 목록에서 연결 사건은 합치고 독립 Application 원본은 그대로 연다', async ({page})=>{
    const root={id:12,status:'analyzing',namespace:'dev',system:'trade',workload:'order',title:'주문 조회',code_action:{state}};
    const child={id:'app-a',state,service:'order',environment:'dev',system:'trade',root_incident_id:'12',reason:'주문 조회 검사'};
    const original={...child,id:'app-b',service:'payment',root_incident_id:null,reason:'결제 상태 확인'};
    const reads=[];
    await page.route('**/api/**',r=>{
      const u=new URL(r.request().url()), send=json=>r.fulfill({json});
      if(u.searchParams.get('work')===work){
        reads.push(u.pathname);
        const list=u.pathname==='/api/incidents'?[root]:[child,original];
        return send({[u.pathname==='/api/incidents'?'items':'incidents']:list,total:list.length});
      }
      if(u.pathname.endsWith('/stream'))return r.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
      return send({items:[],incidents:[],total:0,accounts:[]});
    });
    await page.goto('/incidents?work='+work);
    await expect(page.getByRole('heading',{name:label,exact:true})).toBeVisible();
    await expect(page.locator('.work-row')).toHaveCount(2);
    await expect(page.locator('.work-row').filter({hasText:'주문 조회 검사'})).toHaveAttribute('href','/incidents/12');
    await expect(page.locator('.work-row').filter({hasText:'결제 상태 확인'})).toHaveAttribute('href','/incidents/application/app-b');
    expect(reads).toContain('/api/incidents');expect(reads).toContain('/api/unified/summaries');
  });
}

test('Kubernetes 사건에서 연결된 소스 조사와 실제 읽은 파일이 같은 관제실에 열린다',async({page})=>{
  const fixture=await mockUnifiedRoom(page,{state:'analyzing'});
  const id=fixture.application.id,base=1000;
  await page.route('**/api/application/v1/incidents/'+id+'?view=stage',r=>r.fulfill({json:fixture.application}));
  const events=[
    {event:'detected',data:{}},{event:'analyzing',data:{}},
    {event:'job_queued',data:{job_id:'read-1',kind:'investigate'}},
    {event:'runner_progress',data:{job_id:'read-1',kind:'investigate',attempt:1,seq:1,phase:'pin_source'}},
    {event:'runner_progress',data:{job_id:'read-1',kind:'investigate',attempt:1,seq:2,phase:'read_file',file:'orders/handler.py'}},
    {event:'job_reported',data:{job_id:'read-1',kind:'investigate'}},
  ].map((e,i)=>({...e,id:i+1,at:base+i}));
  await page.route('**/api/application/v1/incidents/'+id+'/activity?*',r=>{
    const after=Number(new URL(r.request().url()).searchParams.get('after'));
    return r.fulfill({json:{id,state:'analyzing',stages:[{state:'detected',occurred_at:base},{state:'analyzing',occurred_at:base+1}],
      events:events.filter(e=>e.id>after),has_more:false,cursor:events.length}});
  });
  await page.goto('/incidents/971');
  const panel=page.getByRole('region',{name:'선택 단계 입력과 결과'});
  await chooseStage(page,'investigation');
  const history=panel.getByRole('combobox',{name:'기록 시점'});
  const fileRecord=await history.getByRole('option',{name:/소스 위치 확인 · orders/}).getAttribute('value');
  await history.selectOption(fileRecord);
  await expect(panel.locator('.app-trace-now')).toContainText('orders/handler.py');
  await expect(panel).toContainText('시도 1');
  await expect(page).toHaveURL(/\/incidents\/971$/);
});

test('실행기 인수 및 연락 끊김과 관제 화면 재연결을 구분한다', async ({page})=>{
  const fixture=await mockUnifiedRoom(page,{state:'analyzing'});
  const id=fixture.application.id, at=1700000000;
  await page.route('**/api/application/v1/incidents/'+id+'?view=stage',r=>r.fulfill({json:fixture.application}));
  const events=[
    {event:'detected',data:{}},{event:'analyzing',data:{}},
    {event:'job_queued',data:{job_id:'source-task',kind:'source'}},
    {event:'job_claimed',data:{job_id:'source-task',kind:'source',attempt:1}},
  ].map((entry,i)=>({...entry,id:i+1,at:at+i}));
  let contact='contact_lost', network=true;
  await page.route('**/api/application/v1/incidents/'+id+'/activity?*',async route=>{
    if(!network)return route.fulfill({status:503,json:{}});
    const after=Number(new URL(route.request().url()).searchParams.get('after')||0);
    const active_jobs=[{job_id:'source-task',kind:'source',attempt:contact==='reclaimed'?2:1,state:contact,
      queued_at:at,claimed_at:at+3,last_heartbeat_at:at+4,last_contact_at:at+4,expires_at:at+34}];
    return route.fulfill({json:{id,state:'analyzing',stages:[{state:'detected',occurred_at:at},
      {state:'analyzing',occurred_at:at+1}],events:events.filter(e=>e.id>after),cursor:events.length,has_more:false,active_jobs}});
  });
  await page.goto('/incidents/971');
  await chooseStage(page,'investigation');
  const panel=page.getByRole('region',{name:'선택 단계 입력과 결과'});
  await expect(panel).toContainText('실행기 연락 끊김 · 재인수 대기');
  await expect(panel).toContainText('하트비트');
  network=false;
  await expect(panel).toContainText('관제 화면 재연결 중');
  await expect(panel).not.toContainText('실행기 연락 끊김 · 재인수 대기');
  network=true; contact='reclaimed';
  await expect(panel).toContainText('실행기가 일감을 다시 인수했습니다');
  await expect(panel).toContainText('시도 2');
});
