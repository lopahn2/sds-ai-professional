import {useState} from 'react';
import {Link} from 'react-router-dom';
import {usePolling} from './usePolling';
import {useHumanAccess} from './HumanAccess';
import type {AppStepContext} from './SemanticStagePanel';
import {applicationFlow,isAppStep} from './applicationFlow';
import type {SemanticNode} from './semanticFlow';
import {appRoles, ApplicationTraceView, type WorkEvent} from './ApplicationWorkTrace';
import {type Data, type ApplicationIncident, object, observedTime, word, releaseProof, matchingFingerprint} from './unified';
import {reading, timeText} from './StageReadings';
import {readingStatus} from './readingStatus';
import {Disclosure} from './Disclosure';
import {ReleaseEvidence} from './ReleaseEvidence';
import {records} from './releaseEvidenceModel';
import {testComparison} from './stageTests';
import {appObservations, ApplicationEvidence} from './ApplicationEvidence';
import {CurrentVersionCheck} from './CurrentVersionCheck';
import {NativeAccountability} from './NativeAccountability';
import {effortKo} from './labels';

const stamp=(value:unknown)=>typeof value==='number'?observedTime(value):typeof value==='string'?timeText(value):'미관측';
const imageText=(value:unknown)=>word(value)?.match(/@sha256:([a-f0-9]{64})$/)?.[1].slice(0,12) ?? '미관측';
function SourceCitations({row,observations}:{row:WorkEvent;observations:ReturnType<typeof appObservations>}) {
  if (!row.data.citations?.length) return null;
  return <ul className="stage-observations">{row.data.citations.map((cite,i)=>{
    const source=observations.find(item=>item.entry.id===cite.ref);
    return <li key={i}><strong>{source?.tag || cite.ref}</strong>{source && <span>{source.answer}</span>}
      {cite.quote && <Disclosure label="인용 상세보기">{()=> <pre className="raw-text">{cite.quote}</pre>}</Disclosure>}</li>;
  })}</ul>;
}
function ModelVerdict({event,observations,kind}:{event:WorkEvent;observations:ReturnType<typeof appObservations>;kind:'verification'|'investigation'}) {
  const meta=event.data;
  const description=reading(meta.reason_ko || meta.summary_ko,meta.readings?.summary_ko?readingStatus(meta.readings.summary_ko.state):'판단 요약 미관측');
  return <li><div className="timeline-meta"><b>{appRoles[meta.role || ''] || '조사 역할'}</b><time>{stamp(event.at)}</time></div>
    {kind==='verification' && <strong>{meta.accepted===true?'원인 근거 수락':meta.accepted===false?'원인 근거 반려':'독립 판정 미관측'}</strong>}
    <p>{description}</p>
    {kind==='investigation' && typeof meta.confidence==='number' && <p>판단 신뢰도 {Math.round(meta.confidence*100)}%</p>}
    {meta.model && <small>당시 호출 {meta.model} · {effortKo(meta.effort)}</small>}
    {!!meta.unresolved?.length && <div><h4>부족한 근거</h4>{meta.unresolved.map((_,i)=><p key={i}>{reading(meta.unresolved_ko?.[i],readingStatus(meta.readings?.['unresolved/'+i]?.state))}</p>)}</div>}
    {!!meta.follow_up?.length && <div><h4>남은 조사 질문</h4>{meta.follow_up.map((_,i)=><p key={i}>{reading(meta.follow_up_ko?.[i],readingStatus(meta.readings?.['follow_up/'+i]?.state))}</p>)}</div>}
    <SourceCitations row={event} observations={observations}/>
  </li>;
}
function FileExcerpt({id,path,digest}:{id:string;path:string;digest:string}) {
  const url='/application/v1/incidents/'+encodeURIComponent(id)+'/patch-change?'+new URLSearchParams({path,digest});
  const result=usePolling<{file:string;diff:string|null;truncated:boolean}>(url,600000);
  if(result.data?.file!==path || !result.data.diff)return <p>{result.error || result.data?'이 버전의 파일 변경 원문 미관측':'파일 변경을 확인하고 있습니다.'}</p>;
  return <><pre className="raw-text">{result.data.diff}</pre>{result.data.truncated && <p>긴 변경은 앞부분만 표시합니다.</p>}</>;
}
function ChangedFiles({paths,patch,build,release,events,doc}:{paths:string[];patch:Data;build:Data;release:Data;events:WorkEvent[];doc:ApplicationIncident}) {
  const [picked,choose]=useState('');
  const file=paths.includes(picked)?picked:paths[0];
  if(!file)return null;
  const edit=events.filter(e=>e.data.file===file && e.data.phase==='edit_file').at(-1);
  const runs=records(object(release.tests ?? build.tests).executed_tests).filter(run=>run.file===file);
  const results=testComparison(patch,release,build,doc.service).rows.filter(row=>runs.some(run=>run.id===row.id));
  const detail=word(patch.diff_sha256);
  return <section><h3>변경 파일</h3><label className="evidence-chooser">파일 선택 <select value={file} onChange={e=>choose(e.target.value)}>
    {paths.map(path=><option key={path} value={path}>{path}</option>)}</select></label>
    <dl className="stage-facts"><dt>파일이 속한 수정안</dt><dd>{reading(edit?.data.change_ko || patch.summary_ko,'수정 이유 미기록')}</dd>
      <dt>같은 파일의 시험</dt><dd>{results.length?results.map(row=><p key={row.id}>{row.label} · 수정 전 {row.before} → 수정 후 {row.after}</p>):'파일에 연결된 전후 시험 미관측'}</dd></dl>
    {detail && <Disclosure key={file+detail} label="코드 변경 상세보기">{()=> <FileExcerpt id={doc.id} path={file} digest={detail}/>}</Disclosure>}</section>;
}
function BuildReading({patch,release,build,service}:{patch:Data;release:Data;build:Data;service:string}) {
  const result=testComparison(patch,release,build,service);
  return <>
    {result.rows.length ? <table className="semantic-test-matrix"><caption>동일 시험의 수정 전후 결과</caption>
      <thead><tr><th scope="col">검증한 동작</th><th scope="col">수정 전</th><th scope="col">수정 후</th></tr></thead>
      <tbody>{result.rows.map(row=><tr key={row.id}><th scope="row">{row.label}<small>{row.id}</small></th><td>{row.before}</td><td>{row.after}</td></tr>)}</tbody>
    </table> : <p className="step-lead">수정 전후 동일 시험의 결과 미관측</p>}
    <dl className="stage-facts"><dt>AI가 추가한 재현 시험</dt><dd>{result.added==null?'미관측':result.added+'개'}</dd>
      <dt>전체 시험</dt><dd>{result.total==null || result.failed==null?'미관측':result.passed!=null?
        result.total+'개 중 '+result.passed+'개 통과 · 실패 '+result.failed+'개':result.failed===0?
        result.total+'개 실행 · 실패 0개':result.total+'개 실행 · 실패 '+result.failed+'개'}</dd>
      <dt>빌드</dt><dd>{build.accepted===true || word(object(release.artifact).build_receipt_sha256)?'산출물 확인':build.accepted===false?'산출물 검증 실패':'산출물 미관측'}</dd>
      {word(object(release.artifact).image) && <><dt>생성 이미지</dt><dd>{imageText(object(release.artifact).image)}</dd></>}</dl>
  </>;
}
function ApprovalReading({app,release}:{app:ApplicationIncident;release:Data}) {
  const merge=object(app.data.merge_review),approval=object(release.approval),tested=object(release.user_test);
  const linked=!!object(app.data.unified_origin).action_id;
  const bound=merge.human_merge_observed===true && matchingFingerprint(merge,release);
  const valid=bound && typeof merge.expires_at==='number' && merge.expires_at*1000>Date.now();
  let message='승인 결과 미관측';
  if (approval.subject) message='prd 승인이 기록됐습니다.';
  else if (release.promotion_disposition==='unnecessary') message='현재 버전의 복구를 확인해 이전 릴리스 승격을 종료했습니다.';
  else if (!linked) message='사람의 PR 병합을 검증할 연결 정보가 없어 prd 승인이 보류됐습니다.';
  else if (!releaseProof(release)) message='유효한 시험·버전·변경 근거가 부족해 prd 승인이 보류됐습니다.';
  else if (!valid) message='같은 버전의 PR 병합 확인이 필요합니다.';
  else if(app.state==='awaiting_prd_approval')message='검증된 버전의 prd 승인이 필요합니다.';
  return <><p className="step-lead">{message}</p>
    <dl className="stage-facts"><dt>PR 병합 확인</dt><dd>{bound?'확인 기록 있음':'미관측'}{typeof merge.observed_at==='number' && ' · '+stamp(merge.observed_at)}</dd>
      <dt>prd 승인</dt><dd>{approval.subject?String(approval.subject)+' · '+stamp(approval.occurred_at):'미기록'}</dd>
      {tested.kind==='human' && <><dt>당시 dev 수동 시험</dt><dd>{tested.passed===true?'통과':tested.passed===false?'실패':'결과 미관측'} · {stamp(tested.occurred_at)}</dd></>}
    </dl><ReleaseEvidence release={release} application={app} focused initialView="risk"/></>;
}
function AppContents({node,context,doc,onChange,readOnly}:{node:SemanticNode;context:AppStepContext;doc:ApplicationIncident;onChange:()=>void;readOnly:boolean}) {
  const {kind,members}=node.semantic;
  const {activity,summary}=context;
  const appMembers=members.filter(isAppStep);
  const allEvents=activity.view?.events ?? [];
  const rounds=records(doc.data.rounds);
  const currentRound=typeof doc.data.round==='number'?doc.data.round:1;
  const available=[...new Set(appMembers.map(m=>m.app.round??currentRound))].sort((a,b)=>a-b);
  const [picked,setRound]=useState<number|null>(null);
  const round=picked!=null && available.includes(picked)?picked:available.at(-1)??currentRound;
  const current=round===currentRound, archived=rounds.find(row=>row.round===round);
  const saved=current?doc.data:object(archived);
  const patch=object(saved.patch),release=object(saved.release),build=object(saved.test_build);
  const selectedIds=new Set(appMembers.filter(m=>(m.app.round??currentRound)===round).flatMap(n=>n.app.eventIds));
  const events=allEvents.filter(e=>selectedIds.has(e.id));
  const teamCalls=kind==='routing'?applicationFlow(activity.view,summary).nodes.filter(step=>step.role && (step.app.round??1)===round):[];
  const teamIds=new Set(teamCalls.flatMap(step=>step.app.eventIds));
  const roles=events.filter(e=>e.event==='role_completed');
  const app={...doc,data:{unified_origin:doc.data.unified_origin,...saved,rounds:[]}};
  const observations=appObservations(doc);
  const latest=observations.filter(item=>item.at!=null).at(-1);
  const repair=kind==='action' && appMembers.some(m=>m.role==='patch' || ['patching','patch_proposed'].includes(m.app.state) || m.app.fileEventIds?.length);
  const changes=Array.isArray(patch.changed_files)?patch.changed_files.filter((path):path is string=>typeof path==='string'):[];
  const lastCheck=current?records(doc.data.current_version_checks).filter(check=>check.release_id===(release.release_id??null)).at(-1):undefined;
  const publicWork=events.some(e=>e.event==='role_started' || e.event==='role_progress' || e.event==='runner_progress' || e.event==='job_claimed' || e.event==='job_queued' || e.event==='investigation_started');
  const handoff=object((doc as ApplicationIncident & {handoff_classification?:Data}).handoff_classification);
  const err=object((doc as ApplicationIncident & {error?:Data}).error);
  return <>
    {available.length>1 && <label className="evidence-chooser">조사·수정 회차 <select value={round} onChange={e=>setRound(Number(e.target.value))}>
      {available.map(r=><option key={r} value={r}>{r}회차{r===currentRound?' · 현재':''}</option>)}</select></label>}
    {kind==='detection' && <><p className="step-lead">{reading(summary.reason_ko ?? summary.reason,'감지 증상 미관측')}</p>
      <dl className="stage-facts"><dt>최초 감지</dt><dd>{stamp(summary.created_at)} · {doc.environment} / {doc.service}</dd>
        <dt>최근 관측</dt><dd>{latest ? latest.answer+' · '+stamp(latest.at!/1000):'후속 관측 미관측'}</dd>
        <dt>사용자 영향</dt><dd>영향 미관측</dd></dl></>}
    {kind==='evidence' && <ApplicationEvidence application={doc}/>}
    {kind==='routing' && <><p className="step-lead">{reading(roles.at(-1)?.data.reason_ko || roles.at(-1)?.data.summary_ko,'팀을 선택한 이유 미관측')}</p>
      <dl className="stage-facts"><dt>선택한 수준</dt><dd>{String(object(doc.data.analysis_routing).effective_level ?? roles.at(-1)?.data.level ?? '미관측')}</dd>
      <dt>분류 신뢰도</dt><dd>{typeof roles.at(-1)?.data.confidence==='number'?Math.round(roles.at(-1)!.data.confidence!*100)+'%':'미관측'}</dd></dl>
      <h3>당시 호출한 모델과 역할 순서</h3><ol className="stage-timeline">{allEvents.filter(e=>e.event==='role_completed' && teamIds.has(e.id)).map(e=><li key={e.id}>
        <b>{appRoles[e.data.role || ''] || '에이전트'}</b> · {e.data.model || '호출 모델 미관측'} · {effortKo(e.data.effort)} <time>{stamp(e.at)}</time></li>)}</ol>
      <Link to="/settings">현재 모델 설정 확인</Link>
      {events.some(e=>e.event==='analysis_retry') && <p>분류 재시도 기록 있음</p>}</>}
    {kind==='investigation' && <>
      {roles.filter(e=>e.data.role!=='critic').length>0 && <><h3>원인 후보와 분석 판단</h3><ol className="stage-timeline">
        {roles.filter(e=>e.data.role!=='critic').map(event=><ModelVerdict key={event.id} kind={kind} event={event} observations={observations}/>)}</ol></>}
      {roles.some(e=>e.data.role==='critic') && <><h3>반론과 남은 의문</h3><ol className="stage-timeline">
        {roles.filter(e=>e.data.role==='critic').map(event=><ModelVerdict key={event.id} kind={kind} event={event} observations={observations}/>)}</ol></>}
      {!roles.length && <p>{node.status==='running'?'원인 후보를 조사하고 있습니다.':'원인 후보 판단 미관측'}</p>}</>}
    {kind==='verification' && <>{roles.length?<ol className="stage-timeline">
      {roles.map(event=><ModelVerdict key={event.id} kind={kind} event={event} observations={observations}/>)}</ol>:<p>독립 판정 미관측</p>}</>}
    {kind==='action' && <><p className="step-lead">{repair?reading(patch.summary_ko || saved.patch_summary_ko || roles.at(-1)?.data.summary_ko,'코드 변경 설명 미관측'):appMembers.at(-1)?.label+' · '+doc.service}</p>
      {repair && <ChangedFiles key={round} paths={changes} build={build} release={release} events={events} patch={patch} doc={doc}/>}
      {repair && <p>영향 범위 · {reading(patch.risk_ko || release.risk_ko,'미관측')}</p>}
      {release.rollback_ko!=null && <p>원복 조건 · {reading(release.rollback_ko)}</p>}</>}
    {kind==='test_build' && <BuildReading patch={patch} release={release} build={build} service={doc.service}/>}
    {kind==='dev_validation' && <>
      {release.protocol==='dev-prd/v2' && <><p className="step-lead">{object(release.user_test).passed===true?'당시 dev 수동 확인 완료':object(release.user_test).passed===false?'당시 dev 수동 확인 실패':'당시 dev 수동 확인 미기록'} · 자동 사용자 여정 QA 미관측</p>
        {!!object(release.user_test).occurred_at && <p>{stamp(object(release.user_test).occurred_at)} · {word(object(release.user_test).subject) || '확인자 미관측'}</p>}</>}
      <p>dev 배포 검증 · {object(object(release.stage_results).dev).verified===true?'확인됨':'미관측'}</p>
      {(release.protocol!=='dev-prd/v2' || !!release.qa) && <ReleaseEvidence release={release} application={app} focused initialView="qa"/>}</>}
    {kind==='approval' && <ApprovalReading app={app} release={release}/>}
    {kind==='recovery' && <><p className="step-lead">{lastCheck?.status==='passed'?doc.environment+' 현재 버전에서 원래 업무 요청 정상 확인':
      lastCheck?.status==='failed'?doc.environment+' 현재 버전에서도 원래 업무 요청 실패':
      object(release.recovery).accepted===true && object(release.recovery).outcome==='passed'?'운영 업무 복구 확인':
      object(object(release.stage_results).prd).verified===true?'인프라 정상 · 업무 복구 미확인':'업무 복구 미확인'}</p>
      {(!lastCheck || !!release.recovery) && <ReleaseEvidence release={release} application={app} focused initialView="recovery"/>}</>}
    {kind==='result' && <>{lastCheck?.status==='passed' && <p className="step-lead">{doc.environment} 현재 버전의 업무 복구 관측 · {stamp(lastCheck.observed_at)}</p>}
      <NativeAccountability id={doc.id} readOnly={readOnly}/></>}
    {kind==='handoff' && <><p className="step-lead">{reading(handoff.label_ko,'운영자가 자동 처리 중단 사유를 확인하고 사건을 인수해야 합니다.')}</p>
      <p>{reading(err.message_ko,'인계 상세 사유 미관측')}</p><dl className="stage-facts"><dt>인수 대상</dt><dd>{doc.environment} / {doc.service}</dd>
      <dt>복구 시도</dt><dd>{handoff.recovery_attempted===true?'기록됨':handoff.recovery_attempted===false?'시도 기록 없음':'미관측'}</dd></dl></>}
    {publicWork && <ApplicationTraceView activity={activity} filter={event=>selectedIds.has(event.id)}/>}
    {current && !readOnly && ['dev_validation','approval','recovery','result'].includes(kind) && <CurrentVersionCheck id={doc.id} releaseId={word(release.release_id)} contacts={activity.view?.active_jobs} onChange={onChange}/>}
  </>;
}

function AppReader({node,context,onChange,readOnly}:{node:SemanticNode;context:AppStepContext;onChange:()=>void;readOnly:boolean}) {
  const id=context.summary.id;
  const query=usePolling<ApplicationIncident>('/application/v1/incidents/'+encodeURIComponent(id)+'?view=stage',12000);
  if (!query.data || query.data.id!==id) return <p role="status">{query.error?'저장된 단계 내용을 확인하지 못했습니다.':'단계 기록을 확인하고 있습니다.'}</p>;
  return <>{query.error && <p role="status">단계 갱신 지연 · 마지막 관측을 표시합니다.</p>}
    <AppContents key={node.id} node={node} context={context} doc={query.data} onChange={()=>{query.refresh();onChange();}} readOnly={readOnly}/></>;
}
export function ApplicationCardReading(props:{node:SemanticNode;context:AppStepContext;onChange:()=>void;readOnly:boolean}) {
  const auth=useHumanAccess('/application/v1');
  if (!auth.connected) return <p>로컬 관제에서 단계 내용을 확인할 수 있습니다.</p>;
  return <AppReader {...props}/>;
}
