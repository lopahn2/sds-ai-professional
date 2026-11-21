import { useState } from 'react';
import { Link } from 'react-router-dom';
import { usePolling } from './usePolling';
import { summaryKo } from './presentation';

type Row={incident_id?:number;id?:string;root_id?:number;title:string;namespace:string;workload:string;status:string};
function ReviewList({app}:{app:boolean}) {
  const [offset,setOffset]=useState(0);
  const {data,error,refresh}=usePolling<{items: Row[];total:number}>((app?'/application/v1':'')+'/reviews/unexplained?limit=25&offset='+offset,10000);
  return <section className="review-list"><h2>{app?'애플리케이션 소명':'Kubernetes 소명'} {data && <small>{data.total}건</small>}</h2>
    {error && <p role="status">목록 갱신 실패 <button className="btn sm" onClick={refresh}>다시 확인</button></p>}
    {!data && !error && <p>확인 중</p>}{data?.items.map(row=><Link key={row.id||row.incident_id} className="review-item" to={row.incident_id?'/incidents/'+row.incident_id:row.root_id?'/incidents/'+row.root_id:'/incidents/application/'+encodeURIComponent(row.id!)}>
      <b>{row.namespace} / {row.workload}</b><span>{summaryKo(row.title,'감지 기록 확인')}</span><small>{row.status==='recovery_review'?'복구 관측 · 확인 대기':'과거 기록 검토'} →</small></Link>)}
    {data?.total===0 && <p>현재 미소명 사건이 없습니다.</p>}
    {data && data.total>25 && <div className="pager"><button disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-25))}>이전</button><span>{offset+1}–{Math.min(offset+25,data.total)}</span><button disabled={offset+25>=data.total} onClick={()=>setOffset(v=>v+25)}>다음</button></div>}
  </section>;
}
export function UnexplainedLedger(){return <><ReviewList app={false}/><ReviewList app/></>;}
