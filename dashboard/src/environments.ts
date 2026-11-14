/** 2026-11-13 dev/prd 전환(docs/operations/liveprobe-real-operation-dev-prd-2026-11-13.md). 백엔드 liveprobe/app/environments.py와 같은 의미다.
 *  - 활성 환경(dev, prd)만 현재 지도·환경 요약·새 실행/승격의 대상이다.
 *  - 퇴역 환경(stg)은 과거 사건·검토 기록·CSV를 찾는 필터에만 남긴다. 새 실행·승격 대상으로 제시하지 않는다.
 *  순수 모듈(React 없음). */
export const ACTIVE_ENVIRONMENTS = ['dev', 'prd'] as const;
export const RETIRED_ENVIRONMENTS = ['stg'] as const;
/** 이력 필터 표시 순서(과거 승격 순서 유지). */
export const HISTORY_ENVIRONMENTS = ['dev', 'stg', 'prd'] as const;
export type HistoryEnvironment = typeof HISTORY_ENVIRONMENTS[number];

export const isRetiredEnvironment = (e: string | null | undefined): boolean => (RETIRED_ENVIRONMENTS as readonly string[]).includes(e ?? '');
export const isHistoryEnvironment = (e: string | null | undefined): e is HistoryEnvironment => (HISTORY_ENVIRONMENTS as readonly string[]).includes(e ?? '');
/** 필터 값은 그대로 두고(서버 environment=stg 이력 조회 유지) 표시 이름에만 퇴역 여부를 밝힌다. */
export const environmentFilterLabel = (e: string): string => isRetiredEnvironment(e) ? `${e} (퇴역·이력)` : e;
export const historyEnvironmentOptions = (): { value: HistoryEnvironment; label: string }[] =>
  HISTORY_ENVIRONMENTS.map(value => ({ value, label: environmentFilterLabel(value) }));
/** URL·입력에서 받은 환경 필터 값. 알려진 이력 환경(퇴역 stg 포함)만 허용하고 나머지는 전체('')로 둔다. */
export const historyEnvironment = (raw: string | null | undefined): HistoryEnvironment | '' => isHistoryEnvironment(raw) ? raw : '';
