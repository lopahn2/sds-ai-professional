import { memo, useCallback, useLayoutEffect, useMemo, useRef, type CSSProperties, type KeyboardEvent } from 'react';
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
type StageActors = ReadonlyMap<string, string[]>;
const noActors: StageActors = new Map();
type GraphMode = 'stage' | 'all';
type GraphProps = {
  flow: Narrative['flow']; selected: string | null; onSelect: (id: string) => void;
  mode: GraphMode; snapshot: boolean; archived: boolean; previousCore: boolean; frameKey: string; activeStages: StageActors;
};
type CardData = {
  node: FlowNode; actors?: string[]; snapshot: boolean; archived: boolean; selected: boolean; mode: GraphMode;
  choose: GraphProps['onSelect'];
};

function selectWithArrow(event: KeyboardEvent<HTMLButtonElement>, choose: GraphProps['onSelect']) {
  if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return;
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const graph = event.currentTarget.closest('.flow-canvas');
  if (!graph) return;
  const buttons = Array.from(graph.querySelectorAll<HTMLButtonElement>('button[data-stage-id]'));
  const index = buttons.indexOf(event.currentTarget);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : Math.min(buttons.length - 1, Math.max(0, index + (event.key === 'ArrowRight' ? 1 : -1)));
  event.preventDefault();
  buttons[next].focus({ preventScroll: true });
  choose(buttons[next].dataset.stageId!);
}

