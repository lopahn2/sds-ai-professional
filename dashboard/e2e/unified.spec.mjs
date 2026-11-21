import {test, expect} from '@playwright/test';
import {mockUnifiedRoom, fingerprint} from './fixtures/unified-room.mjs';
import {tab} from './p15-helpers.mjs';

async function chooseStage(page,id) {
  const graph=page.getByRole('region',{name:'AI 분석 흐름'});
  const back=page.getByRole('button',{name:/흐름으로 돌아가기/});
  if(await back.isVisible())await back.click();
  const all=graph.getByRole('button',{name:'전체 흐름',exact:true});
  if(await all.isVisible())await all.click();
  const button=graph.locator('.react-flow__node[data-id="stage:'+id+'"] button').first();
  await button.focus();
  await button.press('Enter');
  const panel=page.getByRole('region',{name:'선택 단계 입력과 결과'});
  await expect(panel).toHaveAttribute('data-selected-step','stage:'+id);
  return panel;
}
async function openApproval(page) {
  await page.goto('/incidents/971');
  await page.getByRole('navigation',{name:'사건 처리 단계'}).getByRole('button',{name:/운영 승인/}).click();
  const pane=page.getByRole('region',{name:'단계 관제',exact:true});
  await expect(pane.locator('.semantic-stage')).toHaveAttribute('data-selected-step','stage:approval');
  return pane;
}
test('루트 사건의 원인·근거는 선택한 단계에 표시하며 원문은 요청할 때 조회한다',async({page})=>{
  const fixture=await mockUnifiedRoom(page);
  await page.goto('/incidents/971');
  await expect(page.getByText('Kubernetes 단계 · 이전 처리 기록')).toBeVisible();
  await chooseStage(page,'investigation');
  const panel=page.getByRole('region',{name:'선택 단계 입력과 결과'});
  await expect(panel).toContainText('연결 반환 누락을 확인했습니다.');
  await expect(panel).toContainText('연결 반환 실패 원인을 조사하세요.');
  await expect(panel).toContainText('gpt-6.2-sol');
  await chooseStage(page,'evidence');
  await expect(panel).toContainText('연결을 반환하지 않아 주문 요청이 지연됐습니다.');
  await expect(panel.getByText(/RAW_ONLY_SENTINEL/)).toHaveCount(0);
  const raw=()=>fixture.requests.filter(r=>r.path.endsWith('/incidents/native-a')&&!r.search);
  expect(raw()).toHaveLength(0);
  const observation=panel.getByRole('list',{name:'시간순 관측 기록'}).getByRole('listitem')
    .filter({hasText:'연결을 반환하지 않아 주문 요청이 지연됐습니다.'});
  await observation.getByText('관측 원문 상세보기',{exact:true}).click();
  await expect(observation.locator('pre.raw-text')).toContainText('RAW_ONLY_SENTINEL');
  expect(raw().length).toBeGreaterThan(0);
  expect(fixture.requests.some(r=>r.path.includes('/human-sessions'))).toBe(false);
  expect(await page.evaluate(()=>[localStorage.length,sessionStorage.length])).toEqual([0,0]);
  expect(fixture.requests.every(r=>!r.headers.authorization)).toBe(true);
  await tab(page,'대화');
  await expect(page.locator('.handoff-card,.composer,.close-card')).toHaveCount(0);
  await expect(page.getByRole('form',{name:'dev 사용자 시험 기록'})).toHaveCount(0);
  expect(fixture.writes).toHaveLength(0);
});

