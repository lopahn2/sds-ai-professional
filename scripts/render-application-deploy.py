#!/usr/bin/env python3
"""Render only Application resources. JSON is a YAML 1.2 subset; no PyYAML required.

No subprocess, cluster access, dotenv, credential sources, or Secret documents.
Catalog bytes are preserved: both the API and runner hash the original file.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'liveprobe-system'
PART_OF = {'app.kubernetes.io/part-of': 'liveprobe-application'}
API = 'liveprobe-application-api'
DB = 'liveprobe-application-db'
RUNNER = 'liveprobe-application-runner'
CODEX_PVC = 'liveprobe-application-codex'
CODEX_HOME = '/var/lib/liveprobe/application-codex'
IMAGE_PATTERN = re.compile(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[a-f0-9]{64}')
# 2026-11-13: runner 쓰기 권한은 활성 환경 dev/prd에만 둔다. 기존 파일 번호(dev 50/51, prd 54/55)를 유지하고
# 퇴역한 stg 번호 52/53은 비워 둔다. 이 목록은 권한을 넓히지 않도록 catalog를 따라 자동 확장하지 않는다.
RUNNER_ENVIRONMENTS = (('dev', 50), ('prd', 54))


def resource(kind, name, *, namespace=NAMESPACE, spec=None, **fields):
    version = ('apps/v1' if kind in ('Deployment', 'StatefulSet') else
               'networking.k8s.io/v1' if kind in ('Ingress', 'NetworkPolicy') else
               'rbac.authorization.k8s.io/v1' if kind in ('Role', 'RoleBinding') else 'v1')
    doc = {'apiVersion': version, 'kind': kind,
           'metadata': {'name': name, 'namespace': namespace, 'labels': dict(PART_OF)}}
    if spec is not None:
        doc['spec'] = spec
    doc.update(fields)
    return doc


def render(image):
    if not isinstance(image, str) or not IMAGE_PATTERN.fullmatch(image):
        raise ValueError('image must be a repository@sha256:64-lowercase-hex reference')
    profiles = (ROOT / 'deploy/samsung/application-profiles.json').read_bytes()
    catalog = (ROOT / 'deploy/samsung/system-catalog.json').read_bytes()
    for raw in (profiles, catalog):
        if not isinstance(json.loads(raw), dict):
            raise ValueError('configuration must be a JSON object')
    # 실패 폐쇄: runner 권한 대상은 catalog의 활성 환경이어야 하고 퇴역 환경이면 렌더하지 않는다.
    catalog_doc = json.loads(catalog)
    active = catalog_doc.get('environments', [])
    retired = catalog_doc.get('retired_environments', [])
    if not isinstance(active, list) or not isinstance(retired, list) or any(
            namespace not in active or namespace in retired for namespace, _ in RUNNER_ENVIRONMENTS):
        raise ValueError('runner namespaces must be active, non-retired catalog environments')
    config_hash = hashlib.sha256(profiles + b'\x00' + catalog).hexdigest()
    api_labels = {'app': API, **PART_OF}
    db_labels = {'app': DB, **PART_OF}
    docs = {}
    docs['00-api-serviceaccount.yaml'] = resource('ServiceAccount', API, automountServiceAccountToken=False)
    docs['01-runner-serviceaccount.yaml'] = resource('ServiceAccount', RUNNER, automountServiceAccountToken=False)
    docs['10-configmap.yaml'] = resource('ConfigMap', 'liveprobe-application-config', data={
        'application-profiles.json': profiles.decode('utf-8'), 'system-catalog.json': catalog.decode('utf-8')})
    docs['20-db-service.yaml'] = resource('Service', DB, spec={
        'clusterIP': 'None', 'selector': {'app': DB},
        'ports': [{'name': 'postgres', 'port': 5432, 'targetPort': 'postgres'}]})
    docs['21-database.yaml'] = resource('StatefulSet', DB, spec={
        'serviceName': DB, 'replicas': 1,
        'persistentVolumeClaimRetentionPolicy': {'whenDeleted': 'Retain', 'whenScaled': 'Retain'},
        'selector': {'matchLabels': {'app': DB}},
        'template': {'metadata': {'labels': db_labels}, 'spec': {
            'automountServiceAccountToken': False,
            'securityContext': {'fsGroup': 999},
            'containers': [{'name': 'postgres', 'image': 'pgvector/pgvector:0.8.6-pg17-bookworm',
                'imagePullPolicy': 'IfNotPresent', 'ports': [{'name': 'postgres', 'containerPort': 5432}],
                'env': [{'name': 'POSTGRES_DB', 'value': 'liveprobe_application'},
                        {'name': 'POSTGRES_USER', 'value': 'liveprobe_application'},
                        {'name': 'POSTGRES_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': DB, 'key': 'password'}}},
                        {'name': 'PGDATA', 'value': '/var/lib/postgresql/data/pgdata'}],
                'readinessProbe': {'exec': {'command': ['pg_isready', '-U', 'liveprobe_application', '-d', 'liveprobe_application']},
                                   'initialDelaySeconds': 5, 'periodSeconds': 10, 'timeoutSeconds': 5},
                'livenessProbe': {'exec': {'command': ['pg_isready', '-U', 'liveprobe_application', '-d', 'liveprobe_application']},
                                  'initialDelaySeconds': 30, 'periodSeconds': 20, 'timeoutSeconds': 5, 'failureThreshold': 5},
                'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
                'volumeMounts': [{'name': 'data', 'mountPath': '/var/lib/postgresql/data'}]}]}},
        'volumeClaimTemplates': [{'metadata': {'name': 'data', 'labels': db_labels}, 'spec': {
            'accessModes': ['ReadWriteOnce'], 'storageClassName': 'local-path',
            'resources': {'requests': {'storage': '1Gi'}}}}]})
    env = {
        'APPLICATION_DATABASE_URL_FILE': '/run/application/database/url',
        'APPLICATION_IDENTITY_FILE': '/run/application/identity/human.json',
        'APPLICATION_MACHINE_IDENTITY_FILE': '/run/application/identity/machine.json',
        'APPLICATION_PROFILES_FILE': '/etc/application/application-profiles.json',
        'APPLICATION_CATALOG_FILE': '/etc/application/system-catalog.json',
        'APPLICATION_CODEX_HOME': CODEX_HOME,
        'APPLICATION_WORKER_ENABLED': 'true', 'APPLICATION_DATABASE_ISOLATED': 'true',
        'APPLICATION_MODEL': 'gpt-6.2-sol',
        'APPLICATION_CLASSIFIER_MODEL': 'gpt-6-luna', 'APPLICATION_CRITIC_MODEL': 'gpt-6.2-astra'}
    docs['30-api.yaml'] = resource('Deployment', API, spec={
        'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': API}},
        'template': {'metadata': {'labels': api_labels, 'annotations': {'liveprobe.io/application-config-sha256': config_hash}}, 'spec': {
            'serviceAccountName': API, 'automountServiceAccountToken': False,
            'containers': [{'name': 'api', 'image': image, 'imagePullPolicy': 'IfNotPresent',
                'ports': [{'name': 'http', 'containerPort': 8080}],
                'env': [{'name': name, 'value': value} for name, value in env.items()],
                # 인증 접근 실패는 권한 완화로 우회하지 않는다. 준비되지 않은 인증 경로는 배포 차단 항목이다.
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
                                    'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                    'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}},
                'readinessProbe': {'httpGet': {'path': '/api/application/v1/healthz', 'port': 'http'},
                                   'initialDelaySeconds': 5, 'periodSeconds': 10},
                'livenessProbe': {'httpGet': {'path': '/api/application/v1/healthz', 'port': 'http'},
                                  'initialDelaySeconds': 30, 'periodSeconds': 20},
                'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '2', 'memory': '1536Mi'}},
                'volumeMounts': [
                    {'name': 'identity', 'mountPath': '/run/application/identity', 'readOnly': True},
                    {'name': 'database', 'mountPath': '/run/application/database', 'readOnly': True},
                    {'name': 'config', 'mountPath': '/etc/application', 'readOnly': True},
                    # 새 서비스 전용 구독 CLI 홈. 로그인·토큰 갱신을 위해 쓰기 가능하며 기존 관제 인증과 공유하지 않는다.
                    {'name': 'codex-home', 'mountPath': CODEX_HOME},
                    {'name': 'tmp', 'mountPath': '/tmp'}]}],
            # 기존 공유 인증 PVC(liveprobe-codex-spare)는 마운트하지 않는다. fsGroup·seed/chown init container도 쓰지 않는다.
            'volumes': [
                {'name': 'identity', 'secret': {'secretName': 'liveprobe-application-identity', 'defaultMode': 292,
                    'items': [{'key': 'human.json', 'path': 'human.json'}, {'key': 'machine.json', 'path': 'machine.json'}]}},
                {'name': 'database', 'secret': {'secretName': 'liveprobe-application-db-url', 'defaultMode': 292,
                    'items': [{'key': 'url', 'path': 'url'}]}},
                {'name': 'config', 'configMap': {'name': 'liveprobe-application-config'}},
                {'name': 'codex-home', 'persistentVolumeClaim': {'claimName': CODEX_PVC}},
                {'name': 'tmp', 'emptyDir': {'sizeLimit': '256Mi'}}]}}})
    docs['29-codex-pvc.yaml'] = resource('PersistentVolumeClaim', CODEX_PVC, spec={
        'accessModes': ['ReadWriteOnce'], 'storageClassName': 'local-path', 'resources': {'requests': {'storage': '1Gi'}}})
    docs['31-api-service.yaml'] = resource('Service', API, spec={
        'selector': {'app': API}, 'ports': [{'name': 'http', 'port': 8080, 'targetPort': 'http'}]})
    docs['32-ingress.yaml'] = resource('Ingress', API, spec={'ingressClassName': 'traefik', 'rules': [
        {'host': 'application.liveprobe.localhost', 'http': {'paths': [
            {'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': API, 'port': {'number': 8080}}}}]}}]})
    dns = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
           'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}
    docs['40-api-networkpolicy.yaml'] = resource('NetworkPolicy', API, spec={
        'podSelector': {'matchLabels': {'app': API}}, 'policyTypes': ['Ingress', 'Egress'],
        'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                              'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'traefik'}}}],
                     'ports': [{'protocol': 'TCP', 'port': 8080}]}],
        'egress': [
            {'to': [dns], 'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
            {'to': [{'podSelector': {'matchLabels': {'app': DB}}}], 'ports': [{'protocol': 'TCP', 'port': 5432}]},
            {'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': [
                '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10', '127.0.0.0/8']}}],
             'ports': [{'protocol': 'TCP', 'port': 443}]}]})
    docs['41-db-networkpolicy.yaml'] = resource('NetworkPolicy', DB, spec={
        'podSelector': {'matchLabels': {'app': DB}}, 'policyTypes': ['Ingress', 'Egress'],
        'ingress': [{'from': [{'podSelector': {'matchLabels': {'app': API}}}], 'ports': [{'protocol': 'TCP', 'port': 5432}]}],
        'egress': []})
    # 2026-11-13: runner Role/RoleBinding은 활성 환경 dev/prd에만 렌더한다(권한 축소). 파일 번호 체계는 그대로 두어
    # prd는 54/55를 유지하고, 퇴역한 stg 번호 52/53은 더 이상 생성하지 않는다. 클러스터에 이미 있는 stg
    # Role/RoleBinding은 이 렌더로 지워지지 않으며 부모가 이름·UID 확인 후 개별 퇴역한다.
    for namespace, number in RUNNER_ENVIRONMENTS:
        docs[f'{number}-runner-{namespace}-role.yaml'] = resource('Role', RUNNER, namespace=namespace, rules=[
            {'apiGroups': [''], 'resources': ['pods', 'events'], 'verbs': ['get', 'list']},
            {'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']},
            {'apiGroups': ['apps'], 'resources': ['replicasets'], 'verbs': ['get', 'list']},
            {'apiGroups': ['apps'], 'resources': ['deployments'], 'verbs': ['get', 'list', 'watch']},
            {'apiGroups': ['apps'], 'resources': ['deployments'], 'verbs': ['patch'],
             'resourceNames': sorted(json.loads(catalog)['service_to_system'])},
            # 업무 확인 전용: 등록 서비스의 http 8080 프록시 GET만. 서비스 조회·수정·https 내부 포트 없음.
            {'apiGroups': [''], 'resources': ['services/proxy'], 'verbs': ['get'],
             'resourceNames': [f'http:{name}:8080' for name in sorted(json.loads(catalog)['service_to_system'])]}])
        docs[f'{number + 1}-runner-{namespace}-binding.yaml'] = resource('RoleBinding', RUNNER, namespace=namespace,
            roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': RUNNER},
            subjects=[{'kind': 'ServiceAccount', 'name': RUNNER, 'namespace': NAMESPACE}])
    return {name: json.dumps(doc, ensure_ascii=False, indent=2) + '\n' for name, doc in sorted(docs.items())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', type=Path, default=ROOT / 'deploy/application/rendered')
    parser.add_argument('--check', action='store_true', help='compare only; never create or rewrite files')
    args = parser.parse_args(argv)
    try:
        expected = render(args.image)
        existing = {p.name for p in args.out.glob('*') if p.suffix in ('.yaml', '.yml')}
        if args.check:
            same = existing == set(expected) and all(
                not (args.out / name).is_symlink() and (args.out / name).read_bytes() == text.encode('utf-8')
                for name, text in expected.items())
            if not same:
                print('render check failed: missing, changed, or extra YAML', file=sys.stderr)
                return 1
        else:
            if existing - set(expected) or args.out.is_symlink():
                raise ValueError('unexpected output entries')
            args.out.mkdir(parents=True, exist_ok=True)
            for name, text in expected.items():
                path = args.out / name
                if path.is_symlink():
                    raise ValueError('symlink output refused')
                path.write_text(text, encoding='utf-8')
        print('render check passed' if args.check else f'rendered {len(expected)} public resource documents')
        return 0
    except (OSError, ValueError, UnicodeError):
        print('render failed: verify digest image, public JSON inputs and output directory', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
