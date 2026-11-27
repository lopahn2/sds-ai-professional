import './incidentOverview.css';
import { lazy, Suspense, useState, type ReactNode } from 'react';
import { EnterFullScreenIcon } from '@radix-ui/react-icons';
import { clock, type AtlasOverview, type Overview } from './api';
import { bytesKo, cpuKo, numberKo, overviewKpis, percent } from './overviewModel';
import { eventReasonKo } from './labels';
import { Badge, Meter as UsageMeter, StatusIndicator } from '@naria/ui';
import { ServiceAtlas } from './ServiceAtlas';
import { ResourceDetail } from './ResourceDetail';
import { Disclosure } from './Disclosure';
import { RoomDialog } from './RoomDialog';
import { usePolling } from './usePolling';

const TopologyMap = lazy(() => import('./TopologyMap').then(module => ({ default: module.TopologyMap })));
const SOURCES: Record<string, string> = { prometheus: 'Prometheus', kubernetes: 'Kubernetes', hubble: 'Hubble', envoy: 'Envoy' };
const ATLAS_HEADERS = { 'X-LiveProbe-Overview-View': 'atlas' };

/** Compact cluster status for the side area: each figure is a source value, null stays 미관측. */
function Stats({ o }: { o: Overview }) {
  const k = overviewKpis(o);
  const ratio = (used: number | null, capacity: number | null) => percent(used, capacity);
  return <section className="ops-stats" aria-label="클러스터 건강도 요약">
    <UsageMeter label="준비된 노드" value={ratio(k.nodesReady, k.nodesTotal)} valueText={`${numberKo(k.nodesReady)} / ${numberKo(k.nodesTotal)}`} />
    <UsageMeter label="CPU 사용 / 전체" value={ratio(k.cpuUsed, k.cpuCapacity)} valueText={`${numberKo(k.cpuUsed, 2)} / ${numberKo(k.cpuCapacity, 1)} 코어`} warningAt={0.75} dangerAt={0.9} />
    <UsageMeter label="메모리 사용 / 전체" value={ratio(k.memUsed, k.memCapacity)} valueText={`${bytesKo(k.memUsed)} / ${bytesKo(k.memCapacity)}`} warningAt={0.75} dangerAt={0.9} />
    <dl>
      <div><dt>Pod 실행 · 대기 · 실패</dt><dd><b>{numberKo(k.running)}</b> · <span className={k.pending ? 'warn-text' : undefined}>{numberKo(k.pending)}</span> · <span className={k.failed ? 'danger-text' : undefined}>{numberKo(k.failed)}</span></dd></div>
      <div><dt>재시작 · 최근 1시간</dt><dd><b>{numberKo(k.restarts)}</b>회</dd></div>
    </dl>
  </section>;
}
type Warning = Overview['events'][number];

function warningTarget(event: Warning, workloads: Set<string>) {
  const pod = /^Pod\/(.+)-[a-z0-9]{8,10}-[a-z0-9]{5}$/.exec(event.object);
  if (pod && workloads.has(`${event.namespace}/${pod[1]}`)) return pod[1];
  return event.object;
}

