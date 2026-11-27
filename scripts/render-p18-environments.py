#!/usr/bin/env python3
"""Render P18 local application environments. No secrets, no API/collector/admission mutation.

Use liveprobe/.venv/bin/python scripts/render-p18-environments.py --images <json> --output <json>.
The output is a Kubernetes List (valid JSON/YAML). Apply only after reviewing the server dry run.
"""
import argparse
import json
from pathlib import Path
import re

SERVICES = ('inventory', 'order', 'catalog', 'member', 'payment', 'notification', 'delivery')
ENVIRONMENTS = ('dev', 'prd')
DEPENDENCIES = {'order': ('INVENTORY_URL', 'inventory'), 'catalog': ('INVENTORY_URL', 'inventory'),
                'member': ('CATALOG_URL', 'catalog'), 'payment': ('MEMBER_URL', 'member'),
                'notification': ('MEMBER_URL', 'member'), 'delivery': ('NOTIFICATION_URL', 'notification')}
DIGEST = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*@sha256:[0-9a-f]{64}$')


def obj(kind, name, namespace=None, **fields):
    api = {'Deployment': 'apps/v1', 'NetworkPolicy': 'networking.k8s.io/v1', 'Ingress': 'networking.k8s.io/v1',
           'Role': 'rbac.authorization.k8s.io/v1', 'RoleBinding': 'rbac.authorization.k8s.io/v1'}.get(kind, 'v1')
    result = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name}}
    if namespace:
        result['metadata']['namespace'] = namespace
    result.update(fields)
    return result


def peer(ns, labels):
    return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}},
            'podSelector': {'matchLabels': labels}}


