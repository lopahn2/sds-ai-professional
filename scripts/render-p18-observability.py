#!/usr/bin/env python3
"""Render Prometheus rules for P18 without replacing deployments or changing credential references."""
import argparse
import copy
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
# 2026-11-13: 활성 환경 dev/prd만 감시·경보한다. 퇴역한 stg의 과거 지표·경보 이력은 Prometheus 보존 기간 안에서
# 그대로 조회되며 이 선언은 새 기록·경보 대상에서만 stg를 뺀다.
NS = 'dev|prd'
APPS = 'inventory|order|catalog|member|payment|notification|delivery'
SLOW_ROUTES = 'liveprobe:http_business_route_slow_requests:increase1m'


def render():
    base = next(x for x in yaml.safe_load_all((ROOT/'deploy/base/observability/prometheus/prometheus.yaml').read_text())
                if x and x['kind'] == 'ConfigMap' and x['metadata']['name'] == 'prometheus-config')
    result = copy.deepcopy(base)
    # Retain the historical recording queries while collecting the active environments as well.
    result['data']['recording.yml'] = result['data']['recording.yml'].replace(
        'namespace="liveprobe-target"', f'namespace=~"liveprobe-target|{NS}"').replace(
        'app=~"order|inventory"', f'app=~"{APPS}"')
    labels = 'namespace, app, method, uri'
    routes = (f'namespace=~"{NS}", app=~"{APPS}", method=~"GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS",'
              ' uri=~"/api/[A-Za-z0-9_./{}-]{1,145}"')
    buckets = 'http_server_requests_seconds_bucket'
    total = f'sum by ({labels}) (increase({buckets}{{{routes}, le="+Inf"}}[1m]))'
    fast = f'sum by ({labels}) (increase({buckets}{{{routes}, le="0.5"}}[1m]))'
    # Both buckets come from the same instrumentation. Missing buckets or idle routes stay unobserved.
    result['data']['recording.yml'] += (
        f'  - name: p18-business-route-latency\n    interval: 15s\n    rules:\n      - record: {SLOW_ROUTES}\n'
        f'        expr: |\n          clamp_min({total} - {fast}, 0)\n          and on ({labels}) ({total} >= 3)\n'
    )
    alerts = yaml.safe_load(result['data']['alerts.yml'])
    no_endpoints_hold = next(rule['for'] for group in alerts['groups'] for rule in group['rules']
                             if rule.get('alert') == 'TargetServiceNoEndpoints')
    rules = []
    def add(name, query, hold, severity, summary):
        rules.append({'alert': name, 'expr': query, 'for': hold, 'labels': {'severity': severity, 'liveprobe_scope': 'p18'},
                      'annotations': {'summary': summary}})
    counter = 'http_server_requests_seconds_count'
    all_errors = f'{counter}{{namespace=~"{NS}", app=~"{APPS}", status=~"5..", uri=~"/api/.*"}}'
    service_errors = f'sum by (namespace, app) (rate({all_errors}[1m]))'
    route_errors = f'sum by ({labels}) (rate({counter}{{{routes}, status=~"5.."}}[1m]))'
    # Preserve the service threshold while retaining the workload's observed failing routes.
    add('TargetHigh5xxRate', f'(({route_errors} > 0) or on (namespace, app) {service_errors}) '
        f'and on (namespace, app) ({service_errors} > 0.04)',
        '15s', 'critical', '{{ $labels.app }} business requests are returning 5xx')
    add('TargetHighLatency', f'liveprobe:http_server_requests_latency_seconds:avg_rate1m{{namespace=~"{NS}", app=~"{APPS}"}} > 0.5',
        '0s', 'warning', '{{ $labels.app }} mean response time exceeded 500ms')
    add('TargetRouteSlowRequests', f'{SLOW_ROUTES}{{namespace=~"{NS}"}} >= 3',
        '0s', 'warning', '{{ $labels.app }} {{ $labels.method }} {{ $labels.uri }} has repeated requests above 500ms')
    envoy = f'job="envoy-stats",namespace=~"{NS}",pod_uid=~".+"'
    rejections = f'envoy_listener_manager_lds_update_rejected{{{envoy}}}'
    add('TargetProxyListenerUpdateRejected', 'max by (namespace,pod,pod_uid) ('
        f'increase({rejections}[2m]) > 0 and {rejections}'
        f' and on (namespace,pod,pod_uid,instance) (up{{{envoy}}} == 1))',
        '0s', 'warning', '{{ $labels.pod }} rejected an Envoy listener update. Route impact is unobserved.')
    add('TargetConnectionPoolExhausted', f'liveprobe:hikaricp_connections:utilization{{namespace=~"{NS}"}} >= 0.95',
        '0s', 'critical', '{{ $labels.app }} connection pool is exhausted')
    add('TargetWorkloadCrashLooping', f'max by (namespace,pod,container) (kube_pod_container_status_waiting_reason{{namespace=~"{NS}",reason="CrashLoopBackOff"}}) > 0',
        '15s', 'critical', '{{ $labels.container }} is crash-looping')
    oom_reason = f'kube_pod_container_status_last_terminated_reason{{namespace=~"{NS}",reason="OOMKilled"}}'
    termination_time = f'kube_pod_container_status_last_terminated_timestamp{{namespace=~"{NS}"}}'
    # Kubernetes retains lastState after recovery. Match the actual recent exit by Pod UID.
    add('TargetWorkloadOOMKilled', f'max by (namespace,pod,container) (({oom_reason} > 0)'
        f' and on (namespace,pod,uid,container) (time() - {termination_time} >= 0'
        f' and time() - {termination_time} < 120))',
        '0s', 'critical', '{{ $labels.container }} was OOMKilled')
    add('TargetWorkloadImagePullFailing', f'max by (namespace,pod,container,reason) (kube_pod_container_status_waiting_reason{{namespace=~"{NS}",reason=~"ImagePullBackOff|ErrImagePull"}}) > 0',
        '0s', 'warning', '{{ $labels.container }} cannot pull its image')
    add('TargetServiceNoEndpoints', f'count by (namespace,endpoint) (kube_endpoint_info{{namespace=~"{NS}"}}) unless count by (namespace,endpoint) (kube_endpoint_address{{namespace=~"{NS}",ready="true"}})',
        no_endpoints_hold, 'critical', '{{ $labels.endpoint }} has no ready endpoints')
    alerts['groups'].append({'name': 'p18-environments', 'rules': rules})
    result['data']['alerts.yml'] = yaml.safe_dump(alerts, sort_keys=False)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--extract', type=Path)
    args = p.parse_args()
    cm = render()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cm, indent=2)+'\n')
    if args.extract:
        args.extract.mkdir(parents=True, exist_ok=True)
        for name, content in cm['data'].items():
            (args.extract/name).write_text(content)
    print('활성 환경(dev/prd) 경보 선언 생성')
