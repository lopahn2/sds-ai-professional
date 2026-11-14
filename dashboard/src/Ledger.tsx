import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { dateTime, request } from './api';
import { latestOnly } from './latestOnly';
import { ledgerQuery } from './historyQuery';
import { environmentFilterLabel, historyEnvironmentOptions } from './environments';
import { Disclosure } from './Disclosure';
import { summaryKo } from './presentation';

type Entry = { id: number; created_at: string; incident_id: number; reason_code: string; profile: string; level: string | null; workload: string | null; proposed: Record<string, unknown>; rationale: string; impact: string; outcome: string | null; rollback: string | null; operator_involved: boolean; environment?: string | null };
type Ledger = { total: number; by_reason: Record<string, number>; items: Entry[]; offset?: number };
const REASON: Record<string, string> = { high_risk_level: 'HIGH_RISK 판정', low_confidence: '낮은 확신도', agents_disagree: '에이전트 의견 불일치', citation_failed: '인용 검증 실패', router_failure: '라우터 실패',
  action_held_by_policy: '정책이 조치를 보류', action_ran_under_poc_autonomy: 'PoC 자율 실행', operator_stopped: '운영자 중단', action_failed: '조치 실패' };
const OUTCOME: Record<string, string> = { held_for_operator: '운영자에게 보류', proposed_for_approval: '승인 요청' };
const PAGE = 100;

export function Ledger() {
  const [data, setData] = useState<Ledger | null>(null); const [reason, setReason] = useState(''); const [environment, setEnvironment] = useState('');
  const [offset, setOffset] = useState(0); const [error, setError] = useState<string | null>(null);
  const gate = useMemo(() => latestOnly(), []);
  const load = useCallback(() => gate.run(
    () => request<Ledger>(`/review-ledger${ledgerQuery({ reason, environment, offset, limit: PAGE })}`),
    d => { setData(d); setError(null); }, () => setError('검토 기록을 불러오지 못했습니다. 잠시 후 다시 시도하세요.'),
  ), [gate, reason, environment, offset]);
  useEffect(() => { void load(); const t = window.setInterval(() => void load(), 10000); return () => window.clearInterval(t); }, [load]);
  const total = data?.total ?? 0;
  return <div className="page">
    <div className="page-head"><div><h1>검토 기록</h1><p>사람의 검토가 필요하다고 판단된 순간과 그 이유, 결과를 모읍니다. 나중에 검토 기준을 정하는 자료이며 시스템 동작은 바꾸지 않습니다.</p></div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <select className="select" aria-label="환경 필터" value={environment} onChange={e => { setEnvironment(e.target.value); setOffset(0); }}><option value="">전체 환경</option>{historyEnvironmentOptions().map(env => <option key={env.value} value={env.value}>{env.label}</option>)}</select>
        <select className="select" aria-label="사유 필터" value={reason} onChange={e => { setReason(e.target.value); setOffset(0); }}><option value="">모든 사유</option>{Object.entries(REASON).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <a className="btn" href={`/api/review-ledger.csv${ledgerQuery({ reason, environment })}`} download>CSV 내보내기 (조건 일치 {total}건 전체)</a></div></div>
    {error && <div className="error" role="alert">{error}</div>}
    {data && Object.keys(data.by_reason).length > 0 && <p className="muted" style={{ marginBottom: 16 }}>{environment ? `${environmentFilterLabel(environment)} 환경 · ` : ''}{Object.entries(data.by_reason).map(([k, n]) => `${REASON[k] ?? '기타 검토 사유'} ${n}건`).join(' · ')}</p>}
    <div className="card">{!data ? <div className="empty">불러오는 중…</div> : data.items.length === 0 ? <div className="empty">{reason || environment ? '조건에 맞는 기록이 없습니다.' : '아직 기록이 없습니다. 고위험 판정이나 보류된 조치가 생기면 여기에 쌓입니다.'}</div> :
      <table className="table"><thead><tr><th>시각</th><th>사유</th><th>사건</th><th>근거</th><th>예상 영향</th><th>결과</th></tr></thead>
        <tbody>{data.items.map(e => <tr key={e.id}><td className="mono">{dateTime(e.created_at)}</td><td>{REASON[e.reason_code] ?? '기타 검토 사유'}</td>
          <td><Link to={`/incidents/${e.incident_id}`}>#{e.incident_id}</Link><br /><small className="faint">{e.workload}{e.environment ? ` · ${environmentFilterLabel(e.environment)}` : ''}</small></td><td style={{ maxWidth: 360 }}>{summaryKo(e.rationale, '판단 근거 확인 필요')}<Disclosure label="검토 원문 보기">{() => <pre className="raw-text">{JSON.stringify(e, null, 2)}</pre>}</Disclosure></td><td>{summaryKo(e.impact, '영향 확인 중')}</td>
          <td>{e.outcome ? OUTCOME[e.outcome] ?? '결과 확인 필요' : '결과 미기록'}{e.rollback ? ' · 원복 기록 있음' : ''}{e.operator_involved ? ' · 운영자 개입' : ''}</td></tr>)}</tbody></table>}
      <div className="pager"><span>{total ? `${offset + 1}–${Math.min(offset + PAGE, total)} / ${total}` : '0건'}</span><div className="inline"><button className="btn" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - PAGE))}>이전</button><button className="btn" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>다음</button></div></div></div>
  </div>;
}
