import { useRef } from 'react';
import { ApiError, localOperatorCsrf, request } from './api';
export type HumanScope = 'incident:read' | 'incident:create' | 'tester' | 'prd-approver';

/** One local operator per gateway. The API verifies the signed request at every boundary. */
export function useHumanAccess(prefix: '' | '/application/v1' = '') {
  const token = localOperatorCsrf();
  const generation = useRef(0);
  const previous = useRef(token);
  if (previous.current !== token) {
    generation.current++;
    previous.current = token;
  }
  const connected = !!token;
  return { prefix, session: { subject: connected ? '로컬 운영자' : undefined },
    connected, permits: (_scope: HumanScope) => !!localOperatorCsrf(), revision: generation.current,
    generation, call: request };
}
export type HumanAccess = ReturnType<typeof useHumanAccess>;
export function HumanAccessForm({ auth }: { auth: HumanAccess; label: string; scope?: HumanScope; onClear?: () => void }) {
  return auth.connected ? null : <p className="notice" role="status">이 작업은 로컬 운영자 관제 주소에서 열 수 있습니다.</p>;
}
export function authError(error: unknown): string {
  if (error instanceof ApiError && [401, 403].includes(error.status)) return '로컬 운영자 연결을 확인할 수 없습니다. 전용 관제 주소에서 새로고침하세요.';
  if (error instanceof ApiError && error.status === 409) return '상태나 산출물이 변경되었거나 필요한 검증이 남아 있습니다. 최신 내용을 다시 확인하세요.';
  if (error instanceof ApiError && error.status === 422) return '입력값과 확인 대상 지문을 확인하세요.';
  return '요청 결과를 확인하지 못했습니다. 최신 상태를 조회한 뒤 다시 확인하세요.';
}
export const aborted = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
