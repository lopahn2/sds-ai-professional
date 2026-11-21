import { useId, useState } from 'react';
import { summaryKo } from './presentation';
import { Disclosure } from './Disclosure';
import { DiagramIcon } from './DiagramIcon';
import { object, observedTime, type ApplicationIncident, type Data } from './unified';
import { approvedVersion, pairedTest, boolLabel, observedRecord, qaFlows, qaSteps, records, statusLabel } from './releaseEvidenceModel';
import './releaseEvidence.css';

function name(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 100 || /token|secret|password|authorization|https?:|[{}<>\n]|[a-f\d]{32,}/i.test(value)) return '미관측';
  return value;
}
function reading(value: unknown) {
  return summaryKo(value, '미관측', 480);
}
function rows(value: unknown): string {
  return Array.isArray(value) && value.length ? value.map(name).join(' · ') : '미관측';
}
function imageVersion(value: unknown): string {
  if (typeof value !== 'string') return '미관측';
  const sha = value.match(/@sha256:([0-9a-f]{64})$/);
  return sha ? `sha256 ${sha[1].slice(0,12)}…` : '미관측';
}
function Fact({label, value}: {label: string; value: string}) { return <><dt>{label}</dt><dd>{value}</dd></>; }
function Raw({ value, label = '근거 원문 보기' }: { value: unknown; label?: string }) {
  if (!observedRecord(value)) return null;
  return <Disclosure label={label}>{() => <pre className="raw-text">{JSON.stringify(value,null,2)}</pre>}</Disclosure>;
}
const views = [{id:'qa',label:'dev QA',icon:'verify'}, {id:'risk',label:'prd 변경 영향',icon:'action'}, {id:'recovery',label:'복구 확인',icon:'outcome'}] as const;
type View = typeof views[number]['id'];
// The first QA browser receipts predate persisted plans. Expose only fields
// bound by their registered read-only workflows in dev_ui_qa.
const recordedFields = new Set(['상품','주문 가능 여부','재고.수량','재고.상품','재고.판매가능','오류',
  'deliveries.length','payments.length','processed','charged_amount','queue_depth','sent','products.0.price','total']);

