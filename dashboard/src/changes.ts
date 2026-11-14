export type ReleaseStage = 'dev' | 'stg' | 'prd';
export type Verification = { ok?: boolean; rollout?: {status: string}; health?: {status:string}; repro?: {status:string}; exceptions?: {status:string; count?:number; observation_seconds?:number} };
export type ChangeBuild = { commit?:string; branch?:string; image?:string; image_id?:string; repository?:string; pr_url?:string; pr_number?:number; head_sha?:string; pr_state?:string; merge_sha?:string; main_sha?:string; review_completed?:boolean; review_checked_at?:string; merge_build?:{image?:string;commit?:string} };
export type StageEntry = {status:string;previous_image?:string;new_image?:string;started_at?:string;finished_at?:string;verification?:Verification;reverted?:boolean;rollback?:Verification&{status?:string};error?:string;blocked_reason?:string};
export type ChangeRequest = { id:number; incident_id:number; system:string; service:string; namespace_origin:string; status:string; patch_summary_ko:string|null; diff:string|null; diff_sha256?:string|null; tests:{total?:number|null;failed?:number|null;report_tail?:string;risk_ko?:string;added?:string[]}; build:ChangeBuild; stages:Partial<Record<ReleaseStage,StageEntry>>; error:string|null; created_at?:string;updated_at?:string };
export type PromotionPlan = { stage:ReleaseStage; namespace:string; deployment:string;container:string;previous_image:string;new_image:string;side_effects_ko:string[];rollback_ko:string;plan_sha256:string };
const STATUS_KO: Record<string,string> = {awaiting_source:'소스 수신 대기',patching:'패치 생성 중',awaiting_build:'시험·빌드 대기',building:'시험·빌드 중',built:'시험·빌드 완료',patch_failed:'패치 생성 실패',tests_failed:'회귀 시험 실패',build_failed:'이미지 빌드 실패',dev_deploying:'개발 환경 승격·검증 중',dev_verified:'개발 환경 검증 완료',dev_failed:'개발 환경 검증 실패',stg_deploying:'검증 환경 승격·검증 중',stg_verified:'검증 환경 검증 완료',stg_failed:'검증 환경 검증 실패',prd_deploying:'운영 환경 승격·검증 중',prd_verified:'운영 환경 검증 완료',prd_failed:'운영 환경 검증 실패',cancelled:'취소됨',error:'요청 준비 실패'};
export function changeProgress(c:ChangeRequest) {
  // 과거 기록의 단계 위치를 보존하려고 stg 칸은 유지하되 퇴역 환경임을 이름에 밝힌다(새 승격 대상 아님).
  const labels=['소스 수신','패치 생성','시험·빌드','dev','stg(퇴역·이력)','prd'];
  const positions:Record<string,number>={awaiting_source:0,patching:1,patch_failed:1,awaiting_build:2,building:2,tests_failed:2,build_failed:2,built:3,dev_deploying:3,dev_failed:3,dev_verified:4,stg_deploying:4,stg_failed:4,stg_verified:5,prd_deploying:5,prd_failed:5,prd_verified:6};
  const current=positions[c.status] ?? -1;
  // 퇴역 stg 칸은 실제로 거친 기록(완료·실패·진행 중)만 그대로 보이고, 거치지 않았다면 '대기'가 아니라 퇴역으로 표시한다(새로 갈 단계가 아니다).
  return labels.map((label,i)=>{const state=i<current?'완료':i===current?(c.status.endsWith('_failed')?'실패':c.status.endsWith('_deploying')||['awaiting_source','patching','building','awaiting_build'].includes(c.status)?'진행 중':'대기'):'대기';return {label,state:i===4&&state==='대기'?'퇴역·해당 없음':state};});
}
export const changeStatusKo = (status:string) => STATUS_KO[status] ?? '상태 미확인';
/** 2026-11-13 dev/prd 전환: 활성 환경은 dev·prd, stg는 퇴역(이력 전용)이다. 기존 기록의 stg 단계 결과는 그대로 표시한다. */
export const ACTIVE_STAGES: readonly ReleaseStage[] = ['dev', 'prd'];
export const RETIRED_STAGES: readonly ReleaseStage[] = ['stg'];
export const isRetiredStage = (s:string) => (RETIRED_STAGES as readonly string[]).includes(s);
/** 기존 변경 기록에 실제로 남은 단계만 과거 승격 순서대로 돌려준다(퇴역 stg 기록도 이력으로 포함). */
export const recordedStages = (c:Pick<ChangeRequest,'stages'>):ReleaseStage[] => (['dev','stg','prd'] as ReleaseStage[]).filter(s => { const e = c.stages[s]; return !!e && e.status !== 'planned'; });
/** 다음 승격 단계 제안. K8s 제품의 새 승격은 Application으로 이관되어 서버가 410을 돌려주므로 이 화면은 어떤 단계도 제안하지 않는다.
 *  특히 퇴역 환경(stg)은 절대 제안하지 않는다. 과거 dev→stg→prd 기록은 이력 표시(changeProgress·단계 결과)로만 남는다. */
export const nextStage = (_c:ChangeRequest):ReleaseStage|null => null;
export const changePollMs = (changes:ChangeRequest[]) => changes.some(c=>['awaiting_source','patching','awaiting_build','building','dev_deploying','stg_deploying','prd_deploying'].includes(c.status)) ? 2000 : 10000;
export const canCancelChange = (c:ChangeRequest) => !['building','dev_deploying','stg_deploying','prd_deploying','prd_verified','cancelled'].includes(c.status);
export const promotionConfirmText = (p:PromotionPlan) => [`${p.stage} · ${p.deployment} 승격`, ...p.side_effects_ko, `원복 방법: ${p.rollback_ko}`, '정말 진행하시겠습니까?'].join('\n');
// 서버가 실행 전에 거절한 단계(blocked)는 클러스터를 바꾸지 않았으므로 실패·원복으로 표시하지 않는다.
export const stageHeadingKo = (e:StageEntry) => ({verified:'검증 완료',failed:'검증 실패',blocked:'실행 전 중단 · 변경 없음'} as Record<string,string>)[e.status] ?? '진행 중';
export function rollbackKo(e:StageEntry):string {
  if (e.status==='blocked') return '변경이 없어 원복하지 않음';
  if (e.reverted) return '이전 이미지로 원복·검증 완료';
  // 서버는 성공 단계에도 빈 rollback 기록을 남기므로 존재 여부가 아니라 단계 결과로 판단한다.
  return e.status==='failed' ? '원복 미확정 · 사람 확인 필요' : '해당 없음';
}
// 단계 결과에 이미 표시된 오류는 카드 아래에 반복하지 않는다(실행 전 중단은 요청 실패가 아니다).
export function cardErrorKo(c:ChangeRequest):string|null {
  if (!c.error) return null;
  const shown = Object.values(c.stages).some(e => e?.error === c.error);
  return shown ? null : `요청 실패: ${c.error}`;
}
export function safePrUrl(b:ChangeBuild):string|null {
  const expected = `https://github.com/${b.repository}/pull/${b.pr_number}`;
  return /^live-probe\/[a-z0-9-]+$/.test(b.repository ?? '') && Number.isInteger(b.pr_number) && (b.pr_number ?? 0)>0 && b.pr_url === expected ? expected : null;
}