test('자동 dev QA 영수증으로 사람 PR 병합 확인 후 같은 버전만 prd 승인한다',async({page})=>{
  const fixture=await mockUnifiedRoom(page,{state:'awaiting_prd_approval',protocol:'dev-qa-prd/v3'});
  const pane=await openApproval(page);
  const merge=pane.getByRole('form',{name:'사람 병합 확인'});
  const approval=pane.getByRole('form',{name:'prd 배포 승인'});
  await expect(merge.getByRole('checkbox')).toBeEnabled();
  await expect(approval).toHaveCount(0);
  await expect(page.getByRole('form',{name:'dev 사용자 시험 기록'})).toHaveCount(0);
  await merge.getByLabel('등록 저장소 PR 번호').fill('19');
  await expect(merge.getByRole('button',{name:'사람 병합 확인'})).toBeDisabled();
  await merge.getByRole('checkbox').check();
  await merge.getByRole('button',{name:'사람 병합 확인'}).click();
  await expect(approval.getByRole('checkbox')).toBeEnabled();
  await expect(merge).toHaveCount(0);
  await approval.getByRole('checkbox').check();
  await approval.getByRole('button',{name:'prd 배포 승인'}).click();
  await expect.poll(()=>fixture.writes.length).toBe(2);
  expect(fixture.writes.map(r=>r.subject)).toEqual(['local-operator','local-operator']);
  expect(fixture.writes.map(r=>r.body)).toEqual([
    {...fingerprint,pr_number:19},{...fingerprint,qa_receipt_id:fixture.release.qa.receipt_id,risk_sha256:fixture.release.risk_sha256},
  ]);
  expect(fixture.writes.every(r=>r.csrf)).toBe(true);
  expect(fixture.requests.every(r=>!r.headers.authorization)).toBe(true);
});

test('승인 확인 후 계획 지문이 변경되면 기존 확인으로 전송할 수 없다',async({page})=>{
  const fixture=await mockUnifiedRoom(page,{state:'awaiting_prd_approval',protocol:'dev-qa-prd/v3'});
  fixture.observeMerge();
  const pane=await openApproval(page);
  const form=pane.getByRole('form',{name:'prd 배포 승인'});
  await form.getByRole('checkbox').check();
  fixture.release.plan_sha256='d'.repeat(64);
  const reads=fixture.requests.filter(r=>r.path.endsWith('/code-action/detail')).length;
  await form.getByRole('button',{name:'prd 배포 승인'}).click();
  await expect.poll(()=>fixture.requests.filter(r=>r.path.endsWith('/code-action/detail')).length).toBeGreaterThan(reads);
  await expect(form).toHaveCount(0);
  expect(fixture.writes).toHaveLength(0);
});

test('상세 조회가 거절되면 저장 원문과 승인 동작을 노출하지 않는다',async({page})=>{
  const fixture=await mockUnifiedRoom(page,{state:'awaiting_prd_approval'});
  fixture.denyDetails=true;
  const pane=await openApproval(page);
  await expect(pane.getByRole('alert')).toContainText('로컬 운영자 연결을 확인할 수 없습니다');
  await expect(pane.getByRole('form')).toHaveCount(0);
  await expect(page.getByText(/PRIVATE_ERROR_SENTINEL|RAW_ONLY_SENTINEL/)).toHaveCount(0);
  expect(fixture.writes).toHaveLength(0);
});

test('기존 사건은 현재 dev 버전 검증을 명시 요청하고 전송 실패 시 같은 요청 번호로 재시도한다',async({page})=>{
  const fixture=await mockUnifiedRoom(page), writes=[];
  const view={eligible:true, reason_ko:null, release_id:fingerprint.release_id, environment:'dev',service:'inventory',latest:null};
  await page.route('**/api/application/v1/incidents/native-a/current-version-check',async route=>{
    const req=route.request();
    if(req.method()==='GET')return route.fulfill({json:view});
    const body=req.postDataJSON(), headers=await req.allHeaders();
    expect(req.method()).toBe('POST');
    expect(headers['x-liveprobe-operator-csrf']).toBeTruthy();
    expect(headers.origin).toBe(new URL(req.url()).origin);
    writes.push(body);
    if(writes.length===1)return route.fulfill({status:503,json:{}});
    Object.assign(view,{eligible:false, latest:{request_id:body.request_id, job_id:'read-only-work',status:'queued',
      image:null,operation:'GET /api/stock/a',observed_at:null,requested_at:1030,promotion_unnecessary:false}});
    return route.fulfill({json:view});
  });
  await page.goto('/incidents/971');
  const panel=await chooseStage(page,'dev_validation');
  await expect(panel).toContainText('자동 사용자 여정 QA 미관측');
  await panel.getByRole('button',{name:'현재 버전 재검증',exact:true}).click();
  expect(writes).toEqual([]);
  const submit=panel.getByRole('button',{name:'이 환경에서 재검증 요청'});
  await submit.click();
  await expect(panel.getByRole('alert')).toContainText('요청 결과를 확인하지 못했습니다.');
  await submit.click();
  await expect(panel).toContainText('실행기 연결 대기');
  expect(writes).toHaveLength(2);
  expect(writes[0]).toEqual({request_id:expect.stringMatching(/^[a-f0-9-]{36}$/),release_id:fingerprint.release_id});
  expect(writes[1]).toEqual(writes[0]);
  expect(fixture.writes).toHaveLength(0);
});

