// P20 · 관제실 설명 요소: 어떤 구성이 왜 선택됐고, 결론이 무엇을 근거로 하는지 기본 화면에서 보여 준다.
// 원문(인용문·영문 사유)은 요청할 때만 DOM에 들어간다(Disclosure). 한글 문장만 기본 표시한다.
import type { AgentCard, FlowNode, GroupCard, RoutingCard } from './api';
import { costKo, type Incident } from './api';
import { CitationLink } from './CitationLink';
import { Disclosure } from './Disclosure';
import { candidateLabel, quoteSummary, usageSummary, verdictTone, verifierKo } from './explainModel';
import { effortKo } from './labels';
import { confidenceKo, flowStatusKo } from './narrative';
import { numberKo } from './overviewModel';
import { summaryKo } from './presentation';

/** #1 역할별 모델·추론 강도 배치. 이 과제의 핵심이라 접힌 카드에서도 보인다. */
export function RoleModels({ roles }: { roles: GroupCard['roles'] }) {
  if (!roles.length) return null;
  return <ul className="role-models" aria-label="역할별 모델 배치">{roles.map(r => <li key={r.role} data-role={r.role}>
    <b>{r.role_ko || '역할 미기록'}</b>
    <span className="mono">{r.model || '모델 미기록'}</span>
    <span>추론 {effortKo(r.effort)}</span>
    <span className="role-state">{flowStatusKo(r.status)}</span>
  </li>)}</ul>;
}

/** #2 주 분석 모델과 선택 이유. full이면 후보별 점수 표까지 보인다. */
export function ModelChoice({ policy, full = false }: { policy: RoutingCard['model_policy']; full?: boolean }) {
  if (!policy) return null;
  return <div className="model-choice">
    <p><b>주 분석 모델</b> <span className="mono">{policy.selected_model || '미기록'}</span>{policy.reason_ko && <> · {summaryKo(policy.reason_ko, '선택 이유 미기록')}</>}</p>
    {full && policy.candidates.length > 0 && <table className="model-candidates" aria-label="분석 모델 후보 점수">
      <thead><tr><th scope="col">모델</th><th scope="col">적격</th><th scope="col">점수</th><th scope="col">사유</th></tr></thead>
      <tbody>{policy.candidates.map(m => <tr key={m.model} className={m.model === policy.selected_model ? 'selected' : undefined}>
        <th scope="row" className="mono">{m.model}{m.model === policy.selected_model && <span className="badge info">선택</span>}</th>
        <td>{m.eligible ? '적격' : '부적격'}</td><td className="mono">{m.score ?? '미기록'}</td><td>{summaryKo(m.reason_ko, '사유 미기록')}</td>
      </tr>)}</tbody>
    </table>}
  </div>;
}

/** #5 조기 종료·승격·예산 도달 같은 Group 진행 기록(서버가 감사 기록에서 만든 한글 문장). */
export function GroupProgress({ items }: { items: string[] }) {
  if (!items.length) return null;
  return <ol className="group-progress" aria-label="Group 진행 기록">{items.map((t, i) => <li key={i}>{summaryKo(t, '진행 기록 확인 중')}</li>)}</ol>;
}

/** #4 상위 모델 승격의 이유. 승격된 실행과 그 뒤의 재실행 양쪽에 붙는다. */
export function EscalationNote({ n }: { n: FlowNode }) {
  const e = n.escalation ?? n.escalated_from;
  if (!e) return null;
  const path = <span className="mono">{e.from_model || '미기록'} · {effortKo(e.from_effort)} → {e.to_model || '미기록'} · {effortKo(e.to_effort)}</span>;
  return <div className="escalation-note" role="note">
    <p><b>{n.escalation ? '상위 모델로 승격' : '승격 후 재실행'}</b> {n.escalation ? e.reason_ko : `이전 실행이 반려되어 다시 분석했습니다. ${e.reason_ko}`}</p>
    <p className="escalation-path">{path}</p>
    {e.reason_raw && <Disclosure label="승격 사유 원문 보기">{() => <pre className="raw-text">{e.reason_raw}</pre>}</Disclosure>}
  </div>;
}