function RecordedSteps({qa}: {qa:Data}) {
  const reports=records(qa.steps), tests=records(qa.developer_tests);
  const [selected,setSelected]=useState<number|null>(null);
  const ix=selected ?? Math.max(0,reports.findIndex(s=>s.status==='failed'));
  const step=reports[ix];
  if (!step) return <p>저장된 브라우저 검증 단계 미관측</p>;
  const source=tests.find(t=>t.test_id===step.test_id);
  const known:Record<string,string>={open:'화면 열기', input:'값 입력', submit:'전송', network:'응답 확인', dom:'화면 결과', independent_read:'독립 조회', dev_ui:'dev 화면'};
  return <><div className="evidence-path evidence-recorded" role="group" aria-label="저장된 QA 단계">{reports.map((r,i)=>
    <button key={i} type="button" aria-pressed={ix===i} onClick={()=>setSelected(i)}><b>{known[String(r.id)] || name(r.test_id ?? r.id)}</b><small>{statusLabel(r.status)}</small></button>)}</div>
    <dl className="evidence-facts"><Fact label="기대" value={reading(step.expected_ko)}/><Fact label="실제" value={reading(step.summary_ko)}/>
      <Fact label="개발자 시험" value={name(source?.test_id ?? step.test_id)}/>
      <Fact label="시험 파일" value={name(source?.file ?? step.file)}/>
      {typeof step.http_status==='number' && <Fact label="업무 응답" value={`HTTP ${step.http_status}`}/>}
    </dl><Raw value={step}/></>;
}
function QaReading({ qa, plan, runs }: { qa: Data; plan: Data; runs: Data[] }) {
  const flows = qaFlows(qa,plan);
  const [flowId, setFlow] = useState<string|null>(null);
  const [stepId, setStep] = useState<string|null>(null);
  const flow = flows.find(f => f.id === flowId) ?? flows.find(f => f.actual.outcome==='failed' || records(f.actual.steps).some(s=>s.status==='failed')) ?? flows[0];
  const steps = flow ? qaSteps(flow.plan, flow.actual) : [];
  const step = steps.find(s => s.id === stepId) ?? steps.find(s => s.receipt.status === 'failed') ?? steps[0];
  const net = object(flow?.actual.network), dom = object(flow?.actual.dom), independent = object(flow?.actual.independent_read);
  const linkedTests = records(flow?.plan.source_tests);
  const tests = linkedTests.length ? linkedTests : records(qa.developer_tests).filter(t => t.test_id === flow?.id);
  const labels = records(flow?.plan.expectations).map(e => name(e.dom_label)).filter(l => l !== '미관측');
  // A stored browser receipt contains individual observed DOM fields. The full DOM, provider envelope and response stay in the requested original.
  const observedLabels = labels.length ? labels : Object.keys(object(dom.values)).filter(label => recordedFields.has(label));
  const actualDom = observedLabels.map(label => {
    const raw = object(dom.values)[label];
    const value = ['number', 'boolean'].includes(typeof raw) ? String(raw) : name(raw);
    return `${label} · ${value}`;
  });
  const expectationsByStep:Record<string,string>={open:'등록된 dev 화면 열기', input:'시험 입력값 반영', submit:'업무 요청 전송',
    network:`${name(object(flow?.plan.request).method)} 응답 관측`, dom:labels.length ? `응답과 화면값 대조 · ${labels.join(', ')}` : '화면 결과 대조',
    independent_read:`${name(object(flow?.plan.independent_read).service)} 값과 응답 대조`};
  const expected=step?.receipt.expected_ko ? reading(step.receipt.expected_ko) : step?.planned ? expectationsByStep[step.id] : '미관측';
  const numericHttp=typeof step?.receipt.http_status==='number' ? step.receipt.http_status : step?.id==='network' ? net.status : undefined;
  const observed=step?.id==='dom' && actualDom.length && observedRecord(dom) ? actualDom.join(' / ')
    : typeof numericHttp==='number' ? `HTTP ${numericHttp}`
    : step?.id==='independent_read' && observedRecord(independent) ? boolLabel(independent.matched,'독립 조회와 일치','독립 조회와 불일치') : null;
  const reported=step?.receipt.summary_ko ? reading(step.receipt.summary_ko) : null;
  const stepTests=step?.receipt.test_id ? [step.receipt] : tests;
  return <>
    <div className="evidence-intro"><strong>dev 업무 검증</strong><span>{qa.accepted===false?'서버 검증 실패':statusLabel(qa.outcome)}</span>{typeof qa.observed_at === 'number' && <time>{observedTime(qa.observed_at)}</time>}</div>
    {flow ? <>
      {flows.length > 1 && <label className="evidence-chooser">업무 흐름 <select value={flow.id} onChange={e=>{setFlow(e.target.value);setStep(null);}}>{flows.map(f=><option key={f.id} value={f.id}>{name(f.plan.name_ko ?? f.id)}</option>)}</select></label>}
      <p>{name(flow.plan.name_ko ?? flow.id)} · {statusLabel(flow.actual.outcome)}</p>
      {steps.length > 0 && <div className="evidence-path" role="group" aria-label="QA 업무 단계">{steps.map(s=><button key={s.id} type="button" aria-pressed={s.id === step?.id} onClick={()=>setStep(s.id)}><b>{s.label}</b><small>{statusLabel(s.receipt.status)}</small></button>)}</div>}
      {tests.length > 0 && <section aria-label="같은 여정의 수정 전후 시험"><h4>연결된 회귀 시험</h4>{tests.map((ref,index)=>{
        const pair=pairedTest(ref,runs);
        return <div key={index}><b>{name(ref.test_id)}</b><dl className="evidence-facts">
          <Fact label="수정 전" value={pair.before==='error'?'실패':statusLabel(pair.before)}/><Fact label="수정 후" value={pair.after==='error'?'실패':statusLabel(pair.after)}/>
        </dl></div>;
      })}</section>}
      {step && <section aria-label={`${step.label} 검증 근거`}>
        <dl className="evidence-facts"><Fact label="기대" value={expected ?? '미관측'}/>
          <Fact label="실제" value={observed ?? (step.receipt.status === 'not_run' ? '미실행' : reported ?? '미관측')}/>
          {observed && reported && <Fact label="시험 기록" value={reported}/>}
          <Fact label="개발자 시험" value={stepTests.length ? stepTests.map(t=>name(t.test_id)).join(' · ') : '미관측'}/>
          <Fact label="시험 파일" value={stepTests.length ? stepTests.map(t=>name(t.file ?? tests.find(ref=>ref.test_id===t.test_id)?.file)).join(' · ') : '미관측'}/>
        </dl>
        <Raw value={{...step.receipt, ...(step.id === 'network' ? net : step.id === 'dom' ? dom : step.id === 'independent_read' ? independent : {})}}/>
      </section>}
    </> : <RecordedSteps qa={qa}/>}
    <Raw label="QA 영수증 원문 보기" value={qa}/>
  </>;
}

