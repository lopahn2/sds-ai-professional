#!/usr/bin/env python3
"""삼성 세 시스템의 환경별 선언을 생성한다. 클러스터·비밀값은 읽거나 변경하지 않는다."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'deploy/samsung/system-catalog.json'
CATALOG = json.loads(CATALOG_PATH.read_text())
SERVICES = tuple(CATALOG['service_to_system'])
ENVIRONMENTS = tuple(CATALOG['environments'])
OWNERS = CATALOG['service_to_system']
DOMAIN_MODES = CATALOG['domain_modes']


def obj(kind, name, namespace=None, **fields):
    api = {'Deployment': 'apps/v1', 'NetworkPolicy': 'networking.k8s.io/v1',
           'Ingress': 'networking.k8s.io/v1'}.get(kind, 'v1')
    result = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name}}
    if namespace:
        result['metadata']['namespace'] = namespace
    result.update(fields)
    return result


def labels_for(service, ns):
    return {'app': service, 'app.kubernetes.io/part-of': OWNERS[service],
            'liveprobe.io/environment': ns, 'liveprobe.io/managed': 'true',
            'liveprobe.io/lifecycle': 'current'}


def application(service, ns, image):
    java = service in ('order', 'inventory')
    data = {'SYSTEM_NAME': OWNERS[service], 'SERVICE_NAME': service,
            'APP_ENV': ns, 'APP_VERSION': 'samsung-v1', 'SELF_TRAFFIC': '0'}
    if java:
        data.update({'SPRING_PROFILES_ACTIVE': 'chaos', 'CHAOS_STATE_DIR': '/var/run/liveprobe-chaos',
                     'JAVA_TOOL_OPTIONS': '-XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError -Dhttp.keepAlive=false -XX:ActiveProcessorCount=2'})
    else:
        data['FAULT_STATE_DIR'] = '/var/run/liveprobe-chaos'
    live = '/actuator/health/liveness' if java else '/healthz/live'
    ready = '/actuator/health/readiness' if java else '/healthz/ready'
    container = {'name': service, 'image': image, 'imagePullPolicy': 'IfNotPresent',
                 'envFrom': [{'configMapRef': {'name': service + '-config'}}],
                 'ports': [{'name': 'http', 'containerPort': 8080}],
                 'volumeMounts': [{'name': 'chaos-state', 'mountPath': '/var/run/liveprobe-chaos'},
                                  {'name': 'tmp', 'mountPath': '/tmp'}],
                 'startupProbe': {'httpGet': {'path': live, 'port': 8080}, 'periodSeconds': 3, 'failureThreshold': 60},
                 'readinessProbe': {'httpGet': {'path': ready, 'port': 8080}, 'periodSeconds': 3, 'failureThreshold': 2, 'timeoutSeconds': 2},
                 'livenessProbe': {'httpGet': {'path': live, 'port': 8080}, 'periodSeconds': 5, 'failureThreshold': 3, 'timeoutSeconds': 2},
                 'resources': {'requests': {'cpu': '50m' if java else '10m', 'memory': '128Mi' if java else '32Mi'},
                               'limits': {'cpu': '1000m' if java else '500m', 'memory': '512Mi' if java else '256Mi'}},
                 'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}, 'runAsNonRoot': True}}
    if service == 'delivery':
        container['securityContext'].update({'runAsUser': 1000, 'runAsGroup': 1000})
    annotations = {'prometheus.io/scrape': 'true', 'prometheus.io/port': '8080',
                   'prometheus.io/path': '/actuator/prometheus' if java else '/metrics',
                   'prometheus.istio.io/merge-metrics': 'false', 'sidecar.istio.io/rewriteAppHTTPProbers': 'false'}
    if ns == 'prd':
        annotations.update({'sidecar.istio.io/proxyCPU': '10m', 'sidecar.istio.io/proxyMemory': '64Mi',
                            'sidecar.istio.io/proxyMemoryLimit': '128Mi'})
    for key, target in CATALOG['dependencies'].get(service, {}).items():
        data[key] = f'http://{target}.{ns}.svc:8080'
    env = []
    if java:
        env.append({'name': 'CHAOS_OPERATOR_TOKEN', 'valueFrom': {'secretKeyRef': {'name': service + '-chaos-operator', 'key': 'token'}}})
    if service == 'inventory':
        data.update({'INVENTORY_DB_URL': f'jdbc:postgresql://trade-db.liveprobe-data.svc:5432/inventory_{ns}',
                     'INVENTORY_DB_USER': f'inventory_{ns}', 'INVENTORY_DB_POOL_MAX': '3',
                     'INVENTORY_DB_CONNECTION_TIMEOUT_MS': '1000'})
        env.append({'name': 'INVENTORY_DB_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'samsung-inventory-db', 'key': 'password'}}})
    if service == 'order':
        data.update({'INVENTORY_CONNECT_TIMEOUT_MS': '500', 'INVENTORY_TIMEOUT_MS': '1800',
                     'INVENTORY_MAX_RETRIES': '1', 'INVENTORY_RETRY_BACKOFF_MS': '100'})
    if env:
        container['env'] = env
    volumes = [{'name': 'chaos-state', 'emptyDir': {'sizeLimit': '2Mi'}},
               {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]
    service_ports = [{'name': 'http', 'port': 8080, 'targetPort': 8080}]
    if service in DOMAIN_MODES:
        mode = DOMAIN_MODES[service]
        data.update({'DOMAIN_MODE': mode, 'DATABASE_DSN_FILE': '/var/run/samsung/db/dsn',
                     'KAFKA_CONFIG_FILE': '/var/run/samsung/kafka/client.json',
                     'KAFKA_CA_FILE': '/var/run/samsung/kafka/ca.crt',
                     'API_CREDENTIALS_FILE': '/var/run/samsung/api/credentials.json',
                     'TLS_CERT_FILE': '/var/run/samsung/tls/tls.crt',
                     'TLS_KEY_FILE': '/var/run/samsung/tls/tls.key',
                     'READ_ONLY_MODE': '1', 'HTTP_PORT': '8080', 'TLS_PORT': '8443'})
        if mode != 'business':
            data.update({'BUSINESS_TOKEN_FILE': '/var/run/samsung/api/reader-token',
                         'BUSINESS_API_URL': f'https://business-api.{ns}.svc:8443',
                         'BUSINESS_CA_FILE': '/var/run/samsung/tls/ca.crt'})
        for suffix, keys in [('db', ['dsn']), ('kafka', ['client.json', 'ca.crt']),
                             ('api', ['credentials.json'] if mode == 'business' else ['credentials.json', 'reader-token']),
                             ('tls', ['ca.crt', 'tls.crt', 'tls.key'])]:
            volumes.append({'name': 'samsung-' + suffix,
                            'secret': {'secretName': service + '-' + suffix, 'defaultMode': 0o440,
                                       'items': [{'key': key, 'path': key} for key in keys]}})
            container['volumeMounts'].append({'name': 'samsung-' + suffix,
                                             'mountPath': '/var/run/samsung/' + suffix, 'readOnly': True})
        container['ports'].append({'name': 'https-internal', 'containerPort': 8443})
        service_ports.append({'name': 'https-internal', 'port': 8443, 'targetPort': 8443})
    labels = labels_for(service, ns)
    deployment = obj('Deployment', service, ns, spec={
        'replicas': 1, 'revisionHistoryLimit': 10,
        'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 1, 'maxUnavailable': 0}},
        # 기존 selector 불변 필드를 바꾸지 않는다. 시스템 경계는 Pod와 정책의 소속 라벨로 표현한다.
        'selector': {'matchLabels': {'app': service}},
        'template': {'metadata': {'labels': labels.copy(), 'annotations': annotations},
                     'spec': {'automountServiceAccountToken': False, 'securityContext': {'fsGroup': 10001},
                              'containers': [container], 'volumes': volumes}}})
    docs = [obj('ConfigMap', service + '-config', ns, data=data), deployment,
            obj('Service', service, ns, spec={'selector': labels.copy(), 'ports': service_ports})]
    if service != 'inventory':
        docs.append(obj('Ingress', service, ns, spec={'ingressClassName': 'traefik', 'rules': [
            {'host': f'{service}.{OWNERS[service]}.{ns}.localhost', 'http': {'paths': [
                {'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': service, 'port': {'number': 8080}}}}]}}]}))
    for doc in docs:
        doc['metadata']['labels'] = labels.copy()
    return docs


def peer(ns, labels):
    return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}},
            'podSelector': {'matchLabels': labels}}


def tcp(*ports):
    return [{'protocol': 'TCP', 'port': port} for port in ports]


def network_policies(ns):
    current = {'liveprobe.io/managed': 'true', 'liveprobe.io/lifecycle': 'current', 'liveprobe.io/environment': ns}
    docs = [obj('NetworkPolicy', 'default-deny-all', ns,
                spec={'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']})]

    def policy(name, selected, direction, remote, ports):
        return obj('NetworkPolicy', name, ns, spec={'podSelector': selected,
            'policyTypes': [direction.title()], direction: [
                {'from' if direction == 'ingress' else 'to': remote, 'ports': ports}]})

    for system in CATALOG['systems']:
        selected = {'matchLabels': dict(current, **{'app.kubernetes.io/part-of': system['id']})}
        docs.append(obj('NetworkPolicy', system['id'] + '-internal', ns, spec={
            'podSelector': selected, 'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': [{'podSelector': selected}], 'ports': tcp(8080, 8443)}],
            'egress': [{'to': [{'podSelector': selected}], 'ports': tcp(8080, 8443)}]}))
    docs.append(policy('samsung-dns', {'matchLabels': current}, 'egress',
                       [peer('kube-system', {'k8s-app': 'kube-dns'})],
                       [{'protocol': protocol, 'port': 53} for protocol in ('UDP', 'TCP')]))
    docs.append(policy('samsung-public-http', {'matchLabels': current, 'matchExpressions': [
        {'key': 'app', 'operator': 'In', 'values': [s for s in SERVICES if s != 'inventory']}]},
        'ingress', [peer('kube-system', {'app.kubernetes.io/name': 'traefik'})], tcp(8080)))
    docs.append(policy('samsung-observation', {'matchLabels': current}, 'ingress',
                       [peer('liveprobe-observability', {'app': 'prometheus'}),
                        peer('liveprobe-system', {'app': 'liveprobe-api'})], tcp(8080, 15090, 15020, 15021)))
    databases = {'inventory': 'trade-db', 'trade-member': 'trade-db',
                 'delivery-member': 'delivery-db', 'business-api': 'business-db'}
    for service, database in databases.items():
        docs.append(policy('samsung-' + service + '-database', {'matchLabels': labels_for(service, ns)},
                           'egress', [peer('liveprobe-data', {'app': database})], tcp(5432)))
    domains = {'matchLabels': current, 'matchExpressions': [
        {'key': 'app', 'operator': 'In', 'values': list(DOMAIN_MODES)}]}
    docs.append(policy('samsung-domain-kafka', domains, 'egress',
                       [peer(CATALOG['kafka']['namespace'], {'app': CATALOG['kafka']['service']})], tcp(9092)))
    readers = ('trade-member', 'delivery-member')
    for service in readers:
        docs.append(policy('samsung-' + service + '-business-tls', {'matchLabels': labels_for(service, ns)},
                           'egress', [peer(ns, labels_for('business-api', ns))], tcp(8443)))
    docs.append(policy('samsung-business-readers-tls', {'matchLabels': labels_for('business-api', ns)},
                       'ingress', [peer(ns, labels_for(service, ns)) for service in readers], tcp(8443)))
    if ns == 'prd':
        docs.append(policy('samsung-istiod', {'matchLabels': current}, 'egress',
                           [peer('istio-system', {'app': 'istiod'})], tcp(15012, 15017, 443)))
    return docs


def infrastructure_ingress_policies():
    # 서버 쪽 허용도 같은 서비스 소속으로 제한한다. DB·Kafka 본체와 자격은 부모가 준비한다.
    docs = []
    for system, database, services in [
            ('samsung-trade', 'trade-db', ('inventory', 'trade-member')),
            ('samsung-delivery', 'delivery-db', ('delivery-member',)),
            ('samsung-business', 'business-db', ('business-api',))]:
        docs.append(obj('NetworkPolicy', 'samsung-' + database + '-clients', 'liveprobe-data', spec={
            'podSelector': {'matchLabels': {'app': database}}, 'policyTypes': ['Ingress'],
            'ingress': [{'from': [peer(ns, labels_for(service, ns)) for ns in ENVIRONMENTS for service in services],
                         'ports': tcp(5432)}]}))
    docs.append(obj('NetworkPolicy', 'samsung-kafka-domain-clients', CATALOG['kafka']['namespace'], spec={
        'podSelector': {'matchLabels': {'app': CATALOG['kafka']['service']}}, 'policyTypes': ['Ingress'],
        'ingress': [{'from': [peer(ns, labels_for(service, ns)) for ns in ENVIRONMENTS for service in DOMAIN_MODES],
                     'ports': tcp(9092)}]}))
    return docs


DIGEST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$')
LOCAL_TAG = re.compile(r'^(?:liveprobe/|localhost(?::[0-9]+)?/)[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$')


def validate_catalog():
    owners = CATALOG['service_to_system']
    declared = {}
    for system in CATALOG['systems']:
        for service in system['services']:
            if service in declared:
                raise ValueError('카탈로그 서비스 소속이 중복된다')
            declared[service] = system['id']
    if declared != owners or set(owners) != set(SERVICES) or tuple(CATALOG['environments']) != ENVIRONMENTS:
        raise ValueError('카탈로그 시스템·서비스·환경 매핑이 일치하지 않는다')
    for source, mapping in CATALOG['dependencies'].items():
        for target in mapping.values():
            if source not in owners or target not in owners or owners[source] != owners[target]:
                raise ValueError('내부 의존은 존재하는 같은 시스템 서비스만 허용한다')


def validate_images(images, allow_local_tags=False):
    if not isinstance(images, dict) or set(images) != set(SERVICES):
        raise ValueError('이미지는 카탈로그의 아홉 서비스 키를 정확히 포함해야 한다')
    for value in images.values():
        if not isinstance(value, str):
            raise ValueError('이미지 참조는 문자열이어야 한다')
        if DIGEST.fullmatch(value):
            continue
        if allow_local_tags and LOCAL_TAG.fullmatch(value) and value.rsplit(':', 1)[1] != 'latest':
            continue
        raise ValueError('실제 manifest digest 참조가 필요하다. 로컬 고정 태그는 명시적 옵션으로만 허용한다')


def inventory_migration_environments(environments):
    """부모가 이관 검증을 마친 환경만 명시적으로 받으며 상태를 추측하지 않는다."""
    if not isinstance(environments, (list, tuple, set, frozenset)) or any(
            not isinstance(ns, str) or ns not in ENVIRONMENTS for ns in environments):
        raise ValueError('inventory 이관 완료 환경은 dev/stg/prd의 명시적 목록이어야 한다')
    return frozenset(environments)


def validate(docs, existing_resources=None, inventory_migrated_envs=()):
    """생성 결과의 소속·DNS·외부 경로를 재검사하며 모호한 선언은 거절한다."""
    validate_catalog()
    migrated = inventory_migration_environments(inventory_migrated_envs)
    indexed = {}
    for doc in docs:
        metadata = doc['metadata']
        key = (doc['kind'], metadata.get('namespace'), metadata['name'])
        if key in indexed:
            raise ValueError('같은 종류·namespace·이름의 리소스가 중복된다')
        indexed[key] = doc
    for ns in ENVIRONMENTS:
        for service in SERVICES:
            expected = labels_for(service, ns)
            for kind, name in [('Deployment', service), ('ConfigMap', service + '-config'), ('Service', service)]:
                doc = indexed.get((kind, ns, name))
                if doc is None or doc['metadata'].get('labels') != expected:
                    raise ValueError('필수 서비스 리소스 누락 또는 시스템 소속 불일치')
            spec = indexed['Service', ns, service]['spec']
            ports = [{'name': 'http', 'port': 8080, 'targetPort': 8080}]
            if service in DOMAIN_MODES:
                ports.append({'name': 'https-internal', 'port': 8443, 'targetPort': 8443})
            if spec.get('selector') != expected or spec.get('ports') != ports:
                raise ValueError('Service selector·대상 포트가 실제 서비스 소속과 다르다')
            deployment = indexed['Deployment', ns, service]
            expected_pod = expected.copy()
            if service == 'inventory' and ns in migrated:
                expected_pod['liveprobe.io/database'] = 'samsung-trade'
            if deployment['spec']['template']['metadata']['labels'] != expected_pod:
                raise ValueError('Pod template 소속이 리소스 소속과 다르다')
            data = indexed['ConfigMap', ns, service + '-config']['data']
            if (data.get('SYSTEM_NAME'), data.get('SERVICE_NAME'), data.get('APP_ENV')) != (OWNERS[service], service, ns):
                raise ValueError('환경변수 소속이 리소스 소속과 다르다')
            if data.get('SELF_TRAFFIC') != '0':
                raise ValueError('새 구성의 자체 쓰기 트래픽은 꺼져 있어야 한다')
            allowed_urls = {}
            for key, target in CATALOG['dependencies'].get(service, {}).items():
                allowed_urls[key] = f'http://{target}.{ns}.svc:8080'
            if service in DOMAIN_MODES and DOMAIN_MODES[service] != 'business':
                allowed_urls['BUSINESS_API_URL'] = f'https://business-api.{ns}.svc:8443'
            if service == 'inventory':
                allowed_urls['INVENTORY_DB_URL'] = f'jdbc:postgresql://trade-db.liveprobe-data.svc:5432/inventory_{ns}'
            actual_urls = {key: value for key, value in data.items() if key.endswith('_URL')}
            if actual_urls != allowed_urls:
                raise ValueError('서비스 DNS 의존 또는 허용된 내부 TLS 경계가 잘못되었다')
    seen_routes = {}
    generated_ids = {id(d) for d in docs}
    for ingress in list(docs) + list(existing_resources or []):
        if ingress['kind'] != 'Ingress':
            continue
        ns = ingress['metadata'].get('namespace')
        # 같은 종류·namespace·이름은 추가 경로가 아니라 이번 선언으로 교체할 기존 객체다.
        if id(ingress) not in generated_ids and ('Ingress', ns, ingress['metadata']['name']) in indexed:
            continue
        spec = ingress['spec']
        if id(ingress) in generated_ids and ('defaultBackend' in spec or len(spec.get('rules', [])) != 1 or
                len(spec['rules'][0].get('http', {}).get('paths', [])) != 1):
            raise ValueError('공개 Ingress는 단일 서비스의 명시적 host·경로 한 개만 허용한다')
        ingress_class = spec.get('ingressClassName', ingress['metadata'].get('annotations', {}).get('kubernetes.io/ingress.class', ''))
        for rule in spec.get('rules', []):
            for path in rule.get('http', {}).get('paths', []):
                route = (ingress_class, rule.get('host'), path.get('path'), path.get('pathType'))
                if route in seen_routes:
                    raise ValueError('Ingress class·host·path·pathType 충돌')
                seen_routes[route] = ingress['metadata']['name']
                if id(ingress) not in generated_ids:
                    continue
                backend = path.get('backend', {}).get('service', {})
                service = backend.get('name')
                target = indexed.get(('Service', ns, service))
                labels = ingress['metadata'].get('labels', {})
                if (service not in OWNERS or service == 'inventory' or target is None or
                        labels != labels_for(service, ns) or labels != target['metadata'].get('labels')):
                    raise ValueError('Ingress backend 누락·다른 소속·외부 비공개 서비스 경로')
                port = backend.get('port', {}).get('number')
                if port != 8080 or not any(p['port'] == port for p in target['spec']['ports']):
                    raise ValueError('공개 Ingress는 비민감 HTTP 8080만 허용한다')
                if rule['host'] != f'{service}.{OWNERS[service]}.{ns}.localhost' or path['path'] != '/' or path['pathType'] != 'Prefix':
                    raise ValueError('외부 host·경로가 서비스 고유 매핑과 다르다')
    required_ingresses = {(ns, s) for ns in ENVIRONMENTS for s in SERVICES if s != 'inventory'}
    actual_ingresses = {(d['metadata'].get('namespace'), d['metadata']['name']) for d in docs if d['kind'] == 'Ingress'}
    if actual_ingresses != required_ingresses:
        raise ValueError('공개 서비스 Ingress 누락 또는 예상하지 않은 경로')


def render(images, allow_local_tags=False, existing_resources=None, app_version='samsung-v1',
           inventory_migrated_envs=()):
    validate_catalog()
    validate_images(images, allow_local_tags)
    migrated = inventory_migration_environments(inventory_migrated_envs)
    if not isinstance(app_version, str) or not app_version.strip():
        raise ValueError('비어 있지 않은 빌드 버전이 필요하다')
    docs = []
    for index, ns in enumerate(ENVIRONMENTS, 1):
        namespace = obj('Namespace', ns)
        namespace['metadata']['labels'] = {'liveprobe.io/environment': ns, 'liveprobe.io/env-order': str(index),
                                            'istio-injection': 'enabled' if ns == 'prd' else 'disabled'}
        docs.append(namespace)
        for service in SERVICES:
            docs.extend(application(service, ns, images[service]))
        docs.extend(network_policies(ns))
    docs.extend(infrastructure_ingress_policies())
    for doc in docs:
        if doc['kind'] == 'ConfigMap':
            doc['data']['APP_VERSION'] = app_version
        if (doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'inventory' and
                doc['metadata']['namespace'] in migrated):
            doc['spec']['template']['metadata']['labels']['liveprobe.io/database'] = 'samsung-trade'
    validate(docs, existing_resources, inventory_migrated_envs=migrated)
    return docs


def migration_report(images, docs, inventory_migrated_envs=()):
    migrated = inventory_migration_environments(inventory_migrated_envs)
    validate(docs, inventory_migrated_envs=migrated)
    old_policies = ['allow-same-namespace', 'allow-dns', 'allow-ingress-controller', 'allow-observation', 'inventory-postgres']
    removals = [{'kind': 'NetworkPolicy', 'namespace': ns, 'name': name}
                for ns in ENVIRONMENTS for name in old_policies]
    removals.append({'kind': 'NetworkPolicy', 'namespace': 'prd', 'name': 'allow-istiod'})
    removals.append({'kind': 'NetworkPolicy', 'namespace': 'liveprobe-system', 'name': 'p18-inventory-postgres-ingress'})
    return {'state': 'not_applied', 'image_policy': 'digest' if all(DIGEST.fullmatch(v) for v in images.values()) else 'local_template',
            'catalog_sha256': hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest(),
            'resource_count': len(docs), 'images': images,
            'inventory_cutover': {
                'migrated_environments': [ns for ns in ENVIRONMENTS if ns in migrated],
                'pod_label': {'liveprobe.io/database': 'samsung-trade'},
                'requirements': [
                    '환경별 원본 export·freeze·최종 반영과 키·수량·내용 대조 및 단일 writer 전환 검증을 부모가 완료한 뒤에만 이관 완료 환경으로 입력한다.',
                    '이 라벨은 실제 DB 이관 증명이 아니라 부모의 명시적 검증 완료 입력을 반영한다. 미입력 환경에는 붙이지 않는다.',
                    '재생성할 때도 --inventory-migrated-env <환경>을 완료 환경별로 반복한다. 기존 선언이나 별도 상태 파일에서 자동 추측하지 않는다.',
                    'DB 배선은 신규 trade-db를 참조하므로 라벨이 없다는 이유로 전환 전 안전 적용이 보장되지는 않는다. 실제 적용은 보존·검증 뒤 별도 승인한다.']},
            'digest_provenance': '참조 형식만 검증했다. 실제 manifest 종류와 레지스트리 존재 증명은 부모 빌드 영수증이 필요하다.',
            'retired_labels': {'liveprobe.io/lifecycle': 'retired'},
            'retire_after_backup': [{'kind': kind, 'namespace': ns, 'name': name}
                for ns in ENVIRONMENTS for kind, name in [('Deployment', 'member'), ('Service', 'member'), ('Ingress', 'member'), ('ConfigMap', 'member-config')]],
            'policies_requiring_explicit_removal': removals,
            'requirements': [
                '명세는 삭제·적용을 실행하지 않는다. 기존 정책의 가산 허용 제거와 검증은 부모가 명시적으로 시행한다.',
                '기존 이름의 앱·설정·서비스를 갱신하므로 재시작 전 원본 메모리·DB·파일의 export와 freeze·백업을 입증한다.',
                '새 앱도 자체 트래픽은 꺼져 있다. domain은 읽기 전용으로 시작하며 단일 writer 전환 뒤에만 해제한다.',
                '부모가 신규 Secret·DB·Kafka와 해당 namespace를 먼저 준비한다. 기존 Secret은 읽거나 회전하지 않는다.',
                'DB Pod는 app=trade-db|delivery-db|business-db, Kafka Pod는 app=kafka 라벨을 사용해야 한다.',
                '은퇴 라벨은 기존 소속·사건 이력을 바꾸지 않고 리소스와 Pod template에 함께 적용한다.',
                '공개 8080의 민감 HR 우회 거절·실접속 허용/거절·TLS·인증·자료 보존은 별도 런타임 인수 항목이다.',
                'prd는 기존 Istio 주입 설정을 유지한다. 선언 전체 적용이 아니라 검토한 변경 객체만 적용한다.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', required=True, type=Path, help='서비스 키별 실제 manifest digest JSON')
    parser.add_argument('--output', required=True, type=Path, help='새 Kubernetes List JSON 경로')
    parser.add_argument('--changes-output', type=Path, help='삭제 필요 정책과 전환 조건의 별도 명세')
    parser.add_argument('--allow-local-tags', action='store_true', help='실배포 증명이 아닌 명시적 로컬 고정 태그 템플릿 허용')
    parser.add_argument('--existing-resources', type=Path, help='충돌 검사할 기존 비밀 없는 Kubernetes List JSON')
    parser.add_argument('--app-version', default='samsung-v1', help='빌드 고유 버전')
    parser.add_argument('--inventory-migrated-env', action='append', choices=ENVIRONMENTS, default=[],
                        help='부모가 보존·이관·단일 writer 검증을 완료한 환경만 지정. 여러 환경은 반복하고 재생성 때도 재입력')
    args = parser.parse_args()
    try:
        images = json.loads(args.images.read_text())
        existing = None
        if args.existing_resources:
            existing = json.loads(args.existing_resources.read_text())['items']
        docs = render(images, args.allow_local_tags, existing, args.app_version,
                      inventory_migrated_envs=args.inventory_migrated_env)
        changes_path = args.changes_output or args.output.with_suffix('.changes.json')
        protected = {args.images.resolve(), CATALOG_PATH.resolve(), Path(__file__).resolve()}
        if args.existing_resources:
            protected.add(args.existing_resources.resolve())
        if args.output.resolve() == changes_path.resolve() or args.output.resolve() in protected or changes_path.resolve() in protected:
            raise ValueError('입력과 선언·명세 출력 경로는 서로 달라야 한다')
        report = migration_report(images, docs, inventory_migrated_envs=args.inventory_migrated_env)
        for path, content in [(args.output, {'apiVersion': 'v1', 'kind': 'List', 'items': docs}), (changes_path, report)]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + '\n')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'선언 생성 거절: {exc}\n')
    print(f'{len(docs)}개 리소스 선언 생성: {args.output.resolve()}')
    print(f'미적용 전환 명세: {changes_path.resolve()}')


if __name__ == '__main__':
    main()

