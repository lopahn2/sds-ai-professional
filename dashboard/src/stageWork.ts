import type { AppActivity } from './ApplicationWorkTrace';
import { appFinished, isAppStep } from './applicationFlow.ts';
import type { RunEvent } from './feed';
import { flowTitle } from './narrative.ts';
import type { SemanticNode } from './semanticFlow.ts';

export type WorkSources = { coreLive?: boolean; events?: RunEvent[]; application?: AppActivity | null };

/** A queued lease can explain a quiet stage without claiming an agent is running. */
export function waitingRunnerStages(nodes: SemanticNode[], application?: AppActivity | null) {
  const labels = new Set<string>();
  for (const job of application?.active_jobs ?? []) {
    if (appFinished(application?.state) && job.kind !== 'current_version_check') continue;
    const state = { waiting: '실행기 인수 대기', contact_lost: '실행기 연락 끊김', unobserved: '실행기 연결 미관측',
      leased: '', reclaimed: '' }[job.state];
    if (!state) continue;
    const stage = nodes.find(n => n.aliases?.includes('app-job:' + job.job_id));
    if (stage) labels.add(stage.label + ' · ' + state);
  }
  return [...labels].join(' / ');
}

/** A selected card or a lifecycle state alone does not identify a working agent. */
export function activeStageActors(nodes: SemanticNode[], {coreLive, events, application}: WorkSources) {
  const ended = new Set(events?.filter(e => e.kind === 'agent_finished').map(e => e.detail?.call_id));
  const calls = new Map<string, string>();
  for (const {event, data} of application?.events ?? []) {
    if (event === 'analysis_retry' && data.role) {
      for (const [id, role] of calls) if (role === data.role) calls.delete(id);
    }
    if (!data.call_id || !data.role) continue;
    if (event === 'role_completed' || (event === 'role_progress' && ['failed', 'succeeded'].includes(data.phase ?? ''))) {
      calls.delete(data.call_id);
    } else if (event === 'role_started' || event === 'role_progress') {
      calls.set(data.call_id, data.role);
    }
  }
  const closed = appFinished(application?.state);
  const leasedJobs = new Set(application?.active_jobs?.filter(job =>
    (!closed || job.kind === 'current_version_check') &&
    (job.state === 'leased' || job.state === 'reclaimed')).map(job => 'job:' + job.job_id));
  const latestStage = nodes.reduce((index, node) =>
    node.semantic.members.reduce((max, m) => isAppStep(m) ? Math.max(max, m.app.stageIndex) : max, index), -1);
  const active = new Map<string, string[]>();
  for (const stage of nodes) {
    const actors = new Set<string>();
    for (const member of stage.semantic.members) {
      if (isAppStep(member)) {
        const key = member.app.workKey;
        // Some test/build file events also appear on the code change card as historical evidence.
        if (!key || !stage.aliases?.includes(member.id)) continue;
        if (leasedJobs.has(key)) actors.add('실행기 · ' + flowTitle(member));
        else if (!closed && key.startsWith('call:') && member.status === 'running' &&
            member.app.stageIndex === latestStage && member.app.state === application?.state && calls.has(key.slice(5))) {
          actors.add(flowTitle(member));
        }
      } else if (coreLive && member.status === 'running' && !(member.call_id && ended.has(member.call_id))) {
        if ((member.role && ['agent', 'verify', 'router'].includes(member.type)) || member.type === 'investigation') {
          actors.add(flowTitle(member));
        } else if (member.card?.kind === 'action' && member.card.status === 'executing') {
          actors.add('Kubernetes 조치 실행기');
        }
      }
    }
    if (actors.size) active.set(stage.id, [...actors]);
  }
  return active;
}
