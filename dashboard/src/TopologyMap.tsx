import { memo, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Background, MarkerType, ReactFlow, ReactFlowProvider, useReactFlow, useStore, type NodeProps, type Node, type Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { Overview, Workload, WorkloadIncident } from './api';
import { problemsOf, type TopoNode } from './mapModel';
import { bytesKo, connectionsOf, cpuKo, defaultMapScope, environmentOptions, environmentSummary, edgeLabelFits, edgeText, errorRateKo, handlesFor, groupOffset, MAP_H, MAP_W, numberKo, overviewMap, resourceBreakdown, resourceHealth } from './overviewModel';
import { levelKo, placeKo, systemKo } from './labels';
import { GRAPH_ARIA, FacingHandles, GraphControls, revealOnFocus } from './graphShared';
import { DiagramIcon } from './DiagramIcon';
import './diagramTheme.css';
import { Disclosure } from './Disclosure';
const Resource = memo(function Resource({ id, data }: NodeProps) {
  const d = data as { node: TopoNode; workload?: Workload; incident: WorkloadIncident | null; open: (id: string) => void; selected: boolean; alert: string; offset: { x: number; y: number } };
  const n = d.node, w = d.workload; const health = resourceHealth(n, w); const rf = useReactFlow();
  return <div className="topology-resource"><FacingHandles /><button className={`resource-button nodrag nopan ${n.kind === 'ContractEndpoint' ? 'contract-endpoint' : ''} ${d.incident ? 'has-incident' : d.alert !== 'none' ? 'has-warning' : ''} ${d.selected ? 'selected' : ''}`} aria-pressed={d.incident ? undefined : d.selected} aria-label={`${n.name} ${n.kind === 'ContractEndpoint' ? '계약 경로 미배포 · ' : ''}${d.incident ? `장애 ${d.incident.id} 관제실 열기` : '자원 상세 열기'}`} onClick={() => d.open(n.id)} onFocus={e => revealOnFocus(e.currentTarget, rf, id, MAP_W, MAP_H, d.offset)}>
    <div className="resource-top"><span className={`resource-glyph ${n.role === 'data' ? 'data' : ''}`}><DiagramIcon kind={n.role === 'data' ? 'data' : n.role === 'entry' ? 'entry' : 'resource'} /></span><div><strong title={n.name}>{n.name}</strong><small title={n.system ?? undefined}>{systemKo(n.system) || (n.namespace === 'liveprobe-messaging' ? '공통 연계 인프라' : n.kind)}</small></div><span className={`health-dot ${d.alert !== 'none' || d.incident ? 'unhealthy' : health === '준비됨' ? 'ready' : ['미관측', '흐름만 관측', '계약 경로 미배포'].includes(health) ? 'unknown' : 'unhealthy'}`} aria-label={health} /></div>
    <div className="resource-health" title={health}>{d.incident ? <span className="resource-place">{placeKo(d.incident.environment === undefined ? n.environment : d.incident.environment, d.incident.system === undefined ? n.system : d.incident.system, n.name, n.namespace)}</span> : health}{w && w.ready != null && w.desired != null ? ` · ${w.ready}/${w.desired}` : ''}{!d.incident && d.alert !== 'none' && <span className="observation-warning">관측 경고</span>}</div>
    {(d.incident || n.kind === 'ContractEndpoint') && <div className="resource-observation">{d.incident ? '장애 관제실 열기 →' : '배포 미관측 · 계약 경로'}</div>}
    {d.incident && <span className="incident-pin"><i aria-hidden>!</i>{levelKo(d.incident.level)} 장애 ↗</span>}
  </button></div>;
});
function Namespace({ data }: NodeProps) {
  const b = data.breakdown as ReturnType<typeof resourceBreakdown>;
  // 정돈-05: “N개 서비스” used to be the resource count. Services (distinct workloads) and resources by kind are shown apart.
  const stats = data.kind === 'system' ? `서비스 ${b.services}개 · 자원 ${b.resources}개` : `자원 ${b.resources}개`;
  return <div className={`namespace-box ${data.kind === 'system' ? 'system-box' : data.kind === 'environment' ? 'environment-box' : ''}`}><div className="namespace-heading"><span className="namespace-title"><DiagramIcon kind="namespace" /><b>{String(data.name)}</b></span><span className="namespace-stats">{stats}{Number(data.problems) > 0 && <em> · {String(data.problems)}개 확인</em>}</span></div></div>;
}
const types = { resource: Resource, namespace: Namespace };
type MapData = ReturnType<typeof overviewMap>;
function Canvas({ m, open, selected, fitKey }: { m: MapData; open: (id: string) => void; selected: string | null; fitKey: string }) {
  const rf = useReactFlow(); const width = useStore(s => s.width), height = useStore(s => s.height);
  // Fit when geometry changes, not when telemetry alone refreshes; preserve the operator's pan/zoom otherwise.
  useEffect(() => { if (width > 0 && height > 0) { const t = requestAnimationFrame(() => void rf.fitView({ padding: .05, maxZoom: 1 })); return () => cancelAnimationFrame(t); } }, [fitKey, width, height, rf]);
  const flow = useMemo(() => {
    const groups = new Map(m.groups.map(g => [g.id, g]));
    const members = (id: string) => m.nodes.filter(n => n.groupId === id || groups.get(n.groupId)?.parentId === id);
    const nodes: Node[] = [...m.groups.map(g => ({ id: g.id, type: 'namespace', parentId: g.parentId, position: { x: g.x, y: g.y }, width: g.width, height: g.height, data: { name: g.name, kind: g.kind, breakdown: resourceBreakdown(members(g.id).map(n => n.node)), problems: members(g.id).filter(n => n.incident || n.alert !== 'none').length }, style: { width: g.width, height: g.height }, selectable: false, focusable: false, draggable: false })),
      ...m.nodes.map(n => ({ id: n.id, type: 'resource', parentId: n.groupId, extent: 'parent' as const, position: { x: n.x, y: n.y }, width: MAP_W, height: MAP_H, data: { ...n, open, selected: n.id === selected, offset: groupOffset(m.groups, n.groupId) }, style: { width: MAP_W, height: MAP_H }, focusable: false, selectable: false, draggable: false }))];
    const abs = new Map(m.nodes.map(n => { const g = groupOffset(m.groups, n.groupId); return [n.id, { x: n.x + g.x, y: n.y + g.y, group: n.groupId }]; }));
    const pairs = new Set(m.edges.map(e => `${e.source}>${e.target}`));
    const edges: Edge[] = m.edges.map(e => {
      const color = e.relation === 'declared_event' ? '#80d9cc' : e.relation === 'declared_snapshot_api' ? '#89dcb0' : e.state === 'blocked' ? '#ff9da6' : e.state === 'degraded' ? '#f3ca68' : e.observed ? '#b6c0c4' : '#707d83';
      const a = abs.get(e.source)!, b = abs.get(e.target)!;
      // Numbers are drawn only where they cannot land on a card or another label; the resource detail lists every connection.
      const label = !e.contract && groups.get(a.group)?.kind !== 'environment' && edgeLabelFits(a, b, pairs.has(`${e.target}>${e.source}`)) ? edgeText(e, { compact: true }) : undefined;
      const ariaLabel = `${m.nodes.find(n => n.id === e.source)?.node.name} → ${m.nodes.find(n => n.id === e.target)?.node.name} · ${edgeText(e)}`;
      const handles = e.relation === 'declared_event' ? { sourceHandle: 'sb', targetHandle: 'tt' } : e.relation === 'declared_snapshot_api' ? { sourceHandle: 'st', targetHandle: 'tb' } : handlesFor(a, b);
      return { id: e.id, source: e.source, target: e.target, className: e.relation, ...handles, type: 'smoothstep', pathOptions: { borderRadius: 16, offset: e.contract ? 34 : 22 }, ariaLabel, label, labelStyle: { fontSize: 12, fill: '#b9c8da', fontFamily: 'var(--font)' }, labelBgPadding: [6, 2], labelBgBorderRadius: 6, labelBgStyle: { fill: '#11181c', fillOpacity: 1 }, style: { stroke: color, strokeWidth: e.contract ? 1.8 : e.observed ? 1.8 : 1.2, strokeDasharray: e.relation === 'declared_event' ? '8 4' : e.relation === 'declared_snapshot_api' ? '2 5' : e.observed ? undefined : '5 5', opacity: e.stale ? .4 : .9 }, markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 }, focusable: false };
    });
    return { nodes, edges };
  }, [m, open, selected]);
  return <><ReactFlow nodes={flow.nodes} edges={flow.edges} nodeTypes={types} fitView fitViewOptions={{ padding: .05, maxZoom: 1 }} minZoom={.15} maxZoom={1.6} nodesConnectable={false} nodesDraggable={false} elementsSelectable={false} edgesFocusable={false} nodesFocusable={false} deleteKeyCode={null} ariaLabelConfig={GRAPH_ARIA} proOptions={{ hideAttribution: true }}><Background gap={24} size={1} color="#303b40" /></ReactFlow>
    <GraphControls label="지도"><details className="map-legend-toggle"><summary>지도 범례</summary><div><span><i className="legend-line" />실측 네트워크</span><span><i className="legend-line event" />발행·소비 이벤트 계약</span><span><i className="legend-line snapshot" />복구 API 계약</span><span><i className="legend-line dashed" />미관측 구조</span><span><i className="dot danger" />장애 위치</span><span className="map-connection-count">실측 연결 {m.edges.filter(e => e.observed).length}개 · 계약 {m.edges.filter(e => e.contract).length}개</span></div></details></GraphControls></>;
}
/** The operations map: the dominant surface. Selecting a failed resource opens its incident room; others open the side detail. */
export function TopologyMap({ overview: o, selected, onSelect }: { overview: Overview; selected: string | null; onSelect: (id: string | null) => void }) {
  const go = useNavigate(); const envs = environmentOptions(o), envMode = envs.length > 0;
  const [selection, setSelection] = useState<string | null>(null); const [showPlatform, setShowPlatform] = useState(false);
  const [compactServices, setCompactServices] = useState(true);
  const namespace = selection == null || (envMode && selection && !envs.some(e => e.name === selection)) ? defaultMapScope(o) : selection;
  const m = useMemo(() => overviewMap(o, namespace, { showPlatform, compactServices }), [o, namespace, showPlatform, compactServices]);
  const all = useMemo(() => overviewMap(o, namespace, { showPlatform: true }), [o, namespace]);
  const names = [...new Set((o.topology?.nodes ?? []).flatMap(n => n.namespace ? [n.namespace] : []))].sort();
  const open = useMemo(() => (id: string) => { const n = all.nodes.find(n => n.id === id); if (n?.incident) go(`/incidents/${n.incident.id}`); else onSelect(id); }, [all, go, onSelect]);
  const shown = new Set(m.nodes.map(n => n.id));
  const ingressCount = [...m.foldedIngresses.values()].reduce((sum, items) => sum + items.length, 0);
  const fitKey = JSON.stringify([namespace, showPlatform, m.groups.map(g => [g.id, g.x, g.y, g.width, g.height]), m.nodes.map(n => [n.id, n.groupId, n.x, n.y])]);
  // The map opens in the wide infrastructure dialog, whose title names it; the toolbar carries only the controls.
  return <section className="ops-map" aria-label="클러스터 연결 지도"><div className="map-toolbar">
      <select className="select" aria-label={envMode ? "지도 환경" : "지도 네임스페이스"} value={namespace} onChange={e => { setSelection(e.target.value); onSelect(null); }}>{envMode ? <>{envs.map(e => <option key={e.name} value={e.name}>{e.name}</option>)}<option value="">전체 환경</option></> : <><option value="">전체 네임스페이스</option>{names.map(n => <option key={n}>{n}</option>)}</>}</select>
      {m.foldedIngresses.size > 0 && <button type="button" className="btn sm" aria-pressed={!compactServices} onClick={() => setCompactServices(v => !v)}>{compactServices ? `접힌 Ingress ${ingressCount}개 펼치기` : '서비스 단위로 접기'}</button>}
      <select className="select" aria-label="지도 자원 선택" value="" onChange={e => e.target.value && open(e.target.value)}><option value="">자원 상세 또는 장애 열기</option>{all.nodes.map(n => <option value={n.id} key={n.id}>{n.node.name} · {n.node.namespace ?? '외부'}{n.node.kind === 'ContractEndpoint' ? ' · 계약 경로 미배포' : ''}{n.incident ? ' · 장애' : ''}{shown.has(n.id) ? '' : ' · 지도에서 숨김'}</option>)}</select>
      {(envMode || !namespace) && (m.hiddenPlatform > 0 || showPlatform) && <button type="button" className="btn sm" aria-pressed={showPlatform} onClick={() => setShowPlatform(v => !v)}>{showPlatform ? '시스템 구성요소 숨기기' : `시스템 구성요소 ${m.hiddenPlatform}개 표시`}</button>}
      <span className="badge" title={resourceBreakdown(m.nodes.map(n => n.node)).text || undefined}>표시 자원 {m.nodes.filter(n => n.node.kind !== 'ContractEndpoint').length}개 · 서비스 {resourceBreakdown(m.nodes.map(n => n.node)).services}개{m.nodes.some(n => n.node.kind === 'ContractEndpoint') ? ` · 미배포 계약 끝점 ${m.nodes.filter(n => n.node.kind === 'ContractEndpoint').length}개` : ''}</span></div>
    {envMode && <div className="environment-summary" aria-label="환경별 관측 요약">{envs.map(e => <span className={`environment-chip ${namespace === e.name ? 'active' : ''}`} key={e.name}><b>{e.name}</b><span>{environmentSummary(e)}</span></span>)}</div>}
    {m.edges.some(e => e.contract) && <Disclosure className="contract-list" label="연결·계약 설명">{() => <><p>점선 계약은 실측이 아닙니다. 발행·소비 방향과 실제 접속 방향을 구분합니다.</p><ul>{m.edges.filter(e => e.contract).map(e => <li key={e.id}>{e.contract!.environment} · {m.nodes.find(n => n.id === e.source)?.node.name} → {m.nodes.find(n => n.id === e.target)?.node.name} · {edgeText(e)}</li>)}</ul></>}</Disclosure>}
    {envMode && namespace && envs.find(e => e.name === namespace)?.missing && <div className="notice">{namespace} 환경 미관측 · 자원이 없거나 정상이라는 뜻은 아닙니다.</div>}
    {!o.topology && <div className="notice">토폴로지 미관측 · 연결이 없다는 뜻은 아닙니다.</div>}
    {o.topology?.freshness?.stale && <div className="notice">네트워크 흐름 관측이 지연되었습니다. 흐린 연결선은 현재 상태를 보장하지 않습니다.</div>}
    <div className="topology-canvas" aria-label={envMode ? "환경별 시스템 토폴로지" : "네임스페이스별 토폴로지"}><ReactFlowProvider><Canvas m={m} open={open} selected={selected} fitKey={fitKey} /></ReactFlowProvider></div>
  </section>;
}
/** Collected fields of one resource, shown in the side area. Unobserved never reads as healthy. */
export function ResourceDetail({ overview: o, id, onClose }: { overview: Overview; id: string; onClose: () => void }) {
  const m = overviewMap(o, '', { showPlatform: true }); const d = m.nodes.find(n => n.id === id);
  if (!d) return null;
  const links = connectionsOf(m, id);
  return <aside className="resource-detail" aria-label="자원 상세"><div className="section-heading"><div><span className="eyebrow">{d.node.kind} · {d.node.namespace ?? '외부'}</span><h3>{d.node.name}</h3></div><button className="btn sm" onClick={onClose}>자원 상세 닫기</button></div>
    <p>{resourceHealth(d.node, d.workload)}{d.workload?.ready != null && d.workload?.desired != null ? ` · 준비 ${d.workload.ready}/${d.workload.desired}` : ''}</p>
    <p>{placeKo(d.node.environment, d.node.system, d.node.name, d.node.namespace)}</p>
    {d.node.kind === 'ContractEndpoint' && <div className="notice">승인된 연계 경로의 끝점이며 실제 Kubernetes 자원이 아닙니다. 해당 자원의 배포가 관측되기 전까지 준비됨·정상으로 표시하지 않습니다.</div>}
    {problemsOf(d.node).length > 0 && <div className="notice"><b>관측 경고</b><ul>{problemsOf(d.node).map((p, i) => <li key={i}>{p.text}</li>)}</ul><small>수집된 관측 창의 값이며 자원 준비 상태와 별개입니다.</small></div>}
    <dl className="kv"><dt>요청</dt><dd>{numberKo(d.workload?.req_rate, 1)}{d.workload?.req_rate != null ? '/초' : ''}</dd><dt>오류율</dt><dd>{errorRateKo(d.workload?.err_rate)}</dd><dt>상위 95% 지연</dt><dd>{numberKo(d.workload?.p95_ms, 0)}{d.workload?.p95_ms != null ? 'ms' : ''}</dd><dt>CPU · 메모리</dt><dd>{cpuKo(d.workload?.cpu)} · {bytesKo(d.workload?.mem)}</dd><dt>재시작 · 1시간</dt><dd>{numberKo(d.workload?.restarts_1h)}</dd></dl>
    {(m.foldedIngresses.get(id) ?? []).map(ingressId => {
      const ingress = m.nodes.find(n => n.id === ingressId)?.node;
      return ingress && <Disclosure key={ingressId} label={`Ingress · ${ingress.name} 원문 보기`}>{() => <pre className="raw-text">{JSON.stringify(ingress, null, 2)}</pre>}</Disclosure>;
    })}
    {(d.node.services ?? []).map(s => <div className="service-detail" key={s.name}><b>Service · {s.name}</b><span>포트 {(s.ports ?? []).join(', ') || '미관측'}</span><span>준비된 엔드포인트 {numberKo(s.endpoints_ready)}</span></div>)}
    {links.length > 0 && <div className="service-detail"><b>연결 · {links.length}개</b>{links.map(l => <span key={l.id}>{l.direction === 'out' ? '→' : '←'} {l.peer} · {l.text}{l.state === 'blocked' ? ' · 차단' : l.state === 'degraded' ? ' · 저하' : ''}</span>)}</div>}
    <Disclosure label="수집 필드 원문 보기">{() => <pre className="raw-text">{JSON.stringify({ resource: d.node, workload: d.workload ?? null }, null, 2)}</pre>}</Disclosure><p className="muted">미관측은 정상을 뜻하지 않습니다.</p>
  </aside>;
}
