import { usePolling } from './usePolling';
import { useHumanAccess } from './HumanAccess';
import { AccountabilityReading } from './StageReadings';
import type { Accountability } from './accountability';

export function NativeAccountability({id,readOnly=false,part='all'}:{id:string;readOnly?:boolean;part?:'all'|'decision-first'}) {
  const path='/application/v1/incidents/'+encodeURIComponent(id)+'/accountability';
  const {data,error,refresh}=usePolling<Accountability>(path,10000);
  const {connected}=useHumanAccess('/application/v1');
  if(!data)return <p role="status">{error?'소명 기록 미관측':'소명 기록 확인 중'}</p>;
  return <AccountabilityReading value={data} id={id} basePath={path} canWrite={connected&&!readOnly} onChange={refresh} part={part}/>;
}
