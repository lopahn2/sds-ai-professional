import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeftIcon, ArrowRightIcon, ChevronRightIcon, ExclamationTriangleIcon } from '@radix-ui/react-icons';
import { Legend, StatusIndicator, ZoomControls, type StatusKind } from '@naria/ui';
import { clock, type Overview } from './api';
import { ResourceDetail } from './TopologyMap';
import { systemKo } from './labels';
import { useLive } from './live';
import { atlasLanes, caseSummary, chipLabel, envs, laneSummary, linkSummary, mergeCaseSources, observationLabel, readingLabel, readingTone, serviceAtlas, trafficLabel, wirePath, type AtlasCase, type AtlasLane, type AtlasService, type Env, type ReadingTone } from './serviceAtlasModel';
import './serviceAtlas.css';

type Model = ReturnType<typeof serviceAtlas>;
type Selection = { key: string; env: Env; name: string };
const titleOf = (system: string) => systemKo(system) || '소속 미식별';
const statusOf: Record<ReadingTone, StatusKind> = { danger: 'danger', warning: 'warning', ok: 'ok', unknown: 'unknown' };
const sources: Record<string, string> = { kubernetes: 'Kubernetes', prometheus: 'Prometheus', hubble: 'Hubble', envoy: 'Envoy' };
const connected = (s?: string) => s === 'ok' || s === 'connected';
// Mark gaps in the measures this source supplies. A collection warning does not imply a service outage.
function affectedBy(service: AtlasService, source: string) {
  return envs.some(env => {
    const r = service.env[env];
    if (source === 'kubernetes') return !r.resource || r.states.size === 0;
    if (source === 'prometheus' || source === 'envoy') return r.requestRate === null || r.errorRate === null;
    return source === 'hubble' && !r.flowObserved;
  });
}

/** One environment of a service: its name and the naria status (dot + wording). Selecting it opens the detail beside the map. */
function EnvChip({ service, env, selection, onSelect }: { service: AtlasService; env: Env; selection: Selection | null; onSelect: (s: Selection) => void }) {
  const reading = service.env[env], tone = readingTone(reading), detail = readingLabel(reading);
  const selected = selection?.key === service.key && selection.env === env;
  // A ready environment shows only its dot; the words stay for screen readers and the tooltip.
  const quiet = tone === 'ok';
  return <button type="button" className={`atlas-env is-${tone}`} aria-pressed={selected} aria-label={`${service.name} ${env} · ${detail} · 상세 보기`} title={`${env} · ${detail}`} onClick={() => onSelect({ key: service.key, env, name: service.name })}>
    <span className="atlas-env-name">{env}</span><StatusIndicator status={statusOf[tone]}><span className={'atlas-env-text' + (quiet ? ' sr-only' : '')}>{chipLabel(reading)}</span></StatusIndicator>
  </button>;
}

function ServiceNode({ service, meta, gap, selection, onSelect }: { service: AtlasService; meta: string; gap: boolean; selection: Selection | null; onSelect: (s: Selection) => void }) {
  const tones = envs.map(e => readingTone(service.env[e]));
  const tone = tones.includes('danger') ? 'danger' : tones.includes('warning') ? 'warning' : 'neutral';
  const cases = caseSummary(service), line = [cases, meta].filter(Boolean).join(' · ');
  return <article className={`atlas-node is-${tone}${gap ? ' is-source-gap' : ''}`} data-service-key={service.key} aria-label={service.name}>
    <div className="atlas-node-head">
      {tone !== 'neutral' && <span className="atlas-node-icon" aria-hidden><ExclamationTriangleIcon /></span>}
      <div className="atlas-node-text"><h4 title={service.name}>{service.name}</h4>{line && <small title={line}>{cases && <span className="atlas-node-cases">{cases}</span>}{cases && meta && ' · '}{meta}</small>}</div>
    </div>
    <div className="atlas-envs">{envs.map(env => <EnvChip key={env} service={service} env={env} selection={selection} onSelect={onSelect} />)}</div>
  </article>;
}

