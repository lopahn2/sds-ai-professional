import { memo, useCallback, useEffect, useMemo, useRef } from 'react';
import { Background, MarkerType, ReactFlow, ReactFlowProvider, SmoothStepEdge, getSmoothStepPath, useReactFlow, useStore, type NodeProps, type Node, type Edge, type EdgeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { Narrative } from './api';
import type { FlowNode } from './api';
import { FLOW_H, FLOW_W, EDGE_KIND_KO, FLOW_STATUS_SHORT, FLOW_TYPE_KO, flowStatusKo, flowSub, flowTitle, validFlowEdges } from './narrative';
import { GRAPH_ARIA, FacingHandles, GraphControls, revealOnFocus } from './graphShared';
import { DiagramIcon } from './DiagramIcon';
import './diagramTheme.css';
import './analysisFlow.css';
import { summaryKo } from './presentation';
import { phasePositions } from './roomWorkflow';
import { isSemanticNode } from './semanticFlow';
import { useStageTravel } from './StageMotion';

function applicationStage(node: FlowNode | undefined): boolean {
  return !!node && (node.type === 'code_action' || 'app' in node ||
    (isSemanticNode(node) && node.semantic.members.some(member => 'app' in member || member.type === 'code_action')));
}
const StageNode = memo(function StageNode({ id, data }: NodeProps) {
  const d = data as { node: FlowNode; snapshot: boolean; archived: boolean; selected: boolean; choose: (id: string) => void };
  const n = d.node; const rf = useReactFlow();
  const travel = useStageTravel();
  const semantic = isSemanticNode(n) ? n.semantic : null;
  const kind = FLOW_TYPE_KO[n.type] ?? '단계';
  // Depth identifies a graph level, not the execution order of parallel agents.
  const depth = Number.isFinite(n.depth) ? n.depth + 1 : 1;
  const sub = flowSub(n);
  const detail = semantic ? sub : n.model || (sub.startsWith(`${kind} · `) ? sub.slice(kind.length + 3) : '');
  const recorded = d.snapshot || (d.archived && n.status === 'running');
  const handoff = n.type === 'outcome' && /인계/.test(n.label);
  return <div data-travel={d.selected && travel?.to === id ? travel.direction : undefined} className={`flow-step ${n.status} type-${n.type} ${handoff ? 'is-handoff' : ''} ${d.selected ? 'selected' : ''}`}><FacingHandles /><button type="button" className="nodrag nopan" aria-label={`${kind} · ${flowTitle(n)} · ${recorded ? '저장 당시 ' : ''}${flowStatusKo(n.status)}${n.model ? ` · ${n.model}` : ''} · 깊이 ${depth} · 단계 보기`} aria-pressed={d.selected} onClick={() => d.choose(n.id)} onFocus={e => revealOnFocus(e.currentTarget, rf, id, FLOW_W, FLOW_H)}>
    <span className="flow-role-icon"><DiagramIcon kind={handoff ? 'steer' : n.type} /></span>
    <span className="flow-step-top"><strong title={flowTitle(n)}>{flowTitle(n)}</strong><span className={`flow-state ${n.status}`}>{!d.archived && n.status === 'running' && <i aria-hidden="true" />}{recorded ? '당시 ' : ''}{FLOW_STATUS_SHORT[n.status] ?? flowStatusKo(n.status)}</span></span>
    <span className="flow-step-meta">{!semantic && <><span className="flow-depth" title={`흐름 깊이 ${depth}`}>{String(depth).padStart(2, '0')}</span><span className="flow-kind">{kind}</span></>}{detail && <small className={`flow-sub${n.model ? ' flow-model' : ''}`} title={detail}>{detail}</small>}</span>
  </button></div>;
});
const nodeTypes = { step: StageNode };
function StageEdge(props: EdgeProps) {
  const travel = useStageTravel();
  const forward = travel?.from === props.source && travel.to === props.target;
  const reverse = travel?.from === props.target && travel.to === props.source;
  return <><SmoothStepEdge {...props}/>
    {(forward || reverse) && <path key={travel!.to} d={getSmoothStepPath(props)[0]}
      className="stage-travel-edge" data-reverse={reverse || undefined} pathLength={1}
      fill="none" pointerEvents="none" aria-hidden="true"/>}</>;
}
const edgeTypes = { smoothstep: StageEdge };
const COLUMN = FLOW_W + 48;
const ROW = FLOW_H + 32;
const MIN_READABLE = .88;
// naria light tokens: strong-border, warning, teal-700 (reroute, = --diagram-reroute in the room), accent (modern palette), success
const edgeColors: Record<string, string> = { flow: '#707d83', escalate: '#825600', reroute: '#086d72', steer: '#32547c', verify: '#176a46' };
/** Labels only occupy the gap between adjacent rows; skipped/parallel paths retain their full accessible description. */
function analysisFlowEdges(flow: Narrative['flow'], positions = phasePositions(flow.nodes, COLUMN, ROW), archived = false, previousCore = false): Edge[] {
  const byId = new Map(flow.nodes.map(n => [n.id, n]));
  const peers = Array.from(positions.values());
  return validFlowEdges(flow.nodes, flow.edges).map(e => {
    const color = edgeColors[e.kind] ?? edgeColors.flow;
    const planned = byId.get(e.target)?.status === 'planned';
    const a = positions.get(e.source)!, b = positions.get(e.target)!;
    const adjacent = a.y === b.y && Math.abs(a.x - b.x) === COLUMN;
    let handles = a.x < b.x ? ['sr','tl'] : a.x > b.x ? ['sl','tr'] : a.y < b.y ? ['sb','tt'] : ['st','tb'];
    // A join or fork on a wrap boundary can skip a peer in the same column.
    // Route around that peer so the arrow cannot resemble an extra stage.
    if (a.x === b.x && peers.some(p => p.x === a.x && p.y > Math.min(a.y,b.y) && p.y < Math.max(a.y,b.y))) {
      handles = a.x === 0 ? ['sl','tl'] : ['sr','tr'];
    } else if (a.y === b.y && peers.some(p => p.y === a.y && p.x > Math.min(a.x,b.x) && p.x < Math.max(a.x,b.x))) {
      handles = ['st','tt'];
    }
    const clean = summaryKo(e.label, EDGE_KIND_KO[e.kind] ?? '', 40);
    const label = adjacent && e.kind !== 'flow' ? clean : undefined;
    return { ...e, sourceHandle: handles[0], targetHandle: handles[1], ariaLabel: `${EDGE_KIND_KO[e.kind] ?? '연결'}${clean ? ` · ${clean}` : ''}: ${flowTitle(byId.get(e.source)!)} → ${flowTitle(byId.get(e.target)!)}`, type: 'smoothstep', animated: !archived && (!previousCore || applicationStage(byId.get(e.target))) && byId.get(e.target)?.status === 'running', label,
      style: { stroke: color, strokeWidth: e.kind === 'flow' ? 1.5 : 2, strokeDasharray: planned ? '5 5' : e.kind === 'reroute' ? '9 4' : e.kind === 'steer' ? '3 3' : undefined },
      labelStyle: { fill: color, fontSize: 12, fontWeight: 400 }, labelShowBg: true, labelBgPadding: [4, 2], labelBgBorderRadius: 6, labelBgStyle: { fill: '#ffffff' }, markerEnd: { type: MarkerType.ArrowClosed, color }, focusable: false };
  });
}
function Canvas({ flow, selected, choose, snapshot, archived, previousCore, frameKey }: { flow: Narrative['flow']; selected: string | null; choose: (id: string) => void; snapshot: boolean; archived: boolean; previousCore: boolean; frameKey: string }) {
  const rf = useReactFlow();
  const width = useStore(s => s.width), height = useStore(s => s.height);
  const columns = Math.max(1, Math.floor((width - 36 + COLUMN - FLOW_W) / COLUMN));
  const positions = useMemo(() => phasePositions(flow.nodes, COLUMN, ROW, columns), [flow.nodes, columns]);
  const lastFrame = useRef('');
  // Keep focused buttons mounted and retain the viewport during live updates.
  const fit = useCallback(() => {
    if (!width || !height || !positions.size) return;
    const all = Array.from(positions.values());
    const w = Math.max(...all.map(p => p.x)) + FLOW_W;
    const h = Math.max(...all.map(p => p.y)) + FLOW_H;
    const zoom = Math.max(MIN_READABLE, Math.min(1.12, (width - 48) / w, (height - 56) / h));
    const active = selected ? positions.get(selected) : null;
    const center = (available: number, total: number, target: number) => total * zoom <= available
      ? (available - total * zoom) / 2 : Math.min(20, Math.max(available - total * zoom - 20, available / 2 - target * zoom));
    void rf.setViewport({ x: center(width, w, active ? active.x + FLOW_W / 2 : 0),
      y: center(height, h, active ? active.y + FLOW_H / 2 : 0), zoom });
  }, [width, height, positions, selected, rf]);
  useEffect(() => {
    const frame = `${width}x${height}:${frameKey}`;
    if (!width || !height || !positions.size || lastFrame.current === frame) return;
    lastFrame.current = frame;
    fit();
  }, [width, height, positions.size, frameKey, fit]);
  const converted = useMemo(() => {
    const nodes: Node[] = flow.nodes.map(n => ({ id: n.id, type: 'step', position: positions.get(n.id)!, width: FLOW_W, height: FLOW_H, data: { node: n, snapshot, archived: archived || (previousCore && !applicationStage(n)), selected: n.id === selected, choose }, style: { width: FLOW_W, height: FLOW_H }, focusable: false, draggable: false, selectable: false }));
    return { nodes, edges: analysisFlowEdges(flow, positions, archived, previousCore) };
  }, [flow, selected, choose, snapshot, archived, positions, previousCore]);
  return <div className="flow-canvas" aria-label={snapshot ? '저장 당시 에이전트 흐름도' : archived ? '저장된 에이전트 흐름도' : '실시간 에이전트 흐름도'}><ReactFlow nodes={converted.nodes} edges={converted.edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} minZoom={.15} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false} nodesFocusable={false} edgesFocusable={false} deleteKeyCode={null} ariaLabelConfig={GRAPH_ARIA} proOptions={{ hideAttribution: true }}><Background gap={20} size={1} color="#d8dfe1" /></ReactFlow>
    <GraphControls label="흐름" onFit={fit}><span>실선 진행</span><span className="warn-text">주황 승격</span><span className="reroute-text">청록 경로 변경</span><span className="accent-text">파란 점선 지시</span><span className="ok-text">초록 검증</span></GraphControls></div>;
}
/** Each node opens its own observed step. Keyboard focus reveals off-screen branches. */
export function FlowGraph({ flow, selected, onSelect, snapshot = false, archived = snapshot, previousCore = false, frameKey = '' }: { flow: Narrative['flow']; selected: string | null; onSelect: (id: string | null) => void; snapshot?: boolean; archived?: boolean; previousCore?: boolean; frameKey?: string }) {
  if (flow.nodes.length === 0) return <div className="flow-canvas flow-empty" role="status"><span className="flow-empty-icon"><DiagramIcon kind="group" /></span><strong>아직 기록된 흐름이 없습니다.</strong><span>분석 단계가 기록되면 여기에 표시됩니다.</span></div>;
  return <ReactFlowProvider><Canvas snapshot={snapshot} archived={archived} previousCore={previousCore} flow={flow} selected={selected} choose={onSelect} frameKey={frameKey} /></ReactFlowProvider>;
}
