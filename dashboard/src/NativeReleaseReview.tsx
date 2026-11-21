import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { request } from './api';
import { authError, HumanAccessForm, useHumanAccess } from './HumanAccess';
import { ErrorBox } from './ui';
import { currentRelease, fingerprint, matchingFingerprint, object, pendingSupplement, qaProtocol, releaseProof, type Data } from './unified';

type NativeRelease = { incident_id: string; state: string; release: Data | null };
type Review = { record: NativeRelease; origin: Data; merge: Data; round: unknown; pending: boolean };
const reviewKey = ({record,origin,merge,round,pending}: Review) => JSON.stringify([
  record.incident_id, record.state, record.release?.protocol, record.release?.valid, fingerprint(object(record.release)),
  object(record.release?.stage_results).dev, record.release?.user_test, object(record.release?.qa).receipt_id, record.release?.risk_sha256, record.release?.infra, round, pending, origin, merge,
]);
const linked = (origin: Data) => typeof origin.incident_id === 'number' && Number.isSafeInteger(origin.incident_id) && origin.incident_id > 0
  && typeof origin.action_id === 'string' && !!origin.action_id && typeof origin.installation_id === 'string' && !!origin.installation_id && /^[a-f0-9]{64}$/.test(String(origin.request_sha256));
function merged(review: Review, now: number) {
  const r = object(review.record.release), m = review.merge;
  return linked(review.origin) && m.human_merge_observed === true && typeof m.expires_at === 'number'
    && m.expires_at*1000 > now && matchingFingerprint(m,r);
}

