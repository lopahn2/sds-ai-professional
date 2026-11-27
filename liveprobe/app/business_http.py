"""Read-only recovery evidence from recently scraped business HTTP counters."""
from __future__ import annotations

import json
import math

from .route_latency import MAX_ROUTES, METHODS, URI_PATTERN, route_key

ALERT = "TargetHigh5xxRate"
METRIC = "business_http_routes_1m"
MAX_SERIES = MAX_ROUTES * 8
MAX_SCRAPE_AGE = 45


def sample_query(namespace: str, workload: str) -> str:
    method_pattern = '|'.join(sorted(METHODS))
    return (
        'http_server_requests_seconds_count{'
        f'namespace={json.dumps(namespace)},app={json.dumps(workload)},'
        f'method=~{json.dumps(method_pattern)},uri=~{json.dumps(URI_PATTERN)},'
        'status=~"[25][0-9][0-9]"}[1m]'
    )


def observed_samples(rows: list[dict], now: float) -> dict:
    if len(rows) > MAX_SERIES:
        return {}
    result, windows, seen = {}, {}, set()
    first_positive_error_sample = -math.inf
    for row in rows:
        try:
            labels = row['metric']
            key = route_key(labels.get('method'), labels.get('uri'))
            status = labels['status']
            identity = frozenset(labels.items())
            if (key is None or len(status) != 3 or status[0] not in '25' or not status.isdecimal()
                    or identity in seen or not 1 <= len(row['values']) <= 60):
                return {}
            seen.add(identity)
            samples = [(float(at), float(count)) for at, count in row['values']]
            if any(not now - 75 <= at <= now or not math.isfinite(count) or count < 0 for at, count in samples):
                return {}
            outcome = status[0] + 'xx'
            counts = result.setdefault(key, {})
            counts.setdefault(outcome, 0.0)
            if outcome == '5xx' and samples[0][1] > 0:
                if len(samples) == 1:
                    # The first observed value alone cannot establish that errors stopped.
                    return {}
                first_positive_error_sample = max(first_positive_error_sample, samples[0][0])
            for (start, before), (end, after) in zip(samples, samples[1:]):
                if end <= start or after < before:
                    return {}
                if after > before:
                    counts[outcome] += after - before
                    if outcome == '2xx' and now - MAX_SCRAPE_AGE <= start:
                        windows.setdefault(key, []).append((start, end))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return {}
    if len(result) > MAX_ROUTES:
        return {}
    for key, intervals in windows.items():
        # A new error series can already be positive at its first scrape.
        intervals = [window for window in intervals if window[0] >= first_positive_error_sample]
        if not intervals:
            continue
        # The earliest ending interval leaves the widest choice of disjoint later intervals.
        first = min(intervals, key=lambda window: window[1])
        last = max((window for window in intervals if window[0] >= first[1]),
                   key=lambda window: window[1], default=None)
        result[key]['success_scrape_intervals'] = [first, last] if last else [first]
    return result


def confirmed_successes(counts: dict, collected_at: float) -> int:
    intervals = counts.get('success_scrape_intervals') or []
    if not isinstance(intervals, list) or not 1 <= len(intervals) <= 2:
        return 0
    previous = collected_at - MAX_SCRAPE_AGE
    for interval in intervals:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            return 0
        start, end = interval
        if type(start) not in (float, int) or type(end) not in (float, int) or not previous <= start < end <= collected_at:
            return 0
        previous = end
    return len(intervals)
