import { createContext, useContext, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { FlowNode } from './api';

type Selection = Pick<FlowNode, 'id' | 'depth'>;
type Travel = { from: string; to: string; direction: 'forward' | 'backward' };
const StageMotionContext = createContext<Travel | null>(null);
export const useStageTravel = () => useContext(StageMotionContext);

/** One transition drives the connector, destination and detail, including when the live stage advances. */
export function StageMotion({ node, children }: { node: Selection | null; children: ReactNode }) {
  const id = node?.id;
  const depth = node?.depth;
  const previous = useRef<Selection | null>(null);
  const [travel, setTravel] = useState<Travel | null>(null);
  useLayoutEffect(() => {
    const last = previous.current;
    previous.current = id !== undefined && depth !== undefined ? { id, depth } : null;
    if (!last || !id || depth === undefined) {
      setTravel(null);
      return;
    }
    if (last.id === id) return;
    setTravel({ from: last.id, to: id, direction: last.depth > depth ? 'backward' : 'forward' });
  }, [id, depth]);
  useLayoutEffect(() => {
    if (!travel) return;
    // Clear the cue so reopening a hidden pane later cannot replay an old selection.
    const timer = window.setTimeout(() => setTravel(null), 500);
    return () => window.clearTimeout(timer);
  }, [travel]);
  return <StageMotionContext.Provider value={travel?.to === id ? travel : null}>{children}</StageMotionContext.Provider>;
}
