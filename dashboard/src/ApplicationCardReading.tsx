// Application 단계 데이터 공급부: `?view=stage` 문서와 활동 기록을 회차별로 풀어 카드 본문(StageCard)에 넘긴다.
import {useState, type ReactNode} from 'react';
import {Link} from 'react-router-dom';
import {usePolling} from './usePolling';
import {useHumanAccess} from './HumanAccess';
import type {AppStepContext} from './SemanticStagePanel';
import {applicationFlow,isAppStep} from './applicationFlow';
import type {SemanticKind, SemanticNode} from './semanticFlow';
import {ApplicationTraceView, type WorkEvent} from './ApplicationWorkTrace';
import {type Data, type ApplicationIncident, object, observedTime, word} from './unified';
import {Disclosure} from './Disclosure';
import {ReleaseEvidence} from './ReleaseEvidence';
import {records} from './releaseEvidenceModel';
import {testComparison} from './stageTests';
import {appObservations, ApplicationEvidence} from './ApplicationEvidence';
import {CurrentVersionCheck} from './CurrentVersionCheck';
import {NativeAccountability} from './NativeAccountability';
import {effortKo} from './labels';
import {APP_ROLES, approvalHold, type AppSource, type CallRow} from './stageBrief';
import {DataSection, Reading} from './StageBriefView';
import {readingOf} from './readingText';

const stamp=(value:unknown)=>typeof value==='number'?observedTime(value):'미관측';
const imageText=(value:unknown)=>word(value)?.match(/@sha256:([a-f0-9]{64})$/)?.[1].slice(0,12) ?? '미관측';
const PUBLIC_WORK=['role_started','role_progress','runner_progress','job_claimed','job_queued','investigation_started'];

export type AppStageData = {
  source: AppSource; doc: ApplicationIncident; app: ApplicationIncident; events: WorkEvent[];
  round: number; current: boolean; roundPicker: ReactNode; readOnly: boolean;
  patch: Data; release: Data; build: Data; lastCheck?: Data; selectedIds: Set<number>;
  context: AppStepContext; onChange: () => void;
  hold: (kind: SemanticKind) => string | null;
  hasDecision: (kind: SemanticKind) => boolean;
  decision: (kind: SemanticKind) => ReactNode;
};

function FileExcerpt({id,path,digest}:{id:string;path:string;digest:string}) {
  const url='/application/v1/incidents/'+encodeURIComponent(id)+'/patch-change?'+new URLSearchParams({path,digest});
  const result=usePolling<{file:string;diff:string|null;truncated:boolean}>(url,600000);
  if(result.data?.file!==path || !result.data.diff)return <p>{result.error || result.data?'이 버전의 파일 변경 원문 미관측':'파일 변경을 확인하고 있습니다.'}</p>;
  return <><pre className="raw-text">{result.data.diff}</pre>{result.data.truncated && <p>긴 변경은 앞부분만 표시합니다.</p>}</>;
}
export function ChangedFiles({paths,patch,build,release,events,doc}:{paths:string[];patch:Data;build:Data;release:Data;events:WorkEvent[];doc:ApplicationIncident}) {
  const [picked,choose]=useState('');
  const file=paths.includes(picked)?picked:paths[0];
  if(!file)return null;
  const edit=events.filter(e=>e.data.file===file && e.data.phase==='edit_file').at(-1);
  const runs=records(object(release.tests ?? build.tests).executed_tests).filter(run=>run.file===file);
  const results=testComparison(patch,release,build,doc.service).rows.filter(row=>runs.some(run=>run.id===row.id));
  const detail=word(patch.diff_sha256);
  const why=readingOf(edit?.data.change_ko || patch.summary_ko,{state:'ready'});
  return <section><label className="evidence-chooser">파일 선택 <select value={file} onChange={e=>choose(e.target.value)}>
    {paths.map(path=><option key={path} value={path}>{path}</option>)}</select></label>
    <dl className="stage-facts"><div><dt>파일이 속한 수정안</dt><dd>{why && 'text' in why ? why.text : '수정 이유 미기록'}</dd></div>
      <div><dt>같은 파일의 시험</dt><dd>{results.length?results.map(row=><p key={row.id}>{row.label} · 수정 전 {row.before} → 수정 후 {row.after}</p>):'파일에 연결된 전후 시험 미관측'}</dd></div></dl>
    {detail && <Disclosure key={file+detail} label="코드 변경 상세보기">{()=> <FileExcerpt id={doc.id} path={file} digest={detail}/>}</Disclosure>}</section>;
}
function TestMatrix({patch,release,build,service}:{patch:Data;release:Data;build:Data;service:string}) {
  const result=testComparison(patch,release,build,service);
  return <>{result.rows.length ? <table className="semantic-test-matrix"><caption>동일 시험의 수정 전후 결과</caption>
      <thead><tr><th scope="col">검증한 동작</th><th scope="col">수정 전</th><th scope="col">수정 후</th></tr></thead>
      <tbody>{result.rows.map(row=><tr key={row.id}><th scope="row">{row.label}<small>{row.id}</small></th><td>{row.before}</td><td>{row.after}</td></tr>)}</tbody>
    </table> : <p>수정 전후 동일 시험의 결과 미관측</p>}
    {word(object(release.artifact).image) && <p>생성 이미지 · {imageText(object(release.artifact).image)}</p>}</>;
}

