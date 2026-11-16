import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError } from './api';
import { summaryKo } from './presentation';
import { EFFORT_KO } from './labels';
import { Disclosure } from './Disclosure';
import { ErrorBox } from './ui';
import { HumanAccessForm, aborted, authError, useHumanAccess } from './HumanAccess';
import { applicationStateKo, canAttest, fingerprint, latestAnalysisSummary, mergeObserved, object, observed, observedTime, releaseKey, type CodeActionDetail, type CodeActionSummary, type Data } from './unified';

const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toLocaleString('ko-KR') : '미관측';
function RoleSummary({ analysis }: { analysis: Data }) {
  const roles = Array.isArray(analysis.roles) ? analysis.roles.map(object) : [];
  const names: Record<string, string> = { classifier: '수준 분류', analyst: '원인 분석', specialist: '심층 분석', critic: '반대 근거 검토', verifier: '독립 검증', patch: '코드 수정' };
  return <section className="code-role-summary" aria-label="코드 조치 모델 실행 기록"><h4>코드 조치 모델 실행 기록</h4><p className="faint">Kubernetes 분석 사용량과 별도로 표시합니다. 기록된 역할 호출만 표시하며 미관측 비용은 추정하지 않습니다.</p>
    {roles.length ? <ol>{roles.map((role, ix) => { const usage = object(role.usage), result = object(role.result); return <li key={ix}><h4>{names[String(role.role)] || '역할 미관측'}</h4><p>{summaryKo(result.summary_ko || result.reason_ko, '요약 미관측')}</p><dl className="kv"><dt>모델 · 추론 강도</dt><dd>{typeof role.model === 'string' && /^[a-zA-Z0-9_.:-]{1,100}$/.test(role.model) ? role.model : '미관측'} · {EFFORT_KO[String(role.effort)] || '미관측'}</dd><dt>입력 / 출력 토큰</dt><dd>{count(usage.input_tokens)} / {count(usage.output_tokens)}</dd><dt>비용 / 응답 시간</dt><dd>{typeof role.cost_usd === 'number' && Number.isFinite(role.cost_usd) && role.cost_usd >= 0 ? `$${role.cost_usd.toFixed(4)}` : '미관측'} / {typeof role.duration_ms === 'number' && Number.isFinite(role.duration_ms) && role.duration_ms >= 0 ? `${(role.duration_ms / 1000).toFixed(1)}초` : '미관측'}</dd><dt>저장 시각</dt><dd>{observedTime(typeof role.occurred_at === 'number' ? role.occurred_at : null)}</dd></dl></li>; })}</ol> : <p>역할 실행 기록 미관측</p>}
  </section>;
}
function Fingerprints({ release }: { release: Data }) {
  const f = fingerprint(release);
  return <dl className="kv code-fingerprints" aria-label="확인 대상 지문"><dt>릴리스</dt><dd>{f?.release_id ?? '미관측'}</dd><dt>산출물 지문</dt><dd>{f?.artifact_sha256 ?? '미관측'}</dd><dt>계획 지문</dt><dd>{f?.plan_sha256 ?? '미관측'}</dd><dt>이미지</dt><dd>{f?.image ?? '미관측'}</dd></dl>;
}
function Attestation({ detail, action, fresh, now, onDone }: { detail: CodeActionDetail; action: 'user-test' | 'review-merge' | 'approve-prd'; fresh: boolean; now: number; onDone: () => Promise<void> }) {
  const auth = useHumanAccess(), key = releaseKey(detail), release = object(detail.release.release);
  const [confirmed, setConfirmed] = useState<string | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const notes = useRef<HTMLTextAreaElement>(null), outcome = useRef<HTMLSelectElement>(null), pr = useRef<HTMLInputElement>(null);
  const currentKey = useRef(key); currentKey.current = key;
  useEffect(() => { setConfirmed(null); auth.clear(); }, [key, auth.clear]);
  const title = action === 'user-test' ? 'dev 사용자 시험 기록' : action === 'review-merge' ? '사람 병합 확인' : 'prd 배포 승인';
  const available = fresh && canAttest(detail, action, now);
  useEffect(() => { if (!available) { setConfirmed(null); auth.clear(); } }, [available, auth.clear]);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!available || busy || confirmed !== key || currentKey.current !== key || !canAttest(detail, action)) return;
    const f = fingerprint(release); if (!f || !auth.input.current?.value.trim()) return;
    let body: Data = { ...f };
    if (action === 'user-test') {
      const result = outcome.current?.value, note = notes.current?.value.trim();
      if (!['true', 'false'].includes(result || '') || !note) return;
      body = { ...body, passed: result === 'true', notes_ko: note };
    } else if (action === 'review-merge') {
      const number = Number(pr.current?.value); if (!Number.isSafeInteger(number) || number < 1) return;
      body = { ...body, pr_number: number };
    } else body = { ...body, user_test_id: object(release.user_test).id };
    auth.connect(); setBusy(true); setError(''); setNotice('');
    try {
      await auth.call(`/incidents/${detail.summary.incident_id}/code-action/${action}`, { method: 'POST', body: JSON.stringify(body) });
      setNotice(action === 'approve-prd' ? '승인 요청을 기록했습니다. 배포와 업무 검증 결과를 이어서 확인합니다.' : action === 'review-merge' ? '병합 확인 요청을 처리했습니다. 최신 관측 결과를 확인하세요.' : '사용자 시험 결과를 기록했습니다.');
      setConfirmed(null); await onDone();
    } catch (e) { if (!aborted(e)) { setError(authError(e)); setConfirmed(null); await onDone(); } }
    finally { auth.clear(); setBusy(false); }
  }
  return <form className="code-gate" aria-label={title} onSubmit={e => void submit(e)} autoComplete="off">
    <h4>{title}</h4>
    {action === 'review-merge' && <p>등록 저장소에서 PR을 생성하고 사람이 병합한 뒤 확인하세요. 확인 요청은 병합을 실행하지 않습니다. 배포 승인과 실행 시 서버가 병합 상태를 다시 확인합니다.</p>}
    <Fingerprints release={release} />
    {action === 'user-test' && <><label>시험 결과<select ref={outcome} required defaultValue=""><option value="" disabled>결과 선택</option><option value="true">통과</option><option value="false">실패</option></select></label><label>확인한 동작과 결과<textarea ref={notes} required maxLength={4000} rows={2} /></label></>}
    {action === 'review-merge' && <label>등록 저장소 PR 번호<input ref={pr} type="number" min={1} step={1} required /></label>}
    <label>{action === 'user-test' ? '시험자 인증 (tester)' : '승인자 인증 (prd-approver)'}<input ref={auth.input} type="password" autoComplete="off" spellCheck={false} maxLength={8192} required disabled={!available || busy} /></label>
    <label className="code-confirm"><input type="checkbox" checked={confirmed === key} disabled={!available || busy} onChange={e => setConfirmed(e.target.checked ? key : null)} />{action === 'user-test' ? '위 지문의 dev 버전에서 직접 시험한 결과임을 확인합니다.' : action === 'review-merge' ? '위 시험 산출물의 PR 번호와 사람 병합을 확인하도록 요청합니다.' : '위 지문의 시험·병합 결과와 prd 배포 대상을 확인하고 승인합니다.'}</label>
    {!available && <p className="notice">{!fresh ? '최신 상세 정보를 다시 확인해야 합니다.' : action === 'approve-prd' ? '유효한 사용자 시험과 사람 병합 관측이 모두 필요합니다.' : '현재 상태와 유효한 dev 검증·산출물을 먼저 확인해야 합니다.'}</p>}
    <button className="btn primary" type="submit" disabled={!available || busy || confirmed !== key}>{busy ? '확인 중' : title}</button>
    <ErrorBox>{error}</ErrorBox>{notice && <p role="status">{notice}</p>}
  </form>;
}
export function CodeActionCard({ id, summary, summaryError, problem, onChange, readOnly = false, nodeId, flash = false }: { id: string; summary: CodeActionSummary | null; summaryError: boolean; problem: string; onChange: () => void; readOnly?: boolean; nodeId?: string; flash?: boolean }) {
  const auth = useHumanAccess();
  const [detail, setDetail] = useState<CodeActionDetail | null>(null), [fresh, setFresh] = useState(false), [error, setError] = useState('');
  const [connecting, setConnecting] = useState(false), [connectConfirm, setConnectConfirm] = useState(false), [now, setNow] = useState(Date.now());
  const detailEpoch = useRef(0);
  const loadDetail = useCallback(async () => {
    if (!auth.connected || !summary?.linked) return;
    const epoch = ++detailEpoch.current;
    try {
      const result = await auth.call<CodeActionDetail>(`/incidents/${id}/code-action/detail`);
      if (detailEpoch.current !== epoch) return;
      setDetail(result); setFresh(true); setError('');
    } catch (e) {
      if (detailEpoch.current !== epoch || aborted(e)) return;
      setFresh(false); setError(authError(e));
      if (e instanceof ApiError && [401, 403].includes(e.status)) { auth.clear(); setDetail(null); }
    }
  }, [auth.connected, auth.revision, auth.call, auth.clear, id, summary?.linked]);
  useEffect(() => {
    if (!auth.connected) { detailEpoch.current++; setDetail(null); setFresh(false); return; }
    let timer: ReturnType<typeof setTimeout>; let stopped = false;
    const poll = async () => { await loadDetail(); if (!stopped) timer = setTimeout(poll, 5000); };
    void poll(); return () => { stopped = true; detailEpoch.current++; clearTimeout(timer); };
  }, [loadDetail, auth.connected]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const changed = async () => { onChange(); await loadDetail(); };
  async function connect() {
    if (!connectConfirm || !summary?.eligible || !auth.connected || connecting || readOnly) return;
    setConnecting(true); setError('');
    try { await auth.call(`/incidents/${id}/code-action`, { method: 'POST', body: '{}' }); setConnectConfirm(false); onChange(); }
    catch (e) { if (!aborted(e)) setError(authError(e)); }
    finally { setConnecting(false); }
  }
  if (!summary && !summaryError) return <article id={nodeId ? `step-${nodeId}` : undefined} tabIndex={-1} className={`code-action-card${flash ? ' flash' : ''}`} aria-label="코드 조치"><p role="status">코드 조치 상태를 확인하고 있습니다.</p></article>;
  if (!summary?.enabled && !summary?.linked && !summaryError) return null;
  const release = object(detail?.release.release), tests = object(object(detail?.release.test_build).tests || release.tests), patch = object(detail?.application.data.patch), analysis = object(detail?.application.data.analysis);
  const state = summary?.state_ko || (detail ? applicationStateKo(detail.application.state) : '미관측');
  return <article id={nodeId ? `step-${nodeId}` : undefined} tabIndex={-1} className={`code-action-card${flash ? ' flash' : ''}`} aria-label="코드 조치">
    <header><h3>코드 조치</h3><span className="badge info" role="status">{state}</span></header>
    <dl className="kv"><dt>문제</dt><dd>{summaryKo(summary?.problem_ko, problem)}</dd><dt>원인</dt><dd>{summaryKo(summary?.cause_ko || analysis.root_cause_ko, '미관측')}</dd><dt>현재</dt><dd>{summaryKo(summary?.summary_ko, '미관측')}</dd><dt>다음 행동</dt><dd>{summaryKo(summary?.next_ko, '상태를 다시 확인하세요.')}</dd><dt>실행기 연결</dt><dd>{observed(summary?.runner_observed)} · 마지막 관측 {observedTime(summary?.observed_at)}</dd></dl>
    {summaryError && <ErrorBox>코드 조치 상태 갱신에 실패했습니다. 마지막 관측을 표시하며 변경 제어를 잠시 사용할 수 없습니다.</ErrorBox>}
    {summary?.linked && <p className="faint">출처 · Application #{summary.application_id ?? '미관측'} · 코드 조치 #{summary.action_id ?? '미관측'}</p>}
    {!readOnly && (summary?.linked || summary?.eligible) && <HumanAccessForm auth={auth} label={summary.linked ? '상세 조회 인증 (incident:read)' : '코드 조치 연결 인증 (incident:create)'} onClear={() => { setDetail(null); setFresh(false); setConnectConfirm(false); }} />}
    {!readOnly && !summary?.linked && summary?.eligible && <div className="code-gate"><p>이 사건의 검증된 코드 원인을 새 코드 조치로 연결합니다. 소스 확인·모델 분석·시험이 시작될 수 있습니다.</p><label className="code-confirm"><input type="checkbox" checked={connectConfirm} onChange={e => setConnectConfirm(e.target.checked)} />이 사건의 코드 조치를 요청합니다.</label><button className="btn primary" disabled={!auth.connected || !connectConfirm || connecting || summaryError} onClick={() => void connect()}>코드 조치 연결</button></div>}
    <ErrorBox>{error}</ErrorBox>
    {detail && <section className="code-detail" aria-label="코드 조치 확인 내용">
      <h4>수정·검증 요약</h4>
      <p className="code-analysis-conclusion"><b>분석 결론</b> {summaryKo(latestAnalysisSummary(analysis), '미관측')}</p>
      <dl className="kv"><dt>변경</dt><dd>{summaryKo(release.summary_ko || patch.summary_ko, '미관측')}</dd><dt>영향</dt><dd>{summaryKo(release.risk_ko || patch.risk_ko, '미관측')}</dd><dt>원복</dt><dd>{summaryKo(release.rollback_ko, '미관측')}</dd><dt>산출물 유효성</dt><dd>{observed(release.valid)}</dd><dt>회귀 시험</dt><dd>수정 전 실패 {observed(tests.before_failed)} · 수정 후 통과 {observed(tests.after_passed)} · 실행 {count(tests.total)}개</dd><dt>dev 검증</dt><dd>{observed(object(object(release.stage_results).dev).verified)}</dd><dt>prd 검증</dt><dd>{observed(object(object(release.stage_results).prd).verified)}</dd><dt>사용자 시험</dt><dd>{observed(object(release.user_test).passed)} · 만료 {observedTime(typeof object(release.user_test).expires_at === 'number' ? object(release.user_test).expires_at as number : null)}</dd><dt>사용자 확인 내용</dt><dd>{summaryKo(object(release.user_test).notes_ko, '미관측')}</dd><dt>마지막 병합 관측</dt><dd>{mergeObserved(detail, now) ? '시험 산출물의 사람 병합 관측됨' : '병합 미확인'}</dd></dl>
      <RoleSummary analysis={analysis} />
      {release.release_id && release.protocol !== 'dev-prd/v2' ? <p className="notice">이전 승격 절차의 이력입니다. stg는 퇴역했으며 새 시험·승인은 제공하지 않습니다.</p> : null}
      {!readOnly && detail.application.state === 'awaiting_user_test' && <Attestation detail={detail} action="user-test" fresh={fresh && !summaryError} now={now} onDone={changed} />}
      {!readOnly && detail.application.state === 'awaiting_prd_approval' && <><Attestation detail={detail} action="review-merge" fresh={fresh && !summaryError} now={now} onDone={changed} /><Attestation detail={detail} action="approve-prd" fresh={fresh && !summaryError} now={now} onDone={changed} /></>}
      <Disclosure label="코드 조치 원문·로그 보기">{() => <pre className="raw-text">{JSON.stringify({ application: detail.application, release: detail.release, events: detail.events }, null, 2)}</pre>}</Disclosure>
    </section>}
  </article>;
}
