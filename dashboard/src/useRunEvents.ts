import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import type { RunEvent } from './feed';
import { mergeRunEvents, type StreamState } from './execution';

type Snapshot = { events: RunEvent[]; control: string; streaming?: boolean; execution_mode?: 'snapshot' | 'live' };
/** Initial/recovery snapshots plus a narrow SSE stream; chunks never reload the whole incident. */
export function useRunEvents(id: string, finished: boolean, revision: number, onBoundary: () => void) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [control, setControl] = useState('running');
  const [error, setError] = useState(false);
  const [state, setState] = useState<StreamState>('connecting');
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const cursor = useRef(0), done = useRef(finished), boundary = useRef(onBoundary);
  done.current = finished; boundary.current = onBoundary;
  useEffect(() => {
    let stopped = false, loading = false, source: EventSource | null = null;
    let poll: ReturnType<typeof setTimeout>, reconnect: ReturnType<typeof setTimeout>;
    let backoff = 1000, connected = false, supportsStream = false, retryScheduled = false;
    const controller = new AbortController();
    function accept(items: RunEvent[]) {
      if (stopped || !items.length) return;
      const fresh = items.filter(e => e.seq > cursor.current);
      cursor.current = Math.max(cursor.current, ...items.map(e => e.seq));
      setEvents(old => mergeRunEvents(old, items)); setReceivedAt(new Date().toISOString());
      if (fresh.some(e => ['agent_started', 'agent_finished', 'completed'].includes(e.kind))) boundary.current();
    }
    function connect() {
      if (stopped || source || !supportsStream) return;
      const s = new EventSource(`/api/incidents/${encodeURIComponent(id)}/events/stream?since=${cursor.current}`);
      source = s;
      s.addEventListener('run_event', e => {
        if (stopped || source !== s) return;
        try {
          const row = JSON.parse((e as MessageEvent).data) as RunEvent;
          if (!Number.isSafeInteger(row.seq) || row.seq < 1 || typeof row.kind !== 'string') return;
          accept([row]); setError(false);
        } catch { /* A bad transport frame is recovered by the snapshot poll. */ }
      });
      s.onopen = () => { if (!stopped && source === s) { connected = true; backoff = 1000; setState('live'); setError(false); void load(); } };
      s.onerror = () => {
        if (stopped || source !== s) return;
        s.close(); source = null; connected = false; setState('reconnecting');
        void load();
        retryScheduled = true;
        reconnect = setTimeout(() => { retryScheduled = false; connect(); }, backoff);
        backoff = Math.min(backoff * 2, 30000);
      };
    }
    async function load() {
      if (loading || stopped) return;
      loading = true;
      try {
        let page: Snapshot;
        do {
          page = await request<Snapshot>(`/incidents/${id}/events?since=${cursor.current}`, { signal: controller.signal });
          if (stopped) return;
          const previousCursor = cursor.current;
          accept(page.events ?? []); setControl(page.control); setError(false);
          supportsStream = page.streaming === true;
          if (cursor.current === previousCursor) break;
        } while ((page.events?.length ?? 0) >= 500);
        if (!supportsStream) setState(page.execution_mode === 'snapshot' ? 'snapshot' : 'polling');
      } catch { if (!stopped) setError(true); }
      finally {
        loading = false;
        if (!stopped) {
          clearTimeout(poll);
          poll = setTimeout(() => { void load().then(() => { if (!retryScheduled) connect(); }); }, connected ? 10000 : done.current ? 10000 : 2000);
        }
      }
    }
    void load().then(connect);
    return () => { stopped = true; controller.abort(); clearTimeout(poll); clearTimeout(reconnect); source?.close(); };
  }, [id, revision]);
  return { events, control, error, state, receivedAt };
}