const StageNode = memo(function StageNode({ id, data }: NodeProps) {
  const d = data as CardData;
  const n = d.node; const rf = useReactFlow();
  const travel = useStageTravel();
  const semantic = isSemanticNode(n) ? n.semantic : null;
  const kind = FLOW_TYPE_KO[n.type] ?? '단계';
  // Depth identifies a graph level, not the execution order of parallel agents.
  const depth = Number.isFinite(n.depth) ? n.depth + 1 : 1;
  const sub = flowSub(n);
  const detail = d.actors?.join(' · ') || (semantic ? sub : n.model || (sub.startsWith(`${kind} · `) ? sub.slice(kind.length + 3) : ''));
  const recorded = d.snapshot || (d.archived && n.status === 'running');
  const handoff = n.type === 'outcome' && /인계/.test(n.label);
  const status = d.actors?.length ? '작업 중' : (recorded ? '당시 ' : '') + (FLOW_STATUS_SHORT[n.status] ?? flowStatusKo(n.status));
  const spokenStatus = d.actors?.length ? '작업 중 · ' + detail : (recorded ? '저장 당시 ' : '') + flowStatusKo(n.status);
  return <div data-working={d.actors?.length ? true : undefined} data-travel={d.selected && travel?.to === id ? travel.direction : undefined}
    className={`flow-step ${n.status} type-${n.type} ${handoff ? 'is-handoff' : ''} ${d.selected ? 'selected' : ''}`}>
    <FacingHandles />
    <button type="button" className="nodrag nopan" data-stage-id={id}
      aria-label={`${kind} · ${flowTitle(n)} · ${spokenStatus}${n.model ? ` · ${n.model}` : ''} · 깊이 ${depth} · 단계 보기`}
      aria-pressed={d.selected} tabIndex={d.mode === 'stage' && !d.selected ? -1 : 0}
      onClick={() => d.choose(id)}
      onKeyDown={d.mode === 'stage' ? event => selectWithArrow(event, d.choose) : undefined}
      onFocus={d.mode === 'all' ? e => revealOnFocus(e.currentTarget, rf, id, FLOW_W, FLOW_H) : undefined}>
    <span className="flow-role-icon"><DiagramIcon kind={handoff ? 'steer' : n.type} /></span>
    <span className="flow-step-top"><strong title={flowTitle(n)}>{flowTitle(n)}</strong><span className={`flow-state ${n.status}`}>{!!d.actors?.length && <i aria-hidden="true" />}{status}</span></span>
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
function analysisFlowEdges(flow: Narrative['flow'], positions = phasePositions(flow.nodes, COLUMN, ROW), column = COLUMN, active = noActors): Edge[] {
  const byId = new Map(flow.nodes.map(n => [n.id, n]));
  const peers = Array.from(positions.values());
  return validFlowEdges(flow.nodes, flow.edges).map(e => {
    const color = edgeColors[e.kind] ?? edgeColors.flow;
    const planned = byId.get(e.target)?.status === 'planned';
    const a = positions.get(e.source)!, b = positions.get(e.target)!;
    const adjacent = a.y === b.y && Math.abs(Math.abs(a.x - b.x) - column) < .01;
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
    return { ...e, sourceHandle: handles[0], targetHandle: handles[1], ariaLabel: `${EDGE_KIND_KO[e.kind] ?? '연결'}${clean ? ` · ${clean}` : ''}: ${flowTitle(byId.get(e.source)!)} → ${flowTitle(byId.get(e.target)!)}`, type: 'smoothstep', animated: active.has(e.target), label,
      style: { stroke: color, strokeWidth: e.kind === 'flow' ? 1.5 : 2, strokeDasharray: planned ? '5 5' : e.kind === 'reroute' ? '9 4' : e.kind === 'steer' ? '3 3' : undefined },
      labelStyle: { fill: color, fontSize: 12, fontWeight: 400 }, labelShowBg: true, labelBgPadding: [4, 2], labelBgBorderRadius: 6, labelBgStyle: { fill: '#ffffff' }, markerEnd: { type: MarkerType.ArrowClosed, color }, focusable: false };
  });
}
function Canvas({ flow, selected, onSelect, snapshot, archived, previousCore, frameKey, activeStages, mode }: GraphProps) {
  const rf = useReactFlow();
  const width = useStore(s => s.width), height = useStore(s => s.height);
  const ready = useStore(s => !!s.panZoom);
  const stage = mode === 'stage';
  // Four pitches show three complete columns and two half columns. In a narrow
  // pane use two pitches so the compact cards keep their original typography.
  const column = stage && width ? width / (width >= 640 ? 4 : 2) : COLUMN;
  const cardWidth = stage ? Math.min(FLOW_W, column - Math.min(48, Math.max(14, column * .1))) : FLOW_W;
  const fitColumns = Math.floor((width - 36 + COLUMN - FLOW_W) / COLUMN);
  const columns = Math.max(stage ? 5 : 1, fitColumns);
  const positions = useMemo(() => phasePositions(flow.nodes, column, ROW, columns), [flow.nodes, column, columns]);
  const all = Array.from(positions.values());
  const graphWidth = Math.max(0, ...all.map(p => p.x)) + cardWidth;
  const graphHeight = Math.max(0, ...all.map(p => p.y)) + FLOW_H;
  const target = positions.get(selected ?? '') ?? all[0];
  const before = useStore(s => stage && s.transform[0] < -1);
  const after = useStore(s => stage && graphWidth * s.transform[2] + s.transform[0] > s.width + 1);

  const fit = useCallback(() => {
    if (!width || !height || !target) return;
    const zoom = Math.max(MIN_READABLE, Math.min(1.12, (width - 48) / graphWidth, (height - 56) / graphHeight));
    const center = (available: number, total: number, on: number) => total * zoom <= available
      ? (available - total * zoom) / 2 : Math.min(20, Math.max(available - total * zoom - 20, available / 2 - on * zoom));
    void rf.setViewport({ x: center(width, graphWidth, target.x + cardWidth / 2), y: center(height, graphHeight, target.y + FLOW_H / 2), zoom });
  }, [width, height, target, graphWidth, graphHeight, cardWidth, rf]);

  const last = useRef<{frame: string; selected: string|null; mode: GraphMode} | null>(null);
  useLayoutEffect(() => {
    if (!ready || !width || !height || !target) return;
    const frame = [mode, width, height, frameKey, stage ? `${target.x}:${target.y}:${graphHeight}` : ''].join('/');
    if (last.current?.frame === frame) return;
    const animate = stage && last.current?.mode === 'stage' && selected !== last.current.selected &&
      !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    last.current = {frame, selected, mode};
    if (!stage) { fit(); return; }
    const y = graphHeight + 24 <= height ? (height - graphHeight) / 2
      : Math.min(12, Math.max(height - graphHeight - 12, (height - FLOW_H) / 2 - target.y));
    void rf.setViewport({ x: (width - cardWidth) / 2 - target.x, y, zoom: 1 },
      { duration: animate ? 420 : 0, interpolate: 'linear' });
  }, [ready, width, height, mode, frameKey, stage, selected, target, graphHeight, cardWidth, rf, fit]);

  const converted = useMemo(() => {
    const nodes: Node[] = flow.nodes.map(n => ({
      id: n.id, type: 'step', position: positions.get(n.id)!, width: cardWidth, height: FLOW_H,
      data: { node: n, actors: activeStages.get(n.id), snapshot, archived: archived || (previousCore && !applicationStage(n)),
        selected: n.id === selected, choose: onSelect, mode },
      style: { width: cardWidth, height: FLOW_H }, focusable: false, draggable: false, selectable: false,
    }));
    return { nodes, edges: analysisFlowEdges(flow, positions, column, activeStages) };
  }, [flow, selected, onSelect, snapshot, archived, positions, previousCore, activeStages, cardWidth, column, mode]);

  return <div className="flow-canvas" data-mode={mode} data-compact={cardWidth < 195 || undefined}
    data-before={before || undefined} data-after={after || undefined}
    style={{ '--stage-fade': `${cardWidth / 2}px`, '--stage-height': `${graphHeight + 32}px` } as CSSProperties}
    aria-label={snapshot ? '저장 당시 에이전트 흐름도' : archived ? '저장된 에이전트 흐름도' : '실시간 에이전트 흐름도'}>
    <ReactFlow nodes={converted.nodes} edges={converted.edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
      minZoom={.15} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
      nodesFocusable={false} edgesFocusable={false} autoPanOnNodeFocus={!stage} deleteKeyCode={null}
      panOnDrag={!stage} zoomOnScroll={!stage} zoomOnPinch={!stage} zoomOnDoubleClick={!stage}
      panActivationKeyCode={stage ? null : 'Space'} zoomActivationKeyCode={stage ? null : undefined}
      preventScrolling={!stage} ariaLabelConfig={GRAPH_ARIA} proOptions={{ hideAttribution: true }}>
      <Background gap={20} size={1} color="#d8dfe1"/>
    </ReactFlow>
    {!stage && <GraphControls label="흐름" onFit={fit}><span>실선 진행</span><span className="warn-text">주황 승격</span><span className="reroute-text">청록 경로 변경</span><span className="accent-text">파란 점선 지시</span><span className="ok-text">초록 검증</span></GraphControls>}
  </div>;
}

/** Both views share the same graph nodes, source edges, selection and status. */
type FlowGraphProps = Pick<GraphProps, 'flow' | 'selected' | 'onSelect'> & Partial<Omit<GraphProps, 'flow' | 'selected' | 'onSelect'>>;
export function FlowGraph({mode = 'all', snapshot = false, archived = snapshot, previousCore = false, frameKey = '', activeStages = noActors, ...props}: FlowGraphProps) {
  if (!props.flow.nodes.length) return <div className="flow-canvas flow-empty" data-mode={mode} role="status">
    <span className="flow-empty-icon"><DiagramIcon kind="group"/></span><strong>아직 기록된 흐름이 없습니다.</strong><span>분석 단계가 기록되면 여기에 표시됩니다.</span></div>;
  return <ReactFlowProvider><Canvas {...props} mode={mode} snapshot={snapshot} archived={archived}
    previousCore={previousCore} frameKey={frameKey} activeStages={activeStages}/></ReactFlowProvider>;
}
