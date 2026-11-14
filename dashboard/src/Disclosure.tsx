import { useState, type ReactNode } from 'react';
import './incidentUx.css';

/** Raw values are absent from both DOM and accessibility tree until requested.
 * Stable keys preserve the disclosure across polls; incident roots are keyed. */
export function Disclosure({ label = '원문 보기', children, className = '', open: controlled, onOpen }: { label?: string; children: () => ReactNode; className?: string; open?: boolean; onOpen?: (open: boolean) => void }) {
  const [expanded, setExpanded] = useState(false);
  const open = controlled ?? expanded;
  const change = (next: boolean) => { setExpanded(next); onOpen?.(next); };
  return <details className={`disclosure ${className}`} open={open} onToggle={e => { const next = e.currentTarget.open; if (next !== open) change(next); }}><summary onClick={e => { e.preventDefault(); change(!open); }} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ' || e.code === 'Space') { e.preventDefault(); change(!open); } }}>{label}</summary>{open && <div className="disclosure-body">{children()}</div>}</details>;
}
