import { useEffect, useState } from 'react';
import { request } from './api';
import './observationSources.css';

type Check = { state: 'observed' | 'no_samples' | 'unavailable' | 'partial' | 'scrape_failed'; failed_scrapes?: number; checked_at: string;
  tags?: { examined: number; unassigned: number; ambiguous_services: number; missing: Record<string, number> } };
type Source = { adapter: string; label: string; active: string;
  locations: { id: string; label: string }[]; check: Check | null };
const checkLabel = { observed: '수집 확인', no_samples: '확인 구간에 표본 없음', unavailable: '수집 확인 실패', partial: '일부 대상 수집 실패', scrape_failed: '대상 수집 실패' };
const tagLabel: Record<string, string> = { namespace: '환경', app: '서비스', system: '시스템' };

function SourceRow({ source }: { source: Source }) {
  const [active, setActive] = useState(source.active);
  const [location, setLocation] = useState(source.active);
  const [check, setCheck] = useState(source.check);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const path = '/settings/observation-sources/' + source.adapter;
  async function run(save: boolean) {
    setBusy(true); setMessage('');
    try {
      const body = JSON.stringify({ location });
      if (save) {
        await request(path, { method: 'PUT', body });
        setActive(location); setCheck(null); setMessage('수집 위치를 저장했습니다.');
      } else {
        setCheck(await request<Check>(path + '/check', { method: 'POST', body }));
      }
    } catch { setMessage('요청을 처리하지 못했습니다. 다시 시도하세요.'); }
    finally { setBusy(false); }
  }
  const tags = check?.tags;
  return <div className="source-row">
    <div><b>{source.label}</b><small>{check ? checkLabel[check.state] : '미확인'}
      {check && <> · <time dateTime={check.checked_at}>{new Date(check.checked_at).toLocaleTimeString('ko-KR')}</time></>}</small></div>
    <div><label>수집 위치 <select value={location} onChange={e => { setLocation(e.target.value); setMessage(''); }}>
      {!source.locations.some(l => l.id === active) && <option value={active}>등록 해제된 위치</option>}
      {source.locations.map(l => <option key={l.id} value={l.id}>{l.id === 'default' || l.id === 'cluster' ? l.label : l.id + ' · ' + l.label}</option>)}
    </select></label>
    {active !== location ? <button className="btn" disabled={busy} onClick={() => run(true)}>위치 저장</button>
      : <button className="btn" disabled={busy} onClick={() => run(false)}>수집 확인</button>}</div>
    {tags && <small>표본 {tags.examined}개 · 미식별 {tags.unassigned}개
      {Object.entries(tags.missing).filter(([, n]) => n > 0).map(([k, n]) => ' · ' + tagLabel[k] + ' 태그 누락 ' + n + '개')}
      {!!check?.failed_scrapes && <> · 수집 실패 {check.failed_scrapes}개</>}
      {tags.ambiguous_services > 0 && <> · 소속이 겹치는 서비스 {tags.ambiguous_services}개</>}</small>}
    {message && <small role="status">{message}</small>}
  </div>;
}

export function ObservationSources() {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let alive = true;
    request<{ sources: Source[] }>('/settings/observation-sources')
      .then(data => { if (alive) setSources(data.sources); }).catch(() => { if (alive) setError(true); });
    return () => { alive = false; };
  }, []);
  return <section className="card" aria-label="관측 소스"><header>관측 소스</header>
    <div className="body source-list">{error ? '소스 설정을 불러오지 못했습니다.' :
      sources ? sources.map(s => <SourceRow key={s.adapter} source={s} />) : '불러오는 중…'}</div></section>;
}
