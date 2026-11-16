import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { usePolling } from './usePolling';
import { ApiError } from './api';
import { Disclosure } from './Disclosure';
import { ErrorBox } from './ui';
import { HumanAccessForm, aborted, authError, useHumanAccess } from './HumanAccess';
import { summaryKo } from './presentation';
import { applicationStateKo, latestAnalysisSummary, object, observedTime, type ApplicationHistory, type CodeActionSummary, type UnifiedCatalog } from './unified';

function ApplicationHistoryView() {
  const auth = useHumanAccess();
  const [data, setData] = useState<ApplicationHistory | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [search, setSearch] = useState(''), [environment, setEnvironment] = useState(''), [offset, setOffset] = useState(0), [revision, setRevision] = useState(0);
  const anchor = useRef<number | null>(null);
  useEffect(() => {
    if (!auth.connected) { setData(null); setBusy(false); anchor.current = null; return; }
    let active = true; setBusy(true); setError('');
    const params = new URLSearchParams({ limit: '20', offset: String(offset) });
    if (search) params.set('search', search); if (environment) params.set('environment', environment);
    if (anchor.current != null) params.set('as_of', String(anchor.current));
    void auth.call<ApplicationHistory>(`/unified/history?${params}`).then(result => {
      if (!active) return; setData(result); anchor.current = result.as_of ?? null;
    }).catch(e => { if (active && !aborted(e)) { setData(null); setError(authError(e)); if (e instanceof ApiError && [401, 403].includes(e.status)) auth.clear(); } }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [auth.connected, auth.revision, auth.call, auth.clear, search, environment, offset, revision]);
  function filter(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = new FormData(e.currentTarget); anchor.current = null; setOffset(0);
    setSearch(String(form.get('search') || '').trim()); setEnvironment(String(form.get('environment') || '')); setRevision(v => v + 1);
  }
  return <section className="application-history" aria-label="Application 지난 이력">
    <h2>Application 지난 이력</h2><p>저장된 사건은 원래 번호로 보존합니다. 통합 사건이 연결된 경우 같은 관제실에서 이어서 확인합니다.</p>
    <HumanAccessForm auth={auth} label="이력 조회 인증 (incident:read)" onClear={() => { setData(null); setError(''); }} />
    {auth.connected && <form className="application-filters" onSubmit={filter}><label>사건 검색<input name="search" maxLength={120} placeholder="문제 설명 또는 원래 사건 번호" /></label><label>환경<select name="environment" defaultValue=""><option value="">전체</option><option value="dev">dev</option><option value="prd">prd</option><option value="stg">stg · 퇴역 이력</option></select></label><button className="btn" disabled={busy}>검색</button></form>}
    <ErrorBox>{error}</ErrorBox>{busy && <p role="status">저장 이력을 확인하고 있습니다.</p>}
    {data && <><p className="faint">검색 결과 {data.total}건</p><div className="application-history-list">{data.incidents.map(item => <article className="code-action-card" key={item.id}>
      <header><h3>{item.service} · {summaryKo(item.reason, '애플리케이션 장애')}</h3><span className="badge">{applicationStateKo(item.state)}</span></header>
      <dl className="kv"><dt>문제</dt><dd>{summaryKo(item.reason, '장애 신호가 접수되었습니다. 상세 증상은 원문에서 확인하세요.')}</dd><dt>분석 결론</dt><dd>{summaryKo(latestAnalysisSummary(object(item.data?.analysis)), '미관측')}</dd><dt>위치</dt><dd>{item.environment === 'stg' ? 'stg · 퇴역 이력' : item.environment} · {item.system} · {item.service}</dd><dt>저장 시각</dt><dd>{observedTime(item.created_at)}</dd><dt>원래 사건 번호</dt><dd className="mono">{item.id}</dd><dt>다음 행동</dt><dd>{item.root_incident_id ? '연결된 관제실에서 현재 진행을 확인하세요.' : '보존된 처리 요약과 원문을 확인하세요.'}</dd></dl>
      {item.root_incident_id ? <Link className="btn" to={`/incidents/${item.root_incident_id}`}>통합 관제실 #{item.root_incident_id}</Link> : <p className="faint">독립 Application 이력 · 통합 사건 연결 없음</p>}
      <Disclosure label="저장 원문·로그 보기">{() => <pre className="raw-text">{JSON.stringify(item, null, 2)}</pre>}</Disclosure>
    </article>)}</div>{data.total === 0 && <p>조건에 맞는 저장 이력이 없습니다.</p>}<div className="inline"><button className="btn" disabled={busy || offset === 0} onClick={() => setOffset(v => Math.max(0, v - 20))}>이전</button><span>{Math.floor(offset / 20) + 1} 페이지</span><button className="btn" disabled={busy || offset + 20 >= data.total} onClick={() => setOffset(v => v + 20)}>다음</button></div></>}
  </section>;
}
export function ApplicationPage() {
  const catalog = usePolling<UnifiedCatalog>('/unified/catalog', 30000), auth = useHumanAccess(), go = useNavigate();
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [confirmed, setConfirmed] = useState(false);
  const form = useRef<HTMLFormElement>(null), submission = useRef<{ key: string; requestId: string } | null>(null);
  const enabled = catalog.data?.enabled === true && catalog.data?.profiles_configured === true && !catalog.error;
  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (!enabled || !confirmed || busy || !auth.input.current?.value.trim()) return;
    const values = new FormData(e.currentTarget);
    const payload = { environment: String(values.get('environment')), service: String(values.get('service')), reason: String(values.get('reason') || '').trim(), reproduction: null };
    if (!['dev', 'prd'].includes(payload.environment) || !catalog.data?.services.some(s => s.service === payload.service) || !payload.reason) return;
    const key = JSON.stringify(payload); if (submission.current?.key !== key) submission.current = { key, requestId: crypto.randomUUID() };
    auth.connect(); setBusy(true); setError('');
    try {
      const result = await auth.call<{ incident_id: number; code_action: CodeActionSummary }>('/unified/incidents', { method: 'POST', body: JSON.stringify({ ...payload, request_id: submission.current.requestId }) });
      if (!Number.isSafeInteger(result.incident_id) || result.incident_id < 1) throw new Error('invalid incident id');
      go(`/incidents/${result.incident_id}`);
    } catch (e) { if (!aborted(e)) setError(authError(e)); }
    finally { auth.clear(); setBusy(false); setConfirmed(false); }
  }
  return <div className="page application-page"><header><h1>애플리케이션 장애</h1><p>등록된 서비스의 문제를 접수하고 같은 장애 관제실에서 소스 확인·수정·dev 시험·prd 검증을 이어갑니다.</p></header>
    <section className="code-action-card" aria-label="새 애플리케이션 사건"><h2>새 사건 접수</h2>
      {catalog.error && <ErrorBox>등록 서비스를 불러오지 못했습니다. 다시 확인한 뒤 접수하세요.</ErrorBox>}
      {!catalog.data && !catalog.error && <p role="status">등록 서비스를 확인하고 있습니다.</p>}
      {catalog.data && !enabled && <p className="notice">통합 접수 또는 등록 서비스 설정을 확인해야 합니다. 저장 이력은 아래에서 조회할 수 있습니다.</p>}
      <form ref={form} onSubmit={e => void create(e)} className="application-intake" autoComplete="off" onChange={e => { if (e.target instanceof HTMLElement && e.target.getAttribute('name')) setConfirmed(false); }}>
        <div className="application-fields"><label>환경<select name="environment" required disabled={!enabled || busy} defaultValue="dev">{['dev', 'prd'].filter(v => !catalog.data || catalog.data.environments.includes(v)).map(v => <option key={v}>{v}</option>)}</select></label><label>서비스<select name="service" required disabled={!enabled || busy} defaultValue=""><option value="" disabled>서비스 선택</option>{catalog.data?.services.map(s => <option key={s.service} value={s.service}>{s.service} · {s.system}</option>)}</select></label></div>
        <label>문제 상황<textarea name="reason" maxLength={4000} rows={3} required disabled={!enabled || busy} placeholder="어떤 동작에서 어떤 문제가 발생했는지 적어 주세요." /></label>
        <label>사건 접수 인증 (incident:create)<input ref={auth.input} type="password" autoComplete="off" spellCheck={false} required maxLength={8192} disabled={!enabled || busy} /></label>
        <label className="code-confirm"><input type="checkbox" checked={confirmed} disabled={!enabled || busy} onChange={e => setConfirmed(e.target.checked)} />선택한 서비스에 사건을 접수합니다. 모델 분석과 dev 수정·시험이 시작될 수 있으며 prd 배포는 사람 확인과 승인을 거칩니다.</label>
        <button className="btn primary" disabled={!enabled || !confirmed || busy}>{busy ? '접수 확인 중' : '사건 접수·관제실 열기'}</button>
      </form><ErrorBox>{error}</ErrorBox>
    </section><ApplicationHistoryView />
  </div>;
}
