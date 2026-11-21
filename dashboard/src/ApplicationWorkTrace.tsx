import { useEffect, useState } from 'react';
import { Disclosure } from './Disclosure';
import { request } from './api';
import { observedTime, applicationStateKo } from './unified';
import { summaryKo } from './presentation';
import { useHumanAccess } from './HumanAccess';

const phases: Record<string,string> = {inspect_resources:'대상 리소스 확인',read_logs:'장애 로그 조회',pin_source:'배포 이미지와 소스 대조',read_file:'소스 위치 확인',
  reproduce:'변경 전 장애 재현',edit_file:'파일 변경 적용',regression:'변경 후 회귀 시험',build:'시험한 소스 빌드',test_journey:'dev 사용자 여정 시험',
  observe_original_request:'prd 원래 요청 복구 관측',apply_release:'배포 대상 변경',verify_release:'배포 결과 확인',reconcile_write:'변경 결과 재조회',
  started:'모델 호출 시작',waiting:'응답 대기',streaming:'공개 답변 수신',succeeded:'응답 수신 완료',failed:'호출 실패'};
const jobs:Record<string,string>={collect:'클러스터 근거 수집',source:'소스 확인',test_build:'수정·재현 시험',qa:'dev QA',deploy:'배포',verify:'배포 검증',reconcile:'쓰기 결과 확인',recovery:'복구 관측',investigate:'소스 근거 탐색',current_version_check:'현재 버전 업무 재검증'};
export const appRoles:Record<string,string>={classifier:'사건 수준 판단',analyst:'원인 분석',specialist:'심층 원인 추론',critic:'반론 검토',verifier:'독립 검증',patch:'코드 수정'};
export type DisplayReading={state:string;ko:string|null};
export type WorkEvent={id:number; at:number; event:string; data:{job_id?:string; kind?:string; job_environment?:'dev'|'prd'; role?:string; call_id?:string; phase?:string; file?:string; attempt?:number; text?:string; summary_ko?:string; model?:string; reason_ko?:string; tests?:string[]; change_ko?:string; request_ko?:string; evidence_refs?:string[]; before_ko?:string; after_ko?:string; attempt_id?:string; effort?:string; risk?:string; level?:number; confidence?:number; accepted?:boolean; fixable?:boolean; readings?:Record<string,DisplayReading>; unresolved?:string[]; follow_up?:string[]; unresolved_ko?:(string|null)[]; follow_up_ko?:(string|null)[]; risk_ko?:string; citations?:{ref:string;quote:string}[]}};
export type RunnerContact={
  job_id:string; kind:string; attempt:number; state:'waiting'|'leased'|'reclaimed'|'contact_lost'|'unobserved';
  queued_at:number;claimed_at:number|null;last_heartbeat_at:number|null;last_contact_at:number|null;expires_at:number|null;
};
export type AppActivity={active_jobs?:RunnerContact[];id:string; state:string; stages:{state:string;occurred_at:number;round?:number}[];events:WorkEvent[];cursor:number;has_more:boolean};
/** Poll a monotonic replay cursor, catching up every page after reconnect. */
export function useAppActivity(id:string) {
  const {connected}=useHumanAccess('/application/v1');
  const [view,setView]=useState<AppActivity|null>(null), [error,setError]=useState(false);
  useEffect(()=>{
    setView(null);setError(false);
    if(!connected || !id) return;
    const cancel = new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    let cursor = 0;
    let refreshReadingsAt = 0;
    const events = new Map<number,WorkEvent>();
    async function poll() {
      try {
        let after = cursor;
        // Pending translations may change older role records under the same event ID.
        if (Date.now() >= refreshReadingsAt) {
          const pending = Array.from(events.values()).find(event =>
            Object.values(event.data.readings || {}).some(item => ['pending','running'].includes(item.state)));
          if (pending) after = pending.id - 1;
          refreshReadingsAt = Date.now() + 15000;
        }
        let page:AppActivity;
        do {
          page = await request<AppActivity>('/application/v1/incidents/'+encodeURIComponent(id)+'/activity?after='+after,{signal:cancel.signal});
          if(page.id!==id || !Array.isArray(page.events) || page.events.some((event,i)=>event.id<=(i?page.events[i-1].id:after)) ||
            (page.events.at(-1)?.id??after)!==page.cursor || (page.has_more && page.cursor===after)) throw Error('invalid_activity_cursor');
          for (const event of page.events) events.set(event.id,event);
          after = page.cursor;
        } while(page.has_more && !cancel.signal.aborted);
        if(!cancel.signal.aborted){
          cursor = Math.max(cursor,page.cursor);
          setView({...page,events:Array.from(events.values())});
          setError(false);
        }
      } catch {if(!cancel.signal.aborted)setError(true);}
      if(!cancel.signal.aborted) timer=setTimeout(poll,2500);
    }
    void poll();return ()=>{cancel.abort();clearTimeout(timer);};
  },[id,connected]);
  return {view:view?.id===id?view:null,error,connected};
}

