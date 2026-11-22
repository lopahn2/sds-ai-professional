import type { ReactNode } from 'react';
import type { FlowEdge, Narrative } from './api';
import type { RunEvent } from './feed';
import type { StreamState } from './execution';
import { isAppStep, type AppSummary } from './applicationFlow';
import { type useAppActivity } from './ApplicationWorkTrace';
import type { SemanticKind, SemanticNode } from './semanticFlow';
import type { WorkSources } from './stageWork';
import { ApplicationStageSource } from './ApplicationCardReading';
import { StageCardBody } from './StageCard';
import './semanticStage.css';
import { useStageTravel } from './StageMotion';

export type CoreStepContext={data:Narrative;events:RunEvent[];historical:boolean;state:StreamState;error:boolean;receivedAt:string|null;readOnly:boolean};
export type AppStepContext={summary:AppSummary;activity:ReturnType<typeof useAppActivity>};

type Props = {
  node: SemanticNode | null; core?: CoreStepContext; application?: AppStepContext; onChange: () => void;
  /** the semantic flow the card belongs to: lineage reads its incoming edges */
  flow?: { nodes: SemanticNode[]; edges: FlowEdge[] };
  /** live work sources shared with the flow graph (which agent or runner is working now) */
  sources?: WorkSources;
  /** the operator's decision controls for this stage; they render at the top of the card */
  decision?: ReactNode; decisionCovers?: SemanticKind[];
  onSelect?: (id: string) => void;
};

/** One selected stage as a briefing stack: header, decision, lineage, brief, then the data it summarises. */
export function SemanticStagePanel({ node, core, application, onChange, flow, sources = {}, decision, decisionCovers, onSelect }: Props) {
  const travel = useStageTravel();
  if (!node) return <section className="step-panel"><h2>단계 미관측</h2></section>;
  const hasApp = node.semantic.members.some(isAppStep);
  const graph = flow ?? { nodes: [node], edges: [] };
  const body = (app?: Parameters<typeof StageCardBody>[0]['app'], appStatus?: ReactNode) =>
    <StageCardBody node={node} flow={graph} sources={sources} core={core} app={app} appStatus={appStatus}
      decision={decision} decisionCovers={decisionCovers} onSelect={onSelect} onChange={onChange} />;
  return <section className="step-panel semantic-stage" aria-label="선택 단계 입력과 결과" tabIndex={-1} data-selected-step={node.id}
    data-travel={travel?.to === node.id ? travel.direction : undefined}>
    {application && hasApp
      ? <ApplicationStageSource key={application.summary.id} node={node} context={application} readOnly={!!core?.readOnly} onChange={onChange}>{(app, status) => body(app, status)}</ApplicationStageSource>
      : body()}
  </section>;
}
