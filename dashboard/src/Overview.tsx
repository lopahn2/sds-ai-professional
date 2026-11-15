import './incidentOverview.css';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { clock, type Overview } from './api';
import { bytesKo, cpuKo, numberKo, overviewKpis, percent } from './overviewModel';
import { eventReasonKo, levelKo, placeKo } from './labels';
import { Badge, Meter as UsageMeter, StatusIndicator } from '@naria/ui';
import { age } from './api';
import { useLive } from './live';
import { needsOperator, nextStepShort, queueCounts, responseQueue } from './responseQueue';
import { warningSummary } from './presentation';
import { Disclosure } from './Disclosure';
import { ResourceDetail, TopologyMap } from './TopologyMap';
import { usePolling } from './usePolling';
import { ErrorBox, Spinner } from './ui';
const SOURCES: Record<string, string> = { prometheus: 'Prometheus', kubernetes: 'Kubernetes', hubble: 'Hubble', envoy: 'Envoy' };
type SideTab = 'events' | 'namespaces' | 'resource';
/** Compact cluster status for the side area: each figure is a source value, null stays 미관측. */
function Stats({ o }: { o: Overview }) {
  const k = overviewKpis(o);
  const ratio = (used: number | null, capacity: number | null) => percent(used, capacity);
  return <section className="ops-stats" aria-label="클러스터 건강도 요약">
    <UsageMeter label="준비된 노드" value={ratio(k.nodesReady, k.nodesTotal)} valueText={`${numberKo(k.nodesReady)} / ${numberKo(k.nodesTotal)}`} />
    <UsageMeter label="CPU 사용 / 전체" value={ratio(k.cpuUsed, k.cpuCapacity)} valueText={`${numberKo(k.cpuUsed, 2)} / ${numberKo(k.cpuCapacity, 1)} 코어`} warningAt={0.75} dangerAt={0.9} />
    <UsageMeter label="메모리 사용 / 전체" value={ratio(k.memUsed, k.memCapacity)} valueText={`${bytesKo(k.memUsed)} / ${bytesKo(k.memCapacity)}`} warningAt={0.75} dangerAt={0.9} />
    <dl>
      <div><dt>Pod 실행 · 대기 · 실패</dt><dd><b>{numberKo(k.running)}</b> · <span className="warn-text">{numberKo(k.pending)}</span> · <span className="danger-text">{numberKo(k.failed)}</span></dd></div>
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
    {rows.map(([key, records]) => {
      const latest = records[0];
      return <article className="warning-event" key={key}>
        <div><b>{warningTarget(latest, workloads)}</b><span className="badge warn">{eventReasonKo(latest.reason)}</span></div>
        <small>{latest.namespace || '클러스터'} · {clock(latest.at)} · {records.length === 1 && latest.count != null ? `발생 ${latest.count}회` : `경고 기록 ${records.length}건`}</small>
        <p className="warning-summary">{warningSummary(latest.reason, latest.message)}</p>
        <Disclosure label="원문 보기">{() => records.map((record, index) => <div key={index}>
          <small>{clock(record.at)} · {record.object} · {record.reason}</small>
          <pre className="raw-text">{record.message || '메시지 미기록'}</pre>
        </div>)}</Disclosure>
      </article>;
    })}
  </>;
}
/** 첫 화면의 첫 질문은 "지금 내가 결정해야 할 장애가 있는가"다. 승인·인수 대기를 숫자와 목록 맨 앞에 둔다. */
function ResponseQueue({ o }: { o: Overview }) {
  const { open, openState } = useLive();
  const queue = responseQueue(open, o.incidents?.open ?? []);
  const counts = queueCounts(o.incidents?.counts, open, openState === 'ready');
  const waiting = queue.filter(i => needsOperator(i.status, i.claimed));
  const shown = queue.slice(0, 4);
  const decisions = (counts.approval ?? 0) + (counts.handoff ?? 0);
  const calm = openState === 'ready' && queue.length === 0 && decisions === 0 && counts.inProgress === 0;
  return <section className="ops-incidents response-queue" aria-labelledby="response-heading">
    <header>
      <h2 id="response-heading">지금 대응할 일</h2>
      <p className="queue-lead" role="status">{openState === 'error' ? '진행 중 장애 조회에 실패했습니다. 아래는 마지막으로 받은 목록입니다.'
        : decisions || waiting.length ? `운영자 결정이 필요한 장애가 ${Math.max(decisions, waiting.length)}건 있습니다.`
        : queue.length || (counts.inProgress ?? 0) > 0 ? '운영자 결정이 필요한 장애는 없습니다. AI가 분석·조치 중입니다.'
        : calm ? '진행 중인 장애가 없습니다.' : '진행 중 장애를 확인하고 있습니다.'}</p>
    </header>
    <dl className="incident-counts">
      <div className={counts.approval ? 'is-alert' : ''}><dt>승인 대기</dt><dd className={counts.approval == null ? 'is-unknown' : undefined}>{numberKo(counts.approval)}</dd></div>
      <div className={counts.handoff ? 'is-warn' : ''}><dt>미인수 이관</dt><dd className={counts.handoff == null ? 'is-unknown' : undefined}>{numberKo(counts.handoff)}</dd></div>
      <div><dt>진행 중</dt><dd className={counts.inProgress == null ? 'is-unknown' : undefined}>{numberKo(counts.inProgress)}</dd></div>
    </dl>
    {shown.length > 0 ? <ul className="priority-incidents" aria-label="대응 순서대로 정렬한 진행 중 장애">
      {shown.map(i => { const next = nextStepShort(i.status, i.claimed); return <li key={i.id}><Link to={`/incidents/${i.id}`} className={`queue-item tone-${next.tone}`} aria-label={`${i.workload} 장애 ${i.id} · ${next.label} · 관제실 열기`}>
        <span className="queue-item-top"><b>{i.workload}</b><Badge tone={next.tone === 'neutral' ? 'neutral' : next.tone}>{next.label}</Badge></span>
        <small>{placeKo(i.environment, i.system, null, i.namespace)} · {levelKo(i.severity_level)} · #{i.id} · {age(i.created_at)} 경과</small>
      </Link></li>; })}
      {queue.length > shown.length && <li className="queue-more"><Link className="link-btn" to="/incidents">외 {queue.length - shown.length}건 더 보기 →</Link></li>}
    </ul> : <p className="queue-empty">{calm ? <StatusIndicator status="ok">새 장애가 감지되면 여기에 먼저 표시됩니다.</StatusIndicator> : null}</p>}
    <Link className="link-btn queue-all" to="/incidents">전체 장애 보기 →</Link>
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
    <ResponseQueue o={o} />
    <TopologyMap overview={o} selected={selected} onSelect={setSelected} />
    <aside className="ops-side" aria-label="클러스터 상태"><header className="ops-side-head"><b>클러스터 상태</b><span className="faint">{clock(o.generated_at)} 기준</span><button className="btn sm" onClick={refresh}>새로고침</button></header>
      {error && <ErrorBox>갱신에 실패했습니다. 마지막으로 관측한 정보를 표시합니다.</ErrorBox>}
      <section className="cluster-diagnostics" aria-label="클러스터 진단"><h3>클러스터 진단</h3><div className="source-chips" aria-label="데이터 원천 상태">{['kubernetes', 'prometheus', 'hubble', 'envoy'].map(key => { const s = o.sources?.[key]; const ok = s === 'ok' || s === 'connected'; return <div className={`source-chip ${ok ? 'connected' : 'missing'}`} key={key}><b>{SOURCES[key]}</b><StatusIndicator status={ok ? 'ok' : s ? 'warning' : 'unknown'}>{ok ? '연결됨' : s ? '확인 필요' : '미관측'}</StatusIndicator>{s && !ok && <Disclosure label="수집 상태 원문 보기">{() => <pre className="raw-text">{s}</pre>}</Disclosure>}</div>; })}</div><Stats o={o} /></section>
      <nav className="side-tabs" aria-label="보조 정보 전환">{tabs.map(([key, label]) => <button key={key} type="button" aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
      <div className="side-scroll" aria-label={tab === 'events' ? '최근 경고 이벤트' : tab === 'namespaces' ? '네임스페이스별 자원' : '선택한 자원'}>{tab === 'events' ? <Events o={o} /> : tab === 'namespaces' ? <Namespaces o={o} /> : selected && <ResourceDetail key={selected} overview={o} id={selected} onClose={() => setSelected(null)} />}</div>
    </aside>
  </div>;
}
