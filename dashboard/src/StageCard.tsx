// 단계 카드 본문(시안 A · 브리핑 스택): 머리 → (결정) → 이어받음 → 브리프 → 데이터.
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { FlowEdge, FlowNode } from './api';
import { stepExecution } from './execution';
import { ExecutionStream } from './ExecutionStream';
import { dispatchFor } from './stepPanel';
import { flowStatusKo } from './narrative';
import { isAppStep } from './applicationFlow';
import type { SemanticKind, SemanticNode } from './semanticFlow';
import { AccountabilityReading, AgentReading, EvidenceTimeline, GroupReading, RoutingReading, SignalReading, CandidateList } from './StageReadings';
import { EscalationNote } from './Explain';
import { deriveStageBrief, type CallRow } from './stageBrief';
import { activeStageActors, type WorkSources } from './stageWork';
import { BriefPanel, CallRows, DataSection, EvidenceRows, LineageLine, ModelTags, Reading } from './StageBriefView';
import { readingOf, readingString } from './readingText';
import type { CoreStepContext } from './SemanticStagePanel';
import { AppCallBody, AppDataSections, type AppStageData } from './ApplicationCardReading';

export const STAGE_QUESTIONS: Record<SemanticKind, string> = {
  detection: '어떤 장애 신호를 언제 감지했나?', evidence: '시간이 지나면서 무엇이 달라졌나?',
  routing: '왜 이 등급과 조사팀을 선택했나?', investigation: '어떤 원인이 근거를 설명하고 무엇이 남았나?',
  verification: '판단을 수락하거나 반려한 결정적 근거는 무엇인가?', action: '무엇을 바꾸고 어떻게 확인하나?',
  test_build: '원래 실패한 동작이 수정 후 같은 시험을 통과했나?', dev_validation: '시험한 버전이 dev에서 실제로 동작했나?',
  approval: '같은 버전의 prd 배포를 승인할 수 있나?', recovery: '원래 실패한 동작이 어느 환경에서 정상화됐나?',
  result: '왜 장애로 감지됐고 왜 정상화됐나?', handoff: '자동 처리를 멈춘 이유와 필요한 결정은 무엇인가?',
};

/** A core member's dispatched request and its public answer stream. */
function CoreWork({ member, core }: { member: FlowNode; core: CoreStepContext }) {
  const dispatch = dispatchFor(member, core.events), view = stepExecution(member, core.events);
  const live = member.status === 'running' && !core.historical;
  if (!dispatch && !view.records.length && !live) return null;
  return <section className="semantic-call">
    {dispatch && <p><b>전달한 요청</b> {readingString(readingOf(dispatch.task_ko, { era: 'current' })) ?? '요청 원문만 기록됨'}</p>}
    <ExecutionStream node={member} view={view} state={core.state} receivedAt={core.receivedAt} error={core.error} />
  </section>;
}

function CoreCallBody({ call, node, core }: { call: CallRow; node: SemanticNode; core: CoreStepContext }) {
  const member = node.semantic.members.find(m => m.id === call.ref);
  if (!member) return null;
  return <>
    {call.summary && 'text' in call.summary && <p className="call-full">{call.summary.text}</p>}
    {member.card?.kind === 'agent' && <AgentReading node={member} card={member.card} data={core.data} hideLead hideQuotes />}
    {member.type === 'investigation' && <p>{readingString(readingOf(member.job_ko, { era: 'current' })) ?? '조사 질문 기록'}</p>}
    <EscalationNote n={member} /><CoreWork member={member} core={core} />
  </>;
}

type Props = {
  node: SemanticNode; flow: { nodes: SemanticNode[]; edges: FlowEdge[] }; sources: WorkSources;
  core?: CoreStepContext; app?: AppStageData | null; appStatus?: ReactNode;
  decision?: ReactNode; decisionCovers?: SemanticKind[]; onSelect?: (id: string) => void; onChange: () => void;
};

