import type { Incident, OpenIncident, Overview } from './api.ts';
import { nextStepShort } from './responseQueue.ts';
import { summaryKo } from './presentation.ts';
import { applicationStateKo, type ApplicationHistory, type ApplicationIncident } from './unified.ts';

export type Env = 'dev' | 'prd';
export type RootIncident = (OpenIncident | Incident) & { claimed?: boolean; title_ko?: string | null; handoff?: Record<string, unknown> | null };
type SourceRow = Pick<ApplicationIncident, 'id' | 'state' | 'system' | 'service' | 'environment' | 'root_incident_id' | 'reason'>;
export type AtlasCase = { key: string; href: string; system: string; service: string; environment: string; state: string; title: string; label: string; intervention: boolean; approval: boolean };
export type Reading = { metricAt?:number; flowAt?:number; ready?:number|null; desired?:number|null; endpoints?:number|null; flowObserved?:boolean; states: Set<string>; resource: string | null; cases: AtlasCase[]; requestRate: number | null; errorRate: number | null; observedResponses: boolean };
export type AtlasService = { key: string; name: string; system: string; env: Record<Env, Reading> };
export const envs = ['dev', 'prd'] as const;
const active = (s: string): s is Env => s === 'dev' || s === 'prd';
const keyOf = (system: string, service: string) => JSON.stringify([system, service]);
const hazard = new Set(['down', 'degraded', 'no_endpoints', 'warning', 'blocked']);
export function readingStatus(r: Reading): 'risk' | 'healthy' | 'unknown' {
  if (r.cases.length || (r.errorRate ?? 0) > 0 || [...r.states].some(s => hazard.has(s))) return 'risk';
  if (r.states.has('ready')) return 'healthy';
  return 'unknown';
}
function percent(rate: number) {
  return new Intl.NumberFormat('ko-KR',{style:'percent',maximumFractionDigits:2}).format(rate);
}
export function observationLabel(r: Reading) {
  if (r.errorRate != null && r.errorRate > 0) return 'HTTP 5xx ' + (r.errorRate < .0001 ? '0.01% 미만' : percent(r.errorRate));
  if (r.states.has('no_endpoints')) return '연결 대상 없음';
  if (r.states.has('down')) return '중단';
  if (r.states.has('degraded')) return '일부 비정상';
  if (r.states.has('warning')) return '통신 경고';
  if (r.states.has('scaled_down')) return '목표 Pod 0개';
  if (r.states.has('manual')) return '수동 연결';
  if (r.states.has('external_name')) return '외부 DNS';
  // An open incident does not change what was observed: ready Pods stay '준비'.
  return r.states.has('ready') ? '준비' : '미관측';
}
export function trafficLabel(r: Reading, source?: string | null) {
  if (r.errorRate != null && r.errorRate > 0) return observationLabel(r);
  if (r.requestRate === 0 && !r.observedResponses) return '조회 구간 요청 없음';
  if ((r.requestRate ?? 0) > 0 && r.errorRate === 0) return '조회 구간 5xx 없음';
  return source && !['ok','connected'].includes(source) ? 'HTTP 지표 수집 확인 필요' : 'HTTP 응답 미관측';
}
/** naria status tone of one environment reading. Incidents, HTTP 5xx, outages and blocked flows are faults;
 * partial readiness, missing endpoints and network warnings ask for attention. Unobserved never reads as healthy. */
export type ReadingTone = 'danger' | 'warning' | 'ok' | 'unknown';
export function readingTone(r: Reading): ReadingTone {
  if (r.cases.length || (r.errorRate ?? 0) > 0 || r.states.has('down') || r.states.has('blocked')) return 'danger';
  const status = readingStatus(r);
  return status === 'risk' ? 'warning' : status === 'healthy' ? 'ok' : 'unknown';
}
/** What the map shows for one environment: the incident count first, otherwise the observation. */
export const readingLabel = (r: Reading) => r.cases.length ? `장애 ${r.cases.length}건` : observationLabel(r);
export type AtlasLink = { from: string; to: string; observed: boolean };
export type AtlasLane = { system: string; services: AtlasService[]; danger: number; warning: number; unknown: number; unknownEnv: Env | null };
/** Lanes for the service map. Systems joined by a connection sit next to each other with the most-called system
 * between its callers, and connected services come first in their lane so lines stay short. The order depends only
 * on names and connections, never on health, so places do not move while incidents come and go. */