def render(images):
    if set(images) != set(SERVICES) or any(not DIGEST.fullmatch(v) for v in images.values()):
        raise ValueError('exactly seven digest-pinned service images are required')
    docs = []
    for index, ns in enumerate(ENVIRONMENTS, 1):
        n = obj('Namespace', ns)
        n['metadata']['labels'] = {'liveprobe.io/environment': ns, 'liveprobe.io/env-order': str(index),
                                    'istio-injection': 'enabled' if ns == 'prd' else 'disabled'}
        docs.append(n)
        for name in SERVICES:
            system = 'order' if name == 'inventory' else name
            labels = {'app': name, 'app.kubernetes.io/part-of': system, 'liveprobe.io/managed': 'true',
                      'liveprobe.io/environment': ns}
            java = name in ('order', 'inventory')
            data = {'SYSTEM_NAME': system, 'SERVICE_NAME': name, 'APP_ENV': ns, 'APP_VERSION': 'p18-campaign',
                    'SELF_TRAFFIC': '1', 'SELF_TRAFFIC_INTERVAL_SECONDS': '1'}
            if name in DEPENDENCIES:
                key, target = DEPENDENCIES[name]
                data[key] = f'http://{target}.{ns}.svc:8080'
            if java:
                data.update({'SPRING_PROFILES_ACTIVE': 'chaos', 'CHAOS_STATE_DIR': '/var/run/liveprobe-chaos',
                             'JAVA_TOOL_OPTIONS': '-XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError -Dhttp.keepAlive=false -XX:ActiveProcessorCount=2'})
            else:
                data['FAULT_STATE_DIR'] = '/var/run/liveprobe-chaos'
            if name == 'inventory':
                data.update({'INVENTORY_DB_URL': f'jdbc:postgresql://postgres.liveprobe-system.svc:5432/inventory_{ns}',
                             'INVENTORY_DB_USER': f'inventory_{ns}', 'INVENTORY_DB_POOL_MAX': '3',
                             'INVENTORY_DB_CONNECTION_TIMEOUT_MS': '1000'})
            if name == 'order':
                data.update({'INVENTORY_CONNECT_TIMEOUT_MS': '500', 'INVENTORY_TIMEOUT_MS': '1800',
                             'INVENTORY_MAX_RETRIES': '1', 'INVENTORY_RETRY_BACKOFF_MS': '100'})
            cm = obj('ConfigMap', name + '-config', ns, data=data)
            cm['metadata']['labels'] = labels.copy()
            docs.append(cm)
            env = []
            if java:
                env.append({'name': 'CHAOS_OPERATOR_TOKEN', 'valueFrom': {'secretKeyRef': {'name': name + '-chaos-operator', 'key': 'token'}}})
            if name == 'inventory':
                env.append({'name': 'INVENTORY_DB_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'inventory-db', 'key': 'password'}}})
            prefix = '/actuator/health/' if java else '/healthz/'
            live, ready = (prefix + 'liveness', prefix + 'readiness') if java else (prefix + 'live', prefix + 'ready')
            container = {'name': name, 'image': images[name], 'imagePullPolicy': 'IfNotPresent',
                         'envFrom': [{'configMapRef': {'name': name + '-config'}}],
                         'ports': [{'name': 'http', 'containerPort': 8080}],
                         'volumeMounts': [{'name': 'chaos-state', 'mountPath': '/var/run/liveprobe-chaos'},
                                          {'name': 'tmp', 'mountPath': '/tmp'}],
                         'startupProbe': {'httpGet': {'path': live, 'port': 8080}, 'periodSeconds': 3, 'failureThreshold': 60},
                         'readinessProbe': {'httpGet': {'path': ready, 'port': 8080}, 'periodSeconds': 3, 'failureThreshold': 2, 'timeoutSeconds': 2},
                         'livenessProbe': {'httpGet': {'path': live, 'port': 8080}, 'periodSeconds': 5, 'failureThreshold': 3, 'timeoutSeconds': 2},
                         'resources': {'requests': {'cpu': '50m' if java else '10m', 'memory': '128Mi' if java else '32Mi'},
                                       'limits': {'cpu': '1000m' if java else '500m',
                                                  'memory': '512Mi' if java else '256Mi'}},
                         'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']},
                                             'runAsNonRoot': True}}
            if name in ('member', 'delivery'):
                # Node's image declares USER node; kubelet needs its numeric uid to enforce non-root.
                container['securityContext'].update({'runAsUser': 1000, 'runAsGroup': 1000})
            if env:
                container['env'] = env
            annotations = {'prometheus.io/scrape': 'true', 'prometheus.io/port': '8080',
                           'prometheus.io/path': '/actuator/prometheus' if java else '/metrics',
                           'prometheus.istio.io/merge-metrics': 'false', 'sidecar.istio.io/rewriteAppHTTPProbers': 'false'}
            if ns == 'prd':
                annotations.update({'sidecar.istio.io/proxyCPU': '10m', 'sidecar.istio.io/proxyMemory': '64Mi',
                                    'sidecar.istio.io/proxyMemoryLimit': '128Mi'})
            deployment = obj('Deployment', name, ns, spec={'replicas': 1, 'revisionHistoryLimit': 10,
                'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 1, 'maxUnavailable': 0}},
                'selector': {'matchLabels': {'app': name}},
                'template': {'metadata': {'labels': labels.copy(), 'annotations': annotations},
                             'spec': {'automountServiceAccountToken': False,
                                      'securityContext': {'fsGroup': 10001}, 'containers': [container],
                                      'volumes': [{'name': 'chaos-state', 'emptyDir': {'sizeLimit': '2Mi'}},
                                                  {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]}}})
            deployment['metadata']['labels'] = labels.copy()
            docs.append(deployment)
            svc = obj('Service', name, ns, spec={'selector': {'app': name}, 'ports': [{'name': 'http', 'port': 8080, 'targetPort': 8080}]})
            svc['metadata']['labels'] = labels.copy()
            docs.append(svc)
            if name != 'inventory':
                ingress = obj('Ingress', name, ns, spec={'ingressClassName': 'traefik', 'rules': [
                    {'host': f'{system}.{ns}.localhost', 'http': {'paths': [{'path': '/', 'pathType': 'Prefix',
                        'backend': {'service': {'name': name, 'port': {'number': 8080}}}}]}}]})
                ingress['metadata']['labels'] = labels.copy()
                docs.append(ingress)
        # Namespace-local calls only; explicitly scoped ingress/collector/API/DB/istiod paths.
        docs.extend([
            obj('NetworkPolicy', 'default-deny-all', ns, spec={'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}),
            obj('NetworkPolicy', 'allow-same-namespace', ns, spec={'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                'ingress': [{'from': [{'podSelector': {}}]}], 'egress': [{'to': [{'podSelector': {}}]}]}),
            obj('NetworkPolicy', 'allow-dns', ns, spec={'podSelector': {}, 'policyTypes': ['Egress'],
                'egress': [{'to': [peer('kube-system', {'k8s-app': 'kube-dns'})],
                            'ports': [{'protocol': p, 'port': 53} for p in ('UDP', 'TCP')]}]}),
            obj('NetworkPolicy', 'allow-ingress-controller', ns, spec={'podSelector': {}, 'policyTypes': ['Ingress'],
                'ingress': [{'from': [peer('kube-system', {'app.kubernetes.io/name': 'traefik'})], 'ports': [{'protocol': 'TCP', 'port': 8080}]}]}),
            obj('NetworkPolicy', 'allow-observation', ns, spec={'podSelector': {}, 'policyTypes': ['Ingress'],
                'ingress': [{'from': [peer('liveprobe-observability', {'app': 'prometheus'}), peer('liveprobe-system', {'app': 'liveprobe-api'})],
                            'ports': [{'protocol': 'TCP', 'port': p} for p in (8080, 15090, 15020, 15021)]}]}),
            obj('NetworkPolicy', 'inventory-postgres', ns, spec={'podSelector': {'matchLabels': {'app': 'inventory'}}, 'policyTypes': ['Egress'],
                'egress': [{'to': [peer('liveprobe-system', {'app': 'postgres'})], 'ports': [{'protocol': 'TCP', 'port': 5432}]}]}),
        ])
        if ns == 'prd':
            docs.append(obj('NetworkPolicy', 'allow-istiod', ns, spec={'podSelector': {}, 'policyTypes': ['Egress'],
                'egress': [{'to': [peer('istio-system', {'app': 'istiod'})],
                            'ports': [{'protocol': 'TCP', 'port': p} for p in (15012, 15017, 443)]}]}))
        docs.extend([
            obj('Role', 'liveprobe-config-reader', ns, rules=[
                {'apiGroups': [''], 'resources': ['configmaps'], 'verbs': ['get', 'list']},
                {'apiGroups': [''], 'resources': ['persistentvolumeclaims'], 'verbs': ['get']},
                {'apiGroups': ['autoscaling'], 'resources': ['horizontalpodautoscalers'], 'verbs': ['list']},
            ]),
            obj('RoleBinding', 'liveprobe-config-reader', ns,
                roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'liveprobe-config-reader'},
                subjects=[{'kind': 'ServiceAccount', 'namespace': 'liveprobe-system', 'name': 'liveprobe'}]),
        ])
    docs.append(obj('NetworkPolicy', 'p18-inventory-postgres-ingress', 'liveprobe-system',
                    spec={'podSelector': {'matchLabels': {'app': 'postgres'}}, 'policyTypes': ['Ingress'],
                          'ingress': [{'from': [{'namespaceSelector': {'matchExpressions': [{'key': 'liveprobe.io/environment',
                                      'operator': 'In', 'values': list(ENVIRONMENTS)}]}, 'podSelector': {'matchLabels': {'app': 'inventory'}}}],
                                      'ports': [{'protocol': 'TCP', 'port': 5432}]}]}))
    docs.append(obj('NetworkPolicy', 'p18-api-target-egress', 'liveprobe-system',
                    spec={'podSelector': {'matchLabels': {'app': 'liveprobe-api'}}, 'policyTypes': ['Egress'],
                          'egress': [{'to': [{'namespaceSelector': {'matchExpressions': [{'key': 'liveprobe.io/environment',
                                      'operator': 'In', 'values': list(ENVIRONMENTS)}]}}],
                                      'ports': [{'protocol': 'TCP', 'port': 8080}]}]}))
    return docs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--images', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    docs = render(json.loads(args.images.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': docs}, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(docs)}개 리소스 선언 생성: {args.output}')


if __name__ == '__main__':
    main()