export function StageCardBody({ node, flow, sources, core, app, appStatus, decision, decisionCovers = [], onSelect, onChange }: Props) {
  const kind = node.semantic.kind;
  const coreMembers = node.semantic.members.filter(n => !isAppStep(n));
  const hasCore = !!core && coreMembers.length > 0;
  const preparing = !!core && (core.data.interpreting || ['pending', 'pipeline'].includes(core.data.interpret_state?.status ?? ''));
  const actors = activeStageActors(flow.nodes, sources).get(node.id);
  const brief = deriveStageBrief({ node, nodes: flow.nodes, edges: flow.edges, core: hasCore ? { data: core!.data, preparing } : undefined, app: app?.source, actors });
  const accountability = hasCore && kind === 'result' && core!.data.accountability && core!.data.accountability.status !== 'not_applicable' ? core!.data.accountability : null;

  // Decision first: the operator's choice, its hold reason and its buttons sit above everything else.
  const ownDecision = <>
    {accountability && <AccountabilityReading id={core!.data.incident.id} value={accountability} canWrite={!core!.readOnly} onChange={onChange} part="decision" />}
    {app?.decision(kind)}
  </>;
  const hold = app?.hold(kind) ?? null;
  const hasDecision = !!decision || !!hold || !!accountability || !!app?.hasDecision(kind);

  const group = hasCore && kind === 'routing' ? coreMembers.find(n => n.card?.kind === 'group') : undefined;
  const briefExtra = <>
    {group?.card?.kind === 'group' && <GroupReading node={group} card={group.card} data={core!.data} hideLead />}
    {accountability && <AccountabilityReading id={core!.data.incident.id} value={accountability} canWrite={false} onChange={onChange} part="facts" />}
  </>;

  const coreCalls = brief.calls.filter(c => c.origin === 'core'), appCalls = brief.calls.filter(c => c.origin === 'app');
  return <>
    <header className="semantic-stage-title"><h2>{node.label}</h2><span className={`badge stage-badge is-${brief.tone}`}>{flowStatusKo(node.status)}</span><ModelTags models={brief.models} /></header>
    <p className="semantic-question">{STAGE_QUESTIONS[kind]}</p>
    {app?.roundPicker}
    {appStatus}
    {hasDecision && <div className="stage-decision" role="group" aria-label="운영자 결정">{hold && <p className="stage-hold">{hold}</p>}{decision}{ownDecision}</div>}
    <LineageLine rows={brief.lineage} more={brief.lineageMore} onSelect={onSelect} />
    <BriefPanel brief={brief} extra={briefExtra} />
    <div className="stage-data">
      {brief.calls.length > 0 && <DataSection label="판단 기록" count={brief.calls.length} defaultOpen={brief.calls.some(c => c.running)}>{() =>
        <CallRows calls={brief.calls} body={call => call.origin === 'core' && core ? <CoreCallBody call={call} node={node} core={core} /> : app ? <AppCallBody call={call} data={app} /> : null} />}</DataSection>}
      {brief.evidence && brief.evidence.refs > 0 && <DataSection label="근거" count={brief.evidence.refs}>{() => <EvidenceRows summary={brief.evidence!} />}</DataSection>}
      {hasCore && <CoreSections node={node} core={core!} covers={decisionCovers} hasCalls={coreCalls.length > 0} origin={app ? ' · Kubernetes' : ''} />}
      {app && <AppDataSections node={node} data={app} hasCalls={appCalls.length > 0} origin={hasCore ? ' · 애플리케이션' : ''} />}
    </div>
  </>;
}

/** Kubernetes data below the brief: lists, timelines and originals that the brief summarised. */
function CoreSections({ node, core, covers, hasCalls, origin }: { node: SemanticNode; core: CoreStepContext; covers: SemanticKind[]; hasCalls: boolean; origin: string }) {
  const kind = node.semantic.kind, members = node.semantic.members.filter(n => !isAppStep(n)), { data } = core;
  const signal = members.find(n => n.card?.kind === 'signal')?.card;
  const evidence = members.find(n => n.card?.kind === 'evidence')?.card;
  const routing = [...members].reverse().find(n => n.card?.kind === 'routing')?.card;
  const agents = members.filter((n): n is FlowNode & { card: Extract<FlowNode['card'], { kind: 'agent' }> } => n.card?.kind === 'agent');
  const actions = members.filter(n => n.type === 'action' || n.type === 'command');
  const outcome = members.find(n => n.card?.kind === 'outcome')?.card;
  const handoff = (outcome?.kind === 'outcome' ? outcome.handoff : null) ?? data.handoff_view;
  return <>
    {kind === 'detection' && signal?.kind === 'signal' && <DataSection label={'최근 관측과 신호 원문' + origin}>{() => <SignalReading card={signal} data={data} dataOnly />}</DataSection>}
    {kind === 'evidence' && evidence?.kind === 'evidence' && <DataSection label={'시간순 관측 기록' + origin} count={evidence.items.length}>{() => <EvidenceTimeline card={evidence} data={data} hideLead />}</DataSection>}
    {kind === 'routing' && routing?.kind === 'routing' && <DataSection label="판단 과정" count={routing.steps.filter(s => s.key !== 'final' && s.key !== 'model').length}>{() => <>
      <RoutingReading card={routing} dataOnly /><Link to="/settings">현재 모델 설정 확인</Link></>}</DataSection>}
    {kind === 'investigation' && agents.some(n => n.card.candidates.length) && <DataSection label="원인 후보" count={agents.reduce((n, a) => n + a.card.candidates.length, 0)}>{() => <CandidateList runs={agents} />}</DataSection>}
    {kind === 'action' && actions.length > 0 && <DataSection label="실행 기록" count={actions.length}>{() => actions.map(n => <div key={n.id}><EscalationNote n={n} /><CoreWork member={n} core={core} /></div>)}</DataSection>}
    {kind === 'handoff' && !covers.includes('handoff') && handoff && (handoff.summary_raw || handoff.next_steps_raw.length > 0) && <DataSection label="인계 원문">{() =>
      <pre className="raw-text">{[handoff.summary_raw, ...handoff.next_steps_raw].filter(Boolean).join('\n')}</pre>}</DataSection>}
    {!hasCalls && kind === 'investigation' && members.length > 0 && !agents.length && <Reading view={readingOf(node.conclusion_ko, { era: 'current' })} />}
  </>;
}
