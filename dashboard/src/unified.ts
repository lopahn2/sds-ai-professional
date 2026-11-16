/** Display and confirmation contracts; the server remains the authority for every scope/gate. */
export type Data = Record<string, unknown>;
export type CodeActionSummary = {
  enabled: boolean; linked: boolean; incident_id: number; action_id: string | null; application_id: string | null;
  state: string; phase: string; state_ko: string; summary_ko: string; next_ko: string;
  problem_ko?: string | null; cause_ko?: string | null; runner_observed: boolean | null; observed_at: number | null;
  blocks_cluster?: boolean; eligible: boolean; blocked_code: string | null;
};
export type ApplicationIncident = { id: string; state: string; created_at: number; environment: string; service: string; system: string; reason: string; data: Data; stages?: Data[]; root_incident_id?: number | null };
export type CodeActionDetail = { summary: CodeActionSummary; application: ApplicationIncident; release: { incident_id: string; state: string; release: Data | null; test_build?: Data | null }; events: Data[] };
export type UnifiedCatalog = { enabled: boolean; profiles_configured: boolean; environments: string[]; historical_environments?: string[]; services: { service: string; system: string; profile_id: string | null }[] };
export type ApplicationHistory = { incidents: ApplicationIncident[]; total: number; as_of?: number };
export const object = (value: unknown): Data => value && typeof value === 'object' && !Array.isArray(value) ? value as Data : {};
export const word = (value: unknown): string | null => typeof value === 'string' && value.trim() ? value : null;
export const observed = (value: unknown) => value === true ? '확인됨' : value === false ? '미확인' : '미관측';
export const observedTime = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? new Date(value * 1000).toLocaleString('ko-KR') : '미관측';
export const continuationActive = (summary: CodeActionSummary | null) => Boolean(summary?.linked && (summary.blocks_cluster ?? !['completed', 'failed', 'handoff'].includes(summary.state)));
const STATES: Record<string, string> = { created: '접수됨', collecting: '근거 수집 중', source_queued: '소스 확인 대기', analyzing: '원인 분석 중', patching: '수정 중', test_build_queued: '시험·빌드 대기', dev_targets_collecting: 'dev 대상 확인 중', dev_deploy_queued: 'dev 배포 대기', dev_verifying: 'dev 검증 중', awaiting_user_test: 'dev 사용자 시험 대기', awaiting_prd_approval: 'prd 승인 대기', prd_deploy_queued: 'prd 배포 대기', prd_verifying: 'prd 검증 중', reconciliation_required: '쓰기 결과 확인 필요', completed: '처리 완료', failed: '처리 실패', handoff: '운영자 확인 필요' };
export const applicationStateKo = (state: string) => STATES[state] || '진행 상태 확인 필요';
export type Fingerprint = { release_id: string; artifact_sha256: string; plan_sha256: string; image: string };
export function fingerprint(release: Data): Fingerprint | null {
  const artifact = object(release.artifact);
  if (!word(release.release_id) || !/^[a-f0-9]{64}$/.test(String(release.artifact_sha256)) || !/^[a-f0-9]{64}$/.test(String(release.plan_sha256)) || !/^\S+@sha256:[a-f0-9]{64}$/.test(String(artifact.image))) return null;
  return { release_id: release.release_id as string, artifact_sha256: release.artifact_sha256 as string, plan_sha256: release.plan_sha256 as string, image: artifact.image as string };
}
export const currentRelease = (release: Data) => release.valid === true && release.protocol === 'dev-prd/v2' && fingerprint(release) !== null;
export function matchingFingerprint(value: Data, release: Data): boolean {
  const f = fingerprint(release);
  return Boolean(f && Object.entries(f).every(([k, v]) => value[k] === v));
}
export function validUserTest(release: Data, now = Date.now()): boolean {
  const test = object(release.user_test);
  return currentRelease(release) && test.kind === 'human' && test.passed === true && test.invalidated === false && !!word(test.id) && typeof test.expires_at === 'number' && test.expires_at * 1000 > now && matchingFingerprint(test, release);
}
export function mergeObserved(detail: CodeActionDetail, now = Date.now()): boolean {
  const merge = object(detail.application.data.merge_review), release = object(detail.release.release);
  return merge.human_merge_observed === true && matchingFingerprint(merge, release) && typeof merge.expires_at === 'number' && merge.expires_at * 1000 > now;
}
export function releaseKey(detail: CodeActionDetail): string {
  const release = object(detail.release.release);
  return JSON.stringify([detail.application.id, detail.application.state, fingerprint(release), release.valid, release.protocol, release.stage_results, release.user_test, detail.application.data.merge_review]);
}
export function canAttest(detail: CodeActionDetail, action: 'user-test' | 'approve-prd' | 'review-merge', now = Date.now()): boolean {
  const r = object(detail.release.release), state = detail.application.state;
  if (!currentRelease(r)) return false;
  if (action === 'user-test') return state === 'awaiting_user_test' && object(object(r.stage_results).dev).verified === true;
  if (action === 'review-merge') return state === 'awaiting_prd_approval' && validUserTest(r, now);
  return state === 'awaiting_prd_approval' && validUserTest(r, now) && mergeObserved(detail, now);
}

/** A recorded analysis conclusion is not promoted into an invented root-cause field. */
export function latestAnalysisSummary(analysis: Data): unknown {
  if (!Array.isArray(analysis.roles)) return null;
  const role = [...analysis.roles].reverse().map(object).find(r => ['analyst', 'specialist', 'verifier'].includes(String(r.role)) && word(object(r.result).summary_ko));
  return object(role?.result).summary_ko ?? null;
}
