import { useCallback, useEffect, useMemo, useState } from 'react';
import { latestOnly } from './latestOnly';
import { Link, useSearchParams } from 'react-router-dom';
import { ORIGIN_KO, request, type Incident, type IncidentOrigin, type IncidentPage, type Status } from './api';
import { INCIDENT_STATUS_KO, placeKo } from './labels';
import { ApplicationIntake } from './ApplicationPage';
import { applicationStateKo } from './unified';
import { summaryKo } from './presentation';
import { historyResult, whenKo } from './history';
import './incidentHistoryList.css';
import { incidentSymptom } from './presentation';
import { StatusBadge, LevelBadge, ErrorBox } from './ui';
import { useLive } from './live';
import { mergeCaseSources, mergeCases, type AtlasCase } from './serviceAtlasModel';
import { usePolling } from './usePolling';
import type { Overview } from './api';
import type { ApplicationHistory } from './unified';
import { needsOperator, nextStepShort, responseQueue } from './responseQueue';

import { INCIDENT_PAGE as PAGE, incidentListPath, detailFilters, type DetailFilters } from './historyQuery';
import { historyEnvironment, historyEnvironmentOptions, isRetiredEnvironment } from './environments';
const statuses: Status[] = ['recovery_review','open', 'analyzing', 'action_pending', 'acting', 'resolved', 'handed_off', 'failed', 'closed'];
const origins: IncidentOrigin[] = ['evaluation', 'unconfirmed'];
// 대응이 남은 상태를 앞에, 끝난 결과를 뒤에 둔다. 운영자는 보통 "지금 내 차례인 것"부터 찾는다.
const activeFilters = [['action_pending', '승인 대기'], ['handed_off', '운영자 이관'], ['analyzing', '분석 중']] as const;
const resultFilters = [['resolved', '자동 복구'], ['closed', '운영자 종료'], ['failed', '처리 실패']] as const;
type ConsoleRow = Omit<Incident, 'id'> & {id:number|string; kind?:string; native_id?:string; application_state?:string};
type ConsolePage = Omit<IncidentPage,'items'> & {items:ConsoleRow[]; application_coverage?:string};
const kindLabels: Record<string,string> = {all:'전체 유형',kubernetes:'Kubernetes',application:'애플리케이션',mixed:'Kubernetes·애플리케이션'};
function HistoryTime({ incident }: { incident: ConsoleRow }) {
  const result = historyResult(incident);
  return <div className="incident-time">
    {result && <span>{result.timeLabel} {result.at}</span>}
    <small>감지 {whenKo(incident.created_at)}</small>
  </div>;
}
const workLabels = { approval: '운영 승인', waiting: '개입 대기', qa_fix: 'QA 보완', recovery: '복구 관측' };
type PendingWork = keyof typeof workLabels;
const specificState = {qa_fix:'qa_failed', recovery:'awaiting_recovery'} as const;

async function fetchWorkPages<T>(base: string, params: URLSearchParams, field: 'items'|'incidents', signal:AbortSignal) {
  const result:T[]=[];
  let anchor:number|undefined;
  for (;;) {
    params.set('limit','200'); params.set('offset',String(result.length));
    if(anchor!==undefined) params.set('as_of',String(anchor));
    const response = await request<(IncidentPage|ApplicationHistory)&Record<typeof field,T[]>>(base+'?'+params, {signal});
    const page=response[field];
    if(!Array.isArray(page)||!Number.isSafeInteger(response.total)||response.total<0) throw Error('invalid_work_page');
    result.push(...page);
    if(result.length>=response.total) return result;
    if(!page.length||!Number.isFinite(response.as_of)) throw Error('incomplete_work_page');
    anchor ??= response.as_of;
  }
}