function title(e:WorkEvent) {
  const d=e.data;
  if(e.event==='job_claimed')return (d.attempt || 0)>1?'실행기 재인수':'실행기 일감 인수';
  if(e.event==='investigation_started')return '추가 근거 탐색 시작';
  if(e.event==='investigation_finished')return '추가 근거 탐색 결과';
  return phases[d.phase||''] || (e.event==='job_queued'?'일감 대기':e.event==='job_reported'?'결과 보고':e.event==='job_cancelled'?'일감 취소':e.event==='role_completed'?'역할 응답 기록':e.event==='role_started'?'역할 호출':e.event==='analysis_retry'?'분석 재시도':applicationStateKo(e.event));
}
function ChangeExcerpt({incident,event}:{incident:string;event:number}) {
  const [excerpt,setExcerpt]=useState<{diff:string|null;truncated:boolean}|null>(null);
  const [failed,setFailed]=useState(false);
  useEffect(()=>{
    const cancel=new AbortController();
    request<{diff:string|null;truncated:boolean}>('/application/v1/incidents/'+encodeURIComponent(incident)+'/activity/'+event+'/change',
      {signal:cancel.signal}).then(data=>{if(!cancel.signal.aborted)setExcerpt(data);})
      .catch(()=>{if(!cancel.signal.aborted)setFailed(true);});
    return ()=>cancel.abort();
  },[incident,event]);
  return excerpt?.diff ? <><pre className="raw-text">{excerpt.diff}</pre>{excerpt.truncated && <p>긴 파일은 앞부분만 표시합니다.</p>}</>
    : <p>{failed?'변경 내용 조회 실패':excerpt?'변경 원문 미관측':'변경 내용 확인 중'}</p>;
}
const contactText:Record<RunnerContact['state'],string> = {
  waiting:'실행기 연결 대기', leased:'실행기가 일감을 인수했습니다', reclaimed:'실행기가 일감을 다시 인수했습니다',
  contact_lost:'실행기 연락 끊김 · 재인수 대기', unobserved:'실행기 접촉 미관측',
};

function RunnerStatus({job}:{job:RunnerContact}) {
  return <div className="runner-contact" role="status">
    <p><b>{jobs[job.kind] || '실행기 작업'} · {contactText[job.state]}</b>{job.attempt>0 && ' · 시도 '+job.attempt}</p>
    <p>{job.state==='waiting' ? '대기 시작 '+observedTime(job.queued_at)+' · 마지막 수신 '+observedTime(job.last_contact_at)
      : ['인수 '+observedTime(job.claimed_at),'마지막 수신 '+observedTime(job.last_contact_at),'하트비트 '+observedTime(job.last_heartbeat_at),
         '임대 만료 '+observedTime(job.expires_at)].join(' · ')}</p>
  </div>;
}

