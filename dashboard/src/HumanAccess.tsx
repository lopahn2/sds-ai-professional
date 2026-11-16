import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, request } from './api';
/** Passwords live only in the input DOM and this ref, never React state or persistence. */
export function useHumanAccess() {
  const input = useRef<HTMLInputElement>(null), bearer = useRef(''), generation = useRef(0);
  const controllers = useRef(new Set<AbortController>());
  const [connected, setConnected] = useState(false), [revision, setRevision] = useState(0);
  const clear = useCallback(() => {
    bearer.current = ''; if (input.current) input.current.value = '';
    generation.current++; for (const c of controllers.current) c.abort(); controllers.current.clear();
    setConnected(false); setRevision(v => v + 1);
  }, []);
  useEffect(() => () => { bearer.current = ''; generation.current++; if (input.current) input.current.value = ''; for (const c of controllers.current) c.abort(); controllers.current.clear(); }, []);
  const connect = (e?: FormEvent) => {
    e?.preventDefault(); const value = input.current?.value.trim() || '';
    clear(); if (!value) return;
    bearer.current = value; setConnected(true); setRevision(v => v + 1);
  };
  const call = useCallback(async <T,>(path: string, init?: RequestInit): Promise<T> => {
    if (!bearer.current) throw new ApiError(401, 'human_auth_required');
    const epoch = generation.current, controller = new AbortController(); controllers.current.add(controller);
    try {
      const result = await request<T>(path, { ...init, signal: controller.signal, headers: { ...init?.headers, Authorization: `Bearer ${bearer.current}` } });
      if (generation.current !== epoch) throw new DOMException('Authentication cleared', 'AbortError');
      return result;
    } finally { controllers.current.delete(controller); }
  }, []);
  return { input, connected, revision, generation, connect, clear, call };
}
export type HumanAccess = ReturnType<typeof useHumanAccess>;
export function HumanAccessForm({ auth, label, onClear }: { auth: HumanAccess; label: string; onClear?: () => void }) {
  return <div className="human-access">
    {auth.connected ? <div className="inline"><span>사람 인증 입력됨 · 권한은 요청마다 확인합니다.</span><button className="btn sm" type="button" onClick={() => { auth.clear(); onClear?.(); }}>인증 해제</button></div>
      : <form onSubmit={auth.connect} autoComplete="off"><label>{label}<input ref={auth.input} type="password" autoComplete="off" spellCheck={false} required maxLength={8192} /></label><button className="btn" type="submit">인증 적용</button></form>}
    <small>화면을 떠나거나 인증을 해제하면 입력한 인증값을 지웁니다. 운영자 인수는 시험·승인 권한을 대신하지 않습니다.</small>
  </div>;
}
export function authError(error: unknown): string {
  if (error instanceof ApiError && [401, 403].includes(error.status)) return '이 작업에 필요한 사람 권한을 확인하지 못했습니다. 해당 권한의 인증으로 다시 요청하세요.';
  if (error instanceof ApiError && error.status === 409) return '상태나 산출물이 변경되었거나 필요한 검증이 남아 있습니다. 최신 내용을 다시 확인하세요.';
  if (error instanceof ApiError && error.status === 422) return '입력값과 확인 대상 지문을 확인하세요.';
  return '요청 결과를 확인하지 못했습니다. 최신 상태를 조회한 뒤 다시 확인하세요.';
}
export const aborted = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