const riskSteps = [['target','변경 대상'], ['data','데이터 영향'], ['preserve','보존·원복'], ['verify','복구 검증']] as const;
function RiskReading({ release }: { release: Data }) {
  const [step,setStep] = useState<typeof riskSteps[number][0]>('target');
  const risk = object(release.risk);
  const versions=approvedVersion(release);
  const targets = records(risk.targets ?? object(release.targets).prd);
  const changed = records(risk.changed_files).filter(f => f.data_change === true || f.data_access === true || f.kind === 'migration');
  const impacts: Record<string,string> = {destructive:'데이터 삭제·변경 구문 감지', schema_or_data_change:'데이터 구조 변경', data_access_code_changed:'데이터 접근 코드 변경', no_data_path_observed:'저장소 접근 구문 미관측', unknown:'데이터 영향 확인 불가'};
  const recoveryCheck = object(risk.recovery_check);
  return <>
    <div className="evidence-intro"><strong>prd 변경 영향</strong><span>{boolLabel(risk.approval_blocked, '승인 차단', '승인 차단 사유 없음')}</span></div>
    <dl className="evidence-facts" aria-label="검증한 버전과 배포할 버전"><Fact label="dev에서 확인한 이미지" value={imageVersion(versions.tested)}/>
      <Fact label="prd 배포할 이미지" value={imageVersion(versions.target)}/><Fact label="버전 대조" value={boolLabel(versions.match,'일치','불일치')}/></dl>
    <div className="evidence-path" role="group" aria-label="prd 영향 검토 단계">{riskSteps.map(([id,label])=><button type="button" aria-pressed={step===id} key={id} onClick={()=>setStep(id)}>{label}</button>)}</div>
    <dl className="evidence-facts">
      {step==='target' && <><Fact label="영향 서비스" value={rows(release.affected_services)}/><Fact label="prd 대상" value={targets.length ? targets.map(t=>name(t.service)).join(' / ') : '미관측'}/>
        {targets.map((t,i)=><Fact key={i} label={name(t.service)+' 이미지'} value={imageVersion(t.previous_image)+' → '+imageVersion(t.new_image ?? object(release.artifact).image)}/>)}</>}
      {step==='data' && <><Fact label="데이터 변경" value={boolLabel(risk.data_change,'있음','없음')}/><Fact label="파괴적 변경" value={boolLabel(risk.destructive,'감지됨','감지되지 않음')}/>
        <Fact label="영향" value={impacts[String(risk.data_impact)] ?? reading(risk.patch_risk_ko ?? release.risk_ko)}/><Fact label="데이터 관련 파일" value={Array.isArray(risk.changed_files) ? changed.length ? changed.map(f=>name(f.file)).join(' · ') : '0개' : '미관측'}/></>}
      {step==='preserve' && <><Fact label="데이터 보존" value={reading(risk.preservation_ko)}/><Fact label="원복 방법" value={reading(risk.rollback_ko ?? release.rollback_ko)}/></>}
      {step==='verify' && <><Fact label="검증 방법" value={reading(recoveryCheck.summary_ko)}/><Fact label="업무 점검 경로" value={name(recoveryCheck.path)}/>
        <Fact label="실제 복구 관측" value={statusLabel(object(release.recovery).outcome)}/></>}
    </dl>
    <Raw label="변경 영향 원문 보기" value={risk}/>
  </>;
}