export function NativeReleaseReview({ id, state, onChange, decisionOnly=false }: { id: string; state: string; onChange: () => void; decisionOnly?:boolean }) {
  const base = '/application/v1/incidents/' + encodeURIComponent(id);
  const auth = useHumanAccess('/application/v1');
  const scope = state === 'awaiting_user_test' ? 'tester' : 'prd-approver';
  const permitted = auth.permits(scope);
  const [review,setReview] = useState<Review|null>(null), [error,setError] = useState(''), [note,setNote] = useState('');
  const [confirmation,setConfirmation] = useState<string|null>(null), [busy,setBusy]=useState(false);
  const [now,setNow] = useState(Date.now);
  const writing = useRef(false);
  const fetchReview = useCallback(async (signal?:AbortSignal): Promise<Review> => {
    const record = await request<NativeRelease>(base+'/release',{signal});
    if (record?.incident_id !== id) throw new Error('release_identity_mismatch');
    const detail = await request<{id:string; state:string; data:Data}>(base+'?view=stage',{signal});
    if (detail?.id !== id || detail.state!==record.state) throw new Error('review_identity_mismatch');
    return {record,origin:object(detail.data?.unified_origin),merge:object(detail.data?.merge_review),round:detail.data?.round,pending:pendingSupplement(object(detail.data))};
  }, [base,id]);
  useEffect(() => {
    setReview(null); setConfirmation(null); setError('');
    if (!permitted) return;
    const c = new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    const load = async () => {
      try { const data = await fetchReview(c.signal); if (!c.signal.aborted) {setReview(data);setError('');} }
      catch (e) { if (!c.signal.aborted) {setReview(null);setError(authError(e));} }
      if (!c.signal.aborted) timer = setTimeout(load,5000);
    };
    void load(); return () => {c.abort();clearTimeout(timer);};
  },[permitted,auth.revision,fetchReview]);
  useEffect(()=>{const t=setInterval(()=>setNow(Date.now()),1000);return ()=>clearInterval(t);},[]);
  const release = object(review?.record.release), f = fingerprint(release);
  const key = review ? reviewKey(review) : null;
  const isV2 = release.protocol==='dev-prd/v2';
  const testing = isV2 && review?.record.state==='awaiting_user_test' && state==='awaiting_user_test';
  const approving = (isV2 || qaProtocol(release)) && review?.record.state==='awaiting_prd_approval' && state==='awaiting_prd_approval';
  const canTest = testing && !review?.pending && currentRelease(release) && object(object(release.stage_results).dev).verified===true;
  const canReview = approving && !!review && !review.pending && !!releaseProof(release,now) && linked(review.origin) && !merged(review,now);
  const canApprove = approving && !!review && !review.pending && !!releaseProof(release,now) && merged(review,now);
  const action = canTest ? 'user-test' : canReview ? 'review-merge' : canApprove ? 'approve-prd' : null;
  const title = canTest ? 'dev 사용자 시험 기록' : canReview ? '사람 병합 확인' : 'prd 배포 승인';
  async function submit(e:FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!permitted || !key || !f || writing.current || confirmation!==key || !action) return;
    const values = new FormData(e.currentTarget);
    const passed = values.get('result'), notes = String(values.get('notes')||'').trim();
    if (testing && (!['true','false'].includes(String(passed)) || !notes)) {setError('시험 결과와 확인 내용을 입력하세요.');return;}
    const prNumber = Number(values.get('pr'));
    if (canReview && (!Number.isSafeInteger(prNumber) || prNumber < 1)) return;
    const generation=auth.generation.current;
    writing.current=true;setBusy(true);setError('');setNote('');
    try {
      const latest = await fetchReview();
      if (generation!==auth.generation.current || reviewKey(latest)!==key || latest.pending || !auth.permits(scope) || (!testing && (!releaseProof(object(latest.record.release)) || (canApprove && !merged(latest,Date.now()))))) {
        setReview(latest); setConfirmation(null);setError('릴리스 또는 승인 근거가 바뀌었습니다. 최신 내용을 확인하세요.');return;
      }
      const body = testing ? {...f,passed:passed==='true',notes_ko:notes} : canReview ? {...f,pr_number:prNumber} : {...f,...releaseProof(release)};
      await auth.call(base+'/'+action,{method:'POST',body:JSON.stringify(body)});
      setConfirmation(null);setReview(null);setNote(testing?'dev 사용자 시험 결과를 기록했습니다.':canReview?'사람 병합 확인 결과를 기록했습니다.':'prd 승인 결과를 기록했습니다.');onChange();
    } catch (ex) {setError(authError(ex));setConfirmation(null);onChange();}
    finally {writing.current=false;setBusy(false);}
  }
  if (decisionOnly && !action && !error && !note) return null;
  return <section className="native-review" aria-label="릴리스 확인">
    <HumanAccessForm auth={auth} scope={scope} label={scope==='tester'?'시험자 역할 확인':'승인자 역할 확인'} />
    {permitted && review && <>{!decisionOnly && <p>{isV2?'보존된 dev/prd 릴리스':release.protocol==='dev-qa-prd/v3'?'브라우저 QA · 통합 승인 절차':'승격 절차 미관측'}</p>}
      {f && !decisionOnly && <dl className="kv code-fingerprints"><dt>릴리스</dt><dd>{f.release_id}</dd><dt>산출물 지문</dt><dd>{f.artifact_sha256}</dd><dt>계획 지문</dt><dd>{f.plan_sha256}</dd><dt>이미지</dt><dd>{f.image}</dd></dl>}
      {approving && !canApprove && !canReview && <p className="notice">{!linked(review.origin) ? '이 원본 사건에서는 사람 병합을 재검증하는 통합 승인 경로를 확인할 수 없어 prd 승인이 보류됩니다.' : '같은 버전의 유효한 dev 시험과 사람 병합 확인이 필요합니다.'}</p>}
      {!decisionOnly && linked(review.origin) && <Link className="btn sm" to={`/incidents/${review.origin.incident_id}`}>통합 관제실에서 병합·승인 검토</Link>}
      {action && <form className="code-gate" onSubmit={submit} aria-label={title}>
        {testing && <><label>시험 결과<select name="result" required defaultValue=""><option value="" disabled>선택</option><option value="true">통과</option><option value="false">실패</option></select></label>
          <label>확인 내용<textarea name="notes" rows={2} maxLength={1000} required /></label></>}
        {canReview && <label>병합된 PR 번호<input type="number" name="pr" min={1} step={1} required /></label>}
        <label className="code-confirm"><input type="checkbox" checked={confirmation===key} onChange={e=>setConfirmation(e.target.checked?key:null)}/>{testing?'이 dev 릴리스에서 직접 시험한 결과를 기록합니다.':canReview?'이 릴리스의 PR과 사람 병합을 확인하도록 요청합니다.':'확인한 시험·병합 지문과 같은 버전의 prd 배포를 승인합니다.'}</label>
        <button type="submit" className="btn primary" disabled={busy||confirmation!==key}>{title}</button></form>}
      {testing && !canTest && <p>유효한 릴리스 지문과 dev 배포 검증 근거가 필요합니다.</p>}
    </>}
    <ErrorBox>{error}</ErrorBox>{note && <p role="status">{note}</p>}
  </section>;
}