/** Systems as lanes, services as nodes, measured connections drawn through the gaps. Zoom and legend sit under the canvas. */
function ServiceMap({ model, lanes, selection, select, source }: { model: Model; lanes: AtlasLane[]; selection: Selection | null; select: (s: Selection) => void; source: string | null }) {
  const board = useRef<HTMLDivElement>(null), viewport = useRef<HTMLDivElement>(null);
  const marker = 'atlas-arrow' + useId().replace(/[^a-zA-Z0-9]/g, '');
  const [zoom, setZoom] = useState(100), [width, setWidth] = useState<number>();
  const scale = zoom / 100;
  useLayoutEffect(() => {
    const frame = viewport.current;
    if (!frame) return;
    const fit = () => setWidth(frame.clientWidth);
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(frame);
    return () => observer.disconnect();
  }, []);
  const [paths, setPaths] = useState<{ key: string; d: string; observed: boolean }[]>([]);
  const [showLinks, setShowLinks] = useState(true);
  useLayoutEffect(() => {
    const element = board.current;
    if (!element) return;
    function measure() {
      if (!element) return;
      const rect = element.getBoundingClientRect();
      const box = (node: Element) => {
        const r = node.getBoundingClientRect();
        return { left: (r.left - rect.left) / scale, top: (r.top - rect.top) / scale, right: (r.right - rect.left) / scale, bottom: (r.bottom - rect.top) / scale };
      };
      const nodes = new Map([...element.querySelectorAll<HTMLElement>('[data-service-key]')].map(n => [n.dataset.serviceKey!, box(n)]));
      const obstacles = [...nodes.values()];
      setPaths(model.links.flatMap(edge => {
        const a = nodes.get(edge.from), b = nodes.get(edge.to);
        return a && b ? [{ key: JSON.stringify([edge.from, edge.to]), observed: edge.observed, d: wirePath(a, b, obstacles) }] : [];
      }));
    }
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    for (const n of element.querySelectorAll('[data-service-key]')) observer.observe(n);
    return () => observer.disconnect();
  }, [model, lanes, scale, width]);
  const names = useMemo(() => new Map(model.groups.flatMap(([, s]) => s).map(s => [s.key, s.name])), [model]);
  return <>
    <div className={`atlas-viewport${zoom > 100 ? ' atlas-zoomed' : ''}`} ref={viewport}>
      <div className="atlas-board" ref={board} style={{ zoom: scale, width }}>
        {lanes.map(lane => <section className="atlas-lane" key={lane.system} aria-label={titleOf(lane.system)}>
          <header className="atlas-lane-head"><h3>{titleOf(lane.system)}</h3><p className={lane.danger ? 'is-danger' : lane.warning ? 'is-warning' : undefined}>{laneSummary(lane)}</p></header>
          <div className="atlas-lane-track">{lane.services.map(service => <ServiceNode key={service.key} service={service} meta={linkSummary(service, model.links, names)} gap={!!source && affectedBy(service, source)} selection={selection} onSelect={select} />)}</div>
        </section>)}
        {showLinks && paths.length > 0 && <svg className="atlas-wires" aria-hidden="true">
          <defs><marker id={marker} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0L8 4L0 8Z" /></marker></defs>
          {paths.map(p => <path key={p.key} d={p.d} className={p.observed ? 'observed' : 'declared'} markerEnd={`url(#${marker})`} />)}
        </svg>}
      </div>
    </div>
    <footer className="atlas-map-tools">
      <ZoomControls label="서비스 지도 배율" value={scale} min={.8} max={1.6} onZoomOut={() => setZoom(n => Math.max(80, n - 20))} onZoomIn={() => setZoom(n => Math.min(160, n + 20))} onFit={() => setZoom(100)} />
      <div className="atlas-legend">
        <ul aria-label="상태 범례">
          <li><StatusIndicator status="ok">준비</StatusIndicator></li>
          <li><StatusIndicator status="warning">주의</StatusIndicator></li>
          <li><StatusIndicator status="danger">장애·오류</StatusIndicator></li>
          <li><StatusIndicator status="unknown">미관측</StatusIndicator></li>
        </ul>
        {paths.length > 0 && <>
          <Legend label="연결선 범례" items={[{ label: '관측 연결', kind: 'line' }, { label: '선언 연결', kind: 'dashed' }]} />
          <button type="button" className="atlas-link-toggle" aria-pressed={showLinks} onClick={() => setShowLinks(v => !v)}>연결선 {showLinks ? '숨기기' : '표시'}</button>
        </>}
      </div>
    </footer>
  </>;
}

