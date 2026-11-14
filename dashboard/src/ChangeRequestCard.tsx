import { useState } from 'react';
import { request } from './api';
import { canCancelChange, cardErrorKo, changeProgress, changeStatusKo, isRetiredStage, recordedStages, rollbackKo, safePrUrl, stageHeadingKo, type ChangeRequest } from './changes';
import { ErrorBox, Spinner } from './ui';
import { Disclosure } from './Disclosure';
import { summaryKo } from './presentation';

export function ChangeRequestCard({ change: c, onChange, readOnly = false }: {change:ChangeRequest;onChange:()=>void;readOnly?:boolean}) {
  const [busy,setBusy] = useState(false); const [error,setError] = useState('');
  const pr = safePrUrl(c.build);
  async function cancel() {
    if (busy || readOnly) return; setBusy(true); setError('');
    try { await request(`/changes/${c.id}/cancel`, {method:'POST'}); onChange(); }
    catch { setError('취소하지 못했습니다. 실행 중인 단계는 검증·원복이 끝날 때까지 취소할 수 없습니다.'); onChange(); }
    finally { setBusy(false); }
  }
  const running = ['patching','building','dev_deploying','stg_deploying','prd_deploying'].includes(c.status);
  const cardError = cardErrorKo(c);
  return <article className="command-card change-request-card" aria-label={`변경 요청 ${c.id}`} style={{minWidth:0,maxWidth:'100%',flexShrink:0,overflow:'hidden',overflowWrap:'anywhere'}}>
    <header><b>변경 요청 · #{c.id}</b><span role="status" className={`badge ${c.status.endsWith('_failed') || c.status==='error' ? 'danger' : running ? 'info' : 'ok'}`}>{!readOnly && running && <Spinner/>}{changeStatusKo(c.status)}</span></header>
    <p>{c.namespace_origin} · {c.system} · {c.service}</p>
    <ol aria-label="변경 단계" style={{display:'flex',flexWrap:'wrap',gap:'4px 12px',paddingLeft:16}}>{changeProgress(c).map(s=><li key={s.label} aria-current={s.state==='진행 중'?'step':undefined}><b>{s.label}</b> · {s.state}</li>)}</ol>
    <section><h4>패치 요약</h4><p>{c.patch_summary_ko || '소스와 진단을 확인한 뒤 수정안을 만듭니다.'}</p>{c.tests.risk_ko && <p className="muted">위험: {c.tests.risk_ko}</p>}</section>
    {c.diff && <Disclosure label="패치 원문 보기">{() => <pre className="raw-text">{c.diff}</pre>}</Disclosure>}
    <dl className="kv"><dt>회귀 시험</dt><dd>{c.tests.total == null ? '미수행' : `총 ${c.tests.total}개 · 실패 ${c.tests.failed ?? '미확인'}개`}</dd></dl>
    <Disclosure label="이미지·시험·검토 원문 보기">{() => <pre className="raw-text">{JSON.stringify({ build: c.build, tests: c.tests }, null, 2)}</pre>}</Disclosure>
    <section aria-label="GitHub 검토"><h4>코드 검토</h4>{pr ? <><a href={pr} target="_blank" rel="noopener noreferrer" style={{color:'var(--accent)',textDecoration:'underline'}}>실제 PR #{c.build.pr_number} 열기</a><p>상태: {{OPEN:'검토 중',MERGED:'병합됨',CLOSED:'닫힘'}[c.build.pr_state ?? ''] || '미조회'}</p></> : <p className="muted">실제 PR이 생성되면 링크를 표시합니다.</p>}<p>신규 환경 배포는 Application 관제에서 진행합니다.</p></section>
    {/* 기존 기록의 단계 결과만 이력으로 표시한다. 퇴역 stg 결과는 지우지 않고 퇴역·이력임을 밝힌다(새 승격 대상 아님). */}
    {recordedStages(c).map(s=>{const entry=c.stages[s]!;const v=entry.verification;return <section key={s} aria-label={`${s} 검증 결과`}><h4>{s} 단계{isRetiredStage(s) ? '(퇴역·이력)' : ''} · {stageHeadingKo(entry)}</h4><dl className="kv" style={{wordBreak:'keep-all'}}><dt>롤아웃</dt><dd>{summaryKo(v?.rollout?.status, '미수행')}</dd><dt>파드 준비</dt><dd>{summaryKo(v?.health?.status, '미수행')}</dd><dt>장애 요청 재현</dt><dd>{summaryKo(v?.repro?.status, '미수행')}</dd><dt>새 예외 로그</dt><dd>{summaryKo(v?.exceptions?.status, '미관측')}{v?.exceptions?.count != null ? ` · ${v.exceptions.count}건` : ''}</dd><dt>자동 원복</dt><dd>{summaryKo(rollbackKo(entry), '원복 확인 필요')}</dd></dl>{entry.error && <p role="alert" className={entry.status==='blocked' ? 'notice' : 'error'}>{summaryKo(entry.error, '단계 처리에 실패했습니다. 원문을 확인하세요.')}</p>}<Disclosure label={`${s} 단계 원문 보기`}>{() => <pre className="raw-text">{JSON.stringify(entry, null, 2)}</pre>}</Disclosure></section>;})}
    {cardError && <div className="error" role="alert">{summaryKo(cardError, '변경 요청 확인이 필요합니다.')}<Disclosure label="변경 오류 원문 보기">{() => <pre className="raw-text">{c.error}</pre>}</Disclosure></div>}
    <div className="inline">{!readOnly && canCancelChange(c) && <button type="button" className="btn" disabled={busy} onClick={()=>void cancel()}>변경 요청 취소</button>}</div>
    <ErrorBox>{error}</ErrorBox>
  </article>;
}