/** #3·#6·#8 결론의 근거: 원인 후보, 인용, 반대 근거, 참고 문서. */
export function AgentBasis({ card }: { card: AgentCard }) {
  const q = quoteSummary(card), counter = card.counter_evidence ?? [], cites = card.citations ?? [];
  const counterKo = counter.filter(c => c.text_ko), counterRaw = counter.filter(c => !c.text_ko);
  return <div className="agent-basis">
    {card.acceptance?.rejected && <p className="notice" role="note"><b>근거 검사 미통과</b> {card.acceptance.reason_ko}</p>}
    {card.candidates.length > 0 && <><h4>원인 후보</h4><ol className="cause-list">{card.candidates.map((x, i) => <li key={i}>
      <p>{summaryKo(x.cause_ko, '한글 해석 미기록 · 원문 보기에서 확인하세요.')}</p>
      <p className="cause-meta"><span>{confidenceKo(x.confidence)}</span>{verifierKo(x.verifier) && <span className={`badge ${verdictTone(x.verifier)}`}>검증 {verifierKo(x.verifier)}</span>}
        {(x.evidence_refs ?? []).length > 0 && <span className="cause-refs" aria-label="인용한 근거 번호">{x.evidence_refs!.map(r => <code key={r}>{r}</code>)}</span>}</p>
    </li>)}</ol></>}
    {card.quotes !== undefined && <><h4>근거 인용</h4>
      {q.total === 0 ? <p className="muted">이 단계가 남긴 인용이 없습니다.</p> : <>
        <p className="basis-line">인용 {q.total}건 · 수집된 근거로 확인되는 출처 {q.known}건{q.sources.length > 0 && <span className="basis-sources">{q.sources.map(s => <span className="badge" key={s.label}>{s.label} {s.count}</span>)}</span>}</p>
        {q.unknownRefs.length > 0 && <p className="notice" role="note">수집되지 않은 근거 번호를 가리킨 인용이 있습니다: {q.unknownRefs.map(r => <code key={r}>{r}</code>)}</p>}
        <Disclosure label={`인용 원문 보기 (${q.total}건)`}>{() => <ol className="quote-list">{card.quotes!.map((x, i) => <li key={i} className={x.ref_known ? '' : 'unknown'}>
          <p className="quote-meta"><b>{x.ref_ko || '출처 미확인'}</b><code>{x.ref}</code><span>{x.candidate_index == null ? '지지 대상 미지정' : `${candidateLabel(x.candidate_index)} 지지`}</span></p>
          <pre className="raw-text">{x.quote}</pre>
        </li>)}</ol>}</Disclosure>
      </>}</>}
    {card.counter_evidence !== undefined && <><h4>반대 근거</h4>
      {counter.length === 0 ? <p className="muted">기록된 반대 근거가 없습니다.</p> : <>
        <p className="basis-line">반대 근거 {counter.length}건</p>
        {counterKo.length > 0 && <ul className="counter-list">{counterKo.map((c, i) => <li key={i}>{summaryKo(c.text_ko, '원문에서 확인하세요.')}</li>)}</ul>}
        {counterRaw.length > 0 && <Disclosure label={`반대 근거 원문 보기 (${counterRaw.length}건)`}>{() => <ul className="counter-list">{counterRaw.map((c, i) => <li key={i}><pre className="raw-text">{c.text_raw}</pre></li>)}</ul>}</Disclosure>}
      </>}</>}
    {cites.length > 0 && <><h4>참고 문서</h4><ul className="citation-list" aria-label="이 단계가 인용한 참고 문서">{cites.map(c => <li key={c.chunk_id}>
      <p><CitationLink citation={{ ...c, score: c.score ?? 0 }} /><span className="badge">{SOURCE_KIND_KO[c.source] ?? '참고 자료'}</span>{c.section && <span className="muted">{c.section}</span>}</p>
      <Disclosure label="문서 발췌 보기">{() => <pre className="raw-text">{c.content}</pre>}</Disclosure>
    </li>)}</ul></>}
  </div>;
}
const SOURCE_KIND_KO: Record<string, string> = { runbook: '런북', public_doc: '공개 문서', past_incident: '과거 사건', reference: '판단 참고자료' };

/** #7 독립 검증 결과: 무엇을 검증했고 수락·반려했는지. */
export function VerifyResult({ card }: { card: AgentCard }) {
  const verdicts = card.candidate_verdicts ?? [];
  if (!card.verdict && !verdicts.length) return null;
  return <div className={`verify-result ${verdictTone(card.verdict)}`} aria-label="독립 검증 결과">
    <p className="verify-head"><b>검증 판정</b><span className={`badge ${verdictTone(card.verdict)}`}>{card.verdict_ko || '판정 미기록'}</span>{card.next_action_ko && <span>다음 행동 · {card.next_action_ko}</span>}</p>
    {verdicts.length > 0 && <ol className="verdict-list">{verdicts.map((v, i) => { const cand = v.index == null ? null : card.candidates[v.index]; return <li key={i}>
      <p><b>{candidateLabel(v.index)}</b><span className={`badge ${verdictTone(v.verdict)}`}>{v.verdict_ko || '판정 미기록'}</span></p>
      {cand && <p className="muted">검증 대상 · {summaryKo(cand.cause_ko, '한글 해석 미기록')}</p>}
      {v.reason_ko ? <p>{summaryKo(v.reason_ko, '사유는 원문에서 확인하세요.')}</p> : v.reason_raw && <Disclosure label="판정 사유 원문 보기">{() => <pre className="raw-text">{v.reason_raw}</pre>}</Disclosure>}
    </li>; })}</ol>}
  </div>;
}

/** #9 사건의 AI 사용 요약: 호출 수, 모델별 호출, 토큰, 비용, 분석 시간. 빠진 값이 있으면 합계는 미관측이다. */
export function UsageLine({ incident, nodes }: { incident: Incident; nodes: FlowNode[] }) {
  const u = usageSummary(nodes);
  if (!u.calls) return null;
  return <dl className="headline-usage" aria-label="AI 사용 요약">
    <div><dt>모델 호출</dt><dd>{u.calls}회{u.byModel.length > 0 && <span className="usage-models">{u.byModel.map(([m, c]) => <span key={m}><span className="mono">{m}</span> {c}</span>)}</span>}</dd></div>
    <div><dt>토큰 입력 / 출력</dt><dd>{numberKo(u.tokensIn)} / {numberKo(u.tokensOut)}</dd></div>
    <div><dt>비용</dt><dd>{costKo(incident)}</dd></div>
    <div><dt>모델 응답 시간 합</dt><dd>{u.durationMs == null ? '미관측' : `${numberKo(u.durationMs / 1000, 1)}초`}</dd></div>
  </dl>;
}
