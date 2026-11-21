import {useEffect, useRef, useState} from 'react';
import {usePolling} from './usePolling';
import {ApiError, request} from './api';
import {authError} from './HumanAccess';
import {observedTime} from './unified';
import {summaryKo} from './presentation';
import type {RunnerContact} from './ApplicationWorkTrace';

type CheckView = {eligible:boolean;reason_ko:string|null;release_id:string|null;environment:string;service:string;latest:{
  request_id:string;job_id?:string;status:string;image:string|null;reason_ko:string|null;operation:string|null;
  observed_at:number|null;requested_at:number;promotion_unnecessary:boolean
}|null};
const labels:Record<string,string>={queued:'실행기 연결 대기',running:'현재 버전 검증 중',passed:'현재 버전 조치 확인',failed:'원래 동작 실패',unverifiable:'현재 버전 미확인'};

export function CurrentVersionCheck({id,releaseId,contacts,onChange}:{id:string;releaseId:string|null;contacts?:RunnerContact[];onChange:()=>void}) {
  const path='/application/v1/incidents/'+encodeURIComponent(id)+'/current-version-check';
  const result=usePolling<CheckView>(path, v=>['queued','running'].includes(v?.latest?.status || '')?2500:15000);
  const [asking,ask]=useState(false), [busy,setBusy]=useState(false), [error,setError]=useState('');
  const submitKey=useRef<string|null>(null);
  const checking=useRef(false);
  const latestId=result.data?.latest?.request_id;
  useEffect(()=>{if(latestId===submitKey.current){submitKey.current=null;ask(false);}},[latestId]);
  useEffect(()=>{ask(false);submitKey.current=null;setError('');},[id,releaseId]);
  const submit=async()=>{
    if(checking.current || !result.data?.eligible || result.data.release_id!==releaseId || result.error || !asking)return;
    checking.current=true;setBusy(true);setError('');
    const key=submitKey.current??crypto.randomUUID();submitKey.current=key;
    try {
      await request<CheckView>(path,{method:'POST',body:JSON.stringify({request_id:key,release_id:result.data.release_id})});
      ask(false);onChange();
    } catch(e) {
      setError(authError(e));
      if(e instanceof ApiError && e.status===409){ask(false);submitKey.current=null;}
    } finally {checking.current=false;setBusy(false);result.refresh();}
  };
  const view=result.data, run=view?.latest;
  const pending=run && ['queued','running'].includes(run.status);
  const contact=contacts?.find(job=>job.job_id===run?.job_id);
  if (result.errorStatus===404) return null;
  if(!view) return result.error?<p role="status">현재 버전 재검증 가능 여부를 확인하지 못했습니다.</p>:null;
  return <section className="current-version-check"><h3>현재 버전 재검증</h3>
    {run && <><strong>{labels[run.status] ?? '결과 미관측'}</strong>
      <p>{view.environment} · {view.service}{run.operation && ' · '+run.operation}</p>
      {run.image && <p>관측 이미지 <code>{run.image}</code></p>}
      <p>{run.observed_at ? '관측 '+observedTime(run.observed_at) : '접수 '+observedTime(run.requested_at)}
        {pending && ' · 실행기 마지막 수신 '+observedTime(contact?.last_contact_at??null)}</p>
      {run.reason_ko && <p>{summaryKo(run.reason_ko,'확인 근거 미관측')}</p>}
      {run.promotion_unnecessary && <p>기존 릴리스 승격 불필요 · 결정을 기록했습니다.</p>}</>}
    {!view.eligible && view.reason_ko && !pending && <><p>{summaryKo(view.reason_ko,'현재 재검증할 수 없습니다.')}</p><button type="button" className="btn sm" disabled>현재 버전 재검증</button></>}
    {view.eligible && view.release_id!==releaseId && <p role="status">최신 릴리스 기록을 확인하고 있습니다.</p>}
    {view.eligible && view.release_id===releaseId && !asking && <button type="button" className="btn sm" disabled={busy||result.error} onClick={()=>ask(true)}>현재 버전 재검증</button>}
    {asking && view.eligible && view.release_id===releaseId && <div className="code-gate"><p>{view.environment}에 현재 배포된 {view.service} 이미지에서 원래 실패한 동작을 확인합니다. 정상으로 검증되면 기존 승격 대기도 종료합니다.</p>
      <button type="button" className="btn sm primary" disabled={busy||result.error} onClick={()=>void submit()}>이 환경에서 재검증 요청</button>
      <button type="button" className="btn sm" disabled={busy} onClick={()=>ask(false)}>취소</button></div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