export function atlasLanes(groups: [string, AtlasService[]][], links: AtlasLink[]): AtlasLane[] {
  const systemOf = new Map(groups.flatMap(([system, services]) => services.map(s => [s.key, system] as const)));
  const linked = new Set<string>(), joined = new Set<string>(), inbound = new Map<string, number>();
  for (const l of links) {
    linked.add(l.from); linked.add(l.to);
    const from = systemOf.get(l.from), to = systemOf.get(l.to);
    if (from === undefined || to === undefined || from === to) continue;
    joined.add(from); joined.add(to); inbound.set(to, (inbound.get(to) ?? 0) + 1);
  }
  let near = groups.filter(([system]) => joined.has(system));
  const rest = groups.filter(([system]) => !joined.has(system));
  if (near.length > 2) {
    const hub = near.reduce((a, b) => (inbound.get(b[0]) ?? 0) > (inbound.get(a[0]) ?? 0) ? b : a);
    const others = near.filter(g => g !== hub), middle = Math.floor(others.length / 2);
    near = [...others.slice(0, middle), hub, ...others.slice(middle)];
  }
  return [...near, ...rest].map(([system, items]) => {
    const services = [...items.filter(s => linked.has(s.key)), ...items.filter(s => !linked.has(s.key))];
    const tones = services.map(s => envs.map(e => readingTone(s.env[e])));
    const unknownEnvs = new Set(services.flatMap(s => envs.filter(e => readingTone(s.env[e]) === 'unknown')));
    return { system, services,
      danger: tones.filter(t => t.includes('danger')).length,
      warning: tones.filter(t => !t.includes('danger') && t.includes('warning')).length,
      unknown: tones.flat().filter(t => t === 'unknown').length,
      unknownEnv: unknownEnvs.size === 1 ? [...unknownEnvs][0] : null };
  });
}
export function laneSummary(l: AtlasLane) {
  const parts = [`서비스 ${l.services.length}`];
  if (l.danger) parts.push(`장애·오류 ${l.danger}`);
  if (l.warning) parts.push(`주의 ${l.warning}`);
  if (l.unknown) parts.push(`${l.unknownEnv ? l.unknownEnv + ' ' : ''}미관측 ${l.unknown}`);
  if (parts.length === 1) parts.push('모두 준비');
  return parts.join(' · ');
}
/** One line naming a service's connections, e.g. "→ business-api · 선언" or "← 2개 서비스 · 관측". */
export function linkSummary(service: AtlasService, links: AtlasLink[], names: Map<string, string>) {
  const out = links.filter(l => l.from === service.key), into = links.filter(l => l.to === service.key);
  if (!out.length && !into.length) return '';
  const parts: string[] = [];
  if (out.length) parts.push(out.length === 1 ? `→ ${names.get(out[0].to) ?? '서비스'}` : `→ ${out.length}개 서비스`);
  if (into.length) parts.push(into.length === 1 ? `← ${names.get(into[0].from) ?? '서비스'}` : `← ${into.length}개 서비스`);
  const all = [...out, ...into], observed = all.filter(l => l.observed).length;
  parts.push(observed === all.length ? '관측' : observed ? '관측·선언' : '선언');
  return parts.join(' · ');
}
/** Open incidents across both environments, e.g. "장애 4건". The chips carry the per-environment counts. */
export const caseSummary = (service: AtlasService) => {
  const total = envs.reduce((n, e) => n + service.env[e].cases.length, 0);
  return total ? `장애 ${total}건` : '';
};
/** The chip's short wording: "장애 3" keeps two fault chips readable on a laptop-width node; the full count stays in the name. */
export const chipLabel = (r: Reading) => r.cases.length ? `장애 ${r.cases.length}` : observationLabel(r);
export type Box = { left: number; top: number; right: number; bottom: number };
type Point = [number, number];
const crosses = ([x1, y1]: Point, [x2, y2]: Point, o: Box, m = 2) =>
  Math.max(x1, x2) > o.left - m && Math.min(x1, x2) < o.right + m && Math.max(y1, y2) > o.top - m && Math.min(y1, y2) < o.bottom + m;
const format = (points: Point[]) => points.map(([x, y], i) => i === 0 ? `M${x},${y}` : points[i - 1][0] === x ? ` V${y}` : ` H${x}`).join('');
/** Orthogonal line between two service nodes that never runs through another node. It is straight when the nodes share
 * a column or a row and nothing sits between them; otherwise it leaves through the row gap and runs along the column
 * gutter next to the target, then along a side gutter, and as a last resort around everything that lies between. */
