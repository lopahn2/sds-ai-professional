import type { ActiveIncident } from './api.ts';
import type { AlertIncident } from './feed.ts';

export const OPEN_STATES = ['open', 'analyzing', 'action_pending', 'acting', 'recovery_review'] as const;
type Page = { items: ActiveIncident[]; total: number; as_of?: number };
type LivePage = Page & { alerts?: AlertIncident[] };
export function unsupportedView(error: unknown) {
  return error instanceof Error && 'status' in error && (error.status === 422 || error.status === 404);
}
/** 최근 기록의 개수와 관계없이 진행 중 장애를 조회한다. 부분 결과는 정상 목록으로 내보내지 않는다. */
export async function loadOpenIncidents(load: (path: string) => Promise<Page>): Promise<ActiveIncident[]> {
  const loadPages = async (filter: string) => {
    const items: ActiveIncident[] = [];
    let anchor: number | undefined;
    for (let page = 0; page < 100; page++) {
      const asOf = anchor === undefined ? '' : `&as_of=${anchor}`;
      const d = await load(`/incidents?${filter}&limit=500&offset=${items.length}${asOf}`);
      if (!Array.isArray(d.items) || !Number.isInteger(d.total) || d.total < 0) throw new Error('장애 목록 응답 형식 오류');
      if (!d.items.length && items.length < d.total) throw new Error('진행 중 장애 목록이 일부만 조회되었습니다.');
      if (anchor === undefined && typeof d.as_of === 'number' && Number.isSafeInteger(d.as_of) && d.as_of >= 0) anchor = d.as_of;
      items.push(...d.items);
      if (items.length >= d.total) return items;
    }
    throw new Error('진행 중 장애 목록 조회 한도를 넘었습니다.');
  };
  let items: ActiveIncident[];
  try {
    items = await loadPages('view=active&compact=true');
  } catch (error) {
    if (!(error instanceof Error) || !('status' in error) || error.status !== 422) throw error;
    items = (await Promise.all(OPEN_STATES.map(status => loadPages(`status=${status}`)))).flat();
  }
  return [...new Map(items.map(i => [i.id, i])).values()].filter(i => (OPEN_STATES as readonly string[]).includes(i.status)).sort((a, b) => b.id - a.id);
}

export async function loadRootSnapshot(load: (path: string) => Promise<LivePage>, onAlerts: (alerts: AlertIncident[]) => void,
    loadLegacyAlerts: () => void) {
  let snapshot: LivePage | undefined;
  try {
    snapshot = await load('/incidents?view=live&limit=500&offset=0');
  } catch (error) {
    if (!unsupportedView(error)) throw error;
  }
  if (!Array.isArray(snapshot?.alerts)) {
    loadLegacyAlerts();
    return loadOpenIncidents(load);
  }
  onAlerts(snapshot.alerts);
  return loadOpenIncidents(path => path === '/incidents?view=active&compact=true&limit=500&offset=0'
    ? Promise.resolve(snapshot) : load(path));
}