/** The expanded row of one Application role call: its full judgement, gaps and follow-up questions. */
export function AppCallBody({call,data}:{call:CallRow;data:AppStageData}) {
  const event=data.events.find(e=>String(e.id)===call.ref);
  if(!event)return null;
  const meta=event.data, readings=meta.readings;
  const state=(key:string)=>readings?.[key]?.state ?? (readings?'ready':undefined);
  const request=data.events.find(e=>e.event==='role_started' && e.data.call_id && e.data.call_id===meta.call_id)?.data.request_ko;
  return <>
    {meta.role==='verifier' && <p className="call-verdict"><strong>{meta.accepted===true?'원인 근거 수락':meta.accepted===false?'원인 근거 반려':'독립 판정 미관측'}</strong></p>}
    <Reading view={readingOf(meta.reason_ko || meta.summary_ko,{state:state('summary_ko')})} className="call-full"/>
    {typeof meta.confidence==='number' && <p>판단 신뢰도 {Math.round(meta.confidence*100)}%</p>}
    {!!meta.unresolved?.length && <div><h4>부족한 근거</h4>{meta.unresolved.map((_,i)=><Reading key={i} view={readingOf(meta.unresolved_ko?.[i],{state:state('unresolved/'+i)})}/>)}</div>}
    {!!meta.follow_up?.length && <div><h4>남은 조사 질문</h4>{meta.follow_up.map((_,i)=><Reading key={i} view={readingOf(meta.follow_up_ko?.[i],{state:state('follow_up/'+i)})}/>)}</div>}
    {request && <p><b>전달한 요청</b> {request}</p>}
  </>;
}

/** Application data below the brief, by stage kind. */
export function AppDataSections({node,data,hasCalls,origin=''}:{node:SemanticNode;data:AppStageData;hasCalls:boolean;origin?:string}) {
  const {kind}=node.semantic, {doc,app,events,patch,release,build,context,selectedIds,current,readOnly}=data;
  const repair=kind==='action' && node.semantic.members.some(m=>isAppStep(m) && (m.role==='patch' || ['patching','patch_proposed'].includes(m.app.state) || !!m.app.fileEventIds?.length));
  const changes=Array.isArray(patch.changed_files)?patch.changed_files.filter((path):path is string=>typeof path==='string'):[];
  const publicWork=events.some(e=>PUBLIC_WORK.includes(e.event));
  const teamCalls=kind==='routing'?applicationFlow(context.activity.view,context.summary).nodes.filter(step=>step.role && (step.app.round??1)===data.round):[];
  const teamIds=new Set(teamCalls.flatMap(step=>step.app.eventIds));
  const team=(context.activity.view?.events ?? []).filter(e=>e.event==='role_completed' && teamIds.has(e.id));
  const recent=data.source.observations.filter(o=>o.at!=null).slice(-5).reverse();
  return <>
    {kind==='detection' && recent.length>0 && <DataSection label={'최근 관측'+origin} count={recent.length}>{()=> <ul className="stage-observations">{recent.map(o=><li key={o.id}>
      <strong>{o.service ? o.service+' · ' : ''}{o.source}</strong><Reading view={readingOf(o.ko,{state:o.state})}/><time>{new Date(o.at!).toLocaleString('ko-KR')}</time></li>)}</ul>}</DataSection>}
    {kind==='evidence' && <DataSection label={'시간순 관측 기록'+origin} count={data.source.observations.length}>{()=> <ApplicationEvidence application={doc}/>}</DataSection>}
    {kind==='routing' && team.length>0 && !hasCalls && <DataSection label="당시 호출한 모델과 역할 순서" count={team.length}>{()=> <><ol className="stage-timeline">{team.map(e=><li key={e.id}>
      <b>{APP_ROLES[e.data.role || ''] || '에이전트'}</b> · <span className="model-tag">{e.data.model || '호출 모델 미관측'} · {effortKo(e.data.effort)}</span> <time>{stamp(e.at)}</time></li>)}</ol>
      <Link to="/settings">현재 모델 설정 확인</Link>{events.some(e=>e.event==='analysis_retry') && <p>분류 재시도 기록 있음</p>}</>}</DataSection>}
    {kind==='routing' && hasCalls && <p className="stage-data-link"><Link to="/settings">현재 모델 설정 확인</Link></p>}
    {repair && changes.length>0 && <DataSection label="변경 파일" count={changes.length}>{()=> <ChangedFiles key={data.round} paths={changes} build={build} release={release} events={events} patch={patch} doc={doc}/>}</DataSection>}
    {kind==='test_build' && <DataSection label="전후 결과 표" count={testComparison(patch,release,build,doc.service).rows.length}>{()=> <TestMatrix patch={patch} release={release} build={build} service={doc.service}/>}</DataSection>}
    {kind==='dev_validation' && (release.protocol!=='dev-prd/v2' || !!release.qa) && <DataSection label="dev 업무 검증">{()=> <ReleaseEvidence release={release} application={app} focused initialView="qa"/>}</DataSection>}
    {kind==='approval' && <DataSection label="위험 정보">{()=> <ReleaseEvidence release={release} application={app} focused initialView="risk"/>}</DataSection>}
    {kind==='recovery' && (!data.lastCheck || !!release.recovery) && <DataSection label="복구 점검">{()=> <ReleaseEvidence release={release} application={app} focused initialView="recovery"/>}</DataSection>}
    {publicWork && <DataSection label="공개 작업 기록" defaultOpen={node.status==='running'}>{()=> <ApplicationTraceView activity={context.activity} filter={event=>selectedIds.has(event.id)}/>}</DataSection>}
    {current && !readOnly && kind==='dev_validation' && <CurrentVersionCheck id={doc.id} releaseId={word(release.release_id)} contacts={context.activity.view?.active_jobs} onChange={data.onChange}/>}
  </>;
}

