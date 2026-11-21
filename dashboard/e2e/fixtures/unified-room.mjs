import {mockLocalOperator, mockNonce} from './local-operator.mjs';
import {readFileSync} from 'node:fs';

const narrative = JSON.parse(readFileSync(new URL('./narrative-971.json', import.meta.url)));
export const fingerprint = {
  release_id: 'release-a', artifact_sha256: 'a'.repeat(64), plan_sha256: 'b'.repeat(64),
  image: 'registry/inventory@sha256:' + 'c'.repeat(64),
};
const names = {awaiting_user_test:'dev 사용자 시험 대기', awaiting_prd_approval:'prd 승인 대기', prd_deploy_queued:'prd 배포 대기'};
const id = 'native-a';

export async function mockUnifiedRoom(page, {state='awaiting_user_test', linked=true, protocol='dev-prd/v2'}={}) {
  const expiry = Date.now()/1000 + 600;
  const summary = {
    enabled:linked, linked, incident_id:971, action_id:linked?'action-a':null, application_id:linked?id:null,
    phase:linked?'code_action':'kubernetes', state, state_ko:names[state], eligible:false, blocks_cluster:linked,
    summary_ko:'수정한 서비스의 동작을 확인합니다.', problem_ko:'주문 처리에 실패합니다.', cause_ko:'연결 반환 처리가 빠졌습니다.',
    next_ko:'dev 버전에서 주문 동작을 확인하세요.', runner_observed:null, observed_at:null, protocol,
  };
  const application = {id,state,environment:'dev',service:'inventory',system:'trade',created_at:1000,stages:[],
    data:{analysis:{roles:[{role:'analyst',model:'gpt-6.2-sol',effort:'medium',usage:{input_tokens:120,output_tokens:null},
      occurred_at:1000,result:{summary_ko:'연결 반환 누락을 확인했습니다.'}}]},patch:{summary_ko:'연결 반환을 보완했습니다.'},
      raw:'RAW_ONLY_SENTINEL',merge_review:null}};
  const release = {...fingerprint, artifact:{image:fingerprint.image},valid:true,protocol,
    stage_results:{dev:{verified:true}},user_test:null};
  const testReceipt = () => ({...fingerprint,id:'test-a',kind:'human',passed:true,invalidated:false,expires_at:expiry});
  if (protocol==='dev-qa-prd/v3' && state==='awaiting_prd_approval') {
    release.qa={...fingerprint, outcome:'passed', accepted:true, observed_image:fingerprint.image, receipt_id:'e'.repeat(64)};
    release.risk={assessment:'complete', approval_blocked:false, destructive:false};
    release.risk_sha256='f'.repeat(64);
  } else if (state==='awaiting_prd_approval') release.user_test=testReceipt();
  application.data.release=release;
  application.data.unified_origin={incident_id:971, action_id:'action-a'};
  const original={id:'E1',source:'pod_log',service:'inventory',environment:'dev',observed_at:1001,text:'RAW_ONLY_SENTINEL'};
  application.data.collection={evidence:[original]};
  application.data.display_readings={'evidence/0':{state:'ready',ko:'연결을 반환하지 않아 주문 요청이 지연됐습니다.'}};
  const events=[];
  const event=(name,data={})=>{const id=events.length+1;events.push({id,at:1000+id,event:name,data});};
  const stage=name=>{event(name);application.stages.push({state:name,occurred_at:events.at(-1).at,round:1});};
  for(const name of ['detected','collecting','analyzing'])stage(name);
  const dispatch={role:'analyst',call_id:'call-1',model:'gpt-6.2-sol',effort:'medium'};
  event('role_started',{...dispatch,request_ko:'연결 반환 실패 원인을 조사하세요.'});
  event('role_completed',{...dispatch,summary_ko:'연결 반환 누락을 확인했습니다.'});
  if(state!=='analyzing'){
    for(const name of ['patch_proposed','test_build_queued','test_build_passed','dev_verified'])stage(name);
    if(release.qa)stage('qa_passed');
    stage(state);
  }
  function transition(next) {
    state=next; application.state=next; summary.state=next; summary.state_ko=names[next];stage(next);
  }
  const requests=[], writes=[], created=new Map();
  await mockLocalOperator(page);
  const fixture={summary,application,release,requests,writes,created,denyDetails:false,loseFirstCreate:false,
    observeMerge(){application.data.merge_review={...fingerprint,human_merge_observed:true,observed_at:Date.now()/1000,expires_at:expiry};},
  };
  await page.route('**/api/**',async route=>{
    const req=route.request(), url=new URL(req.url()), path=url.pathname, headers=await req.allHeaders();
    const send=(json,status=200,extra={})=>route.fulfill({json,status,headers:{'Cache-Control':'no-store',...extra}});
    const active=(headers.cookie||'').split('; ').includes('lp_operator_csrf='+mockNonce);
    const csrf=()=>active && headers['x-liveprobe-operator-csrf']===mockNonce && headers.origin===url.origin;
    const body=req.postDataJSON();
    requests.push({path,search:url.search,method:req.method(),headers,body});
    if(req.method()!=='GET'){
      if(!csrf() || headers.authorization)return send({detail:'PRIVATE_ERROR_SENTINEL'},403);
      writes.push({path,body,subject:'local-operator',csrf:headers['x-liveprobe-operator-csrf']});
      if(path==='/api/unified/incidents'){
        if(!created.has(body.request_id))created.set(body.request_id,971);
        if(fixture.loseFirstCreate){fixture.loseFirstCreate=false;return send({},503);}
        return send({incident_id:created.get(body.request_id),code_action:summary},201);
      }
      if(path.endsWith('/user-test')){release.user_test=testReceipt();transition('awaiting_prd_approval');}
      else if(path.endsWith('/review-merge'))fixture.observeMerge();
      else if(path.endsWith('/approve-prd')){
        if(!application.data.merge_review)return send({},409);
        transition('prd_deploy_queued');
      } else return send({},404);
      return send({ok:true});
    }
    if(path.endsWith('/code-action/detail')){
      if(!active || fixture.denyDetails)return send({detail:'PRIVATE_ERROR_SENTINEL'},403);
      return send({summary,application,release:{incident_id:id,state,release},events:[]});
    }
    if(path.endsWith('/code-action'))return send(summary);
    const native='/api/application/v1/incidents/'+id;
    if(path===native || path===native+'/activity' || path===native+'/current-version-check'){
      if(!linked || !active || fixture.denyDetails)return send({detail:'PRIVATE_ERROR_SENTINEL'},403);
      if(path===native+'/activity'){
        const after=Number(url.searchParams.get('after')||0), page=events.filter(event=>event.id>after);
        return send({id,state,stages:application.stages,events:page,cursor:page.at(-1)?.id??after,has_more:false,active_jobs:[]});
      }
      if(path.endsWith('/current-version-check'))return send({eligible:false,reason_ko:'원래 요청을 확인하지 못했습니다.',
        release_id:release.release_id,service:application.service,environment:application.environment,latest:null});
      if(url.searchParams.get('view')==='stage'){
        const {text,...safe}=original, {raw,...data}=application.data;
        return send({...application,data:{...data,collection:{evidence:[{...safe,has_original:true}]}}});
      }
      return send(application);
    }
    if(path.endsWith('/narrative')){
      const n=structuredClone(narrative);n.view_mode='live';n.incident.status=linked?'action_pending':'handed_off';n.incident.closed_at=null;
      if(linked)n.flow.nodes.push({id:'code-action-action-a',type:'code_action',label:'코드 수정 조치',role_ko:'코드 수정 조치',
        job_ko:'수정하고 검증합니다.',status:'running',inputs:[],lane:0,depth:Math.max(...n.flow.nodes.map(row=>row.depth))+1,card:null});
      return send(n);
    }
    if(path==='/api/incidents/971')return send({incident:{...narrative.incident,status:linked?'action_pending':'handed_off',closed_at:null},actions:[]});
    if(path.endsWith('/events'))return send({events:[],control:'running',streaming:false,execution_mode:'live'});
    if(path==='/api/unified/catalog')return send({enabled:true,profiles_configured:true,environments:['dev','prd'],
      services:[{service:'inventory',system:'trade',profile_id:'inventory'}]});
    if(path==='/api/console/incidents')return send({items:[{id:'legacy-native-17',native_id:'legacy-native-17',kind:'application',
      environment:'stg',namespace:'stg',workload:'inventory',system:'trade',status:'resolved',title:'퇴역 환경의 주문 이력',
      created_at:'2026-09-01T03:00:00Z',application_state:'completed'}],total:1,as_of:1000,newer_since_as_of:0,application_coverage:'observed'});
    if(path==='/api/unified/summaries')return send({incidents:[{id:'legacy-native-17',environment:'stg',service:'inventory',
      reason:'퇴역 환경의 주문 이력',state:'completed',root_incident_id:null}],total:1,as_of:1000});
    if(path==='/api/stream')return route.fulfill({contentType:'text/event-stream',body:': fixture\n\n'});
    if(path==='/api/llm/accounts')return send({accounts:[],mode:'mock'});
    return send({items:[],commands:[],incidents:[],total:0});
  });
  return fixture;
}