function Events({ o }: { o: Overview }) {
  const all = o.events ?? [];
  const observed = ['ok', 'connected'].includes(o.sources?.kubernetes);
  const [showAll, setShowAll] = useState(false);
  if (!all.length) return <p className="atlas-empty"><StatusIndicator status={observed && Array.isArray(o.events) ? 'ok' : 'unknown'}>{observed && Array.isArray(o.events) ? '관측된 경고가 없습니다.' : '경고 수집 상태를 확인하세요.'}</StatusIndicator></p>;
  const groups = new Map<string, Warning[]>();
  const workloads = new Set((o.workloads ?? []).map(w => `${w.namespace}/${w.name}`));
  const newest = [...all].sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''));
  for (const event of newest) {
    const key = JSON.stringify([event.namespace, warningTarget(event, workloads), event.reason]);
    const group = groups.get(key);
    if (group) group.push(event); else groups.set(key, [event]);
  }
  const rows = [...groups.entries()];
  return <>{!observed && <p className="atlas-source-message is-warning">마지막으로 수집한 경고입니다.</p>}
    {(showAll ? rows : rows.slice(0, 5)).map(([key, records]) => {
      const latest = records[0];
      return <article className="warning-event" key={key}>
        <div><b>{warningTarget(latest, workloads)}</b><Badge tone="warning">{eventReasonKo(latest.reason)}</Badge></div>
        <small>{latest.namespace || '클러스터'} · {clock(latest.at)} · {records.length === 1 && latest.count != null ? `발생 ${latest.count}회` : `경고 기록 ${records.length}건`}</small>
        <Disclosure label="원문 보기">{() => records.map((record, index) => <div key={index}>
          <small>{clock(record.at)} · {record.object} · {record.reason}</small>
          <pre className="raw-text">{record.message || '메시지 미기록'}</pre>
        </div>)}</Disclosure>
      </article>;
    })}
    {rows.length > 5 && <button type="button" className="atlas-more warning-more" aria-expanded={showAll} onClick={() => setShowAll(v => !v)}>{showAll ? '최근 경고만 보기' : `경고 더 보기 · ${rows.length - 5}개`}</button>}
  </>;
}
function Namespaces({ o }: { o: Overview }) {
  const ns = o.cluster?.namespaces ?? [];
  if (!ns.length) return <p className="atlas-empty"><StatusIndicator status="unknown">네임스페이스 정보가 아직 없습니다.</StatusIndicator></p>;
  return <>{ns.map(n => <article className="ns-row" key={n.name}><div><b className="mono">{n.name}</b><Badge tone={n.workloads_unhealthy ? 'danger' : 'neutral'}>비정상 {numberKo(n.workloads_unhealthy)} / {numberKo(n.workloads)}</Badge></div>
    <dl><dt>Pod 실행 / 대기 / 실패</dt><dd>{numberKo(n.pods_running)} / {numberKo(n.pods_pending)} / {numberKo(n.pods_failed)}</dd><dt>재시작 · 1시간</dt><dd>{numberKo(n.restarts_1h)}</dd><dt>CPU · 메모리</dt><dd>{cpuKo(n.cpu_cores)} · {bytesKo(n.mem_bytes)}</dd></dl></article>)}</>;
}
export function ClusterDiagnostics({ overview, source, refresh, openInfra }: {overview: Overview; source: string; refresh: () => void; openInfra: () => void}) {
  const [tab, setTab] = useState('events');
  return <section className="atlas-diagnostics" aria-label="클러스터 수집 진단">
    <header className="atlas-rail-title"><h2>수집 진단</h2><button type="button" className="nds-button nds-button--outline nds-control--sm" onClick={refresh}><span>새로고침</span></button></header>
    <p className="atlas-dim">마지막 관측 {clock(overview.generated_at)}</p>
    <div className="source-chips">{Object.entries(SOURCES).map(([key, label]) => {
      const value = overview.sources?.[key], ok = value === 'ok' || value === 'connected';
      return <section className="source-chip" data-highlight={key === source} key={key}><b>{label}</b>
        <StatusIndicator status={ok ? 'ok' : value ? 'warning' : 'unknown'}>{ok ? '연결됨' : value ? '수집 확인 필요' : '미관측'}</StatusIndicator>
        {value && !ok && <details><summary>상세보기</summary><pre className="raw-text">{value}</pre></details>}</section>;
    })}</div>
    <Stats o={overview}/>
    {/* The rail is too narrow to read a topology, so the map opens in a wide dialog. */}
    <button type="button" className="atlas-infra-open nds-button nds-button--outline nds-control--sm" aria-haspopup="dialog" onClick={openInfra}><EnterFullScreenIcon aria-hidden/><span>인프라 연결 지도 열기</span></button>
    <nav className="side-tabs" aria-label="수집 진단 항목">{[['events','경고 이벤트'],['namespaces','네임스페이스']].map(([id,label]) =>
      <button type="button" key={id} aria-pressed={tab===id} onClick={()=>setTab(id)}>{label}</button>)}</nav>
    {tab==='events' ? <Events o={overview}/> : <Namespaces o={overview}/>}
  </section>;
}

/** The infrastructure topology at full width; a chosen resource opens its detail beside the map.
 *  The page behind is inert, so the dialog carries its own observation time and refresh. */
function InfraMapDialog({ overview, refresh, onClose }: { overview: Overview; refresh: () => void; onClose: () => void }) {
  const [resource, setResource] = useState<string | null>(null);
  return <RoomDialog title="인프라 연결 지도" className="infra-dialog" onClose={onClose}
    actions={<><span className="infra-observed">마지막 관측 {clock(overview.generated_at)}</span><button type="button" className="btn sm" onClick={refresh}>새로고침</button></>}>
    <div className="infra-body">
      <Suspense fallback={<p role="status">인프라 지도를 불러오는 중입니다.</p>}>
        <TopologyMap overview={overview} selected={resource} onSelect={setResource}/>
      </Suspense>
      {resource && <ResourceDetail overview={overview} id={resource} onClose={() => setResource(null)}/>}
    </div>
  </RoomDialog>;
}

export function OverviewPage() {
  const [inspecting, setInspecting] = useState(false);
  const [infra, setInfra] = useState(false);
  // A single polling stream supplies both views. Keep the last map during a shape switch.
  const { data, error, refresh } = usePolling<AtlasOverview | Overview>('/overview', 10000, {
    deduplicate: false, headers: inspecting || infra ? undefined : ATLAS_HEADERS,
  });
  const full = data && 'cluster' in data ? data : null;
  const fullWarning = error && <p className="atlas-source-message is-warning" role="status">상세 관측 조회 실패 <button type="button" onClick={refresh}>다시 확인</button></p>;
  const details = (render: (value: Overview) => ReactNode) => full
    ? <>{fullWarning}{render(full)}</>
    : fullWarning || <p role="status">상세 관측을 불러오는 중입니다.</p>;
  // The dialog renders outside the rail so the rail's scoped styles do not reach the map.
  return <div className="atlas-page"><h1 className="sr-only">운영 관제</h1>
    <ServiceAtlas overview={data} error={error} refresh={refresh} onInspecting={setInspecting}
      diagnostics={source => details(value => <ClusterDiagnostics overview={value} source={source} refresh={refresh} openInfra={() => setInfra(true)}/>)}
      resourceDetail={(id, onClose) => details(value => <ResourceDetail overview={value} id={id} onClose={onClose} />)} />
    {infra && full && <InfraMapDialog overview={full} refresh={refresh} onClose={() => setInfra(false)}/>}
  </div>;
}
