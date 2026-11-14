/** 정돈-04: request strings for history lists. Every filter is applied by the server to the whole history before
 *  counting and paging. Later pages are pinned with `as_of` (the newest id when page 1 was read). */
export const detailFilters = ['start', 'end', 'system', 'service'] as const;
export type DetailFilters = Partial<Record<typeof detailFilters[number], string>>;
export const INCIDENT_PAGE = 50;
export function incidentListPath(p: DetailFilters & { offset: number; status?: string; search?: string; environment?: string; origin?: string; asOf?: number | null }) {
  const u = new URLSearchParams({ limit: String(INCIDENT_PAGE), offset: String(p.offset) });
  for (const key of detailFilters) if (p[key]) u.set(key, p[key]!);
  if (p.status) u.set('status', p.status); if (p.search) u.set('search', p.search);
  if (p.environment) u.set('environment', p.environment); if (p.origin) u.set('origin', p.origin);
  if (p.offset > 0 && p.asOf != null) u.set('as_of', String(p.asOf));
  return `/incidents?${u.toString()}`;
}
/** The review ledger page, its total, the reason breakdown and the CSV export all use the same filter string. */
export function ledgerQuery(p: { reason?: string; environment?: string; offset?: number; limit?: number }) {
  const u = new URLSearchParams();
  if (p.limit != null) u.set('limit', String(p.limit)); if (p.offset) u.set('offset', String(p.offset));
  if (p.reason) u.set('reason', p.reason); if (p.environment) u.set('environment', p.environment);
  const q = u.toString(); return q ? `?${q}` : '';
}
