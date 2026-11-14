import type { ReactNode } from 'react';
import { incidentStatusKo, incidentStatusTone, levelKo, levelTone } from './labels';
export const StatusBadge = ({ value, recorded = false, recoveryKind }: { value: string; recorded?: boolean; recoveryKind?: string | null }) => <span className={`badge ${incidentStatusTone(value)}`}>{recorded ? '저장 당시 ' : ''}{incidentStatusKo(value, recoveryKind)}</span>;
export const LevelBadge = ({ value }: { value: string | null | undefined }) => <span className={`badge ${levelTone(value)}`}>{levelKo(value)}</span>;
export const ErrorBox = ({ children }: { children?: ReactNode }) => children ? <div className="error" role="alert">{children}</div> : null;
export const Spinner = () => <span className="spinner" aria-hidden="true" />;
