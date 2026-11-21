import {useRef,useState,type FormEvent} from 'react';
import {HumanAccessForm,authError,aborted,useHumanAccess} from './HumanAccess';
import {ErrorBox} from './ui';
import {summaryKo} from './presentation';
import {followUpOnly,object,observedTime,qaProtocol,type ApplicationIncident,type Data} from './unified';
import {records} from './releaseEvidenceModel';

type Props={application:ApplicationIncident;path:string;prefix?:''|'/application/v1';fresh:boolean;reload:()=>Promise<ApplicationIncident>;onDone:(message?:string)=>void};
const statusText:Record<string,string>={pending:'안전한 실행 경계 대기',applied:'반영됨',superseded:'새 보완으로 대체',closed_before_apply:'종료 후 기록',recorded:'기록됨'};
function writeKey(a:ApplicationIncident) {
  const r=object(a.data.release);
  return JSON.stringify([a.id,a.state,a.data.round,r.release_id,r.plan_sha256,r.valid,r.protocol,object(r.qa).receipt_id]);
}
export function SupplementForm({application, path, prefix='', fresh, reload, onDone}:Props){
  const auth=useHumanAccess(prefix),closed=followUpOnly(application);
  const [kind,setKind]=useState<'revise'|'rerun_qa'>('revise'),[note,setNote]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const posting=useRef(false), retry=useRef<{key:string;id:string}|null>(null);
  const release=object(application.data.release);
  const canRerun=application.state==='qa_failed' && qaProtocol(release) && release.valid===true;
  const effective=canRerun?kind:'revise',title=closed?'후속 메모': '보완 요청';
  const history=[...records(application.data.supplements),...records(application.data.followups)].sort((a,b)=>Number(b.requested_at??0)-Number(a.requested_at??0));
  async function submit(event:FormEvent){
    event.preventDefault();
    const text=note.trim();
    if(!text||!fresh||!auth.permits('incident:create')||posting.current)return;
    const key=JSON.stringify([path,auth.session.subject,effective,text]);
    if(retry.current?.key!==key)retry.current={key,id:crypto.randomUUID()};
    const requestId=retry.current.id,generation=auth.generation.current;
    setBusy(true);posting.current=true;setError('');
    try{
      const latest=await reload();
      if(auth.generation.current!==generation||!auth.permits('incident:create'))return;
      if(latest.id!==application.id)throw Error('incident_identity_mismatch');
      const previous=[...records(latest.data.supplements),...records(latest.data.followups)].some(row=>row.request_id===requestId);
      if(!previous && writeKey(latest)!==writeKey(application)){setError('사건 상태나 회차가 바뀌었습니다. 최신 내용을 확인하고 다시 요청하세요.');onDone();return;}
      const result=await auth.call<{incident_id:string;followup?:Data;supplement?:Data}>(path,{method:'POST',body:JSON.stringify({request_id:requestId,kind:effective,note_ko:text})});
      const saved=result.followup??result.supplement;
      if(result.incident_id!==application.id||saved?.request_id!==requestId)throw Error('unexpected_supplement_receipt');
      const notice=result.followup?'종료 기록에 후속 메모를 남겼습니다.':saved.status==='pending'?'보완을 접수했습니다. 진행 중인 작업이 안전한 경계에 도달하면 반영합니다.':'보완 결과를 기록했습니다.';
      setNote('');retry.current=null;onDone(notice);
    }catch(ex){if(!aborted(ex)){setError(authError(ex));onDone();}}
    finally{posting.current=false;setBusy(false);}
  }
  return <section aria-label="운영자 보완"><h4>{title}</h4>
    <form className="code-gate" aria-label={title} onSubmit={submit}>
      <p>{closed?'종료 상태와 복구 기록을 유지한 채 이 사건에 메모를 남깁니다.':'요청한 내용을 같은 사건에 반영합니다. 수정본은 dev에서 다시 검증하고 prd 승인을 받습니다.'}</p>
      {canRerun&&<label>작업<select value={effective} disabled={busy} onChange={e=>setKind(e.target.value as typeof kind)}><option value="revise">내용 보완</option><option value="rerun_qa">현재 버전 dev QA 재실행</option></select></label>}
      <label>{closed?'메모 내용':'요청 내용'}<textarea rows={2} maxLength={4000} value={note} onChange={e=>setNote(e.target.value)} required disabled={busy}/></label>
      <HumanAccessForm auth={auth} scope="incident:create" label="운영자 역할 확인"/>
      <button type="submit" className="btn primary" disabled={!fresh||busy||!note.trim()||!auth.permits('incident:create')}>{busy?'접수 확인 중':closed?'후속 메모 저장':effective==='rerun_qa'?'QA 재실행 요청':'보완 요청 보내기'}</button>
    </form><ErrorBox>{error}</ErrorBox>
    {history.length>0&&<ol aria-label="보완·후속 이력">{history.map((row,index)=><li key={String(row.id??row.request_id??index)}><b>{row.kind==='rerun_qa'?'QA 재실행': row.kind==='followup_note'||row.effects==='none'?'후속 메모':'보완'}</b> · {statusText[String(row.status)]??'기록됨'} · {observedTime(typeof row.requested_at==='number'?row.requested_at:null)}
      <p>{summaryKo(row.note_ko,'내용 미관측')}</p></li>)}</ol>}
  </section>;
}
