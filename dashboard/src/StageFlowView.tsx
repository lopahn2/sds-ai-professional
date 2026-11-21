import { useState } from 'react';
import { DiagramIcon } from './DiagramIcon';
import { FlowGraph } from './FlowGraph';
import { flowStatusKo } from './narrative';
import type { semanticFlow } from './semanticFlow';
import { activeStageActors, waitingRunnerStages, type WorkSources } from './stageWork';

type Props = {
  flow: ReturnType<typeof semanticFlow>; selected: string | null; onSelect: (id: string) => void;
  all: boolean; sources: WorkSources; pending?: boolean; delayed?: boolean; control?: string;
  snapshot?: boolean; archived?: boolean; previousCore?: boolean;
};

export function StageFlowView({flow, selected, onSelect, all, sources, pending, delayed, control,
  snapshot = false, archived = false, previousCore = false}: Props) {
  const [revealKey, setRevealKey] = useState(0);
  const working = activeStageActors(flow.nodes, snapshot ? {} : sources);
  const waiting = flow.nodes.find(n => n.status.startsWith('awaiting_'));
  const runnerWait = waitingRunnerStages(flow.nodes, sources.application);
  function idleLabel() {
    if (snapshot) return '저장 당시의 작업 기록';
    if (delayed) return '현재 작업 갱신 지연';
    if (pending) return '현재 작업 확인 중';
    if (runnerWait) return runnerWait;
    if (archived) return '종료된 사건';
    if (control === 'paused') return '분석 일시정지';
    if (control === 'stopping') return '분석 중단 중';
    return waiting ? waiting.label + ' · ' + flowStatusKo(waiting.status) : '실행 중인 에이전트 미관측';
  }
  const props = { flow, selected, archived, previousCore, snapshot, activeStages: working };
  return <>
    {working.size > 0 ? <nav className="stage-work-locator" aria-label="현재 작업 위치">
      <span><DiagramIcon kind="agent"/>현재 작업</span>
      {flow.nodes.filter(n => working.has(n.id)).map(n => <button key={n.id} type="button"
        aria-label={n.label + ' 현재 작업 보기 · ' + working.get(n.id)!.join(', ')}
        onClick={() => { setRevealKey(k => k + 1); onSelect(n.id); }}>
        <strong>{n.label}</strong><span>{working.get(n.id)!.join(' · ')}</span>
      </button>)}
    </nav> : (all || runnerWait || delayed) && <p className="stage-work-idle" role="status">{idleLabel()}</p>}
    <FlowGraph {...props} mode={all ? 'all' : 'stage'} frameKey={String(revealKey)} onSelect={onSelect}/>
  </>;
}
