"""Read-only symptom clearance proof. Readiness is not proof of cause or AI remediation."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from .evidence import Bundle
from .k8s import parse_ts
from .router import CRITICAL_SOURCES, _metric_latency_signal, extract_features, rules_decide

K8S_SIGNALS = {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'InvalidImageName',
               'TargetWorkloadImagePullFailing', 'NoEndpoints', 'TargetServiceNoEndpoints',
               'OOMKilled', 'OOMKilling', 'TargetWorkloadOOMKilled'}
HTTP_SIGNALS = {'High5xx', 'OrderHigh5xxRate', 'InventoryHigh5xxRate', 'TargetHigh5xxRate', 'ApplicationException'}
LATENCY_SIGNALS = {'HighLatency', 'SlowResponse', 'TargetHighLatency'}
POOL_SIGNALS = {'PoolExhausted', 'PoolHold'}
SUPPORTED = K8S_SIGNALS | HTTP_SIGNALS | LATENCY_SIGNALS | POOL_SIGNALS


def observed_at(b: Bundle) -> datetime | None:
    stamps = [i.collected_at for i in b.items if i.source in CRITICAL_SOURCES and not i.error]
    return max(stamps) if stamps else None


def _containers(b):
    return {(p['name'], c['name']): c for p in b.pods for c in p.get('containers', [])}


def recovery_proof(first: Bundle, second: Bundle, signals: list[str], *, min_interval: float = 0.1) -> dict | None:
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
    snapshots = []
    for b in (first, second):
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
                or f['ready_pods'] != len(b.pods) or f['waiting_reasons'] or f['service_has_no_endpoints']):
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
        # Current metrics, not a cumulative restart total or historical log text.
        if _metric_latency_signal(b.metrics) or any(float(v) > 0 for v in (b.metrics.get('http_5xx_rate_per_s') or {}).values()):
            return None
        if set(signals) & (HTTP_SIGNALS | LATENCY_SIGNALS | POOL_SIGNALS):
            metric = latest.get('prometheus')
            if not metric or metric.error:
                return None
            if (set(signals) & HTTP_SIGNALS and
                    (b.workload not in (b.metrics.get('http_5xx_rate_per_s') or {})
                     or (b.metrics.get('http_rate_per_s') or {}).get(b.workload, 0) <= 0)):
                return None
            if set(signals) & LATENCY_SIGNALS and b.workload not in (b.metrics.get('mean_latency_ms_1m') or {}):
                return None
            if set(signals) & POOL_SIGNALS and any(b.workload not in (b.metrics.get(k) or {}) for k in
                    ('db_pool_active', 'db_pool_max', 'db_pool_pending_threads', 'db_pool_acquire_timeouts_5m')):
                return None
        stamp = observed_at(b)
        assert stamp is not None
        snapshots.append({'observed_at': stamp.isoformat(), 'namespace': b.namespace, 'workload': b.workload,
                          'ready_pods': f['ready_pods'], 'ready_endpoints': b.service['ready_endpoints'],
                          'containers': [{'pod': p, 'name': n, 'restart_count': c.get('restart_count'),
                                          'last_terminated_at': c.get('last_terminated_at')} for (p, n), c in _containers(b).items()]})
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