export function wirePath(a: Box, b: Box, obstacles: Box[] = [], gap = 16, rowGap = 8) {
  const half = gap / 2, mid = (x: number, y: number) => Math.round((x + y) / 2);
  const same = (o: Box) => (o.left === a.left && o.top === a.top) || (o.left === b.left && o.top === b.top);
  const others = obstacles.filter(o => !same(o));
  const clear = (points: Point[]) => points.slice(1).every((p, i) => !others.some(o => crosses(points[i], p, o)));
  const ay = mid(a.top, a.bottom), by = mid(b.top, b.bottom), routes: Point[][] = [];
  if (Math.min(a.right, b.right) - Math.max(a.left, b.left) >= 24) {
    const x = mid(Math.max(a.left, b.left), Math.min(a.right, b.right));
    routes.push(a.bottom <= b.top ? [[x, a.bottom], [x, b.top]] : [[x, a.top], [x, b.bottom]]);
  } else if (Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) >= 24) {
    const y = mid(Math.max(a.top, b.top), Math.min(a.bottom, b.bottom));
    routes.push(a.right <= b.left ? [[a.right, y], [b.left, y]] : [[a.left, y], [b.right, y]]);
  } else {
    const down = a.bottom <= b.top, right = b.left >= a.right, x = mid(a.left, a.right);
    const lane = down ? a.bottom + rowGap / 2 : a.top - rowGap / 2, gutter = right ? b.left - half : b.right + half;
    routes.push([[x, down ? a.bottom : a.top], [x, lane], [gutter, lane], [gutter, by], [right ? b.left : b.right, by]]);
  }
  const rightSide = Math.max(a.right, b.right) + half, leftSide = Math.min(a.left, b.left) - half;
  routes.push([[a.right, ay], [rightSide, ay], [rightSide, by], [b.right, by]]);
  routes.push([[a.left, ay], [leftSide, ay], [leftSide, by], [b.left, by]]);
  const between = others.filter(o => o.bottom > Math.min(ay, by) && o.top < Math.max(ay, by));
  const outside = Math.max(a.right, b.right, ...between.map(o => o.right)) + half;
  routes.push([[a.right, ay], [outside, ay], [outside, by], [b.right, by]]);
  return format(routes.find(clear) ?? routes[routes.length - 1]);
}
const isApproval = (state: string) => ['awaiting_prd_approval', 'awaiting_user_test'].includes(state);
export const caseHref = (a: SourceRow) => a.root_incident_id ? `/incidents/${a.root_incident_id}` : `/incidents/application/${encodeURIComponent(a.id)}`;
export function mergeCases(roots: RootIncident[], apps: SourceRow[]): AtlasCase[] {
  const rows = new Map<string, AtlasCase>();
  for (const i of roots) {
    const claim = i.claimed ?? Boolean(i.handoff?.claimed_by);
    const code = (i as RootIncident & { code_action?: { linked?: boolean; state?: string; state_ko?: string } }).code_action;
    const codeState = code?.state;
    const approval = codeState ? isApproval(codeState) : i.status === 'action_pending';
    const intervention = approval || i.status === 'recovery_review' || codeState === 'recovery_review' || codeState === 'qa_failed' || codeState === 'awaiting_merge' || (i.status === 'handed_off' && !claim);
    const key = String(i.id);
    rows.set(key, { key, href: `/incidents/${i.id}`, system: i.system || '', service: i.workload, environment: i.environment || i.namespace, state: codeState || i.status,
      title: summaryKo(i.title_ko || i.title, '장애 신호 확인'), label: codeState ? applicationStateKo(codeState) : nextStepShort(i.status, claim).label, intervention, approval });
  }
  for (const i of apps) {
    const key = i.root_incident_id ? String(i.root_incident_id) : 'app:' + i.id;
    const approval = isApproval(i.state);
    rows.set(key, { key, href: caseHref(i), system: i.system || '', service: i.service, environment: i.environment, state: i.state,
      title: summaryKo(i.reason, rows.get(key)?.title || '애플리케이션 변경 확인'), label: applicationStateKo(i.state), intervention: approval || ['qa_failed','awaiting_merge','recovery_review','handoff'].includes(i.state), approval });
  }
  return [...rows.values()].sort((a,b) => Number(b.approval) - Number(a.approval) || Number(b.intervention) - Number(a.intervention) || a.service.localeCompare(b.service) || a.key.localeCompare(b.key, 'en', {numeric:true}));
}
export function mergeCaseSources(open: RootIncident[], openState: string, overview: RootIncident[] | undefined, waiting: SourceRow[]) {
  // The overview query retains handoffs for one hour. Lifetime unclaimed totals are separate.
  const roots = new Map((overview || []).filter(i => openState !== 'ready' || i.status === 'handed_off').map(i => [i.id,i]));
  for (const i of open) roots.set(i.id,i);
  return mergeCases([...roots.values()],waiting);
}
export async function loadWaiting(load: (path: string) => Promise<ApplicationHistory>, group: 'waiting_people' | 'automatic' = 'waiting_people'): Promise<SourceRow[]> {
  const rows = new Map<string, SourceRow>();
  let anchor: number | undefined;
  for (let page = 0; page < 100; page++) {
    const params = new URLSearchParams({ group, limit: '200', offset: String(rows.size) });
    if (anchor !== undefined) params.set('as_of', String(anchor));
    const d = await load('/unified/summaries?' + params);
    if (!Array.isArray(d.incidents) || !Number.isInteger(d.total) || d.total < 0) throw new Error('대기 목록 응답 오류');
    const before = rows.size;
    for (const row of d.incidents) if (row.id && row.service && row.state) rows.set(row.id, row);
    if (before + d.incidents.length !== rows.size) throw new Error('대기 목록 중복 또는 누락');
    if (rows.size >= d.total) return [...rows.values()];
    if (!d.incidents.length || !Number.isFinite(d.as_of)) break;
    anchor ??= d.as_of;
  }
  throw new Error('대기 목록 일부만 조회됨');
}
export function serviceAtlas(o: Overview | null, cases: AtlasCase[]) {
  const services = new Map<string, AtlasService>();
  const ids = new Map<string, string>();
  const readings = new Map<string, Reading>();
  const groups = new Map<string, AtlasService[]>();
  function get(system: string, name: string) {
    const key = keyOf(system, name);
    let item = services.get(key);
    if (!item) {
      const empty = (): Reading => ({ states: new Set(), resource: null, cases: [], requestRate: null, errorRate: null, observedResponses:false });
      item = { key, name, system, env: {dev: empty(), prd: empty()} };
      services.set(key, item);
      if (!groups.has(system)) groups.set(system, []);
      groups.get(system)!.push(item);
    }
    return item;
  }
  const nodes = o?.topology?.nodes || [];
  for (const w of o?.workloads || []) {
    const env = w.environment || w.namespace;
    if (!active(env) || !w.name || ['Job','CronJob','Pod'].includes(w.kind)) continue;
    const s = get(w.system || '', w.name), e = s.env[env];
    ids.set(w.id, s.key); readings.set(w.id,e);
    if (typeof w.req_rate === 'number' && (e.requestRate===null || w.req_rate>=e.requestRate)) {
      e.requestRate=w.req_rate; e.metricAt=w.metric_at??undefined;
    }
    if (typeof w.err_rate === 'number') e.errorRate = Math.max(e.errorRate ?? 0,w.err_rate);
    if (w.desired === 0 && w.ready === 0) e.states.add('scaled_down');
    if (!e.resource) { e.ready = w.ready; e.desired = w.desired; }
    e.resource ??= w.id;
    if (w.ready != null && w.desired != null && w.desired > 0) e.states.add(w.ready >= w.desired ? 'ready' : 'degraded');
  }
  for (const n of nodes) {
    const env = n.environment || n.namespace || '';
    if (!active(env) || n.lifecycle === 'retired' || !n.name || !(n.role === 'service' || n.kind === 'Service')) continue;
    const s = get(n.system || '', n.name), e = s.env[env];
    ids.set(n.id, s.key); readings.set(n.id,e);
    if (n.kind === 'Service' && typeof n.endpoints_ready === 'number') e.endpoints = n.endpoints_ready;
    if (e.endpoints == null) e.endpoints = n.services?.find(s => s.name === n.name)?.endpoints_ready;
    e.resource ??= n.id;
    e.states.add(n.status);
    if (n.network_warnings?.length || n.no_endpoints?.length) e.states.add('warning');
  }
  for (const c of cases) {
    if (active(c.environment) && c.service) get(c.system, c.service).env[c.environment].cases.push(c);
  }
  const links = new Map<string, { from: string; to: string; observed: boolean }>();
  function connect(f: string, t: string, observed: boolean) {
    const from = ids.get(f), to = ids.get(t);
    if (!from || !to || from === to) return;
    const key = JSON.stringify([from,to]);
    const prev = links.get(key);
    if (!prev || observed) links.set(key, {from,to,observed});
  }
  for (const e of o?.topology?.contract_edges || []) connect(e.from, e.to, false);
  for (const e of o?.topology?.observed_edges || []) {
    connect(e.from, e.to, true);
    // The destination returned these HTTP responses. A failing dependency does not prove its caller failed.
    const target = readings.get(e.to);
    if (e.last_seen != null) {
      const source = readings.get(e.from);
      for (const endpoint of [source,target]) if (endpoint) {
        endpoint.flowObserved=true;
        endpoint.flowAt=Math.max(e.last_seen,endpoint.flowAt??0);
      }
    }
    if (!target) continue;
    if (typeof e.err_rate === 'number') target.errorRate = Math.max(target.errorRate ?? 0, e.err_rate);
    if (typeof e.req_rate === 'number') {
      if (target.requestRate===null || e.req_rate>=target.requestRate) {
        target.requestRate=e.req_rate; target.metricAt=e.metric_at??undefined;
      }
      if (e.req_rate > 0) target.observedResponses = true;
    }
  }
  for (const items of groups.values()) items.sort((a,b) => a.name.localeCompare(b.name));
  return { groups: [...groups].sort(([a],[b]) => (a || '\uffff').localeCompare(b || '\uffff')), links: [...links.values()], count: services.size };
}
