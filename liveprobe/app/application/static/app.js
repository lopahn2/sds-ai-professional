'use strict';
(() => {
const API_PREFIX = '/api/application/v1';
const STATE_LABELS = {
  detected: '감지', collecting: '근거 수집 중', source_pinning: '소스 고정 중',
  analysis_queued: '분석 대기', analyzing: '분석 중', patch_proposed: '수정안 제안',
  test_build_queued: '시험·빌드 대기', test_build_passed: '시험·빌드 통과',
  dev_targets_collecting: 'dev 대상 수집 중', dev_deploy_queued: 'dev 배포 대기', dev_verifying: 'dev 업무 확인 중', dev_verified: 'dev 업무 확인 완료',
  stg_targets_collecting: 'stg 대상 수집 중', stg_deploy_queued: 'stg 배포 대기', stg_verifying: 'stg 업무 확인 중', stg_verified: 'stg 업무 확인 완료',
  prd_targets_collecting: 'prd 대상 수집 중', prd_deploy_queued: 'prd 배포 대기', prd_verifying: 'prd 업무 확인 중', prd_verified: 'prd 업무 확인 완료',
  awaiting_user_test: '사용자 시험 대기', awaiting_prd_approval: 'prd 승인 대기',
  reconciliation_required: '쓰기 결과 재확인 필요', handoff: '담당자 이관', failed: '실패', completed: '완료'
};
const ROLE_LABELS = {classifier:'수준 분류', analyst:'원인 분석', specialist:'전문 분석', critic:'반론 검토', verifier:'독립 검증', patch:'수정안 작성'};
const CHECK_LABELS = {database:'독립 저장소',human_identity:'사용자 인증',machine_identity:'실행기 인증',profiles:'실행 프로필',model:'모델 실행 구성(설치·설정)',runner:'실행기 최근 연결'};
// 정돈-07: '구성됨'은 호출 성공을 뜻하지 않는다. 호출 성공·실패는 저장된 실행 기록으로만 표시한다.
const MODEL_STATE_LABELS = {unconfigured:'구성 안 됨', configured_unverified:'구성됨 · 호출 성공 기록 없음', recent_success:'최근 역할 실행 성공 기록 있음',
  recent_failure:'최근 모델 호출 실패 기록 있음', unknown:'저장소 미준비로 확인 불가'};
const GROUP_LABELS = {automatic:'자동 진행 중', waiting_people:'사람 시험·승인 대기', handoff:'담당자 이관', closed:'종료',unknown:'상태 확인 필요'};
const PEOPLE_STATES = ['awaiting_user_test','awaiting_prd_approval'];
// 2026-11-13 dev/prd 전환(liveprobe/app/environments.py와 같은 값). 활성 환경은 dev·prd, stg는 퇴역(과거 기록 조회 전용).
// 신규 release는 'dev-prd/v2'(dev 사용자 시험 → prd 승인), protocol 필드가 없거나 모르는 값이면 기존 'dev-stg-prd/v1'(stg 사용자 시험) 기록으로 읽는다.
const RELEASE_PROTOCOL = 'dev-prd/v2', LEGACY_RELEASE_PROTOCOL = 'dev-stg-prd/v1';
const ACTIVE_ENVIRONMENTS = ['dev','prd'], RETIRED_ENVIRONMENTS = ['stg'];
const USER_TEST_ENVIRONMENT = {[RELEASE_PROTOCOL]:'dev', [LEGACY_RELEASE_PROTOCOL]:'stg'};
const isRetired = env => RETIRED_ENVIRONMENTS.includes(env);
function stateGroup(state) {
  if (PEOPLE_STATES.includes(state)) return 'waiting_people';
  if (state === 'handoff') return 'handoff';
  if (state === 'completed' || state === 'failed') return 'closed';
  return Object.hasOwn(STATE_LABELS,state) ? 'automatic' : 'unknown';
}
const POLL_MS = 15000, PAGE_SIZE = 50;
const TEST_LABELS = {passed:'통과',failed:'실패',skipped:'건너뜀',error:'오류'};
// 요약은 알려진 구조화 코드로만 표시한다. 정확한 코드·메시지는 원문 보기에서 보존한다.
const CODE_LABELS = {
  legacy_release_read_only:'기존 dev→stg→prd 절차의 기록은 읽기 전용입니다(stg 퇴역)', environment_retired:'퇴역 환경(stg)에는 새 작업을 배정하지 않습니다',
  dev_verification_required:'dev 업무 확인이 먼저 필요합니다',
  after_not_passed:'수정 후 시험이 통과하지 못했습니다', before_not_failed:'수정 전 재현 시험이 실패하지 않았습니다(재현 안 됨)',
  regression_not_executed:'회귀 시험이 실행되지 않았습니다', regression_not_passed:'회귀 시험이 통과하지 못했습니다', regression_not_failed:'회귀 시험이 수정 전 실패를 재현하지 못했습니다',
  test_build_evidence_invalid:'시험·빌드 결과 증거가 계약과 맞지 않아 거부했습니다', native_test_report_missing:'시험 보고서가 없습니다',
  patch_tests_missing:'수정안에 추가 시험이 없습니다', patch_invalid:'수정안 형식이 올바르지 않습니다', patch_context_mismatch:'수정안이 고정한 소스와 맞지 않습니다',
  patch_snapshot_mismatch:'수정안이 다른 소스 스냅샷을 가리킵니다', patch_paths_invalid:'수정안의 파일 경로가 허용 범위를 벗어났습니다',
  patch_contains_sensitive_material:'수정안에 민감 정보가 포함되어 거부했습니다', sensitive_patch_content:'수정안에 민감 정보가 포함되어 거부했습니다',
  collect_failed:'근거 수집에 실패했습니다', collect_evidence_missing:'수집 결과에 로그·근거가 없습니다', collect_evidence_invalid:'수집 결과 형식이 올바르지 않습니다',
  collect_evidence_scope_invalid:'수집 근거가 사건 범위를 벗어났습니다', collect_target_invalid:'수집 대상 정보가 올바르지 않습니다', collect_target_mismatch:'수집 대상이 사건 대상과 다릅니다',
  source_files_missing:'소스 파일이 없습니다', source_image_mismatch:'장애 이미지와 소스 지문이 일치하지 않습니다', source_integrity_invalid:'소스 무결성 검증에 실패했습니다',
  source_provenance_unverified:'장애 당시 소스 출처를 확인하지 못했습니다', source_affected_services_invalid:'영향 서비스 정보가 올바르지 않습니다',
  release_target_invalid:'배포 대상 정보가 올바르지 않습니다', release_target_mismatch:'배포 대상이 승인 대상과 다릅니다', release_generation_conflict:'배포 세대가 충돌했습니다',
  release_invalidated_during_write:'쓰기 중 승인·산출물이 무효화되었습니다', release_fingerprint_conflict:'산출물 지문이 바뀌었습니다',
  target_owned_by_other_release:'대상이 다른 배포에 잠겨 있습니다', deploy_readback_mismatch:'배포 후 다시 읽은 상태가 요청과 다릅니다',
  write_outcome_unresolved:'쓰기 결과를 확정하지 못했습니다(재확인 필요)', write_result_unknown:'쓰기 결과를 알 수 없습니다', write_readback_unknown:'쓰기 후 상태를 읽지 못했습니다',
  write_lease_expired:'쓰기 권한 임대가 만료되었습니다', write_authorization_revoked:'쓰기 권한이 회수되었습니다', job_lease_expired:'작업 임대가 만료되었습니다',
  job_authorization_revoked:'작업 권한이 회수되었습니다', job_fence_conflict:'다른 실행기가 같은 작업을 맡았습니다', job_result_conflict:'같은 작업에 다른 결과가 이미 보고되었습니다',
  cas_conflict:'다른 변경과 충돌했습니다(동시 수정)', stg_verification_required:'stg 업무 확인이 먼저 필요합니다(기존 절차 기록)', previous_stage_not_verified:'이전 환경 확인이 끝나지 않았습니다',
  user_test_failed:'사용자 시험이 실패로 기록되었습니다', user_test_invalid_or_expired:'사용자 시험 기록이 무효이거나 만료되었습니다', user_test_state_conflict:'사용자 시험 상태가 충돌했습니다',
  approval_state_conflict:'승인 상태가 충돌했습니다', approval_or_release_invalid:'승인 또는 산출물이 유효하지 않습니다', reconciliation_required:'쓰기 결과 재확인이 필요합니다',
  model_unconfigured:'모델이 구성되지 않았습니다', model_transport_failed:'모델 호출 통신에 실패했습니다', model_output_invalid:'모델 출력이 형식 계약을 지키지 않았습니다',
  runner_execution_failed:'실행기 작업이 실패했습니다', runner_not_provisioned_or_transport_failed:'실행기가 준비되지 않았거나 통신에 실패했습니다',
  kubernetes_transport_failed:'Kubernetes 호출에 실패했습니다', rollout_not_observed:'롤아웃 완료를 관측하지 못했습니다', image_readback_mismatch:'배포 후 이미지가 요청과 다릅니다',
  high_risk_handoff:'고위험으로 판단해 담당자에게 이관했습니다', unregistered_business_path:'등록되지 않은 업무 확인 경로입니다', unregistered_service:'등록되지 않은 서비스입니다',
  test_database_unavailable:'시험 DB를 사용할 수 없습니다', test_network_unavailable:'시험 네트워크를 사용할 수 없습니다', manifest_digest_not_observed:'이미지 manifest digest를 관측하지 못했습니다',
  provisioning_required:'독립 저장소·인증·실행기·모델 연결을 준비해야 합니다', storage_unavailable:'저장소를 사용할 수 없습니다', identity_unconfigured:'인증 파일이 구성되지 않았습니다',
  authentication_required:'인증이 필요합니다', authentication_invalid:'인증을 확인할 수 없습니다', scope_forbidden:'이 작업의 권한이 없습니다', identity_kind_forbidden:'사람과 실행기의 인증 경로는 분리되어 있습니다',
  database_unconfigured:'독립 저장소 초기화가 필요합니다', service_unknown:'등록된 서비스가 아닙니다', profiles_unconfigured:'등록 카탈로그·실행 프로필이 필요합니다',
  reproduction_unregistered:'등록된 읽기 재현 요청만 허용합니다', incident_not_found:'사건을 찾을 수 없습니다', metadata_review_unavailable:'승인 대상 검토 기록을 확인할 수 없습니다',
  request_rejected:'요청이 거부되었습니다', request_invalid:'요청 형식을 확인하세요', list_page_invalid:'페이지 범위를 확인하세요', list_group_invalid:'상태 필터를 확인하세요', list_filter_invalid:'검색 조건을 확인하세요', list_dates_invalid:'조회 기간을 확인하세요',
};
const label = (labels, key, fallback='미관측') => typeof key === 'string' && Object.hasOwn(labels,key) ? labels[key] : fallback;
const codeKo = c => c ? label(CODE_LABELS,c,'상세 사유 확인 필요') : '미관측';
const HANDOFF_LABELS = {authority_boundary:'권한·데이터 안전 경계', bounded_recovery_exhausted:'제한된 자율 복구 소진'};
const QUALITY_LABELS = {observed:'직접 관측', inferred:'추론', synthetic:'합성(시험)', unobserved:'미관측'};
const SOURCE_LABELS = {pod_log:'Pod 로그', k8s_event:'Kubernetes 이벤트', metric:'지표', trace:'호출 추적', service_call:'서비스 호출'};
const WRITE_LABELS = {written:'적용됨', not_written:'적용 안 됨', unknown:'결과 불명(재확인 필요)', partial:'일부 적용'};
const EFFORT_LABELS = {low:'낮음', medium:'보통', high:'높음', xhigh:'매우 높음', minimal:'최소'};
const SYSTEM_LABELS = {'samsung-trade':'삼성 트레이드 시스템','samsung-delivery':'삼성 딜리버리 시스템','samsung-business':'삼성 비즈니스 시스템'};
const MODE_LABELS = {isolated_test:'격리 시험 모드(합성 응답)', runtime:'운영 모드'};
const $ = id => document.getElementById(id);
const list = value => Array.isArray(value) ? value : [];
const obj = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const value = v => v === null || v === undefined || v === '' ? '미관측' : typeof v === 'object' ? '미관측' : String(v);
const yesNo = v => v === true ? '확인' : v === false ? '미충족' : '미관측';
const passFail = v => v === true ? '통과' : v === false ? '실패' : '미관측';
function stateLabel(state) { return label(STATE_LABELS,state,'상태 확인 필요'); }
function errorKo(e) {
  if (!e) return null;
  return codeKo(typeof e === 'string' ? e : obj(e).code);
}
const envLabel = env => isRetired(env) ? 'stg (퇴역·이력)' : ACTIVE_ENVIRONMENTS.includes(env) ? env : '환경 확인 필요';
const count = n => Number.isFinite(n) && n >= 0 ? n : '미관측';
function stamp(v) {
  if (v === null || v === undefined || v === '') return '미관측';
  const d = new Date(typeof v === 'number' ? v * 1000 : v);
  return Number.isNaN(d.getTime()) ? '시각 미관측' : d.toLocaleString('ko-KR', {hour12:false});
}
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs || {})) if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
// 조건부 항목(null·undefined)은 그리지 않는다. replaceChildren(null)은 'null' 글자를 넣으므로 걸러 낸다.
function put(id, ...nodes) { $(id).replaceChildren(...nodes.flat().filter(n => n !== null && n !== undefined)); }
function facts(rows) { return el('dl', {class:'facts'}, rows.map(([key,v]) => [el('dt',{},key),el('dd',{},value(v))]).flat()); }
function empty(text='미관측') { return el('p',{class:'muted'},text); }
function lines(items) { return items.length ? items.map(x=>el('li',{},value(x))) : [el('li',{class:'muted'},'미관측')]; }
function table(columns, rows) {
  if (!rows.length) return empty();
  return el('table',{},el('thead',{},el('tr',{},columns.map(c=>el('th',{scope:'col'},c)))),
    el('tbody',{},rows.map(row=>el('tr',{},row.map((cell,i)=>el('td',{'data-label':columns[i]},cell instanceof Node ? cell : value(cell)))))));
}
function status(text) { $('live-status').textContent = text; }
// 원문은 JS 메모리에만 보관하고 명시적으로 펼칠 때 텍스트 노드를 만든다.
// 같은 응답이면 열린 원문 DOM도 재사용한다(선택·키보드 포커스 보존).
const rawViews = new Map();
function syncRaw(id) {
  const entry=rawViews.get(id); if (!entry) return;
  if (!$(id).open) {
    for (const target of entry.targets) put(target);
    entry.mounted=null; return;
  }
  if (entry.mounted!==entry.key) { entry.render(); entry.mounted=entry.key; }
}
function rawView(id, targets, data, render) {
  const previous=rawViews.get(id);
  rawViews.set(id,{targets,key:JSON.stringify(data),render,mounted:previous?.mounted ?? null});
  syncRaw(id);
}
function rawJson(id, target, data) { rawView(id,[target],data,()=>put(target,JSON.stringify(data,null,2) ?? '미관측')); }
function resetRaw(scope=document) {
  for (const node of scope.querySelectorAll('details[data-raw]')) {
    node.open=false; syncRaw(node.id); rawViews.delete(node.id);
  }
}
for (const node of document.querySelectorAll('details[data-raw]')) node.addEventListener('toggle',()=>syncRaw(node.id));
function uiError(message,detail=null) { const error=new Error(message); error.uiMessage=message; error.detail=detail; return error; }
function clearError() { $('error-banner').hidden = true; $('error-message').textContent = ''; resetRaw($('error-banner')); }
function showError(error) {
  $('error-message').textContent = error.uiMessage || '요청 결과를 확인할 수 없습니다. 다시 조회하세요.';
  rawJson('raw-error','error-raw',error.detail ?? {message:error.message});
  $('error-banner').hidden = false; status('요청에 실패했습니다. 오류 내용을 확인하세요.');
}
function resetIncidentView() {
  resetRaw($('incident-detail'));
  setRecordVisibility(false);
  for (const id of ['test-notes','test-passed']) $(id).value='';
  $('approve-confirm').checked=false; confirmedFor=null;
  for (const id of ['incident-summary','next-action-body','approve-digest','approval-verification','human-records','env-status','test-results','patch-overview','collection-meta','analysis-overview']) put(id);
}
function operatorCsrf() {
  return document.cookie?.match(/(?:^|;\s*)lp_operator_csrf=([a-f0-9]{64}\.[a-f0-9]{64})(?:;|$)/)?.[1];
}
function renderAccess() { $('workspace').hidden=false; }
let hasReadRole = !!operatorCsrf(), current = null, selectedId = null, revision = 0, catalogReady = false, busy = false;
const permits = () => !!operatorCsrf();
// 정돈-04/06: 목록 페이지 상태(서버 필터·as_of 고정), 상세 응답 순서 번호, 승인 확인 당시 지문.
let listState = {offset:0, asOf:null, total:0, filters:{}}, listSeq = 0, detailSeq = 0, confirmedFor = null;
async function api(path, {method='GET',body,readiness=false,query=null}={}) {
  if (!/^\/(?:readyz|catalog|healthz|summaries|incidents(?:\/[^/?#]+(?:\/(?:summary|release|user-test|approve-prd|review-merge))?)?)$/.test(path)) throw new Error('허용되지 않은 요청 경로입니다.');
  if (query) { const q=new URLSearchParams(Object.entries(query).filter(([,v])=>v!==null&&v!==undefined&&v!=='')).toString(); if (q) path += '?' + q; }
  const headers = {'Accept':'application/json', 'X-LiveProbe-Client':'dashboard'};
  if (method !== 'GET' && operatorCsrf()) headers['X-LiveProbe-Operator-CSRF']=operatorCsrf();
  if (body) headers['Content-Type'] = 'application/json';
  const response = await fetch(API_PREFIX + path, {method,headers,credentials:'same-origin',cache:'no-store',referrerPolicy:'no-referrer', ...(body ? {body:JSON.stringify(body)} : {})});
  const text = await response.text();
  let data;
  try { data=JSON.parse(text); }
  catch { throw uiError('서버 응답 형식을 확인할 수 없습니다.',{status:response.status,body:text}); }
  if (!data || typeof data!=='object' || Array.isArray(data)) throw uiError('서버 응답 형식을 확인할 수 없습니다.',{status:response.status,body:text});
  if (!response.ok && !(readiness && response.status === 503)) throw uiError(data.code?codeKo(data.code):'서버 응답을 확인할 수 없습니다.',data);
  return data;
}
function release() { return obj(current?.data?.release); }
// 아직 release가 없는 사건은 현행 절차(v2)로 안내한다. release가 있으면 protocol 필드로만 판단한다(없으면 기존 v1 기록).
function releaseProtocol(r) {
  r = obj(r);
  if (!r.release_id && r.protocol === undefined) return RELEASE_PROTOCOL;
  return r.protocol === RELEASE_PROTOCOL ? RELEASE_PROTOCOL : LEGACY_RELEASE_PROTOCOL;
}
const isLegacyRelease = r => releaseProtocol(r) !== RELEASE_PROTOCOL;
const userTestEnv = r => USER_TEST_ENVIRONMENT[releaseProtocol(r)];
// 사용자 시험 직전 업무 확인: v2는 dev, v1은 stg 결과를 본다.
const userTestReady = r => obj(obj(r.stage_results)[userTestEnv(r)]).verified === true;
function protocolKo(r) {
  r = obj(r);
  if (!r.release_id) return '미관측';
  if (!isLegacyRelease(r)) return 'dev → prd (dev-prd/v2) · 사용자 시험 환경 dev';
  return `기존 dev → stg → prd (dev-stg-prd/v1${r.protocol === undefined ? ' · protocol 필드 없음' : ''}) · stg 퇴역 · 조회 전용`;
}
function stateLabelFor(state, r) {
  return state === 'awaiting_user_test' && obj(r).release_id ? `${userTestEnv(r)} 사용자 시험 대기` : stateLabel(state);
}
function renderUserTestEnv(r) { for (const node of document.querySelectorAll('.user-test-env')) node.textContent = userTestEnv(r); }
function fingerprint(r) { return {release_id:r.release_id,artifact_sha256:r.artifact_sha256,plan_sha256:r.plan_sha256,image:r.artifact?.image}; }
function exactRelease(r) { return r.valid === true && [r.release_id,r.artifact_sha256,r.plan_sha256,r.artifact?.image].every(v=>typeof v==='string' && v.length>0); }
function validUserTest(r) {
  const t=obj(r.user_test);
  return t.passed === true && t.invalidated === false && typeof t.id === 'string' && Number.isFinite(t.expires_at) && t.expires_at * 1000 > Date.now();
}
function resetApprovalReview() {
  rawViews.delete('approval-target');put('approve-digest');put('approval-verification');
}
$('approval-target').addEventListener('toggle',()=>{
  if (!$('approval-target').open) { $('approve-confirm').checked=false; confirmedFor=null; }
  gates();
});
function gates() {
  // v1(dev→stg→prd) 기록은 서버가 읽기 전용(legacy_release_read_only)으로 거부하고 stg는 퇴역했으므로 시험·승인 제출을 열지 않는다.
  // 사용자 시험 직전 확인 환경은 프로토콜별로 계산한다(v2 dev, v1 stg). 현행 v2에서는 dev 업무 확인이 통과해야 한다.
  const r=release(),valid=exactRelease(r) && !isLegacyRelease(r) && !busy;
  $('create-submit').disabled = !permits('incident:create') || !catalogReady || busy;
  $('user-test-submit').disabled = !permits('tester') || !valid || current?.state!=='awaiting_user_test' || !userTestReady(r);
  $('approve-confirm').disabled = !$('approval-target').open || current?.state!=='awaiting_prd_approval';
  $('approve-submit').disabled = !permits('prd-approver') || !$('approval-target').open || !valid || current?.state!=='awaiting_prd_approval' || !validUserTest(r) || !$('approve-confirm').checked
    || confirmedFor !== approvalKey(r);
}
// 정돈-06: 승인 확인은 확인 당시의 지문(산출물·계획·이미지·시험 기록)에만 유효하다. 바뀌면 확인만 해제한다.
function approvalKey(r) { return JSON.stringify([current?.id,current?.state,r.release_id,r.artifact_sha256,r.plan_sha256,r.artifact?.image,r.user_test?.id]); }
async function loadReadiness() {
  try {
    const data=await api('/readyz',{readiness:true,bearer:''});
    const checks=obj(data.checks),model=obj(data.model_status),people=obj(data.waiting_people);
    const missing=Object.keys(CHECK_LABELS).filter(k=>checks[k]!==true);
    put('readiness-body',el('p',{class:data.ready===true?'ok':'warning'},data.ready===true?'실행 구성 확인됨':'실행 구성 확인 필요'),
      missing.length?el('p',{class:'warning'},'점검 필요: ',missing.map(k=>`${CHECK_LABELS[k]}${checks[k]===false?'':' (미관측)'}`).join(' · ')):null,
      el('p',{class:'muted'},`사용자 시험 대기 ${count(people.awaiting_user_test)} · prd 승인 대기 ${count(people.awaiting_prd_approval)}`));
    put('readiness-checks',facts(Object.keys(CHECK_LABELS).map(k=>[CHECK_LABELS[k],checks[k]===true?'준비됨':checks[k]===false?'확인 필요':'미관측'])),
      facts([['모델 호출 기록',label(MODEL_STATE_LABELS,model.state)],['최근 역할 실행 성공',stamp(model.last_success_at)],
        ['최근 모델 호출 실패',model.last_failure_code?`${stamp(model.last_failure_at)} · ${codeKo(model.last_failure_code)}`:'미관측'],['실행 모드',label(MODE_LABELS,data.mode)]]));
    rawJson('raw-readiness','readiness-raw',data);
  } catch(e) {
    put('readiness-body',el('p',{class:'warning'},'준비 상태를 조회하지 못했습니다. 대기 건수도 미관측입니다.'));
    put('readiness-checks',empty('연결을 확인한 뒤 다시 조회하세요.'));
    rawJson('raw-readiness','readiness-raw',e.detail ?? {message:e.message});
  }
}
async function prerequisites() {
  await Promise.all([
    loadReadiness(),
    api('/catalog',{bearer:''}).then(data=>{
      // 새 사건은 활성 환경에만 만든다. 구 서버가 퇴역 stg를 보내도 선택지로 보이지 않는다.
      const retired=[...RETIRED_ENVIRONMENTS,...list(data.retired_environments)];
      const environment=$('create-environment').value,service=$('create-service').value;
      $('create-environment').replaceChildren(el('option',{value:''},'환경 선택'),...list(data.environments).filter(x=>!retired.includes(x)).map(x=>el('option',{value:x},envLabel(x))));
      $('create-service').replaceChildren(el('option',{value:''},'서비스 선택'),...list(data.services).map(s=>el('option',{value:s.service},`${value(s.service)} · ${label(SYSTEM_LABELS,s.system,'등록 시스템')}`)));
      $('create-environment').value=environment; $('create-service').value=service;
      catalogReady=data.profiles_configured===true;
      if (!catalogReady) status('등록 카탈로그·실행 프로필을 확인해야 사건을 생성할 수 있습니다.');gates();
    }).catch(e=>{catalogReady=false;showError(e);gates();})
  ]);
}
function readFilters() {
  return {start:$('filter-start').value, end:$('filter-end').value, system:$('filter-system').value.trim(),
    service:$('filter-service').value.trim(), environment:$('filter-environment').value, group:$('filter-group').value, search:$('filter-search').value.trim()};
}
// 정돈-04: 서버가 전체 이력에 필터를 적용한다. 첫 페이지는 최신 기준, 이후 페이지는 첫 페이지의 as_of로 고정한다.
async function loadList({offset=listState.offset, filters=listState.filters, pinned=offset>0}={}) {
  const rev=revision, seq=++listSeq;
  const asOf=pinned ? listState.asOf : null;
  const data=await api(hasReadRole?'/incidents':'/summaries',{query:{...filters,limit:PAGE_SIZE,offset,as_of:asOf}});
  if (rev!==revision || seq!==listSeq) return;           // 늦게 도착한 이전 응답은 버린다.
  if (!Array.isArray(data.incidents) || !Number.isFinite(data.total)) throw uiError('사건 목록 응답을 확인할 수 없습니다. 건수는 미관측입니다.',data);
  listState={offset,asOf:data.as_of ?? null,total:Number.isFinite(data.total)?data.total:0,filters};
  const rows=list(data.incidents);
  const focusedId=document.activeElement?.closest?.('.incident-select')?.dataset.id;
  $('list-count').textContent = `전체 ${listState.total}건`;
  put('incident-list',rows.length ? rows.map(row=>{
    const group=stateGroup(row.state);
    const b=el('button',{type:'button',class:`incident-select group-${group}`,'aria-pressed':row.id===selectedId,'data-id':row.id},
      el('span',{class:'incident-service'},value(row.service)),el('span',{class:'state'},stateLabel(row.state)),
      el('span',{class:'muted'},`${label(SYSTEM_LABELS,row.system,'시스템 확인 필요')} · ${envLabel(row.environment)}`),el('time',{},group === 'closed' ? '종료 '+stamp(terminalTime(row)) : stamp(row.created_at)),el('small',{},'#'+value(row.id).slice(0,8)));
    b.addEventListener('click',()=>void selectIncident(row.id, true));return b;
  }) : empty(Object.values(filters).some(Boolean)?'조건에 맞는 사건이 없습니다.':'사건이 없습니다.'));
  if (focusedId) $('incident-list').querySelector(`[data-id="${CSS.escape(focusedId)}"]`)?.focus({preventScroll:true});
  const pages=Math.max(1,Math.ceil(listState.total/PAGE_SIZE)), page=Math.floor(offset/PAGE_SIZE)+1;
  $('list-page').textContent=`${page} / ${pages} 페이지`;
  $('list-prev').disabled=offset<=0; $('list-next').disabled=offset+PAGE_SIZE>=listState.total;
  const newer=Number(data.newer_since_as_of)||0;
  $('list-newer').hidden=!(pinned && newer>0);
  $('list-newer-text').textContent=`이 페이지를 연 뒤 새 사건 ${newer}건이 들어왔습니다.`;
  status(`조건에 맞는 사건 ${listState.total}건 중 ${rows.length}건을 표시했습니다.`);
}
function terminalTime(incident) {
  return list(incident.stages).findLast(stage => stage?.state === incident.state)?.occurred_at;
}
function setRecordVisibility(expanded) {
  const toggle = $('history-toggle');
  const records = $('response-records');
  toggle.setAttribute('aria-expanded', String(expanded));
  toggle.textContent = expanded ? '대응 기록 접기' : '대응 기록 보기';
  records.hidden = !toggle.hidden && !expanded;
  if (records.hidden) {
    for (const raw of records.querySelectorAll('details[data-raw][open]')) {
      raw.open = false;
      syncRaw(raw.id);
    }
  }
}
$('history-toggle').addEventListener('click', () => {
  setRecordVisibility($('history-toggle').getAttribute('aria-expanded') !== 'true');
});
function renderNextAction() {
  const s=current.state, group=stateGroup(s), r=release(), env=userTestEnv(r);
  const legacy=!!r.release_id && isLegacyRelease(r);
  const reason=errorKo(current.error);
  const historical = group === 'closed';
  const toggle = $('history-toggle');
  if (toggle.hidden === historical) {
    toggle.hidden = !historical;
    setRecordVisibility(false);
  }
  $('next-title').textContent = historical ? '처리 결과' : '현재 상태와 다음 행동';
  if (historical) {
    const verified = obj(r.stage_results).prd?.verified;
    const result = s === 'failed' ? reason || '실패 사유가 기록되지 않았습니다.'
      : verified === true ? 'prd 업무 확인을 통과했습니다.'
      : verified === false ? '완료 처리되었습니다. prd 업무 확인은 통과하지 못했습니다.'
      : '완료 처리되었습니다. prd 업무 확인 결과는 미기록입니다.';
    put('next-action-body', el('p', {class:s === 'failed' ? 'warning' : ''}, result));
    return;
  }
  const text = group==='unknown' ? '현재 상태를 확인하지 못했습니다. 원문 기록과 연결 상태를 확인하세요. 시험·승인은 보낼 수 없습니다.'
    : s==='reconciliation_required' ? '쓰기 결과를 다시 확인하는 중입니다. 실행기가 실제 상태를 읽을 때까지 승인·재시도를 보내지 마세요.'
    : legacy && group==='waiting_people' ? '기존 dev→stg→prd(v1) 절차의 기록입니다. stg는 퇴역해 이 기록으로는 사용자 시험·prd 승인을 보낼 수 없습니다(서버 읽기 전용). 필요하면 새 사건으로 dev→prd 절차를 시작하세요.'
    : legacy && group==='automatic' ? '기존 dev→stg→prd(v1) 절차의 기록입니다. 퇴역한 stg에는 새 작업을 배정하지 않으며, 남은 단계는 서버가 담당자 이관으로 정리합니다.'
    : s==='awaiting_user_test' ? `${env} 사용자 시험이 필요합니다. ${env}에서 실제로 시험한 뒤 아래 “${env} 사용자 시험 기록”에 결과를 남기세요.`
    : s==='awaiting_prd_approval' ? 'prd 승인이 필요합니다. 사용자 시험 기록과 승인 대상 지문을 확인한 뒤 명시적으로 승인하세요.'
    : s==='handoff' ? '자동 진행이 멈춰 담당자 확인이 필요합니다. 사유와 근거를 확인하세요.'
    : '자동으로 진행 중입니다. 지금 사람이 할 일은 없습니다. 자동 갱신으로 다음 단계를 확인하세요.';
  const hc=obj(current.handoff_classification);
  put('next-action-body',el('p',{class:`next next-${group}`},el('b',{},`${stateLabelFor(s,r)} · ${GROUP_LABELS[group]}`)),el('p',{},text),
    r.release_id ? el('p',{class:'muted'},'승격 절차: ',protocolKo(r)) : null,
    reason && (group==='handoff'||group==='closed'||s==='reconciliation_required') ? el('p',{class:'warning'},'사유: ',reason) : null,
    hc.class && group==='handoff' ? el('p',{class:'muted'},'이관 분류: ',label(HANDOFF_LABELS,hc.class,'분류 확인 필요'),
      hc.recovery_attempted===true ? ` · 자율 복구 시도 ${list(hc.attempts).length}건 기록` : ' · 자율 복구 시도 기록 없음') : null,
    r.valid===false ? el('p',{class:'warning'},'승격 무효: ',codeKo(r.invalidation_reason)||'사유 미관측') : null);
}
async function fetchDetail(id) {
  const path='/incidents/' + encodeURIComponent(id);
  if (!hasReadRole) return api(`${path}/summary`);
  const [detail,rr]=await Promise.all([api(path),api(`${path}/release`)]);
  if (detail.id!==id || rr.incident_id!==id) throw new Error('사건 응답의 대상이 일치하지 않습니다.');
  return {...detail,data:{...obj(detail.data),release:rr.release,
    test_build:Object.hasOwn(rr,'test_build')?rr.test_build:detail.data?.test_build}};
}
async function selectIncident(id, fromList=false) {
  if (selectedId!==id) resetIncidentView();
  const rev=++revision, seq=++detailSeq;selectedId=id;current=null;confirmedFor=null;$('approve-confirm').checked=false;
  $('detail-sections').hidden=true;$('detail-title').textContent='사건을 불러오는 중입니다.';put('incident-summary');clearError();gates();
  try {
    const next=await fetchDetail(id);
    if (rev!==revision || seq!==detailSeq) return;
    current=next;
    renderDetail();
    for (const b of $('incident-list').querySelectorAll('button')) b.setAttribute('aria-pressed',b.dataset.id===id?'true':'false');
    if (fromList && window.matchMedia('(max-width: 760px)').matches) $('incident-detail').focus();
    status('사건을 열었습니다.');
    $('incident-detail').focus({preventScroll:true});
  } catch(e) { if(rev===revision){$('detail-title').textContent='사건을 불러오지 못했습니다.';showError(e);} }
}
// 정돈-06: 자동 갱신은 선택·작성 중인 시험 기록·포커스·펼침 상태를 바꾸지 않고 표시 영역만 갱신한다.
async function refreshDetail() {
  if (!selectedId || busy) return;
  const id=selectedId, rev=revision, seq=++detailSeq;
  const next=await fetchDetail(id);
  if (rev!==revision || seq!==detailSeq || selectedId!==id || busy) return;   // 선택이 바뀌었거나 더 새 응답이 있으면 버린다.
  current=next;
  renderDetail({quiet:true});
}
function renderDetail({quiet=false}={}) {
  const d=obj(current.data),r=release(),patch=obj(d.patch),collection=obj(d.collection);
  $('detail-title').textContent = `${value(current.service)} · ${stateLabelFor(current.state,r)}`;
  if (current.projection === 'local-read/v1') {
    $('detail-sections').hidden=true;
    put('incident-summary',facts([['시스템',current.system],['환경',current.environment],['상태',stateLabel(current.state)],['감지',stamp(current.created_at)],['사유',current.reason],['다음 행동',current.next_ko]]));
    gates(); return;
  }
  renderUserTestEnv(r);
  $('detail-sections').hidden=false;
  // 승인 확인은 지문이 같을 때만 유지한다. 바뀌면 확인만 해제하고 알린다(자동 승인 없음).
  if ($('approve-confirm').checked && confirmedFor!==approvalKey(r)) {
    $('approve-confirm').checked=false;confirmedFor=null;
    if (quiet) status('승인 대상 지문이나 단계가 바뀌어 승인 확인을 해제했습니다. 다시 확인하세요.');
  }
  renderNextAction();
  const summary = [['시스템', label(SYSTEM_LABELS,current.system,'시스템 확인 필요')], ['환경', envLabel(current.environment)], ['감지 시각', stamp(current.created_at)]];
  if (stateGroup(current.state) === 'closed') {
    summary.push(['종료 시각', stamp(terminalTime(current))]);
  } else {
    summary.push(['감지 사유', current.reason ? codeKo(current.reason) : '영향 확인 중']);
  }
  put('incident-summary', facts(summary));
  rawJson('raw-incident','incident-raw',current);
  rawView('raw-stages',['stage-timeline'],current.stages,()=>put('stage-timeline',list(current.stages).length ? current.stages.map(s=>el('li',{},el('b',{},stateLabel(s.state)),el('time',{},stamp(s.occurred_at)),el('pre',{},JSON.stringify(s,null,2)))) : el('li',{},'미관측')));
  put('collection-meta',facts([['수집 시각',stamp(collection.observed_at)],['로그 표본',Array.isArray(collection.logs)?collection.logs.length:'미관측'],['근거 기록',Array.isArray(collection.evidence)?collection.evidence.length:'미관측']]));
  rawView('raw-collection',['collection-logs','evidence-table'],collection,()=>{
    put('collection-logs',table(['서비스','시각','정제 원문'],list(collection.logs).map(x=>[x.service,stamp(x.observed_at),el('pre',{},value(x.text))])));
    put('evidence-table',table(['근거 ID','서비스 · 환경','시각','출처 · 품질','정제 원문'],list(collection.evidence).map(x=>[
      x.id,`${value(x.service)} · ${value(x.environment)}`,stamp(x.observed_at),`${value(x.source)} · ${value(x.quality)}`,el('pre',{},value(x.text))])));
  });
  const roles=list(d.analysis?.roles);
  put('analysis-overview',roles.length?table(['역할','수정 가능','수락'],roles.map(row=>[label(ROLE_LABELS,row.role,'분석 역할'),yesNo(row.result?.fixable),yesNo(row.result?.accepted)])):empty('분석 결과 미관측'));
  rawView('raw-analysis',['analysis-roles'],roles,()=>put('analysis-roles',roles.length?roles.map(row=>el('div',{class:'role-result'},el('h4',{},label(ROLE_LABELS,row.role,'분석 역할')),el('pre',{},JSON.stringify(row,null,2)))):empty()));
  const diff=typeof patch.diff==='string'?patch.diff:'';
  const files=[...new Set([...diff.matchAll(/^\+\+\+ (?:b\/)?(.+)$/gm)].map(m=>m[1]))];
  put('patch-overview',Object.keys(patch).length?facts([['변경 파일',files.length],['추가 시험',list(patch.tests_added).length],['위험·부작용',patch.risk_ko?'기록 있음 · 원문에서 확인':'미관측']]):empty('수정안 미관측'));
  rawView('raw-patch',['patch-summary','patch-files','patch-tests','patch-diff'],patch,()=>{
    put('patch-summary',facts([['수정 요약',patch.summary_ko],['위험·부작용',patch.risk_ko],['diff_sha256',patch.diff_sha256]]));
    put('patch-files',lines(files));put('patch-tests',lines(list(patch.tests_added)));
    put('patch-diff',diff ? diff.split('\n').map(line=>el('span',{class:line.startsWith('+++')||line.startsWith('---')?'diff-context':line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':'diff-context'},line+'\n')) : '미관측');
  });
  renderTests(obj(r.tests || d.test_build?.tests),list(patch.tests_added),obj(d.test_build));
  rawView('raw-artifacts',['artifacts'],r,()=>put('artifacts',el('pre',{},JSON.stringify({release_id:r.release_id,artifact:r.artifact,artifact_sha256:r.artifact_sha256,plan_sha256:r.plan_sha256},null,2))));
  renderEnvironments(r);
  rawView('raw-explain',['release-explain'],[r.summary_ko,r.risk_ko,r.rollback_ko,patch.summary_ko,patch.risk_ko,r.invalidation_reason],()=>put('release-explain',facts([['설명',r.summary_ko||patch.summary_ko],['부작용',r.risk_ko||patch.risk_ko],['원복 방법',r.rollback_ko],['승격 절차',protocolKo(r)],['승격 유효성',r.valid===true?'유효':r.valid===false?'무효':'미관측'],['무효 사유',r.invalidation_reason]])));
  renderHuman(r,patch);
  gates();
}
function renderHuman(r,patch) {
  const user=obj(r.user_test),approval=obj(r.approval),legacy=isLegacyRelease(r);
  const test=current.state==='awaiting_user_test',approve=current.state==='awaiting_prd_approval';
  $('user-test-form').hidden=!test || legacy;
  $('approve-form').hidden=!approve || legacy;
  $('human-section').hidden=!(test || approve || r.user_test || r.approval);
  // 승인 대상 검토를 펼치면 전체 지문·위험·원복·검증 결과를 읽고 확인할 수 있다.
  // 사용자 시험 대기에서는 시험 폼을 먼저 제공하고 기술 지문은 원문에서 확인한다.
  $('approval-target').hidden=!approve || legacy;
  if (approve && !legacy) {
    const verification=r.stage_results?.dev?.verification;
    const checks=Array.isArray(verification)?verification.map(v=>[v.service,passFail(v.business?.ok),v.business?.status_code,v.business?.body_sha256,v.image]):list(obj(verification).checks).map(v=>[v.name,passFail(v.passed),v.status_code,v.body_sha256,verification.image]);
    rawView('approval-target',['approve-digest','approval-verification'],[r,patch],()=>{
    put('approve-digest',facts([['승인 대상 이미지 (전체 digest)',r.artifact?.image],['release_id',r.release_id],['artifact_sha256',r.artifact_sha256],['plan_sha256',r.plan_sha256]]),
      table(['prd 대상 서비스','환경','UID','resourceVersion'],list(r.targets?.prd).map(t=>[t.service,t.environment,t.uid,t.resource_version])));
    put('approval-verification',facts([['수정 설명',r.summary_ko||patch.summary_ko],['위험·부작용',r.risk_ko||patch.risk_ko],['원복 방법',r.rollback_ko],['dev 업무 확인',passFail(r.stage_results?.dev?.verified)],['사용자 시험',passFail(user.passed)],['시험 유효성',validUserTest(r)?'유효':'무효 또는 미관측'],['시험 시각',stamp(user.occurred_at)],['시험 만료',stamp(user.expires_at)]]),
      facts([['시험 기록 ID',user.id],['자동 시험 수정 전 재현',yesNo(r.tests?.before_failed)],['수정 후 시험 통과',yesNo(r.tests?.after_passed)],['회귀 시험 수행',yesNo(r.tests?.regression_executed)],['시험 보고서 sha256',r.tests?.report_sha256]]),
      table(['dev 확인 항목','업무 결과','HTTP 상태','본문 sha256','확인 이미지'],checks));
    });
  } else { $('approval-target').open=false; resetApprovalReview(); }
  put('human-records',el('h4',{},`${userTestEnv(r)} 사용자 시험 기록`),Object.keys(user).length?facts([['시험 결과',passFail(user.passed)],['시험 시각',stamp(user.occurred_at)],['시험 만료',stamp(user.expires_at)],['시험 무효화',yesNo(user.invalidated)]]):empty('시험 기록 미관측'),
    el('h4',{},'prd 승인 기록'),Object.keys(approval).length?facts([['승인 시각',stamp(approval.occurred_at)],['승인 만료',stamp(approval.expires_at)],['승인 무효화',yesNo(approval.invalidated)]]):empty('승인 기록 미관측'));
  rawJson('raw-human','human-raw',{user_test:r.user_test,approval:r.approval});
  $('approval-gate').textContent = !exactRelease(r)?'유효한 산출물·계획 지문을 받지 못해 시험·승인을 보낼 수 없습니다.':legacy?'기존 dev→stg→prd(v1) 기록은 stg 퇴역으로 읽기 전용입니다. 시험·승인을 보낼 수 없습니다.':test?`${userTestEnv(r)} 시험을 실제로 수행한 뒤 결과를 기록하세요.`:approve?'사용자 시험 기록과 승인 대상 지문을 확인하세요.':'현재 상태에서는 시험·승인을 보낼 수 없습니다.';
}
function renderTests(t,added,record={}) {
  put('test-results',facts([['수정 전 재현 실패',yesNo(t.before_failed)],['수정 후 통과',yesNo(t.after_passed)],['회귀 시험 수행',yesNo(t.regression_executed)],['전체 시험 수',count(t.total)],['실패 수',count(t.failed)]]),
    facts([['실행기 오류',codeKo(record.error_code)],['수락 거부',codeKo(record.rejection_code)]]));
  rawView('raw-tests',['test-details'],[t,added,record],()=>{
    const normalized=list(t.executed_tests).map(x=>typeof x==='string'?{id:x}:obj(x));
    put('test-details',facts([['시험 보고서 sha256',t.report_sha256],['실행기 오류 코드',record.error_code],['수락 거부 코드',record.rejection_code]]),
      table(['시험 ID','파일','실행 구간','실제 상태','추가 시험'],normalized.map(x=>[x.id,x.file,x.phase==='before'?'수정 전':x.phase==='after'?'수정 후':value(x.phase),label(TEST_LABELS,x.status,'상태 확인 필요'),added.includes(x.id)?'추가 시험':'미관측'])),el('pre',{},JSON.stringify(record,null,2)));
  });
}
function renderEnvironments(r) {
  // 활성 환경(dev, prd)을 기본으로 보이고, 기존 기록에 stg 결과가 실제로 있을 때만 퇴역 stg 행을 과거 순서 자리에 덧붙인다.
  const results=obj(r.stage_results);
  const envs=[...ACTIVE_ENVIRONMENTS];
  for (const env of RETIRED_ENVIRONMENTS) if (Object.hasOwn(results,env)) envs.splice(1,0,env);
  put('env-status',envs.map(env=>{
    const s=obj(results[env]);
    return el('div',{class:'env-result','data-env':env},el('h4',{},envLabel(env)),facts([['배포 요청 결과',s.deployed===true?'적용 확인':s.deployed===false?'실패':'미관측'],['업무 확인 결과',s.verified===true?'별도 확인 통과':s.verified===false?'미확인':'미관측'],['쓰기 결과',label(WRITE_LABELS,s.write_outcome)],['오류',errorKo(s.error)]]));
  }));
  rawJson('raw-environments','env-raw',results);
}
$('refresh').addEventListener('click',async()=>{
  clearError();await prerequisites();try{await loadList();if(selectedId)await refreshDetail();}catch(e){showError(e);}
});
$('list-filter').addEventListener('submit',async e=>{
  e.preventDefault();
  const filters = readFilters();
  if (filters.start && filters.end && filters.start > filters.end) {
    showError(uiError('종료일은 시작일 이후여야 합니다.'));
    return;
  }
  clearError();
  try { await loadList({offset:0,filters,pinned:false}); } catch(err) { showError(err); }
});
$('list-prev').addEventListener('click',async()=>{try{await loadList({offset:Math.max(0,listState.offset-PAGE_SIZE),pinned:listState.offset-PAGE_SIZE>0});}catch(e){showError(e);}});
$('list-next').addEventListener('click',async()=>{try{await loadList({offset:listState.offset+PAGE_SIZE,pinned:true});}catch(e){showError(e);}});
$('list-newest').addEventListener('click',async()=>{try{await loadList({offset:0,pinned:false});}catch(e){showError(e);}});
// 자동 갱신: 사용자가 쓰는 중인 입력(시험 기록·토큰·필터)은 건드리지 않는다. 숨겨진 탭·전송 중에는 쉬고, 실패는 상태줄에만 알린다.
let polling = false;
async function poll() {
  if (polling || busy || document.hidden || !$('auto-refresh').checked) return;
  polling = true;
  try {
    await loadReadiness();
    await loadList(); if(selectedId) await refreshDetail();
  } catch(e) { status('자동 갱신에 실패했습니다. 다음 주기에 다시 시도합니다.'); }
  finally { polling = false; }
}
setInterval(()=>void poll(), POLL_MS);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) void poll(); });
$('create-form').addEventListener('submit',async e=>{
  e.preventDefault();if(!hasReadRole||busy||!catalogReady)return;
  const rev=revision;busy=true;gates();clearError();
  try {
    const d=await api('/incidents',{method:'POST',body:{environment:$('create-environment').value,service:$('create-service').value,reason:$('create-reason').value.trim()}});
    if(rev!==revision)return;
    $('create-reason').value='';$('create-panel').open=false;await loadList({offset:0,pinned:false});await selectIncident(d.id);status('사건을 생성했습니다. 최신 진행 상태를 확인하세요.');
  }catch(e){if(rev===revision)showError(e);}finally{busy=false;gates();}
});
async function attest(kind) {
  if(busy||!current)return;
  const r=release(),id=current.id,rev=revision,isTest=kind==='user-test';
  // 버튼 비활성화와 별개로 전송 직전에도 다시 확인한다: v1 기록(퇴역 stg 절차)은 시험·승인을 보내지 않는다.
  if(!exactRelease(r)||isLegacyRelease(r)||(!isTest && (!$('approval-target').open||!validUserTest(r)||!$('approve-confirm').checked||confirmedFor!==approvalKey(r))))return;
  if(!permits(isTest?'tester':'prd-approver'))return;
  const body={...fingerprint(r),...(isTest?{passed:$('test-passed').value==='true',notes_ko:$('test-notes').value.trim()}:{user_test_id:r.user_test.id})};
  busy=true;gates();clearError();
  try {
    await api(`/incidents/${encodeURIComponent(id)}/${isTest?'user-test':'approve-prd'}`,{method:'POST',body});
    if(rev!==revision)return;
    await selectIncident(id);if(isTest){$('test-notes').value='';$('test-passed').value='';}
    status(isTest?`${userTestEnv(r)} 시험 결과를 기록했습니다.`:'명시적 prd 승인 요청을 기록했습니다. 배포·업무 확인은 별도입니다.');
  }catch(e){if(rev===revision)showError(e);}finally{busy=false;gates();}
}
$('user-test-form').addEventListener('submit',e=>{e.preventDefault();if(!$('user-test-submit').disabled)void attest('user-test');});
$('approve-form').addEventListener('submit',e=>{e.preventDefault();if(!$('approve-submit').disabled)void attest('approve-prd');});
$('approve-confirm').addEventListener('change',()=>{confirmedFor=$('approve-confirm').checked&&current?approvalKey(release()):null;gates();});
renderAccess();
// Preview is selected once, without moving keyboard focus or changing the active filter.
void prerequisites().then(async()=>{
  const initialSeq=listSeq+1;
  const initialRevision=revision;
  await loadList();
  if(selectedId || initialSeq!==listSeq || initialRevision!==revision) return;
  const first=$('incident-list').querySelector('.group-waiting_people[data-id]') || $('incident-list').querySelector('[data-id]');
  if(first) await selectIncident(first.dataset.id);
  else $('detail-title').textContent='표시할 사건이 없습니다';
}).catch(showError);
})();
