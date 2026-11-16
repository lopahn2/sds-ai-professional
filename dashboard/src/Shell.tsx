import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityLogIcon, BellIcon, DashboardIcon, GearIcon, ReaderIcon } from '@radix-ui/react-icons';
import { StatusIndicator } from '@naria/ui';
import { needsOperator, nextStepShort, responseQueue } from './responseQueue';
import { age } from './api';
import { alertText } from './feed';
import { placeKo } from './labels';
import { LiveProvider, useLive } from './live';
import { LevelBadge, StatusBadge } from './ui';
/** Breadcrumb for the global bar: where the operator is, not a slogan. */
export function whereKo(path: string): string[] {
  const m = path.match(/^\/incidents\/(\d+)/);
  if (m) return ['장애 관제', `장애 #${m[1]}`];
  if (path.startsWith('/incidents')) return ['장애 관제'];
  if (path.startsWith('/application')) return ['애플리케이션 장애'];
  if (path.startsWith('/ledger')) return ['검토 기록'];
  if (path.startsWith('/settings')) return ['설정'];
  return ['운영 관제'];
}
/** Ongoing incidents live behind one top-right button on every screen instead of a list competing with the map. */
function AlertMenu() {
  const { open, openState } = useLive(); const loc = useLocation();
  const [shown, setShown] = useState(false); const box = useRef<HTMLDivElement>(null); const button = useRef<HTMLButtonElement>(null);
  useEffect(() => setShown(false), [loc.pathname]);
  useEffect(() => {
    if (!shown) return;
    const away = (e: PointerEvent) => { if (!box.current?.contains(e.target as Node)) setShown(false); };
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { setShown(false); button.current?.focus(); } };
    document.addEventListener('pointerdown', away); document.addEventListener('keydown', key);
    return () => { document.removeEventListener('pointerdown', away); document.removeEventListener('keydown', key); };
  }, [shown]);
  // 운영자 결정이 필요한 장애(승인 대기)를 수준보다 먼저 올린다.
  const items = useMemo(() => responseQueue(open), [open]);
  const waiting = items.filter(i => needsOperator(i.status, i.claimed)).length;
  return <div className="alert-menu" ref={box}>
    <button ref={button} type="button" className={`alert-button ${waiting ? 'active' : items.length ? 'ongoing' : ''}`} aria-haspopup="true" aria-expanded={shown} aria-controls="alert-panel" aria-label={openState === 'ready' ? `진행 중 장애 ${items.length}건` : openState === 'error' ? '진행 중 장애 조회 실패' : '진행 중 장애 조회 중'} onClick={() => setShown(v => !v)}>
      <BellIcon aria-hidden /><span className="alert-label">진행 중 장애</span><b className="alert-count">{openState === 'ready' ? items.length : '?'}</b>
    </button>
    {shown && <div className="alert-panel" id="alert-panel" role="region" aria-label="진행 중 장애 목록">
      <header><b>진행 중 장애</b><span className="faint">{openState === 'ready' ? `${items.length}건 · 대응 필요 ${waiting}건 먼저` : '조회 확인 필요'}</span></header>
      {openState !== 'ready' && <p className="notice" role="status">{openState === 'loading' ? '진행 중 장애 조회 중입니다.' : '알림 조회 실패 · 표시된 목록은 마지막 조회 기준입니다.'}</p>}
      {items.length === 0 ? (openState === 'ready' && <p className="empty">진행 중인 장애가 없습니다.</p>) : <ul className="alert-list">{items.map(i => <li key={i.id}><Link className="alert-item" to={`/incidents/${i.id}`} aria-label={`${i.workload} 장애 ${i.id} 관제실 열기`}>
        <span className="alert-item-top"><strong>{i.workload}</strong><LevelBadge value={i.severity_level} /></span>
        <span className="alert-item-meta"><span className="mono">{placeKo(i.environment, i.system, null, i.namespace)} · #{i.id}</span><StatusBadge value={i.status} /><span>{age(i.created_at)} 경과</span></span>
        <span className={`alert-item-next tone-${nextStepShort(i.status, i.claimed).tone}`}>{nextStepShort(i.status, i.claimed).label}</span>
      </Link></li>)}</ul>}
      <Link className="alert-all" to="/incidents">전체 장애 목록 보기 →</Link>
    </div>}
  </div>;
}
const NAV = [['/', '운영 관제', '관제', DashboardIcon], ['/incidents', '장애 관제', '장애', BellIcon], ['/application', '애플리케이션 장애', '앱 장애', ActivityLogIcon], ['/ledger', '검토 기록', '검토', ReaderIcon], ['/settings', '설정', '설정', GearIcon]] as const;
function Frame() {
  const { open, toasts, dismiss, accounts } = useLive();
  const go = useNavigate(); const loc = useLocation();
  const link = ({ isActive }: { isActive: boolean }) => isActive ? 'active' : '';
  const crumbs = whereKo(loc.pathname);
  return <div className="app">
    <nav className="nav" aria-label="주 메뉴">
      <Link className="brand" to="/" aria-label="라이브프로브 운영 관제"><i aria-hidden><ActivityLogIcon /></i><span>라이브프로브<small>장애 대응 · 코드 조치</small></span></Link>
      <span className="nav-caption">작업 공간</span>
      {NAV.map(([to, label, short, Icon]) => <NavLink key={to} to={to} end={to === '/'} className={link} aria-label={label}><span className="nav-icon" aria-hidden><Icon /></span><span className="lbl">{label}</span><span className="nav-short" aria-hidden>{short}</span>{to === '/incidents' && open.length > 0 && <span className="count">{open.length}</span>}</NavLink>)}
      <div className="foot"><StatusIndicator status="info">로컬 운영 환경</StatusIndicator><small>클러스터 · 애플리케이션 관제</small></div>
    </nav>
    <div className="bar"><nav className="crumbs" aria-label="현재 위치">{crumbs.map((c, i) => <span key={c} aria-current={i === crumbs.length - 1 ? 'page' : undefined}>{i > 0 && <span className="bar-divider" aria-hidden>/</span>}{c}</span>)}</nav>
      <div className="bar-right"><span className="bar-account"><StatusIndicator status={!accounts ? 'unknown' : accounts.accounts.some(a => a.state === 'available') ? 'ok' : 'warning'}>{!accounts ? '분석 계정 확인 중' : accounts.accounts.some(a => a.state === 'available') ? '분석 계정 사용 가능' : '분석 계정 확인 필요'}</StatusIndicator></span><AlertMenu /></div></div>
    <main className="main"><Outlet /></main>
    <div className="toasts" aria-live="polite">{toasts.map(t => <button key={t.key} className={`toast ${t.kind}`} onClick={() => { dismiss(t.key); go(`/incidents/${t.id}`); }}>{alertText(t)} · 관제실 열기 ↗</button>)}</div>
  </div>;
}
export default function Shell() {
  const go = useNavigate();
  const onOpen = useCallback((id: number) => go(`/incidents/${id}`), [go]);
  return <LiveProvider onOpenIncident={onOpen}><Frame /></LiveProvider>;
}