function useSpecificWork(work:PendingWork, environment:string,system:string, service:string) {
  const [rows,setRows]=useState<AtlasCase[]>([]), [state,setState]=useState<'loading'|'ready'|'error'>('loading'), [rev,setRev]=useState(0);
  useEffect(()=>{
    if(work!=='qa_fix' && work!=='recovery') return;
    const cancel=new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    setRows([]);setState('loading');
    const filters=new URLSearchParams({work});
    for(const [key,value] of Object.entries({environment,system,service})) if(value) filters.set(key,value);
    async function load() {
      const [roots,apps]=await Promise.allSettled([
        fetchWorkPages<Incident>('/incidents',new URLSearchParams(filters),'items',cancel.signal),
        fetchWorkPages<ApplicationHistory['incidents'][number]>('/unified/summaries',new URLSearchParams(filters),'incidents',cancel.signal),
      ]);
      if(cancel.signal.aborted) return;
      setRows(mergeCases(roots.status==='fulfilled'?roots.value:[],apps.status==='fulfilled'?apps.value:[])
        .filter(i=> i.state === specificState[work as keyof typeof specificState]));
      setState(roots.status==='fulfilled' && apps.status==='fulfilled'?'ready':'error');
      timer=setTimeout(load,10000);
    }
    void load();
    return ()=>{cancel.abort();clearTimeout(timer);};
  },[work,environment,system,service,rev]);
  return {rows,state,refresh:()=>setRev(n=>n+1)};
}