export function ApplicationTraceView({activity,filter}:{activity:ReturnType<typeof useAppActivity>;filter?:(e:WorkEvent)=>boolean}) {
  const {view,error,connected}=activity;
  const events=(view?.events||[]).filter(e=>e.event==='runner_progress'||e.event.startsWith('job_')||e.event.startsWith('role_')||e.event==='analysis_retry'||e.event==='investigation_started'||e.event==='investigation_finished').filter(filter||(()=>true));
  const [picked,pick]=useState<number|null>(null);
  const current=events.find(e=>e.id===picked)??events.at(-1);
  const role=current&&(appRoles[current.data.role||'']||jobs[current.data.kind||'']||'실행기');
  const checks=current?.data.job_id && view?.events.find(e=>e.event==='job_reported' && e.data.job_id===current.data.job_id && e.data.attempt===current.data.attempt)?.data;
  const contacts=(view?.active_jobs || []).filter(job=>!filter || events.some(event=>event.data.job_id===job.job_id));
  if(!connected)return <p role="status">로컬 관제에서 작업 기록을 열 수 있습니다.</p>;
  return <section className="app-work-trace" aria-label="실제 작업 경로"><header><h3>공개 작업 기록</h3></header>
    {error && <p role="status">관제 화면 재연결 중 · 마지막으로 받은 작업 기록을 표시합니다.</p>}
    {!error && picked===null && contacts.map(job=><RunnerStatus key={job.job_id} job={job}/>)}
    {current?.event==='investigation_started' && <p><b>조사 질문</b> {summaryKo(current.data.request_ko,'조사 질문 미관측',1200)}</p>}
    {current?.event==='investigation_finished' && <><p>{summaryKo(current.data.reason_ko,'탐색 결과 미관측',1200)}</p>
      {!!current.data.evidence_refs?.length && <p>찾은 근거 · {current.data.evidence_refs.join(' · ')}</p>}</>}
    {current?.data.call_id && (() => {
      const dispatch=events.find(e=>e.event==='role_started' && e.data.call_id===current.data.call_id);
      return dispatch?.data.request_ko ? <div className="app-call-request"><h4>에이전트에 전달한 요청</h4>
        <p>{dispatch.data.request_ko}</p>{!!dispatch.data.evidence_refs?.length && <p>전달 근거 · {dispatch.data.evidence_refs.join(' · ')}</p>}</div> : null;
    })()}
    {current ? <><div className="app-trace-now" key={current.id}><b>{role}</b><span aria-hidden>→</span><strong>{current.data.file||title(current)}</strong></div>
      <p>{current.data.file && <>{title(current)} · </>}{observedTime(current.at)}{current.data.attempt && ' · 시도 '+current.data.attempt}</p>
      {(current.data.text||current.data.summary_ko) && <p>{summaryKo(current.data.text||current.data.summary_ko,'공개 답변 미관측',1600)}</p>}
      {current.data.file && <><p>{summaryKo(current.data.change_ko,'변경 이유 미기록')}</p>{current.data.tests?.length ? <p>수정안의 검증 시험 · {current.data.tests.join(' · ')}</p> : null}
        {checks && <p>{[checks.before_ko,checks.after_ko].filter(Boolean).join(' · ')}</p>}
        {view && current.data.phase==='edit_file' && <Disclosure key={current.id} label="코드 변경 상세보기">{()=> <ChangeExcerpt incident={view.id} event={current.id}/>}</Disclosure>}</>}
      {events.length>1 && <label className="evidence-chooser">기록 시점 <select value={picked??''} onChange={e=>pick(e.target.value?Number(e.target.value):null)}>
        <option value="">최근 수신 기록</option>{events.map(e=><option key={e.id} value={e.id}>{observedTime(e.at)} · {title(e)}{e.data.file && ' · '+e.data.file}</option>)}</select></label>}
    </> : <p>{view?'이 단계에서 보고된 작업 기록이 없습니다.':error?'작업 기록 미관측':'작업 기록 확인 중'}</p>}
  </section>;
}
export function ApplicationWorkTrace({id}:{id:string}) {return <ApplicationTraceView activity={useAppActivity(id)}/>;}