test('원본 이력 딥링크를 유지하고 응답이 유실된 신규 접수는 같은 요청 번호로 복구한다',async({page})=>{
  const fixture=await mockUnifiedRoom(page);
  await page.goto('/application');
  const old=page.getByRole('link',{name:'장애 legacy-native-17 inventory 관제실 열기'});
  await expect(old).toHaveAttribute('href','/incidents/application/legacy-native-17');
  await expect(old).toContainText('stg');
  expect(fixture.requests.some(r=>r.path==='/api/unified/history')).toBe(false);
  await page.getByText('새 애플리케이션 장애 접수',{exact:true}).click();
  const form=page.getByRole('region',{name:'새 애플리케이션 사건'});
  await form.getByRole('combobox',{name:'서비스',exact:true}).selectOption('inventory');
  await form.getByLabel('문제 상황').fill('주문 처리에 실패합니다.');
  await expect(form.getByRole('textbox',{name:'문제 상황'})).toBeEnabled();
  fixture.loseFirstCreate=true;
  const submit=form.getByRole('button',{name:'사건 접수·관제실 열기'});
  await form.getByRole('checkbox').check();await submit.click();
  await expect(form.getByRole('alert')).toBeVisible();
  await form.getByRole('checkbox').check();await submit.click();
  await expect(page).toHaveURL(/\/incidents\/971$/);
  await expect(page.getByRole('navigation',{name:'사건 처리 단계'})).toBeVisible();
  expect(fixture.writes).toHaveLength(2);
  expect(fixture.writes[0].body).toEqual(fixture.writes[1].body);
  expect(fixture.created.size).toBe(1);
  expect(fixture.writes[0].body.environment).toBe('dev');
});

test('통합 미구성 사건에서는 원래 Kubernetes 인계와 운영자 대화로 이동한다',async({page})=>{
  const fixture=await mockUnifiedRoom(page,{linked:false});
  await page.goto('/incidents/971');
  await expect(page.getByRole('button',{name:'코드 조치·검증 열기'})).toHaveCount(0);
  await tab(page,'대화');
  const handoff=page.getByRole('article',{name:'운영자 인계',exact:true});
  await expect(handoff.getByRole('button',{name:'인계 인수'})).toBeVisible();
  await expect(page.getByRole('article',{name:'코드 조치',exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'후속 작업 요청',exact:true}).click();
  await expect(page.getByRole('textbox',{name:'후속 작업 요청 내용'})).toBeVisible();
  expect(fixture.writes).toHaveLength(0);
});

test('자식 작업이 생기기 전에 거절된 애플리케이션 접수는 종료 사유로 열린다',async({page})=>{
  const fixture=await mockUnifiedRoom(page,{linked:false});
  Object.assign(fixture.summary,{enabled:true,application_id:'reserved-without-child',state:'intake_rejected',
    state_ko:'접수 종료 · 등록되지 않은 재현 요청',next_ko:'등록된 서비스와 증상으로 접수할 수 있습니다.'});
  const stamp=new Date().toISOString();
  await page.route('**/api/incidents/971',r=>r.fulfill({json:{incident:{id:971,workload:'inventory',namespace:'dev',
    status:'failed',family_hint:'application',created_at:stamp,updated_at:stamp,closed_at:stamp},actions:[]}}));
  await page.goto('/incidents/971');
  await expect(page.getByRole('heading',{name:'inventory · 접수 종료 · 등록되지 않은 재현 요청'})).toBeVisible();
  await expect(page.getByRole('navigation',{name:'사건 처리 단계'})).toHaveCount(0);
  expect(fixture.requests.some(r=>r.path.includes('reserved-without-child'))).toBe(false);
  expect(fixture.writes).toHaveLength(0);
});
