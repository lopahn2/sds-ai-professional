// 현실적인 Application 원인 조사 기록: 역할 5개, 인용 20개(같은 소스 반복 포함), 모든 한글 정리 상태.
// variant: current(완료) · running(심층 추론 진행 중) · legacy(새 정리 도입 이전 기록) · blocked(인용 불일치로 중단)
import {mockUnifiedRoom} from './unified-room.mjs';

const SRC = 'src/main/java/io/liveprobe/target/inventory/AppFaultBusiness.java';
const TEST = 'src/test/java/io/liveprobe/target/inventory/InventoryBusinessTest.java';
const DIV = 'if (quantity == 7) return total / 0;', TRIM = 'if ("b".equals(item)) return nullValue.trim();';

export async function mockAppInvestigation(page, {variant='current'}={}) {
  const fixture = await mockUnifiedRoom(page, {state: variant==='running' ? 'analyzing' : 'analyzing'});
  const {application, events} = fixture;
  const legacy = variant==='legacy';
  application.data.collection = {evidence: [
    {id:'E1-inventory', source:'pod_log', service:'inventory', environment:'dev', observed_at:1001, text:'java.lang.ArithmeticException: / by zero at AppFaultBusiness.reserve'},
    {id:'E2-inventory', source:'rollout', service:'inventory', environment:'dev', observed_at:1002, text:'rollout inventory revision 42 available'},
    {id:'E3-inventory', source:'k8s_event', service:'inventory', environment:'dev', observed_at:1003, text:'Readiness probe failed'},
    {id:'E4-inventory', source:'deployment', service:'inventory', environment:'dev', observed_at:1004, text:'1/1 replicas ready'},
    {id:'E5-inventory', source:'pod_log', service:'inventory', environment:'dev', observed_at:1005, text:'java.lang.NullPointerException at AppFaultBusiness.find'},
  ]};
  application.data.display_readings = legacy
    ? Object.fromEntries([0,1,2,3,4].map(i=>['evidence/'+i,{state:'legacy',ko:null}]))
    : {'evidence/0':{state:'ready',ko:'재고 예약 요청에서 0으로 나누기 예외가 발생했습니다.'},
       'evidence/1':{state:'pending',ko:null},
       'evidence/2':{state:'failed',ko:null},
       'evidence/3':{state:'legacy',ko:null},
       'evidence/4':{state:'ready',ko:'재고 조회 중 IllegalStateException이 기록됐습니다.'}};
  application.data.analysis_routing = {effective_level:2};
  if (variant==='blocked') application.data.error = {code:'citation_not_grounded', message_ko:'인용이 수집 근거와 맞지 않아 분석을 멈췄습니다.'};

  // Replace the default single-analyst history with a full investigation round.
  events.length = 0; application.stages.length = 0;
  const event=(name,data={})=>{const id=events.length+1;events.push({id,at:1000+id,event:name,data});};
  const stage=name=>{event(name);application.stages.push({state:name,occurred_at:events.at(-1).at,round:1});};
  const readings=(entries)=>legacy ? undefined : entries;
  const call=(role,id,model,effort)=>({role,call_id:id,model,effort});
  for (const name of ['detected','collecting','analysis_queued']) stage(name);
  event('role_started',{...call('classifier','c0','gpt-6-luna','low'),request_ko:'사건 수준과 조사팀을 정하세요.'});
  event('role_completed',{...call('classifier','c0','gpt-6-luna','low'),level:2,confidence:.86,summary_ko:'코드 결함으로 보고 심층 조사팀을 배정합니다.',readings:readings({summary_ko:{state:'ready',ko:null}})});
  stage('analyzing');
  event('role_started',{...call('analyst','c1','gpt-6.1-sol','medium'),request_ko:'재고 예약 실패의 원인을 찾으세요.'});
  event('analysis_retry',{role:'analyst',reason:'citation_not_grounded'});
  event('role_started',{...call('analyst','c1b','gpt-6.1-sol','medium'),request_ko:'인용을 수집 근거에서 다시 고르세요.'});
  event('role_completed',{...call('analyst','c1b','gpt-6.1-sol','medium'),confidence:.91,unresolved:[],
    summary_ko:'재고 예약 실패는 입력 전용 분기에서 시작됩니다.',readings:readings({summary_ko:{state:'ready',ko:null}}),
    citations:[{ref:'source:'+SRC,quote:DIV},{ref:'source:'+SRC,quote:TRIM},{ref:'file_sha256:'+SRC,quote:DIV},
      {ref:'E1-inventory',quote:'ArithmeticException: / by zero'},{ref:'E1-inventory',quote:'/ by zero'},{ref:'E2-inventory',quote:'revision 42'},
      {ref:'original:signal',quote:'HTTP 500 /reserve'},{ref:'cache:missing-ref',quote:'cached value'}]});
  event('role_started',{...call('specialist','c2','gpt-6.1-sol','medium'),request_ko:'분기별 영향 범위와 수정 방법을 정하세요.'});
  if (variant==='running') {
    event('role_progress',{...call('specialist','c2','gpt-6.1-sol','medium'),phase:'streaming'});
  } else {
    event('role_completed',{...call('specialist','c2','gpt-6.1-sol','medium'),confidence:.97,unresolved:[],
      summary_ko:'AppFaultBusiness의 입력 전용 분기 2곳(quantity==7, item=="b")이 0 나눗셈과 null.trim()을 일으킵니다. 국소 수정이 가능합니다.',
      follow_up:['regress'],follow_up_ko:['수정 후 quantity=7과 item="b" 요청을 다시 시험합니다.'],
      readings:readings({summary_ko:{state:'ready',ko:null},'follow_up/0':{state:'ready',ko:null}}),
      citations:[{ref:'source:'+SRC,quote:DIV},{ref:'source:'+SRC,quote:TRIM},{ref:'source:'+TEST,quote:'assertEquals(7, reserve(7))'},
        {ref:'E1-inventory',quote:'/ by zero'},{ref:'E3-inventory',quote:'Readiness probe failed'},{ref:'E4-inventory',quote:'1/1 replicas ready'},
        {ref:'E5-inventory',quote:'NullPointerException'},{ref:'original:signal',quote:'HTTP 500 /reserve'}]});
    event('role_started',{...call('critic','c3','gpt-6.2-astra','high'),request_ko:'원인 판단을 반박해 보세요.'});
    event('role_completed',{...call('critic','c3','gpt-6.2-astra','high'),accepted:variant!=='blocked',confidence:.9,unresolved:[],
      summary_ko:null,follow_up:['a','b'],follow_up_ko:[null,'배포 변경 이후 같은 요청을 재현합니다.'],
      readings:readings({summary_ko:{state:'failed',ko:null},'follow_up/0':{state:'pending',ko:null},'follow_up/1':{state:'ready',ko:null}}),
      citations:[{ref:'E1-inventory',quote:'ArithmeticException'},{ref:'E2-inventory',quote:'revision 42 available'},
        {ref:'source:'+SRC,quote:DIV},{ref:'file_sha256:'+SRC,quote:TRIM}]});
    stage('verifying');
    event('role_started',{...call('verifier','c4','gpt-6.1-sol','medium')});
    event('role_completed',{...call('verifier','c4','gpt-6.1-sol','medium'),accepted:variant!=='blocked',confidence:.93,
      summary_ko:variant==='blocked'?'인용이 수집 근거와 맞지 않습니다.':'두 예외 경로가 입력 분기 제거로 모두 사라집니다.',readings:readings({summary_ko:{state:'ready',ko:null}}),
      citations:[{ref:'E1-inventory',quote:'/ by zero'}]});
  }
  application.state = variant==='running' ? 'analyzing' : variant==='blocked' ? 'handoff' : 'verifying';
  fixture.summary.state = application.state;
  await mockSummary(page, application);
  return fixture;
}

/** The Application room reads its headline from /summary before the stage stream. */
export async function mockSummary(page, application) {
  await page.route('**/api/application/v1/incidents/'+application.id+'/summary', route => route.fulfill({json:{
    id:application.id, state:application.state, service:application.service, environment:application.environment,
    system:application.system, created_at:application.created_at, reason:'재고 예약 요청이 실패합니다.', reason_ko:'재고 예약 요청이 실패합니다.'}}));
}