function WorkCases({ work }: { work: PendingWork }) {
  const { open, openState, waiting, waitingState, refresh } = useLive();
  const [params, setParams] = useSearchParams();
  const environment = params.get('environment') || '';
  const service = params.get('service') || '';
  const system = params.get('system') || '';
  const { data: overview, error: overviewError, refresh: refreshOverview } = usePolling<Overview>('/overview', 15000);
  const cases = useMemo(() => mergeCaseSources(open, openState, overview?.incidents?.open, waiting), [open, openState, overview, waiting]);
  const specific=work==='qa_fix'||work==='recovery', filtered=useSpecificWork(work,environment,system,service);
  const rows = specific?filtered.rows:cases.filter(i => (work === 'approval' ? i.approval : i.intervention)
    && (!environment || i.environment === environment) && (!system || i.system === system) && (!service || i.service === service));
  const complete = specific?filtered.state==='ready':openState === 'ready' && waitingState === 'ready' && !!overview && !overviewError;
  const errors = specific?(filtered.state==='error'?['사건 목록']:[]):[openState === 'error' ? 'Kubernetes 사건' : '', waitingState === 'error' ? 'Application 대기 기록' : '', overviewError ? '최근 인계 기록' : ''].filter(Boolean);
  const title = workLabels[work];
  const selectEnvironment = (value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set('environment', value); else next.delete('environment');
    setParams(next);
  };
  return <div className="page incidents-page">
    <div className="page-head"><h1>{title}</h1><span className="badge">{complete ? `${rows.length}건` : errors.length ? '일부 미관측' : '집계 중'}</span></div>
    <nav className="history-results" aria-label="장애 업무별 보기"><Link to="/incidents">전체 장애</Link>
      {Object.entries(workLabels).map(([key,label])=><Link key={key} to={'/incidents?work='+key} aria-current={work===key?'page':undefined}>{label}</Link>)}</nav>
    <div className="list-filters"><label>환경 <select className="select" value={environment} onChange={e => selectEnvironment(e.target.value)}>
      <option value="">전체 환경</option>{historyEnvironmentOptions().map(env => <option key={env.value} value={env.value}>{env.label}</option>)}</select></label>
      {system && <span>{system}</span>}{service && <strong>{service}</strong>}</div>
    {errors.length > 0 && <div className="notice" role="status">{errors.join(' · ')} 갱신 지연 <button className="btn sm" onClick={() => { if(specific) filtered.refresh(); else {refresh();refreshOverview();} }}>다시 불러오기</button></div>}
    <div className="incident-list card">{rows.map(i => <Link key={i.key} className="incident-row work-row" to={i.href} state={{incidentListSearch:'?' + params.toString()}}>
      <div className="incident-identity"><strong>{i.service}</strong><small>{[i.environment,i.system].filter(Boolean).join(' · ')}</small></div>
      <div><span className="badge">{i.label}</span><p>{i.title}</p></div><span className="faint mono">#{i.key.replace(/^app:/,'')}</span><span className="row-arrow" aria-hidden>→</span>
    </Link>)}
    {!rows.length && <div className="empty" role="status">{complete ? (work==='recovery'?'복구 관측 중인 사건이 없습니다.':'대기 중인 사건이 없습니다.') : errors.length ? '현재 확인된 대기 사건이 없습니다. 위 조회 상태를 확인하세요.' : '불러오는 중…'}</div>}</div>
  </div>;
}
export function Incidents() {
  const [params] = useSearchParams();
  const work = params.get('work');
  return work && Object.hasOwn(workLabels,work) ? <WorkCases key={work} work={work as PendingWork} /> : <HistoryList />;
}
function HistoryList() {
  const [params, setParams] = useSearchParams();
  const kind = params.get('kind') || 'all';
  const filter = statuses.includes(params.get('status') as Status) ? params.get('status')! : '';
  const query = params.get('search')?.slice(0, 120) ?? '';
  // 퇴역 stg도 과거 사건 조회용 필터 값으로 계속 허용한다(서버 environment=stg 이력 조회).
  const environment = historyEnvironment(params.get('environment'));
  const origin = origins.includes(params.get('origin') as IncidentOrigin) ? params.get('origin')! : '';
  const [start, end, system, service] = detailFilters.map(key => params.get(key) ?? '');
  const n = Number(params.get('offset')); const offset = Number.isSafeInteger(n) && n >= 0 ? n : 0;
  const a = Number(params.get('as_of')); const asOf = offset > 0 && Number.isSafeInteger(a) && a > 0 ? a : null;
  const [search, setSearch] = useState(query);
  const { open } = useLive();
  const waiting = useMemo(() => responseQueue(open).filter(i => needsOperator(i.status, i.claimed)), [open]);
  const [page, setPage] = useState<ConsolePage | null>(null); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  useEffect(() => setSearch(query), [query]);
  const set = (next: DetailFilters & { kind?:string; status?: string; search?: string; offset?: number; environment?: string; origin?: string; asOf?: number | null }) => {
    const u = new URLSearchParams(); const s = next.status ?? filter, q = next.search ?? query, o = next.offset ?? offset;
    const env = next.environment ?? environment, org = next.origin ?? origin, pin = next.asOf === undefined ? asOf : next.asOf;
    if ((next.kind ?? kind) !== 'all') u.set('kind',next.kind ?? kind);
    if (env) u.set('environment', env); if (org) u.set('origin', org);
    if (s) u.set('status', s); if (q) u.set('search', q); if (o) { u.set('offset', String(o)); if (pin != null) u.set('as_of', String(pin)); }
    for (const key of detailFilters) {
      const value = next[key] ?? params.get(key);
      if (value) u.set(key, value);
    }
    setParams(u);
  };
  // Every request (first load and each poll) shares one abort signal per filter, and only the newest may apply its result:
  // a late poll response for an old filter can never overwrite the list of the current filter.
  const gate = useMemo(() => latestOnly(), []);
  const load = useCallback((signal: AbortSignal) => gate.run(
    () => request<ConsolePage>('/console'+incidentListPath({ offset, status: filter, search: query, environment, origin, asOf, start, end, system, service })+'&kind='+encodeURIComponent(kind), { signal }),
    d => { if (signal.aborted) return; setPage(d); setError(''); setLoading(false); },
    e => { if (!signal.aborted && !(e instanceof Error && e.name === 'AbortError')) { setError('장애 목록을 불러오지 못했습니다. 다시 시도하세요.'); setLoading(false); } },
  ), [gate, kind, filter, offset, query, environment, origin, asOf, start, end, system, service]);
  useEffect(() => { const controller = new AbortController(); setPage(null); setError(''); setLoading(true); void load(controller.signal); const t = window.setInterval(() => void load(controller.signal), 5000); return () => { controller.abort(); clearInterval(t); }; }, [load]);
  const items = page?.items ?? []; const total = page?.total ?? 0; const anchor = page?.as_of ?? null; const newer = page?.newer_since_as_of ?? 0;
  const filtered = Boolean(kind !== 'all' || filter || query || environment || origin || start || end || system || service);
  const partial = (kind==='all' || kind==='application') && (page?.application_coverage==='unavailable' || page?.application_coverage==='not_configured');
  const countLabel = !page ? (error ? '집계 미확인' : '집계 확인 중') : partial ? `확인된 사건 ${total}건` : filtered ? `조건 일치 ${total}건` : `전체 ${total}건`;
  return <div className="page incidents-page">
    <div className="page-head"><div><h1>장애 관제</h1></div><span className="badge">{countLabel}</span></div>
    <ApplicationIntake/>
    <div className="list-filters"><label>장애 유형 <select aria-label="장애 유형" className="select" value={kind} onChange={e=>set({kind:e.target.value,offset:0,asOf:null})}>
      {Object.entries(kindLabels).map(([value,label])=><option value={value} key={value}>{label}</option>)}</select></label></div>
    {partial && <p role="status" className="notice">{page?.application_coverage==='not_configured'?'애플리케이션 원본 저장소 미연결':'애플리케이션 원본 저장소의 목록을 확인할 수 없습니다.'}</p>}
    {waiting.length > 0 && <section className="attention-strip" aria-label="운영자 결정이 필요한 장애">
      <strong>내 결정이 필요한 장애 {waiting.length}건</strong>
      <ul>{waiting.slice(0, 5).map(i => <li key={i.id}><Link to={`/incidents/${i.id}`}>{i.workload} <span className="mono">#{i.id}</span> · {nextStepShort(i.status, i.claimed).label}</Link></li>)}</ul>
    </section>}
    <nav className="history-results" aria-label="장애 상태별 보기">
      <button type="button" aria-pressed={filter === ''} onClick={() => set({status:'', offset:0, asOf:null})}>전체</button>
      <span>대응 중</span>
      {activeFilters.map(([value, label]) =>
        <button type="button" key={value} aria-pressed={filter === value} onClick={() => set({status:value, offset:0, asOf:null})}>{label}</button>
      )}
      <span>처리 결과</span>
      {resultFilters.map(([value, label]) =>
        <button type="button" key={value} aria-pressed={filter === value} onClick={() => set({status:value, offset:0, asOf:null})}>{label}</button>
      )}
    </nav>
    <form className="list-filters" onSubmit={e => {
      e.preventDefault();
      const form = new FormData(e.currentTarget);
      const extra = Object.fromEntries(detailFilters.map(k => [k, String(form.get(k) ?? '').trim()]));
      if (extra.start && extra.end && extra.start > extra.end) {
        setError('종료일은 시작일 이후여야 합니다.');
        return;
      }
      set({ ...extra, search: search.trim(), offset: 0, asOf: null });
    }}>
      <select className="select" aria-label="장애 환경 필터" value={environment} onChange={e => set({ environment: e.target.value, offset: 0, asOf: null })}><option value="">전체 환경</option>{historyEnvironmentOptions().map(env => <option key={env.value} value={env.value}>{env.label}</option>)}</select>
      <input className="input" aria-label="장애 검색" placeholder="제목 또는 워크로드 검색" maxLength={120} value={search} onChange={e => setSearch(e.target.value)} />
      <button className="btn" type="submit">검색</button>
      <details className="history-more"><summary>상세 필터</summary>
        <div key={JSON.stringify([start,end,system,service])}>
      <select className="select" aria-label="출처 필터" value={origin} onChange={e => set({ origin: e.target.value, offset: 0, asOf: null })}><option value="">모든 출처</option>{origins.map(o => <option key={o} value={o}>{ORIGIN_KO[o]}</option>)}</select>
      <select className="select" aria-label="상태 필터" value={filter} onChange={e => set({ status: e.target.value, offset: 0, asOf: null })}><option value="">모든 상태</option>{statuses.map(s => <option key={s} value={s}>{INCIDENT_STATUS_KO[s]}</option>)}</select>
          <label>시작일 (한국시간)<input className="input" type="date" name="start" defaultValue={start} /></label>
          <label>종료일 (포함)<input className="input" type="date" name="end" defaultValue={end} /></label>
          <label>시스템 ID<input className="input" name="system" maxLength={120} defaultValue={system} /></label>
          <label>서비스 이름<input className="input" name="service" maxLength={120} defaultValue={service} /></label>
        </div>
      </details>
    </form>
    {offset > 0 && newer > 0 && <p className="notice" role="status">이 목록을 연 뒤 새 장애 {newer}건이 들어왔습니다. 현재 페이지는 그대로 유지됩니다. <button className="btn sm" type="button" onClick={() => set({ offset: 0, asOf: null })}>최신 목록 보기</button></p>}
    <ErrorBox>{error}</ErrorBox>
    <div className="incident-list card">{loading ? <div className="empty" role="status">불러오는 중…</div> : !page && error ? null : items.length === 0 ? <div className="empty">{filtered ? '조건에 맞는 장애가 없습니다.' : '표시할 장애가 없습니다.'}</div> : items.map(i => <Link className="incident-row" key={(i.native_id?'app':'root')+i.id} to={i.native_id?'/incidents/application/'+encodeURIComponent(i.native_id):`/incidents/${i.id}`} state={{incidentListSearch: params.size ? `?${params.toString()}` : ''}} aria-label={`장애 ${i.id} ${i.workload} 관제실 열기`}>
      <div className="incident-identity"><span className="faint mono">#{i.id}</span><strong>{i.workload}</strong><small className="muted">{placeKo(i.environment, i.system, null, i.namespace)}{isRetiredEnvironment(i.environment) ? ' · 퇴역 환경 이력' : ''}{i.origin === 'evaluation' ? ' · 평가 실행' : ''}</small><span className="incident-title-ko">{i.native_id?summaryKo(i.title,'문제 요약 미관측'):incidentSymptom(i)}</span></div>
      <div className="incident-badges"><small>{kindLabels[i.kind || 'kubernetes']}</small>{i.severity_level && <LevelBadge value={i.severity_level} />}{i.application_state ? <span className="badge">{applicationStateKo(i.application_state)}</span> : <StatusBadge value={i.status} recoveryKind={i.recovery_kind} />}{needsOperator(i.status, Boolean(i.handoff?.claimed_by)) && <span className="incident-next">{i.stage?.key === 'code_action' ? i.stage.label : nextStepShort(i.status).label}</span>}</div>
      <HistoryTime incident={i} /><span className="row-arrow" aria-hidden>→</span>
    </Link>)}
      {page && !loading && <div className="pager"><span>{total ? `${offset + 1}–${Math.min(offset + PAGE, total)} / ${total}` : '0건'}</span><div className="inline"><button className="btn" disabled={!offset} onClick={() => set({ offset: Math.max(0, offset - PAGE), asOf: offset - PAGE > 0 ? asOf ?? anchor : null })}>이전</button><button className="btn" disabled={offset + PAGE >= total} onClick={() => set({ offset: offset + PAGE, asOf: asOf ?? anchor })}>다음</button></div></div>}
    </div>
  </div>;
}