function CaseList({ items }: { items: AtlasCase[] }) {
  return <ul className="atlas-cases">{items.map(c => <li key={c.key}><Link to={c.href}>
    <span className="atlas-case-head"><b>{c.service}</b><em>{c.environment || '환경 미식별'}</em><span className={`nds-badge nds-tone--${c.intervention ? 'warning' : 'neutral'}`}>{c.label}</span></span>
    <span className="atlas-case-title">{c.title}</span>
    <small>{c.key.startsWith('app:') ? 'Application 이력' : `#${c.key}`}<ChevronRightIcon aria-hidden /></small>
  </Link></li>)}</ul>;
}

export function ServiceAtlas({ overview, error, refresh, diagnostics }: { overview: Overview | null; error: boolean; refresh: () => void; diagnostics: (source: string) => ReactNode }) {
  const { open, openState, automatic: automaticApplications, automaticState, waiting, waitingState } = useLive();
  const [selection, setSelection] = useState<Selection | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [resourceDetails, setResourceDetails] = useState(false);
  const [limit, setLimit] = useState(5);
  const mapTrigger = useRef<HTMLElement | null>(null);
  const select = (s: Selection) => {
    mapTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSource(null); setResourceDetails(false); setSelection(s);
  };
  const openSource = (key: string, trigger: HTMLElement) => { mapTrigger.current = trigger; setSelection(null); setSource(key); };
  const sourceBack = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (source) sourceBack.current?.focus({ preventScroll: !window.matchMedia('(max-width:840px)').matches }); }, [source]);
  const asideHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (selection) asideHeading.current?.focus({ preventScroll: !window.matchMedia('(max-width:840px)').matches }); }, [selection]);
  const cases = useMemo(() => mergeCaseSources(open, openState, overview?.incidents?.open, [...automaticApplications, ...waiting]), [open, openState, overview, automaticApplications, waiting]);
  const model = useMemo(() => serviceAtlas(overview, cases), [overview, cases]);
  const lanes = useMemo(() => atlasLanes(model.groups, model.links), [model]);
  const services = model.groups.flatMap(([, s]) => s);
  const selected = services.find(s => s.key === selection?.key);
  const reading = selection && selected?.env[selection.env];
  const pending = cases.filter(c => c.intervention);
  const automatic = cases.filter(c => !c.intervention && c.state !== 'handed_off');
  const complete = openState === 'ready' && automaticState === 'ready' && waitingState === 'ready' && !!overview && !error;
  const approvals = cases.filter(c => c.approval).length;
  const affected = services.filter(s => envs.some(e => s.env[e].cases.length)).length;
  const counts = overview?.incidents?.counts;
  const availability = useMemo(() => {
    const unavailable: string[] = [];
    if (error || openState === 'error') unavailable.push('Kubernetes 사건');
    if (automaticState === 'error' && waitingState !== 'error') unavailable.push('Application 진행 사건');
    if (waitingState === 'error') unavailable.push('Application 개입 대기');
    return unavailable.join(' · ');
  }, [error, openState, waitingState, automaticState]);
  const states = Object.entries(sources).map(([key, label]) => ({ key, label, status: overview?.sources?.[key] }));
  const problems = states.filter(s => !connected(s.status)), healthy = states.length - problems.length;
  const gapSource = source && source !== 'all' && overview && !connected(overview.sources?.[source]) ? source : null;
  return <div className="atlas-workspace">
    <section className="atlas-map" aria-labelledby="atlas-title">
      <header className="atlas-map-head">
        <div><h2 id="atlas-title">서비스 지도</h2><p>{model.count ? `시스템 ${model.groups.length} · 서비스 ${model.count}${model.links.length ? ` · 연결 ${model.links.length}` : ''}` : '시스템·서비스 관측'}{overview?.generated_at && <span> · {clock(overview.generated_at)} 기준</span>}</p></div>
        {overview && <nav className="atlas-sources" aria-label="수집원 상태">
          {problems.length === 0
            ? <button type="button" className="nds-button nds-button--outline nds-control--sm" aria-pressed={source === 'all'} onClick={e => openSource('all', e.currentTarget)}><StatusIndicator status="ok">수집원 {healthy}개 연결됨</StatusIndicator></button>
            : <>
              {problems.map(s => <button type="button" key={s.key} className="nds-button nds-button--outline nds-control--sm" aria-pressed={source === s.key} onClick={e => openSource(s.key, e.currentTarget)}><StatusIndicator status={s.status ? 'warning' : 'unknown'}>{s.label} · {s.status ? '수집 확인 필요' : '미관측'}</StatusIndicator></button>)}
              {healthy > 0 && <button type="button" className="nds-button nds-button--outline nds-control--sm" aria-pressed={source === 'all'} onClick={e => openSource('all', e.currentTarget)}><StatusIndicator status="ok">그 외 {healthy}개 연결됨</StatusIndicator></button>}
            </>}
        </nav>}
      </header>
      {overview?.preview_notice && <p className="atlas-source-message is-info" role="note"><b>{overview.preview_notice.source_ko}</b> · {new Date(overview.preview_notice.observed_at).toLocaleString('ko-KR')} · {overview.preview_notice.message_ko}</p>}
      {error && <p className="atlas-source-message is-danger" role="status">서비스 지도 {overview ? '갱신 실패 · 마지막 관측' : '조회 실패'} <button type="button" onClick={refresh}>다시 확인</button></p>}
      {!overview && !error && <p className="atlas-source-message is-info" role="status">클러스터 서비스와 연결을 확인하고 있습니다.</p>}
      {overview && overview.sources?.kubernetes && !connected(overview.sources.kubernetes) && <p className="atlas-source-message is-warning">Kubernetes 수집 상태 확인 필요</p>}
      {gapSource && <p className="atlas-source-message is-warning">{sources[gapSource]} 관련 관측값이 없는 서비스에 점선 테두리를 표시합니다.</p>}
      {model.count ? <ServiceMap model={model} lanes={lanes} selection={selection} select={select} source={gapSource} /> : overview && <p className="atlas-blank">현재 지도에 표시할 dev·prd 서비스가 관측되지 않았습니다.</p>}
    </section>
    <aside className="atlas-interventions" aria-label="운영자 작업">
      {source ? <>
        <button ref={sourceBack} type="button" className="atlas-return nds-button nds-button--ghost nds-control--sm" onClick={() => { setSource(null); mapTrigger.current?.focus(); }}><ArrowLeftIcon aria-hidden /><span>지도로 돌아가기</span></button>
        {diagnostics(source)}
      </> : selection ? <>
        <button type="button" className="atlas-return nds-button nds-button--ghost nds-control--sm" onClick={() => { setSelection(null); mapTrigger.current?.focus(); }}><ArrowLeftIcon aria-hidden /><span>지도로 돌아가기</span></button>
        <div className="atlas-detail-head"><h2 ref={asideHeading} tabIndex={-1}>{selection.name}</h2><p className="atlas-dim">{selection.env} · {selected ? titleOf(selected.system) : '현재 관측 없음'}</p></div>
        {reading && <>
          <p className="atlas-reading"><StatusIndicator status={statusOf[readingTone({ ...reading, cases: [] })]}>{[...new Set([observationLabel(reading), trafficLabel(reading, overview?.sources?.prometheus)])].join(' · ')}</StatusIndicator></p>
          <section className="atlas-rail-section">
            <h3>연결된 사건{reading.cases.length ? ` · ${reading.cases.length}` : ''}</h3>
            {reading.cases.length ? <CaseList items={reading.cases} /> : <p className="atlas-empty">{complete ? '현재 연결된 사건이 없습니다.' : '현재 조회된 사건이 없습니다. 일부 관측을 확인 중입니다.'}</p>}
          </section>
          <dl className="atlas-facts">
            <div><dt>Pod 준비 / 목표</dt><dd>{reading.ready ?? '미관측'} / {reading.desired ?? '미관측'}</dd></div>
            <div><dt>준비된 연결 대상</dt><dd>{reading.endpoints ?? '미관측'}</dd></div>
            <div><dt>요청률</dt><dd>{reading.requestRate == null ? '미관측' : reading.requestRate.toLocaleString('ko-KR', { maximumFractionDigits: 3 }) + '건/초'}</dd></div>
            <div><dt>수집 상태</dt><dd>{['kubernetes', 'prometheus'].map(k => sources[k] + ' ' + (connected(overview?.sources?.[k]) ? '연결됨' : overview?.sources?.[k] ? '확인 필요' : '미관측')).join(' · ')}</dd></div>
            <div><dt>마지막 관측</dt><dd>자원 {reading.resource && overview?.inventory_observed_at ? clock(overview.inventory_observed_at) : '미관측'} · 요청률 {reading.metricAt ? clock(new Date(reading.metricAt * 1000).toISOString()) : '미관측'}{reading.flowAt && <> · 흐름 {clock(new Date(reading.flowAt * 1000).toISOString())}</>}</dd></div>
          </dl>
          {reading.resource && overview && <><button type="button" className="atlas-action nds-button nds-button--outline nds-control--sm" aria-expanded={resourceDetails} onClick={() => setResourceDetails(v => !v)}><span>인프라 자원 상세</span></button>{resourceDetails && <ResourceDetail overview={overview} id={reading.resource} onClose={() => setResourceDetails(false)} />}</>}
        </>}
      </> : <>
        <div className="atlas-rail-title"><h2>운영자 개입</h2><span className={`nds-badge nds-tone--${pending.length ? 'warning' : 'neutral'}`} aria-label="개입 대기 건수">{complete ? pending.length : pending.length ? `${pending.length}+` : '—'}</span></div>
        <dl className="atlas-kpis">
          <div><dt>승인 대기</dt><dd className={complete && approvals ? 'is-warning' : undefined}>{complete ? approvals : '—'}</dd></div>
          <div><dt>영향 서비스</dt><dd className={complete && affected ? 'is-danger' : undefined}>{complete ? affected : '—'}</dd></div>
          <div><dt>자동 분석·조치</dt><dd>{openState === 'ready' ? automatic.length : '—'}</dd></div>
        </dl>
        <Link className={`atlas-action nds-button nds-button--${complete && approvals ? 'primary' : 'outline'} nds-control--md nds-full`} to="/incidents?work=approval"><span>운영 승인 {complete ? approvals + '건' : '조회'}</span><ArrowRightIcon aria-hidden /></Link>
        {availability && <p className="atlas-coverage" role="status">{availability} 미관측</p>}
        {!availability && !complete && <p className="atlas-coverage" role="status">개입 대상을 확인하고 있습니다.</p>}
        {pending.length ? <>
          <CaseList items={pending.slice(0, limit)} />
          {pending.length > limit && <button type="button" className="atlas-more" onClick={() => setLimit(n => n + 5)}>개입 대기 더 보기 · {pending.length - limit}</button>}
        </> : <p className="atlas-empty"><StatusIndicator status={complete ? 'ok' : 'unknown'}>{complete ? '지금 개입이 필요한 사건이 없습니다.' : '현재 조회된 개입 대상이 없습니다.'}</StatusIndicator></p>}
        <Link to="/incidents?work=waiting" className="atlas-more">개입 대기 전체 보기<ArrowRightIcon aria-hidden /></Link>
        <section className="atlas-rail-section">
          <h3 className="atlas-auto-title">자동 처리 중<span className="nds-badge nds-tone--neutral">{openState === 'ready' ? automatic.length : '—'}</span></h3>
          {automatic.length ? <CaseList items={automatic.slice(0, 3)} /> : <p className="atlas-dim">{openState === 'ready' ? '진행 중인 자동 처리가 없습니다.' : '자동 처리 목록을 확인하고 있습니다.'}</p>}
          <Link to="/incidents" className="atlas-more">전체 장애 보기<ArrowRightIcon aria-hidden /></Link>
        </section>
        <section className="atlas-rail-section atlas-history">
          <h3>이관 이력</h3>
          <Link to="/incidents?status=handed_off">전체 기간 미인수 <b>{counts?.unclaimed_handoff != null ? counts.unclaimed_handoff.toLocaleString('ko-KR') + '건' : '미관측'}</b><ChevronRightIcon aria-hidden /></Link>
          <p className="atlas-dim">현재 집계와 별도로 누적됩니다.</p>
        </section>
      </>}
    </aside>
  </div>;
}
