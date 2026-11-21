import {useState} from 'react';
import {Disclosure} from './Disclosure';
import {object,word,type ApplicationIncident,type Data} from './unified';
import {summaryKo} from './presentation';
import {readingStatus} from './readingStatus';
import {usePolling} from './usePolling';

const questions:Record<string,string>={deployment:'Pod 준비',rollout:'배포 변경',pod_log:'애플리케이션 로그',k8s_event:'리소스 이벤트'};
function observationTime(value:unknown){
  const time=typeof value==='number'?value*1000:typeof value==='string'?Date.parse(value):NaN;
  return Number.isFinite(time)?time:null;
}
export function appObservations(application:ApplicationIncident){
  const collection=object(application.data.collection);
  const readings=object(application.data.display_readings);
  return (Array.isArray(collection.evidence)?collection.evidence:[]).map((value,index)=>{
    const entry=object(value), translation=object(readings['evidence/'+index]);
    return {entry, index, tag:questions[String(entry.source)] || '기타 관측', at:observationTime(entry.observed_at),
      answer:summaryKo(translation.ko, readingStatus(translation.state),1200)};
  }).sort((a,b)=>(a.at??Infinity)-(b.at??Infinity) || a.index-b.index);
}
const time=(at:number|null)=>at==null?'시각 미관측':new Date(at).toLocaleString('ko-KR');

function ObservationOriginal({id,entry}:{id:string;entry:Data}) {
  // This component is mounted only after the operator opens the quoted observation.
  const original=usePolling<ApplicationIncident>('/application/v1/incidents/'+encodeURIComponent(id),600000);
  const rows=object(original.data?.data.collection).evidence;
  const match=original.data?.id===id && Array.isArray(rows)?rows.map(object).find(row=>row.id===entry.id && row.observed_at===entry.observed_at):null;
  return match && word(match.text)?<pre className="raw-text">{String(match.text)}</pre>:<p>{original.error?'관측 원문을 확인하지 못했습니다.':original.data?'일치하는 관측 원문 미기록':'원문을 확인하고 있습니다.'}</p>;
}
export function ApplicationEvidence({application}:{application:ApplicationIncident}){
  const [filter,setFilter]=useState<string|null>(null);
  const entries=appObservations(application), tags=[...new Set(entries.map(e=>e.tag))];
  if(!entries.length)return <p>저장된 수집 결과 미관측</p>;
  return <section><h3>조사 질문별 변화</h3>
    <ul className="question-changes">{tags.map(tag=>{
      const observed=entries.filter(row=>row.tag===tag),first=observed[0],last=observed[observed.length-1];
      return <li key={tag}><button type="button" className="link-btn" onClick={()=>setFilter(tag)} aria-pressed={filter===tag}>{tag} · {observed.length}건</button>
        <span>최초 {first.answer} <time>{time(first.at)}</time></span>
        {first!==last && <span>최근 {last.answer} <time>{time(last.at)}</time></span>}</li>;
    })}</ul>
    <h3>시간순 관측 {filter?entries.filter(e=>e.tag===filter).length:entries.length}건</h3>
    {filter && <button type="button" className="link-btn" onClick={()=>setFilter(null)}>전체 관측 보기</button>}
    <ol className="stage-timeline" aria-label="시간순 관측 기록">{entries.filter(e=>!filter || e.tag===filter).map(({entry,answer,index,tag,at})=><li key={index}>
      <div className="timeline-meta"><time>{time(at)}</time><span>{tag}</span></div>
      <b>{word(entry.service) || '서비스 미관측'}</b><p>{answer}</p>
      {(word(entry.text) || entry.has_original===true) && <Disclosure label="관측 원문 상세보기">{()=>word(entry.text)?<pre className="raw-text">{String(entry.text)}</pre>:<ObservationOriginal id={application.id} entry={entry}/>}</Disclosure>}
    </li>)}</ol></section>;
}
