import { useEffect,useState } from 'react';
import {request} from './api';
import {ReleaseEvidence} from './ReleaseEvidence';
import {SupplementForm} from './SupplementForm';
import {ErrorBox} from './ui';
import {HumanAccessForm,useHumanAccess,authError} from './HumanAccess';
import {type Data,type ApplicationIncident} from './unified';
import {ApplicationEvidence} from './ApplicationEvidence';
export function NativeOriginal({ id, evidence = false, supplement=false, collection=false, initialView='qa', onChange }: { id: string; evidence?: boolean; supplement?:boolean; collection?:boolean; initialView?:'qa'|'risk'|'recovery'; onChange?:(message?:string)=>void }) {
  const auth = useHumanAccess('/application/v1');
  const [refresh,setRefresh]=useState(0);
  const base='/application/v1/incidents/' + encodeURIComponent(id);
  const [view, setView] = useState<{application: ApplicationIncident; release: Data|null; generation: number}|null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setError('');
    if (!auth.connected) {setView(null);return;}
    const c = new AbortController(), generation = auth.generation.current;
    let timer:ReturnType<typeof setTimeout>;
    async function load(){
      try {
        const [application,result] = await Promise.all([
          request<ApplicationIncident>(base,{signal:c.signal}),
          evidence ? request<{incident_id:string; release:Data|null}>(base+'/release',{signal:c.signal}) : null,
        ]);
        if(application.id!==id || (result && result.incident_id!==id)) throw new Error('incident_identity_mismatch');
        if(c.signal.aborted || auth.generation.current!==generation)return;
        setView({application,release:result?.release??null,generation});
        setError('');
      } catch(e) {
        if(!c.signal.aborted)setError(authError(e));
      } finally {
        if(!c.signal.aborted && (collection || evidence))timer=setTimeout(load,12000);
      }
    }
    void load();
    return () => {c.abort();clearTimeout(timer);};
  }, [id, base, refresh, evidence, collection, auth.connected, auth.generation, auth.revision]);
  if (!auth.connected) return <HumanAccessForm auth={auth} scope={supplement?'incident:create':'incident:read'} label={supplement?'운영자 역할 확인':evidence ? '검증 근거 열람 권한' : '원문 열람 권한'}/>;
  if (error) return <><ErrorBox>{error}</ErrorBox><button type="button" className="btn sm" onClick={()=>setRefresh(v=>v+1)}>다시 확인</button></>;
  if (!view || view.generation !== auth.generation.current) return <p role="status">기록을 확인하고 있습니다.</p>;
  if(supplement) return <SupplementForm application={view.application} prefix="/application/v1" path={base+'/supplements'} fresh
    reload={()=>request<ApplicationIncident>(base)} onDone={message=>{setRefresh(v=>v+1);onChange?.(message);}}/>;
  if(collection) return <ApplicationEvidence application={view.application}/>;
  return evidence ? <ReleaseEvidence release={view.release} application={view.application} initialView={initialView}/> : <pre className="raw-text">{JSON.stringify(view.application,null,2)}</pre>;
}
