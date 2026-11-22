import { useRef, useLayoutEffect, useState, type FormEvent } from 'react';
import { Link, Navigate, useParams, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { usePolling } from './usePolling';
import { NativeReleaseReview } from './NativeReleaseReview';
import {NativeOriginal} from './ApplicationStage';
import {applicationFlow,activeAppStep,appFinished} from './applicationFlow';
import {useAppActivity} from './ApplicationWorkTrace';
import { StageMotion } from './StageMotion';
import {SemanticStagePanel} from './SemanticStagePanel';
import {semanticFlow, semanticForSource} from './semanticFlow';
import {StageFlowView} from './StageFlowView';
import {roomPhases,phaseStatus} from './roomWorkflow';
import {DiagramIcon} from './DiagramIcon';
import './operatorRoom.css';
import { Disclosure } from './Disclosure';
import { ErrorBox } from './ui';
import { HumanAccessForm, aborted, authError, useHumanAccess } from './HumanAccess';
import { summaryKo } from './presentation';
import { applicationStateKo, observedTime,  type Data, type CodeActionSummary, type UnifiedCatalog } from './unified';

type PublicIncident = { id: string; state?: string | null; created_at?: number | null; environment?: string | null;
  service?: string | null; system?: string | null; reason?: string | null; reason_ko?: string | null; next_ko?: string | null; root_incident_id?: number | string | null; protocol?: string | null; round?: number | null; qa?: Data | null; recovery?: Data | null };
const text = (value: unknown) => typeof value === 'string' && value.trim() ? value : '미관측';
const stateLabel = (value?: string | null) => value ? applicationStateKo(value) : '상태 미관측';

function OriginalIncident({ id }: { id: string }) {
  const [supplementReceipt,setSupplementReceipt]=useState('');
  const back = useLocation().state?.incidentListSearch;
  const list = typeof back === 'string' && back.startsWith('?') ? '/incidents' + back : '/incidents';
  const result = usePolling<PublicIncident>(`/application/v1/incidents/${encodeURIComponent(id)}/summary`, 10000);
  const item = result.data?.id === id ? result.data : null;
  const activity=useAppActivity(id);
  const [selected,setSelected]=useState<string|null>(null);
  const [detail,setDetail]=useState(false);
  const [all,setAll]=useState(false);
  const [wide,setWide]=useState(false);
  const graphButton=useRef<HTMLButtonElement>(null);
  const feed=useRef<HTMLDivElement>(null);
  const moveFocus=useRef<'flow'|'stage'|null>(null);
  useLayoutEffect(()=>{
    const target=moveFocus.current;
    moveFocus.current=null;
    if(target==='flow')graphButton.current?.focus({preventScroll:true});
    if(target==='stage')feed.current?.querySelector<HTMLElement>('.step-panel')?.focus({preventScroll:true});
  },[detail,selected]);
  const rawFlow = applicationFlow(activity.view, item);
  const flow = semanticFlow(rawFlow, {incidentStatus:activity.view?.state || item?.state || undefined});
  const rawActive = activeAppStep(rawFlow.nodes);
  const active = rawActive && semanticForSource(flow.nodes, rawActive.id);
  const step = (selected && semanticForSource(flow.nodes, selected)) || active || flow.nodes.at(-1) || null;
  const done = appFinished(activity.view?.state || item?.state);
  const choose=(key:string|null)=>{
    if(window.matchMedia('(max-width:900px)').matches && !document.activeElement?.closest('.flow-canvas[data-mode="stage"]'))moveFocus.current='stage';
    setSelected(key);setDetail(true);
  };
  const showGate = (step?.semantic.kind === 'approval' && item?.state === 'awaiting_prd_approval')
    || (step?.semantic.kind === 'dev_validation' && item?.state === 'awaiting_user_test');
  // One live-work source for the graph and the card, so both show the same working stage.
  const sources = {application: activity.error ? null : activity.view};
  if(item?.root_incident_id && /^[1-9]\d*$/.test(String(item.root_incident_id)))return <Navigate replace to={'/incidents/'+item.root_incident_id} state={{incidentListSearch:back}}/>;
  return <div className="incident-room application-incident-room tab-flow">
    <section className="headline" aria-label="장애 상황 요약"><div className="headline-top"><Link to={list}>← 장애 목록</Link><span className="mono">#{id}</span><span className="badge">애플리케이션</span></div>
      <h1>{item ? text(item.service) + ' · ' + summaryKo(item.reason_ko || item.reason, '문제 요약 미관측') : '애플리케이션 장애'}</h1>
      {item && <p className="headline-impact">{stateLabel(item.state)} · {text(item.environment)} · {text(item.system)} · {observedTime(item.created_at)}</p>}
    </section>
    {result.error || (result.data && !item) ? <><ErrorBox>{result.errorStatus===404?'이 번호의 사건을 찾을 수 없습니다.':'사건 요약을 확인하지 못했습니다.'}</ErrorBox><button className="btn" onClick={result.refresh}>다시 확인</button></>:null}
    {item && <><nav className="room-phases" aria-label="사건 처리 단계">{roomPhases.map(p=>{
      const nodes=flow.nodes.filter(n=>n.semantic.phase===p.id);
      const focus=active?.semantic.phase===p.id ? active : nodes[0];
      return <button type="button" key={p.id} disabled={!focus} aria-pressed={step?.semantic.phase===p.id} onClick={()=>{if(focus){setAll(false);choose(focus.id);}}}><DiagramIcon kind={p.icon}/><span><strong>{p.label}</strong><small>{phaseStatus(nodes,done)}</small></span></button>;
    })}</nav>
    <StageMotion node={activity.view ? step : null}>
    <div className={'room-columns '+(detail?'show-stage-detail ':'')+(wide?'wide-stage-detail':'')}>
      <section className="room-pane flow-pane" aria-label="AI 분석 흐름"><header className="pane-heading"><h2>AI 분석 흐름</h2><button type="button" className="btn sm" ref={graphButton} aria-pressed={all} onClick={()=>{setAll(v=>!v);setDetail(false);}}>{all?'단계 흐름':'전체 흐름'}</button></header>
        <StageFlowView all={all} flow={flow} selected={step?.id ?? null} onSelect={choose} archived={done}
          sources={sources}
          pending={!activity.view} delayed={activity.error}/>
      </section>
      <section className="room-pane conversation-pane chat-pane" aria-label="단계 관제"><header className="pane-heading"><div><button type="button" className="link-btn back-flow" onClick={()=>{moveFocus.current='flow';setDetail(false);}}>← 흐름으로 돌아가기</button><h2>단계 관제</h2></div>
        <button type="button" className="btn sm widen-stage" aria-pressed={wide} onClick={()=>{if(step)setSelected(step.id);setWide(v=>!v);}}>{wide?'기본 너비로':'단계 넓게 보기'}</button></header>
        <div className="chat-feed" ref={feed} onFocusCapture={e=>{if(!selected && step && e.target.closest(".step-panel"))setSelected(step.id);}}>
          {activity.error && <p role="status">작업 기록 갱신 지연 · 마지막 관측을 표시합니다.</p>}
          <SemanticStagePanel key={step?.id} node={step} application={{summary:item, activity}} onChange={result.refresh}
            flow={flow} sources={sources} onSelect={choose}
            decision={showGate ? <NativeReleaseReview key={item.state} id={id} state={item.state!} onChange={result.refresh} decisionOnly/> : undefined}/>
          <Disclosure label={done?'후속 메모 열기':'사건 보완 열기'}>{()=> <NativeOriginal id={id} supplement onChange={message=>{if(message)setSupplementReceipt(message);result.refresh();}}/>}</Disclosure>
          {supplementReceipt && <p role="status">{supplementReceipt}</p>}
        </div></section></div></StageMotion></>}
    {!item && !result.error && <p role="status">사건 요약을 확인하고 있습니다.</p>}
  </div>;
}
export function ApplicationPage() {
  const [params] = useSearchParams();
  const selected = params.get('incident');
  return <Navigate replace to={selected ? '/incidents/application/'+encodeURIComponent(selected) : '/incidents?kind=application'}/>;
}
export function NativeIncidentRoom() {
  const {id=''}=useParams();
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(id)) return <p role="alert">사건 번호를 확인하세요.</p>;
  return <OriginalIncident key={id} id={id}/>;
}
export function ApplicationIntake() {
  const catalog = usePolling<UnifiedCatalog>('/unified/catalog', 30000);
  const intakeMessage = catalog.errorStatus === 404 ? '이전 설치 버전으로 통합 사건 접수가 구성되지 않았습니다.'
    : catalog.errorStatus === 403 ? '등록된 서비스 목록을 조회할 권한이 필요합니다.'
    : catalog.error ? '등록 서비스 조회가 지연되고 있습니다.'
    : catalog.data ? '새 사건 접수를 위한 서비스 설정이 필요합니다.' : '서비스 등록 상태 확인 중';
  return <div className="console-intake">
    {catalog.data?.enabled && catalog.data.profiles_configured && !catalog.error
      ? <Disclosure label="새 애플리케이션 장애 접수">{() => <IntegratedApplication catalog={catalog.data!} />}</Disclosure>
      : <section aria-label="새 애플리케이션 사건"><h2>애플리케이션 장애 접수</h2><p role={catalog.error && catalog.errorStatus !== 404 ? 'alert' : undefined}>{intakeMessage}</p><button className="btn sm" type="button" onClick={catalog.refresh}>다시 확인</button></section>}
  </div>;
}
function IntegratedApplication({ catalog }: { catalog: UnifiedCatalog }) {
  const auth = useHumanAccess(), go = useNavigate();
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [confirmed, setConfirmed] = useState(false);
  const form = useRef<HTMLFormElement>(null), submission = useRef<{ key: string; requestId: string } | null>(null);
  const enabled = catalog.enabled === true && catalog.profiles_configured === true;
  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (!enabled || !confirmed || busy || !auth.permits('incident:create')) return;
    const values = new FormData(e.currentTarget);
    const payload = { environment: String(values.get('environment')), service: String(values.get('service')), reason: String(values.get('reason') || '').trim(), reproduction: null };
    if (!['dev', 'prd'].includes(payload.environment) || !catalog.services.some(s => s.service === payload.service) || !payload.reason) return;
    const key = JSON.stringify(payload); if (submission.current?.key !== key) submission.current = { key, requestId: crypto.randomUUID() };
    setBusy(true); setError('');
    try {
      const result = await auth.call<{ incident_id: number; code_action: CodeActionSummary }>('/unified/incidents', { method: 'POST', body: JSON.stringify({ ...payload, request_id: submission.current.requestId }) });
      if (!Number.isSafeInteger(result.incident_id) || result.incident_id < 1) throw new Error('invalid incident id');
      go(`/incidents/${result.incident_id}`);
    } catch (e) { if (!aborted(e)) setError(authError(e)); }
    finally { setBusy(false); setConfirmed(false); }
  }
  return <section className="code-action-card" aria-label="새 애플리케이션 사건">
      <form ref={form} onSubmit={e => void create(e)} className="application-intake" autoComplete="off" onChange={e => { if (e.target instanceof HTMLElement && e.target.getAttribute('name')) setConfirmed(false); }}>
        <div className="application-fields"><label>환경<select name="environment" required disabled={!enabled || busy} defaultValue="dev">{['dev', 'prd'].filter(v => catalog.environments.includes(v)).map(v => <option key={v}>{v}</option>)}</select></label><label>서비스<select name="service" required disabled={!enabled || busy} defaultValue=""><option value="" disabled>서비스 선택</option>{catalog.services.map(s => <option key={s.service} value={s.service}>{s.service} · {s.system}</option>)}</select></label></div>
        <label>문제 상황<textarea name="reason" maxLength={4000} rows={3} required disabled={!enabled || busy} placeholder="어떤 동작에서 어떤 문제가 발생했는지 적어 주세요." /></label>
        <HumanAccessForm auth={auth} scope="incident:create" label="사건 접수 역할 확인" />
        <label className="code-confirm"><input type="checkbox" checked={confirmed} disabled={!enabled || busy} onChange={e => setConfirmed(e.target.checked)} />선택한 서비스에 사건을 접수합니다. 모델 분석과 dev 수정·시험이 시작될 수 있으며 prd 배포는 사람 확인과 승인을 거칩니다.</label>
        <button className="btn primary" disabled={!enabled || !confirmed || busy}>{busy ? '접수 확인 중' : '사건 접수·관제실 열기'}</button>
      </form><ErrorBox>{error}</ErrorBox>
    </section>;
}
