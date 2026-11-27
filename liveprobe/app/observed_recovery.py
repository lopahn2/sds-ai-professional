"""Read-only symptom clearance proof. Readiness is not proof of cause or AI remediation."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from . import business_http
from .evidence import Bundle
from .k8s import parse_ts
from .route_latency import ALERT as ROUTE_LATENCY_ALERT, METRIC as ROUTE_LATENCY_METRIC
from .router import CRITICAL_SOURCES, _metric_latency_signal, extract_features, rules_decide

OOM_SIGNALS = frozenset({'OOMKilled', 'OOMKilling', 'TargetWorkloadOOMKilled'})
_OOM_RECHECK_SIGNALS = OOM_SIGNALS | {'CrashLoopBackOff', 'TargetWorkloadCrashLooping', 'TargetWorkloadNotReady'}
_OOM_TERMINATION_WINDOW_SECONDS = 120
K8S_SIGNALS = OOM_SIGNALS | {
    'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'InvalidImageName', 'TargetWorkloadImagePullFailing',
    'TargetWorkloadCrashLooping', 'TargetWorkloadNotReady', 'NoEndpoints', 'TargetServiceNoEndpoints',
}
HTTP_SIGNALS = {'High5xx', 'OrderHigh5xxRate', 'InventoryHigh5xxRate', 'TargetHigh5xxRate', 'ApplicationException'}
LATENCY_SIGNALS = {'HighLatency', 'SlowResponse', 'TargetHighLatency'}
POOL_SIGNALS = {'PoolExhausted', 'PoolHold', 'TargetConnectionPoolExhausted'}
SUPPORTED = K8S_SIGNALS | HTTP_SIGNALS | LATENCY_SIGNALS | POOL_SIGNALS | {ROUTE_LATENCY_ALERT}


def observed_at(b: Bundle) -> datetime | None:
    stamps = [i.collected_at for i in b.items if i.source in CRITICAL_SOURCES and not i.error]
    return max(stamps) if stamps else None


def ready_after_recent_oom(bundle: Bundle, signals: list[str]) -> bool:
    """Recheck a plausible recovery before classification; this is only an eligibility check."""
    kinds = set(signals)
    if not kinds.intersection(OOM_SIGNALS) or not kinds <= _OOM_RECHECK_SIGNALS:
        return False
    if (not bundle.pods or not bundle.service or not bundle.service.get('ready_endpoints')
            or any(not pod.get('containers') or not all(c.get('ready') is True for c in pod['containers'])
                   for pod in bundle.pods)):
        return False
    stamp = observed_at(bundle)
    if stamp is None:
        return False
    for container in (c for pod in bundle.pods for c in pod['containers']):
        restarts = container.get('restart_count')
        if (container.get('last_terminated_reason') != 'OOMKilled' or container.get('last_exit_code') != 137
                or type(restarts) is not int or restarts < 1):
            continue
        ended = parse_ts(container.get('last_terminated_at'))
        if (ended is not None and ended.tzinfo is not None
                and 0 < (stamp - ended).total_seconds() <= _OOM_TERMINATION_WINDOW_SECONDS):
            return True
    return False


def _containers(b):
    return {(p['name'], c['name']): c for p in b.pods for c in p.get('containers', [])}


def _stable_pool_counters(b, metric):
    if not isinstance(metric.payload, dict):
        return {}
    trace_rows = (metric.payload or {}).get('_pool_timeout_traces') or []
    if not isinstance(trace_rows, list):
        return {}
    owned = {p['name'] for p in b.pods}
    collected = metric.collected_at.timestamp()
    covered, counters = set(), {}
    for row in trace_rows:
        try:
            pod, instance, pool = (row[key] for key in ('pod', 'instance', 'pool'))
            (first_at, first), (last_at, last) = row['samples']
            key = (pod, instance, pool)
            if (pod not in owned or not all(isinstance(v, str) and v for v in key) or key in counters
                    or not all(type(v) in (float, int) and math.isfinite(v) and v >= 0
                               for v in (first_at, last_at, first, last))
                    or not collected - 60 <= first_at < last_at <= collected
                    or collected - last_at > 35 or first != last):
                return {}
        except (KeyError, TypeError, ValueError):
            return {}
        covered.add(pod)
        counters[key] = last
    return counters if covered == owned else {}


def _logs_observed(b: Bundle) -> bool:
    latest = {i.source: i for i in b.items if i.source in ('loki', 'pod_log')}
    return not latest or any(not i.error for i in latest.values())


def _restart_masked(b: Bundle) -> bool:
    from .evidence_depth import probe_restart_masking
    return bool(probe_restart_masking(b.pods, b.events))


def recovery_proof(first: Bundle, second: Bundle, signals: list[str], *, min_interval: float = 0.1,
                   required_routes: frozenset[str] = frozenset(),
                   required_http_routes: frozenset[str] = frozenset()) -> dict | None:
    if not signals or set(signals) - SUPPORTED:
        return None
    if (first.namespace, first.workload) != (second.namespace, second.workload):
        return None
    start, end = observed_at(first), observed_at(second)
    if start is None or end is None or (end - start).total_seconds() < min_interval:
        return None
    now = datetime.now(timezone.utc)
    if any(not 0 <= (now - stamp).total_seconds() <= 120 for stamp in (start, end)):
        return None
    snapshots, pool_counters = [], None
    check_routes = ROUTE_LATENCY_ALERT in signals
    check_http = bool(HTTP_SIGNALS.intersection(signals))
    http_routes = None
    route_times = {}
    if (check_routes and not required_routes) or (business_http.ALERT in signals and not required_http_routes):
        return None
    for b, successes_required in ((first, 1), (second, 2)):
        latest = {i.source: i for i in b.items}
        if any(s not in latest or latest[s].error or latest[s].payload is None for s in CRITICAL_SOURCES):
            return None
        if any(not 0 <= (now - latest[s].collected_at).total_seconds() <= 120 for s in CRITICAL_SOURCES):
            return None
        f = extract_features(b)
        if rules_decide(f)[0] == 'HIGH_RISK' or not b.deployment or not b.pods or not b.service:
            return None
        desired = b.deployment.get('replicas', 0)
        if (desired <= 0 or b.deployment.get('ready_replicas', 0) < desired
                or b.deployment.get('available_replicas', 0) < desired or b.deployment.get('unavailable_replicas', 0)
                or f['ready_pods'] != len(b.pods) or f['waiting_reasons'] or f['service_backend_state'] != 'ready'):
            return None
        own_pods = {p['name'] for p in b.pods}
        if set(b.service.get('matching_pods') or []) != own_pods or not b.service.get('selector'):
            return None
        if any(p.get('terminating') or p.get('phase') not in (None, 'Running') for p in b.pods):
            return None
        for metric_name in ('http_5xx_rate_per_s', 'http_rate_per_s', 'mean_latency_ms_1m',
                            'db_pool_active', 'db_pool_max', 'db_pool_pending_threads', 'db_pool_acquire_timeouts_5m'):
            if any(not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0
                   for v in (b.metrics.get(metric_name) or {}).values()):
                return None
        # A rolling timeout increase can outlive the active pool failure.
        metrics = b.metrics
        historical_timeout_confirmed = False
        timeouts = metrics.get('db_pool_acquire_timeouts_5m') or {}
        if set(signals) & POOL_SIGNALS and timeouts.get(b.workload, 0) > 0:
            prom = latest.get('prometheus')
            active = (metrics.get('db_pool_active') or {}).get(b.workload)
            limit = (metrics.get('db_pool_max') or {}).get(b.workload)
            pending = (metrics.get('db_pool_pending_threads') or {}).get(b.workload)
            if (prom and not prom.error and active is not None and limit is not None and 0 <= active < limit and pending == 0
                    and 0 <= (now - prom.collected_at).total_seconds() <= 120):
                stable = _stable_pool_counters(b, prom)
                if stable and (pool_counters is None or stable == pool_counters):
                    pool_counters = stable
                    historical_timeout_confirmed = True
                    metrics = {**metrics, 'db_pool_acquire_timeouts_5m': {**timeouts, b.workload: 0}}
        if _metric_latency_signal(metrics) or any(float(v) > 0 for v in (metrics.get('http_5xx_rate_per_s') or {}).values()):
            return None
        if set(signals) & (HTTP_SIGNALS | LATENCY_SIGNALS | POOL_SIGNALS):
            metric = latest.get('prometheus')
            if not metric or metric.error:
                return None
            if check_http:
                if not 0 <= (now - metric.collected_at).total_seconds() <= 45:
                    return None
                requests = (metric.payload or {}).get(business_http.METRIC) or {}
                if any(counts.get('5xx', 0) > 0 for counts in requests.values()):
                    return None
                succeeded = {route for route, counts in requests.items()
                             if counts.get('2xx', 0) > 0 and business_http.confirmed_successes(
                                 counts, metric.collected_at.timestamp()) >= successes_required}
                http_routes = succeeded if http_routes is None else http_routes & succeeded
                if not http_routes or not required_http_routes <= http_routes:
                    return None
            if set(signals) & LATENCY_SIGNALS and b.workload not in (b.metrics.get('mean_latency_ms_1m') or {}):
                return None
            if set(signals) & POOL_SIGNALS and any(b.workload not in (b.metrics.get(k) or {}) for k in
                    ('db_pool_active', 'db_pool_max', 'db_pool_pending_threads', 'db_pool_acquire_timeouts_5m')):
                return None
        if check_routes:
            prom = latest.get('prometheus')
            if not prom or prom.error or not 0 <= (now - prom.collected_at).total_seconds() <= 120:
                return None
            available = b.metrics.get(ROUTE_LATENCY_METRIC) or {}
            collected = prom.collected_at.timestamp()
            for key in required_routes:
                sample = available.get(key) or {}
                at, count = sample.get("at"), sample.get("slow_requests")
                if (type(at) not in (int, float) or not collected - 45 <= at <= collected
                        or at <= route_times.get(key, -math.inf) or type(count) not in (int, float)
                        or not 0 <= count <= 1e-9):
                    return None
                route_times[key] = at
        stamp = observed_at(b)
        assert stamp is not None
        pod_uids = {p['name']: p.get('uid') for p in b.pods}
        snapshots.append({'observed_at': stamp.isoformat(), 'namespace': b.namespace, 'workload': b.workload,
                          'ready_pods': f['ready_pods'], 'ready_endpoints': b.service['ready_endpoints'],
                          'service_uid': b.service.get('uid'), 'service_selector': b.service.get('selector'),
                          'pod_labels': {p['uid']: p.get('labels') for p in b.pods if p.get('uid')},
                          'containers': [{'pod': p, 'pod_uid': pod_uids[p], 'name': n, 'restart_count': c.get('restart_count'),
                                          'last_terminated_at': c.get('last_terminated_at')} for (p, n), c in _containers(b).items()]})
        if historical_timeout_confirmed:
            snapshots[-1]['pool_timeout_counter_stable'] = True
        if check_routes:
            snapshots[-1]['business_routes'] = {key: available[key] for key in sorted(required_routes)}
        if check_http:
            snapshots[-1]['business_http_routes'] = requests
    before, after = _containers(first), _containers(second)
    if not before or before.keys() != after.keys():
        return None
    if any(not p.get('uid') for b in (first, second) for p in b.pods):
        return None
    if {p['name']: p['uid'] for p in first.pods} != {p['name']: p['uid'] for p in second.pods}:
        return None
    for key, c in after.items():
        old = before[key]
        if c.get('restart_count') is None or old.get('restart_count') is None:
            return None
        if c['restart_count'] != old['restart_count'] or c.get('last_terminated_at') != old.get('last_terminated_at'):
            return None
        term = parse_ts(c.get('last_terminated_at'))
        if term and term >= start:
            return None
    # Only newly timestamped events/errors are current. Unstamped errors are unknown, not cleared.
    for event in second.events:
        if event.get('type') == 'Warning' or event.get('reason') == 'Killing':
            stamp = parse_ts(event.get('last'))
            if stamp is None or stamp >= start:
                return None
    # Unknown is not clear: a failed log read cannot show that the errors stopped.
    if not _logs_observed(second):
        return None
    # A clean window right after a kubelet restart is the restart itself, not recovery: a leak or a
    # saturated limit returns once the fresh container fills up again.
    if set(signals) & (HTTP_SIGNALS | LATENCY_SIGNALS | POOL_SIGNALS) and _restart_masked(second):
        return None
    for line in second.logs:
        if any(word in line.lower() for word in ('error', 'exception', 'traceback', 'failed', 'timeout', 'refused')):
            # A timestamped historical line can be excluded; unstamped errors remain unproven.
            stamp = next((parse_ts(token) for token in line.split()[:3] if parse_ts(token)), None)
            if stamp is None or stamp >= start:
                return None
    if second.code_refs.get('exceptions'):
        return None
    if 'ApplicationException' in signals:
        sources = {i.source: i for i in second.items}
        if any(s not in sources or sources[s].error or sources[s].payload is None for s in ('loki', 'code_refs')):
            return None

    return {'cause_confirmed': False, 'ai_remediation': False, 'signals_checked': sorted(set(signals)),
            'observations': snapshots}
