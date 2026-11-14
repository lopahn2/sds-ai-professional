import './incidentOverview.css';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { clock, type Overview } from './api';
import { bytesKo, cpuKo, numberKo, overviewKpis, percent } from './overviewModel';
import { eventReasonKo, incidentStatusKo, placeKo } from './labels';
import { Disclosure } from './Disclosure';
import { ResourceDetail, TopologyMap } from './TopologyMap';
import { usePolling } from './usePolling';
import { ErrorBox, Spinner } from './ui';
function Meter({ used, capacity }: { used: number | null; capacity: number | null }) { const p = percent(used, capacity); return <div className={`meter ${p == null ? 'unobserved' : ''}`} aria-label={p == null ? '사용률 미관측' : `사용률 ${Math.round(p)}%`}><span style={{ width: `${p ?? 0}%` }} /></div>; }
const SOURCES: Record<string, string> = { prometheus: 'Prometheus', kubernetes: 'Kubernetes', hubble: 'Hubble', envoy: 'Envoy' };
type SideTab = 'events' | 'namespaces' | 'resource';
/** Compact cluster status for the side area: each figure is a source value, null stays 미관측. */
function Stats({ o }: { o: Overview }) {
  const k = overviewKpis(o);
  return <section className="ops-stats" aria-label="클러스터 건강도 요약"><dl>
    <div><dt>준비된 노드</dt><dd><b>{numberKo(k.nodesReady)}</b> / {numberKo(k.nodesTotal)}</dd><Meter used={k.nodesReady} capacity={k.nodesTotal} /></div>
    <div><dt>CPU 사용 / 전체</dt><dd><b>{numberKo(k.cpuUsed, 2)}</b> / {numberKo(k.cpuCapacity, 1)} 코어</dd><Meter used={k.cpuUsed} capacity={k.cpuCapacity} /></div>
    <div><dt>메모리 사용 / 전체</dt><dd><b>{bytesKo(k.memUsed)}</b> / {bytesKo(k.memCapacity)}</dd><Meter used={k.memUsed} capacity={k.memCapacity} /></div>
    <div><dt>Pod 실행 · 대기 · 실패</dt><dd><b>{numberKo(k.running)}</b> · <span className="warn-text">{numberKo(k.pending)}</span> · <span className="danger-text">{numberKo(k.failed)}</span></dd></div>
    <div><dt>재시작 · 최근 1시간</dt><dd><b>{numberKo(k.restarts)}</b>회</dd></div>

  </dl></section>;
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
  if (!all.length) return <div className="empty">{observed && Array.isArray(o.events) ? '관측된 경고가 없습니다.' : '경고 수집 상태를 확인하세요.'}</div>;
  const groups = new Map<string, Warning[]>();
  const workloads = new Set((o.workloads ?? []).map(w => `${w.namespace}/${w.name}`));
  const newest = [...all].sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''));
  for (const event of newest) {
    const key = JSON.stringify([event.namespace, warningTarget(event, workloads), event.reason]);
    const group = groups.get(key);
    if (group) group.push(event); else groups.set(key, [event]);
  }
  const rows = [...groups.entries()];
  return <>{!observed && <p className="notice">마지막으로 수집한 경고입니다.</p>}
    {(showAll ? rows : rows.slice(0, 5)).map(([key, records]) => {
      const latest = records[0];
      return <article className="warning-event" key={key}>
        <div><b>{warningTarget(latest, workloads)}</b><span className="badge warn">{eventReasonKo(latest.reason)}</span></div>
        <small>{latest.namespace || '클러스터'} · {clock(latest.at)} · {records.length === 1 && latest.count != null ? `발생 ${latest.count}회` : `경고 기록 ${records.length}건`}</small>
        <Disclosure label="원문 보기">{() => records.map((record, index) => <div key={index}>
          <small>{clock(record.at)} · {record.object} · {record.reason}</small>
          <pre className="raw-text">{record.message || '메시지 미기록'}</pre>
        </div>)}</Disclosure>
      </article>;
    })}
    {rows.length > 5 && <button type="button" className="link-btn warning-more" aria-expanded={showAll} onClick={() => setShowAll(v => !v)}>{showAll ? '최근 경고만 보기' : `경고 더 보기 · ${rows.length - 5}개`}</button>}
  </>;
}
function IncidentSummary({ o }: { o: Overview }) {
  const counts = o.incidents?.counts;
  const needsAttention = (status: string) => status === 'action_pending' || status === 'handed_off';
  const recent = [...(o.incidents?.open ?? [])]
    .sort((a, b) => Number(needsAttention(b.status)) - Number(needsAttention(a.status)) || b.id - a.id)
    .slice(0, 3);
  return <section className="ops-incidents" aria-labelledby="response-heading">
    <header><h2 id="response-heading">장애 현황</h2><Link className="link-btn" to="/incidents">전체 장애 보기 →</Link></header>
    <dl className="incident-counts">
      <div><dt>진행 중</dt><dd>{numberKo(counts?.in_progress)}</dd></div>
      <div><dt>승인 대기</dt><dd>{numberKo(counts?.awaiting_approval)}</dd></div>
      <div><dt>미인수 이관 · 누적</dt><dd>{numberKo(counts?.unclaimed_handoff)}</dd></div>
    </dl>
    {recent.length > 0 && <ul className="priority-incidents" aria-label="최근 대응 사건">
      {recent.map(i => <li key={i.id}><Link to={`/incidents/${i.id}`}>
        <b>{i.workload}</b><small>{placeKo(i.environment, i.system, null, i.namespace)}</small>
        <span>{incidentStatusKo(i.status)}</span><small>#{i.id}</small>
      </Link></li>)}
    </ul>}
  </section>;
}
function Namespaces({ o }: { o: Overview }) {
  const ns = o.cluster?.namespaces ?? [];
  if (!ns.length) return <div className="empty">네임스페이스 정보가 아직 없습니다.</div>;
  return <>{ns.map(n => <article className="ns-row" key={n.name}><div><b className="mono">{n.name}</b><span className={`badge ${n.workloads_unhealthy ? 'danger' : ''}`}>비정상 {numberKo(n.workloads_unhealthy)} / {numberKo(n.workloads)}</span></div>
    <dl><dt>Pod 실행 / 대기 / 실패</dt><dd>{numberKo(n.pods_running)} / {numberKo(n.pods_pending)} / {numberKo(n.pods_failed)}</dd><dt>재시작 · 1시간</dt><dd>{numberKo(n.restarts_1h)}</dd><dt>CPU · 메모리</dt><dd>{cpuKo(n.cpu_cores)} · {bytesKo(n.mem_bytes)}</dd></dl></article>)}</>;
}
export function OverviewPage() {
  const { data: o, error, refresh } = usePolling<Overview>('/overview', 10000);
  const [selected, setSelected] = useState<string | null>(null); const [tab, setTab] = useState<SideTab>('events');
  useEffect(() => { if (selected) setTab('resource'); else setTab(t => t === 'resource' ? 'events' : t); }, [selected]);
  if (!o) return <div className="page"><h1>운영 관제</h1>{error ? <><ErrorBox>운영 정보를 불러오지 못했습니다.</ErrorBox><button className="btn" onClick={refresh}>다시 불러오기</button></> : <div className="empty" role="status"><Spinner /> 클러스터 관측 정보를 불러오는 중입니다.</div>}</div>;
  const tabs: [SideTab, string][] = [['events', '경고 이벤트'], ['namespaces', '네임스페이스'], ...(selected ? [['resource', '자원 상세'] as [SideTab, string]] : [])];
  return <div className="ops"><h1 className="sr-only">운영 관제</h1>
    <IncidentSummary o={o} />
    <TopologyMap overview={o} selected={selected} onSelect={setSelected} />
    <aside className="ops-side" aria-label="클러스터 상태"><header className="ops-side-head"><b>클러스터 상태</b><span className="faint">{clock(o.generated_at)} 기준</span><button className="btn sm" onClick={refresh}>새로고침</button></header>
      {error && <ErrorBox>갱신에 실패했습니다. 마지막으로 관측한 정보를 표시합니다.</ErrorBox>}
      <Disclosure label="클러스터 진단">{() => <><div className="source-chips" aria-label="데이터 원천 상태">{['kubernetes', 'prometheus', 'hubble', 'envoy'].map(key => { const s = o.sources?.[key]; const ok = s === 'ok' || s === 'connected'; return <div className={`source-chip ${ok ? 'connected' : 'missing'}`} key={key}><i className={`dot ${ok ? 'ok' : 'warn'}`} /><b>{SOURCES[key]}</b><span>{ok ? '연결됨' : s ? '확인 필요' : '미관측'}</span>{s && !ok && <Disclosure label="수집 상태 원문 보기">{() => <pre className="raw-text">{s}</pre>}</Disclosure>}</div>; })}</div><Stats o={o} /></>}</Disclosure>
      <nav className="side-tabs" aria-label="보조 정보 전환">{tabs.map(([key, label]) => <button key={key} type="button" aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
      <div className="side-scroll" aria-label={tab === 'events' ? '최근 경고 이벤트' : tab === 'namespaces' ? '네임스페이스별 자원' : '선택한 자원'}>{tab === 'events' ? <Events o={o} /> : tab === 'namespaces' ? <Namespaces o={o} /> : selected && <ResourceDetail key={selected} overview={o} id={selected} onClose={() => setSelected(null)} />}</div>
    </aside>
  </div>;
}
