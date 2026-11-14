import { useState } from 'react';
import type { FlowNode } from './api';
import { clock } from './api';
import type { ExecutionView, StreamState } from './execution';
import { Disclosure } from './Disclosure';
import { executionStageKo, publicAnswer } from './presentation';

const CONNECTION: Record<StreamState, string> = { connecting: '연결 중', live: '실시간 연결', reconnecting: '재연결 중 · 저장 기록으로 갱신', polling: '저장 기록으로 갱신', snapshot: '저장 표본' };
const PHASE: Record<string, string> = { started: '요청 중', waiting: '응답 대기', responding: '답변 수신 중', succeeded: '응답 수신 완료', failed: '호출 실패' };

export function ExecutionStream({ node, view, state, error, receivedAt }: { node: FlowNode; view: ExecutionView; state: StreamState; error: boolean; receivedAt: string | null }) {
  const [show, setShow] = useState(true);
  const running = node.status === 'running' && !view.final;
  const finalOk = view.final?.detail?.status === 'succeeded';
  if (!view.records.length && !running) return null;
  const last = view.records.at(-1);
  return <div className="execution-stream" aria-label="실시간 실행 내용">
    <div className="execution-head">
      <b>실행 내용</b>
      <span className={`badge ${error || state === 'reconnecting' ? 'warn' : running ? 'info' : ''}`}>
        {running ? CONNECTION[state] : '저장 기록'}
      </span>
      <button type="button" className="link-btn" aria-expanded={show} onClick={() => setShow(v => !v)}>{show ? '실행 내용 접기' : '실행 내용 펼치기'}</button>
    </div>
    {show && <>
      {state === 'snapshot' && <p className="muted" role="note">저장 당시 실행 중이었던 표본입니다. 이 화면에서 실제 분석은 실행되지 않으며 새 답변은 발생하지 않습니다.</p>}
      {error && <p role="status" className="warning-text">갱신이 지연되고 있습니다. 마지막으로 받은 기록을 유지하며 다시 연결합니다.</p>}
      <p className="execution-stage" role="status">{last ? executionStageKo(last.kind, last.detail?.phase, last.detail?.status) : '공개 답변을 기다리고 있습니다.'}</p>
      {view.attempts.map((a, index) => <section className="execution-attempt" key={a.id} aria-label={`호출 ${index + 1}`}>
        <div className="execution-attempt-head"><b>호출 {index + 1}</b><span className={`badge ${a.phase === 'failed' ? 'danger' : 'info'}`}>{PHASE[a.phase] || '실행 기록'}</span></div>
        {a.text ? <>
          <p className="faint">{view.final ? finalOk && a.phase === 'succeeded' && index === view.attempts.length - 1 ? '수신한 공개 답변입니다. 확정 결과는 위 분석 결과를 확인하세요.' : '이 응답은 확정 결론으로 사용되지 않았습니다. 실패·재시도 기록을 확인하세요.' : '생성 중인 임시 답변입니다. 최종 결과는 달라질 수 있습니다.'}</p>
          {publicAnswer(a.text) ? <pre className="execution-output">{publicAnswer(a.text)}</pre> : <p className="muted">구조화된 응답을 수신 중입니다. 읽을 수 있는 요약이 준비되면 표시합니다.</p>}
          <Disclosure label="공개 답변 원문 보기">{() => <pre className="raw-text">{a.text}</pre>}</Disclosure>
        </> : <p className="muted">{a.mode === 'complete_only' ? '이 제공자는 완료 응답을 한 번에 전달합니다.' : a.mode === 'messages' ? '공개 답변 메시지를 기다리고 있습니다.' : a.phase === 'failed' ? '이 호출에서 공개 답변을 받지 못했습니다.' : '공개 답변 항목이 도착하면 여기에 표시됩니다.'}</p>}
      </section>)}
      {running && !view.attempts.length && <p className="muted">{state === 'snapshot' ? '저장된 생성 중 답변이 없습니다. 아래 스트리밍 예시 링크에서 수신 동작을 확인할 수 있습니다.' : '실행 기록을 기다리고 있습니다. 모델의 공개 답변이 도착하면 함께 표시됩니다.'}</p>}
      {last && <p className="faint execution-last">마지막 기록 {clock(last.at)}{running && receivedAt ? ` · 수신 ${clock(receivedAt)}` : ''}</p>}
      <Disclosure label="실행 기록 원문 보기">{() => <ol className="execution-records">{view.records.map(e => <li key={e.seq}><time>{clock(e.at)}</time><pre className="raw-text">{e.summary}</pre></li>)}</ol>}</Disclosure>
      <span className="sr-only" role="status">{running && state !== 'snapshot' ? '분석 실행 내용을 갱신 중입니다.' : '분석 실행 기록 표시'}</span>
    </>}
  </div>;
}
