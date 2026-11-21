import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { loadOpenIncidents } from './liveSnapshot';
import { subscribeIncidents, type SourceLike } from './liveStream';
import { request, type Incident } from './api';
import { diffAlerts, alertText } from './feed';
import { loadWaiting, caseHref } from './serviceAtlasModel';
import { applicationStateKo } from './unified';

export type Accounts = { mode: string; active?: string | null; accounts: { name: string; state: string; blocked_for_s?: number }[]; note?: string };
type ReadState = 'loading' | 'ready' | 'error';
type Waiting = Awaited<ReturnType<typeof loadWaiting>>;
type Notice = { href: string; label: string; key: number; at: number };
type Ctx = { open: Incident[]; openState: ReadState; automatic: Waiting; automaticState: ReadState; waiting: Waiting; waitingState: ReadState; accounts: Accounts | null; refresh: () => void; notices: Notice[]; unread: number; read: () => void };
const C = createContext<Ctx | null>(null);
export const useLive = () => { const v = useContext(C); if (!v) throw new Error('LiveProvider missing'); return v; };

export function LiveProvider({children}: {children: ReactNode}) {
  const [open,setOpen] = useState<Incident[]>([]), [openState,setOpenState] = useState<ReadState>('loading');
  const [waiting,setWaiting] = useState<Waiting>([]), [waitingState,setWaitingState] = useState<ReadState>('loading');
  const [automatic,setAutomatic] = useState<Waiting>([]), [automaticState,setAutomaticState] = useState<ReadState>('loading');
  const [accounts,setAccounts] = useState<Accounts|null>(null);
  const [notices,setNotices] = useState<Notice[]>([]), [readThrough,setReadThrough] = useState(0);
  const serial = useRef(0), prev = useRef<Incident[]|null>(null), prevWaiting = useRef<Waiting|null>(null);
  const busy = useRef({ roots:false, history:false, application:false, automatic:false });
  const add = useCallback((href: string, label: string) => {
    const notice = {href,label,key:++serial.current,at:Date.now()};
    // Root and linked Application snapshots can report the same observed transition seconds apart.
    setNotices(previous => previous.some(n => n.href===href && n.label===label && notice.at-n.at<10000)
      ? previous : [notice,...previous].slice(0,40));
  }, []);
  const read = useCallback(() => setReadThrough(serial.current), []);
  const refresh = useCallback(() => {
    if (!busy.current.history) {
      busy.current.history = true;
      request<{items:Incident[]}>('/incidents?limit=200').then(({items}) => {
        for (const event of diffAlerts(prev.current,items)) add(`/incidents/${event.id}`,alertText(event));
        const previous = new Map(prev.current?.map(i => [i.id,i]));
        for (const item of items) {
          const before = previous.get(item.id);
          if (!before) continue;
          const codeState = (i: Incident) => (i as Incident & {code_action?: {state?: string}}).code_action?.state;
          const state = codeState(item);
          if (state && state !== codeState(before) && ['qa_failed','awaiting_user_test','awaiting_prd_approval'].includes(state))
            add(`/incidents/${item.id}`, `${applicationStateKo(state)} · ${item.workload}`);
          else if (!state && before.status !== item.status && item.status === 'action_pending')
            add(`/incidents/${item.id}`, `운영 승인 대기 · ${item.workload}`);
        }
        prev.current=items;
      }).catch(() => undefined).finally(() => {busy.current.history=false;});
    }
    if (!busy.current.roots) {
      busy.current.roots = true;
      loadOpenIncidents(path => request(path)).then(items => {setOpen(items);setOpenState('ready');}).catch(() => setOpenState('error')).finally(() => {busy.current.roots=false;});
    }
    if (!busy.current.automatic) {
      busy.current.automatic = true;
      loadWaiting(path => request(path), 'automatic').then(items => {
        setAutomatic(items);setAutomaticState('ready');
      }).catch(() => setAutomaticState('error')).finally(() => {busy.current.automatic=false;});
    }
    if (!busy.current.application) {
      busy.current.application = true;
      loadWaiting(path => request(path)).then(rows => {
        if (prevWaiting.current) {
          const before = new Map(prevWaiting.current.map(row=>[row.id,row.state]));
          for (const row of rows) if (before.get(row.id) !== row.state) add(caseHref(row), `${applicationStateKo(row.state)} · ${row.service}`);
        }
        prevWaiting.current=rows; setWaiting(rows); setWaitingState('ready');
      }).catch(() => setWaitingState('error')).finally(() => {busy.current.application=false;});
    }
    request<Accounts>('/llm/accounts').then(setAccounts).catch(() => undefined);
  }, [add]);
  useEffect(() => {refresh(); const timer=window.setInterval(refresh,5000);return () => window.clearInterval(timer);}, [refresh]);
  useEffect(() => subscribeIncidents(refresh, {
    createSource: url => new EventSource(url) as unknown as SourceLike,
    setTimeout: (fn, ms) => window.setTimeout(fn, ms), clearTimeout: h => window.clearTimeout(h as number),
  }),[refresh]);
  return <C.Provider value={{open,openState,automatic,automaticState,waiting,waitingState,accounts,refresh,notices,unread:notices.filter(n=>n.key>readThrough).length,read}}>{children}</C.Provider>;
}