function RecoveryReading({ recovery }: {recovery:Data}) {
  const start=recovery.window_start ?? object(recovery.window).start, end=recovery.window_end ?? object(recovery.window).end;
  const errors=recovery.error_count ?? recovery.error_observations;
  const http=recovery.status_code ?? object(recovery.business).status_code;
  return <><div className="evidence-intro"><strong>prd 복구 관측</strong><span>{recovery.accepted===false?'서버 검증 실패':statusLabel(recovery.outcome)}</span></div><dl className="evidence-facts">
    <Fact label="원래 실패 요청의 서비스" value={name(recovery.service)}/><Fact label="확인한 요청 경로" value={name(recovery.path)}/>
    <Fact label="운영 이미지" value={imageVersion(recovery.image)}/><Fact label="방법" value={recovery.method==='original_error_absent'?'원래 오류의 재발 여부 확인':recovery.method==='harmless_check'?'무해한 읽기 점검':'미관측'}/>
    <Fact label="원래 오류" value={boolLabel(recovery.original_error_unmatched,'일치하는 오류 근거 없음','오류 신호 식별됨')}/>
    <Fact label="관측 구간" value={typeof start==='number' && typeof end==='number' ? observedTime(start)+' ~ '+observedTime(end) : '미관측'}/>
    <Fact label="오류 관측" value={typeof errors==='number' ? errors+'건' : '미관측'}/>
    {typeof recovery.success_count==='number' && <Fact label="성공 요청" value={recovery.success_count+'건'}/>}
    <Fact label="업무 점검" value={typeof http==='number' ? 'HTTP '+http : boolLabel(object(recovery.business).ok)}/>
    <Fact label="확인 시각" value={observedTime(typeof recovery.observed_at === 'number'? recovery.observed_at:null)}/>
    </dl><Raw label="복구 점검 원문 보기" value={recovery}/></>;
}

export function ReleaseEvidence({release,application,initialView='qa',focused=false}: {release: Data|null|undefined; application:ApplicationIncident;initialView?:View;focused?:boolean}) {
  const [view,setView] = useState<View>(initialView);
  const [selectedRound,setRound] = useState('current');
  const labelId=useId();
  const rounds=records(application.data?.rounds);
  const round=rounds.find((r,ix)=>String(r.round ?? ix+1)===selectedRound);
  const r=round ? object(round.release) : object(release);
  const qa=object(r.qa ?? round?.qa);
  const plan=object(qa.qa_plan ?? r.qa_plan ?? (round ? round.qa_plan : application.data?.qa_plan));
  return <section className="release-evidence" aria-labelledby={labelId}>
    <header><h3 id={labelId}>{focused ? views.find(v=>v.id===view)?.label : '검증·변경 근거'}</h3>
      {rounds.length>0 && <label className="evidence-chooser">회차 <select value={round ? selectedRound : 'current'} onChange={e=>setRound(e.target.value)}><option value="current">현재 회차</option>
        {rounds.map((it,ix)=><option key={String(it.round ?? ix+1)} value={String(it.round ?? ix+1)}>이전 {String(it.round ?? ix+1)}회차</option>)}</select></label>}
    </header>
    {!focused && <div className="evidence-views" role="group" aria-label="검증 근거 선택">{views.map(v=><button key={v.id} type="button" aria-pressed={view===v.id} onClick={()=>setView(v.id)}><DiagramIcon kind={v.icon}/>{v.label}</button>)}</div>}
    {round && <p className="evidence-past">이전 회차 · 승인 기록 {boolLabel(object(r.approval ?? round.approval).invalidated,'무효화됨','무효화 미표시')}</p>}
    <div className="evidence-reading" key={selectedRound}>{view==='qa'? <QaReading qa={qa} plan={plan} runs={records(object(r.tests).executed_tests)}/> : view==='risk'? <RiskReading release={r}/> : <RecoveryReading recovery={object(r.recovery ?? round?.recovery)}/>}</div>
    {round && <Raw value={round} label="이전 회차 기록 원문 보기"/>}
  </section>;
}