function Resolver({node,context,doc,readOnly,onChange,children}:{node:SemanticNode;context:AppStepContext;doc:ApplicationIncident;readOnly:boolean;onChange:()=>void;children:(data:AppStageData)=>ReactNode}) {
  const {members}=node.semantic, {activity,summary}=context;
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
  const roundIds=new Set(applicationFlow(activity.view,summary).nodes.filter(step=>(step.app.round??currentRound)===round).flatMap(step=>step.app.eventIds));
  const roundEvents=allEvents.filter(e=>roundIds.has(e.id) || selectedIds.has(e.id));
  const app={...doc,data:{unified_origin:doc.data.unified_origin,...saved,rounds:[]}};
  const observations=appObservations(doc).map(o=>({id:String(o.entry.id ?? ''),source:String(o.entry.source ?? ''),service:word(o.entry.service),at:o.at,ko:o.ko,state:o.state}));
  const lastCheck=current?records(doc.data.current_version_checks).filter(check=>check.release_id===(release.release_id??null)).at(-1):undefined;
  const check=(kind:SemanticKind)=>current && !readOnly && (kind==='approval' || kind==='recovery');
  const data:AppStageData={
    source:{doc,saved,events,roundEvents,observations,summary},
    doc,app,events,round,current,readOnly,patch,release,build,lastCheck,selectedIds,context,onChange,
    roundPicker:available.length>1 ? <label className="evidence-chooser stage-round">조사·수정 회차 <select value={round} onChange={e=>setRound(Number(e.target.value))}>
      {available.map(r=><option key={r} value={r}>{r}회차{r===currentRound?' · 현재':''}</option>)}</select></label> : null,
    hold:kind=>kind==='approval' ? approvalHold(app,release) : null,
    hasDecision:kind=>check(kind) || kind==='result',
    decision:kind=><>{kind==='result' && <NativeAccountability id={doc.id} readOnly={readOnly} part="decision-first"/>}
      {check(kind) && <CurrentVersionCheck id={doc.id} releaseId={word(release.release_id)} contacts={activity.view?.active_jobs} onChange={onChange}/>}</>,
  };
  return <>{children(data)}</>;
}
function Loader(props:{node:SemanticNode;context:AppStepContext;readOnly:boolean;onChange:()=>void;children:(data:AppStageData|null,status:ReactNode)=>ReactNode}) {
  const id=props.context.summary.id;
  const query=usePolling<ApplicationIncident>('/application/v1/incidents/'+encodeURIComponent(id)+'?view=stage',12000);
  if (!query.data || query.data.id!==id) return <>{props.children(null,<p role="status">{query.error?'저장된 단계 내용을 확인하지 못했습니다.':'단계 기록을 확인하고 있습니다.'}</p>)}</>;
  const stale=query.error ? <p role="status">단계 갱신 지연 · 마지막 관측을 표시합니다.</p> : null;
  return <Resolver key={props.node.id} node={props.node} context={props.context} doc={query.data} readOnly={props.readOnly}
    onChange={()=>{query.refresh();props.onChange();}}>{data=>props.children(data,stale)}</Resolver>;
}
/** Resolves the Application half of a stage (access, stored document, round) and hands it to the card body. */
export function ApplicationStageSource(props:{node:SemanticNode;context:AppStepContext;readOnly:boolean;onChange:()=>void;children:(data:AppStageData|null,status:ReactNode)=>ReactNode}) {
  const auth=useHumanAccess('/application/v1');
  if (!auth.connected) return <>{props.children(null,<p>로컬 관제에서 단계 내용을 확인할 수 있습니다.</p>)}</>;
  return <Loader {...props}/>;
}
