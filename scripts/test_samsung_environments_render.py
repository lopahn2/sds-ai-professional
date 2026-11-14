"""삼성 선언 생성기의 실제 출력과 실패 폐쇄 계약을 검사한다."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/render-samsung-environments.py'
CATALOG = json.loads((ROOT / 'deploy/samsung/system-catalog.json').read_text())
OWNERS = CATALOG['service_to_system']


def renderer():
    spec = importlib.util.spec_from_file_location('samsung_renderer', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def images():
    # 합성 digest는 순수 단위시험 입력이며 배포 이미지 영수증이 아니다.
    return {name: 'liveprobe/' + name + '@sha256:' + 'a' * 64 for name in OWNERS}


def find(docs, kind, ns, name):
    return next(d for d in docs if d['kind'] == kind and
                d['metadata'].get('namespace') == ns and d['metadata']['name'] == name)


class RenderContract(unittest.TestCase):
    def test_catalog_ownership_and_three_environment_resources(self):
        self.assertTrue(SCRIPT.exists(), '삼성 카탈로그 기반 선언 생성기가 아직 없다')
        module = renderer()
        docs = module.render(images())
        self.assertEqual(set(module.SERVICES), set(OWNERS))
        # 2026-11-13 dev/prd 전환: catalog의 활성 환경이 dev/prd 두 개이고 stg는 retired_environments(이력 전용)다.
        # 선언 생성기는 활성 환경만 렌더하므로 Namespace 2개, Deployment 9×2=18, Ingress 8×2=16이다(이전 3환경 27/24).
        self.assertEqual(CATALOG['environments'], ['dev', 'prd'])
        self.assertEqual(CATALOG.get('retired_environments'), ['stg'])
        self.assertEqual([d['metadata']['name'] for d in docs if d['kind'] == 'Namespace'], ['dev', 'prd'])
        self.assertEqual(len([d for d in docs if d['kind'] == 'Deployment']), 18)
        self.assertEqual(len([d for d in docs if d['kind'] == 'Ingress']), 16)
        # 퇴역 환경에는 어떤 실행 선언도 만들지 않는다(기존 stg 자원은 부모가 개별 퇴역, 데이터·Secret 보존).
        self.assertFalse([d for d in docs if d['metadata'].get('namespace') == 'stg' or
                          (d['kind'] == 'Namespace' and d['metadata']['name'] == 'stg')])
        self.assertFalse(any(d['kind'] in ('Secret', 'StatefulSet', 'PersistentVolumeClaim', 'ClusterRole', 'ClusterRoleBinding') for d in docs))
        for ns in CATALOG['environments']:
            for service, system in OWNERS.items():
                labels = {'app': service, 'app.kubernetes.io/part-of': system,
                          'liveprobe.io/environment': ns, 'liveprobe.io/managed': 'true',
                          'liveprobe.io/lifecycle': 'current'}
                for kind, name in [('Deployment', service), ('Service', service), ('ConfigMap', service + '-config')]:
                    self.assertEqual(find(docs, kind, ns, name)['metadata']['labels'], labels)
                deployment = find(docs, 'Deployment', ns, service)
                self.assertEqual(deployment['spec']['template']['metadata']['labels'], labels)
                pod = deployment['spec']['template']['spec']
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertFalse(any('hostPath' in v for v in pod['volumes']))
                container = pod['containers'][0]
                self.assertEqual(container['name'], service)
                self.assertEqual(container['image'], images()[service])
                self.assertTrue(container['securityContext']['runAsNonRoot'])
                self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
                if service == 'delivery':
                    self.assertEqual(container['securityContext']['runAsUser'], 1000)
                data = find(docs, 'ConfigMap', ns, service + '-config')['data']
                self.assertEqual((data['SYSTEM_NAME'], data['SERVICE_NAME'], data['APP_ENV']), (system, service, ns))
                self.assertEqual(data['SELF_TRAFFIC'], '0')
                if service != 'inventory':
                    ingress = find(docs, 'Ingress', ns, service)
                    self.assertEqual(ingress['metadata']['labels'], labels)
                    rule = ingress['spec']['rules'][0]
                    self.assertEqual(rule['host'], f'{service}.{system}.{ns}.localhost')
                    self.assertEqual(rule['http']['paths'][0]['backend']['service'], {'name': service, 'port': {'number': 8080}})

    def test_inventory_cutover_is_explicit_environment_scoped_and_reproducible(self):
        module = renderer()
        before = module.render(images())
        try:
            after = module.render(images(), inventory_migrated_envs=('dev',))
        except TypeError as exc:
            self.fail(f'환경별 검증 완료 입력이 아직 지원되지 않는다: {exc}')
        label = 'liveprobe.io/database'
        for ns in CATALOG['environments']:
            for service in OWNERS:
                old = find(before, 'Deployment', ns, service)['spec']['template']['metadata']['labels']
                new = find(after, 'Deployment', ns, service)['spec']['template']['metadata']['labels']
                self.assertNotIn(label, old)
                expected = dict(old)
                if (ns, service) == ('dev', 'inventory'):
                    expected[label] = 'samsung-trade'
                self.assertEqual(new, expected)
                for kind, name in [('Deployment', service), ('Service', service), ('ConfigMap', service + '-config')]:
                    self.assertEqual(find(after, kind, ns, name)['metadata'], find(before, kind, ns, name)['metadata'])
                self.assertEqual(find(after, 'Service', ns, service)['spec'], find(before, 'Service', ns, service)['spec'])
        module.validate(after, inventory_migrated_envs=('dev',))
        self.assertEqual(module.render(images(), inventory_migrated_envs=('dev',)), after)
        self.assertEqual(module.render(images(), existing_resources=after), before)

    def test_inventory_cutover_validation_keeps_exact_ownership_and_label_allowlist(self):
        module = renderer()
        good = module.render(images(), inventory_migrated_envs=('dev',))
        with self.assertRaises(ValueError):
            module.validate(good)
        with self.assertRaises(ValueError):
            module.validate(module.render(images()), inventory_migrated_envs=('dev',))
        for invalid in ('dev', ['qa'], ['dev', None], None, {'dev': True}, ['stg'], ['dev', 'stg']):
            # 2026-11-13: 퇴역한 stg도 이관 완료 환경으로 받지 않는다(퇴역 환경 실행 거부).
            with self.subTest(environments=invalid), self.assertRaises(ValueError):
                module.render(images(), inventory_migrated_envs=invalid)
            with self.subTest(validate_environments=invalid), self.assertRaises(ValueError):
                module.validate(good, inventory_migrated_envs=invalid)
        mutations = []
        # 2026-11-13: 이관하지 않은 다른 활성 환경의 inventory 라벨 거부 사례를 stg 대신 prd로 검사한다(stg는 더 이상 렌더되지 않음).
        for ns, service, key, value in [
                ('prd', 'inventory', 'liveprobe.io/database', 'samsung-trade'),
                ('dev', 'order', 'liveprobe.io/database', 'samsung-trade'),
                ('dev', 'inventory', 'liveprobe.io/database', 'samsung-business'),
                ('dev', 'inventory', 'arbitrary', 'true'),
                ('dev', 'inventory', 'app.kubernetes.io/part-of', 'samsung-business')]:
            docs = copy.deepcopy(good)
            find(docs, 'Deployment', ns, service)['spec']['template']['metadata']['labels'][key] = value
            mutations.append(docs)
        for kind, name in [('Deployment', 'inventory'), ('Service', 'inventory'), ('ConfigMap', 'inventory-config')]:
            for key, value in [('liveprobe.io/database', 'samsung-trade'), ('arbitrary', 'true')]:
                docs = copy.deepcopy(good)
                find(docs, kind, 'dev', name)['metadata']['labels'][key] = value
                mutations.append(docs)
        docs = copy.deepcopy(good)
        find(docs, 'Service', 'dev', 'inventory')['spec']['selector']['liveprobe.io/database'] = 'samsung-trade'
        mutations.append(docs)
        for docs in mutations:
            with self.subTest(), self.assertRaises(ValueError):
                module.validate(docs, inventory_migrated_envs=('dev',))

    def test_inventory_cutover_cli_and_report_reapply_only_declared_environments(self):
        scratch_root = Path.home() / '.hermes/cache/scratch'
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch_root) as scratch:
            folder = Path(scratch)
            source, output, report = (folder / name for name in ('images.json', 'apps.json', 'changes.json'))
            source.write_text(json.dumps(images()))
            args = [sys.executable, '-B', str(SCRIPT), '--images', str(source), '--output', str(output), '--changes-output', str(report)]
            # 2026-11-13 dev/prd 전환: 활성 환경 두 개(dev, prd)를 명시 선택한다. 퇴역한 stg는 아래에서 거부를 검사한다.
            selected = args + ['--inventory-migrated-env', 'dev', '--inventory-migrated-env', 'prd']
            result = subprocess.run(selected, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = output.read_bytes()
            docs = json.loads(first)['items']
            renderer().validate(docs, inventory_migrated_envs=('dev', 'prd'))
            changes = json.loads(report.read_text())
            self.assertEqual(changes['inventory_cutover']['migrated_environments'], ['dev', 'prd'])
            self.assertEqual(changes['inventory_cutover']['pod_label'], {'liveprobe.io/database': 'samsung-trade'})
            self.assertTrue(changes['inventory_cutover']['requirements'])
            result = subprocess.run(selected, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), first)
            invalid = folder / 'invalid.json'
            result = subprocess.run(args[:args.index('--output') + 1] + [str(invalid), '--inventory-migrated-env', 'qa'], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(invalid.exists())
            # 퇴역 환경 stg를 실행 대상으로 지정하면 출력 없이 거부한다.
            retired = folder / 'retired.json'
            result = subprocess.run(args[:args.index('--output') + 1] + [str(retired), '--inventory-migrated-env', 'stg'], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(retired.exists())
            result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text())['inventory_cutover']['migrated_environments'], [])
            renderer().validate(json.loads(output.read_text())['items'])

    def test_internal_dependencies_and_inventory_database(self):
        docs = renderer().render(images())
        for ns in CATALOG['environments']:
            for service, dependencies in CATALOG['dependencies'].items():
                data = find(docs, 'ConfigMap', ns, service + '-config')['data']
                for key, target in dependencies.items():
                    self.assertEqual(data.get(key), f'http://{target}.{ns}.svc:8080')
                    self.assertEqual(OWNERS[service], OWNERS[target])
            inventory = find(docs, 'ConfigMap', ns, 'inventory-config')['data']
            self.assertEqual(inventory.get('INVENTORY_DB_URL'), f'jdbc:postgresql://trade-db.liveprobe-data.svc:5432/inventory_{ns}')
            self.assertEqual(inventory['INVENTORY_DB_USER'], f'inventory_{ns}')
            for service in ('inventory', 'order'):
                env = find(docs, 'Deployment', ns, service)['spec']['template']['spec']['containers'][0]['env']
                by_name = {v['name']: v for v in env}
                self.assertEqual(by_name['CHAOS_OPERATOR_TOKEN']['valueFrom']['secretKeyRef'], {'name': service + '-chaos-operator', 'key': 'token'})
                if service == 'inventory':
                    self.assertEqual(by_name['INVENTORY_DB_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'samsung-inventory-db', 'key': 'password'})
        self.assertNotIn('http://member.', str(docs))
        self.assertNotIn("'name': 'inventory-db'", str(docs))

    def test_domain_modes_internal_tls_and_secret_file_mounts(self):
        docs = renderer().render(images())
        common = {'DATABASE_DSN_FILE': '/var/run/samsung/db/dsn',
                  'KAFKA_CONFIG_FILE': '/var/run/samsung/kafka/client.json',
                  'KAFKA_CA_FILE': '/var/run/samsung/kafka/ca.crt',
                  'API_CREDENTIALS_FILE': '/var/run/samsung/api/credentials.json',
                  'TLS_CERT_FILE': '/var/run/samsung/tls/tls.crt',
                  'TLS_KEY_FILE': '/var/run/samsung/tls/tls.key',
                  'READ_ONLY_MODE': '1', 'HTTP_PORT': '8080', 'TLS_PORT': '8443'}
        for ns in CATALOG['environments']:
            for service, mode in CATALOG['domain_modes'].items():
                data = find(docs, 'ConfigMap', ns, service + '-config')['data']
                self.assertEqual(data.get('DOMAIN_MODE'), mode)
                for key, value in common.items():
                    self.assertEqual(data.get(key), value)
                if mode != 'business':
                    self.assertEqual(data.get('BUSINESS_API_URL'), f'https://business-api.{ns}.svc:8443')
                    self.assertEqual(data.get('BUSINESS_TOKEN_FILE'), '/var/run/samsung/api/reader-token')
                    self.assertEqual(data.get('BUSINESS_CA_FILE'), '/var/run/samsung/tls/ca.crt')
                else:
                    self.assertNotIn('BUSINESS_TOKEN_FILE', data)
                pod = find(docs, 'Deployment', ns, service)['spec']['template']['spec']
                container = pod['containers'][0]
                self.assertIn({'name': 'https-internal', 'containerPort': 8443}, container['ports'])
                ports = find(docs, 'Service', ns, service)['spec']['ports']
                self.assertIn({'name': 'https-internal', 'port': 8443, 'targetPort': 8443}, ports)
                volumes = {v['name']: v for v in pod['volumes']}
                mounts = {m['name']: m for m in container['volumeMounts']}
                for suffix, keys in [('db', {'dsn'}), ('kafka', {'client.json', 'ca.crt'}),
                                     ('api', {'credentials.json'} if mode == 'business' else {'credentials.json', 'reader-token'}),
                                     ('tls', {'ca.crt', 'tls.crt', 'tls.key'})]:
                    volume = volumes['samsung-' + suffix]
                    self.assertEqual(volume['secret']['secretName'], service + '-' + suffix)
                    self.assertEqual({i['key'] for i in volume['secret']['items']}, keys)
                    self.assertEqual({i['path'] for i in volume['secret']['items']}, keys)
                    self.assertEqual(volume['secret']['defaultMode'], 0o440)
                    self.assertEqual(mounts['samsung-' + suffix]['mountPath'], '/var/run/samsung/' + suffix)
                    self.assertTrue(mounts['samsung-' + suffix]['readOnly'])
                ingress = find(docs, 'Ingress', ns, service)
                self.assertNotIn('8443', str(ingress))

    def test_network_policies_allow_only_system_and_contract_paths(self):
        docs = renderer().render(images())
        policies = [d for d in docs if d['kind'] == 'NetworkPolicy']
        self.assertTrue(policies, '시스템 경계를 강제할 네트워크 정책이 없다')

        def matches(selector, labels):
            if any(labels.get(k) != v for k, v in selector.get('matchLabels', {}).items()):
                return False
            for expr in selector.get('matchExpressions', []):
                key, op, values = expr['key'], expr['operator'], expr.get('values', [])
                if op == 'In' and labels.get(key) not in values:
                    return False
                if op == 'NotIn' and labels.get(key) in values:
                    return False
            return True

        def pod(ns, service, system=None):
            labels = {'app': service}
            if service in OWNERS:
                labels.update({'app.kubernetes.io/part-of': system or OWNERS[service],
                               'liveprobe.io/managed': 'true', 'liveprobe.io/environment': ns,
                               'liveprobe.io/lifecycle': 'current'})
            if service == 'traefik':
                labels['app.kubernetes.io/name'] = 'traefik'
            if service == 'kube-dns':
                labels['k8s-app'] = 'kube-dns'
            return ns, labels

        def allows(direction, source, target, port, protocol='TCP'):
            local, remote = (source, target) if direction == 'egress' else (target, source)
            for policy in policies:
                spec = policy['spec']
                if policy['metadata']['namespace'] != local[0] or not matches(spec['podSelector'], local[1]):
                    continue
                for rule in spec.get(direction, []):
                    if not any(p.get('port') == port and p.get('protocol', 'TCP') == protocol for p in rule.get('ports', [])):
                        continue
                    for peer in rule.get('to' if direction == 'egress' else 'from', []):
                        ns_labels = {'kubernetes.io/metadata.name': remote[0], 'liveprobe.io/environment': remote[0]}
                        if 'namespaceSelector' in peer:
                            if not matches(peer['namespaceSelector'], ns_labels):
                                continue
                        elif remote[0] != local[0]:
                            continue
                        if matches(peer.get('podSelector', {}), remote[1]):
                            return True
            return False

        for ns in CATALOG['environments']:
            deny = find(docs, 'NetworkPolicy', ns, 'default-deny-all')['spec']
            self.assertEqual(deny, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']})
            self.assertFalse(any(p['metadata']['name'] == 'allow-same-namespace' for p in policies))
            for source in OWNERS:
                for target in OWNERS:
                    for direction in ('ingress', 'egress'):
                        self.assertEqual(allows(direction, pod(ns, source), pod(ns, target), 8080), OWNERS[source] == OWNERS[target], (ns, source, target, direction))
                        self.assertFalse(allows(direction, pod(ns, source), pod('stg' if ns != 'stg' else 'dev', target), 8080))
                self.assertTrue(allows('egress', pod(ns, source), pod('kube-system', 'kube-dns'), 53, 'UDP'))
                self.assertTrue(allows('egress', pod(ns, source), pod('kube-system', 'kube-dns'), 53))
                self.assertFalse(allows('egress', pod(ns, source), pod('outside', 'unknown'), 443))
                self.assertEqual(allows('ingress', pod('kube-system', 'traefik'), pod(ns, source), 8080), source != 'inventory')
                self.assertFalse(allows('ingress', pod('kube-system', 'traefik'), pod(ns, source), 8443))
                for observer in [('liveprobe-observability', 'prometheus'), ('liveprobe-system', 'liveprobe-api')]:
                    self.assertTrue(allows('ingress', pod(*observer), pod(ns, source), 8080))
                self.assertEqual(allows('egress', pod(ns, source), pod('liveprobe-messaging', 'kafka'), 9092), source in CATALOG['domain_modes'])
                self.assertEqual(allows('egress', pod(ns, source), pod('istio-system', 'istiod'), 15012), ns == 'prd')
            for source, database in [('inventory', 'trade-db'), ('trade-member', 'trade-db'), ('delivery-member', 'delivery-db'), ('business-api', 'business-db')]:
                self.assertTrue(allows('egress', pod(ns, source), pod('liveprobe-data', database), 5432))
                self.assertTrue(allows('ingress', pod(ns, source), pod('liveprobe-data', database), 5432))
                other = 'business-db' if database != 'business-db' else 'trade-db'
                self.assertFalse(allows('egress', pod(ns, source), pod('liveprobe-data', other), 5432))
                self.assertFalse(allows('ingress', pod(ns, source), pod('liveprobe-data', other), 5432))
            for source in CATALOG['domain_modes']:
                self.assertTrue(allows('ingress', pod(ns, source), pod('liveprobe-messaging', 'kafka'), 9092))
            for source in OWNERS:
                for direction in ('ingress', 'egress'):
                    permitted = source in ('trade-member', 'delivery-member', 'business-api')
                    self.assertEqual(allows(direction, pod(ns, source), pod(ns, 'business-api'), 8443), permitted)
                    if source in ('trade-member', 'delivery-member'):
                        self.assertFalse(allows(direction, pod(ns, source), pod('stg' if ns != 'stg' else 'dev', 'business-api'), 8443))

    def test_exact_service_images_and_explicit_local_tag_opt_in(self):
        module = renderer()
        for invalid in [{}, dict(images(), member='liveprobe/member@sha256:' + 'a' * 64),
                        dict(images(), order='liveprobe/order:latest'), dict(images(), order=None),
                        dict(images(), order='liveprobe/order@sha256:' + 'a' * 63),
                        dict(images(), order='liveprobe/order@sha256:' + 'A' * 64)]:
            with self.subTest(images=invalid):
                with self.assertRaises(ValueError):
                    module.render(invalid)
        local = {name: 'liveprobe/' + name + ':samsung-validation' for name in OWNERS}
        with self.assertRaises(ValueError):
            module.render(local)
        docs = module.render(local, allow_local_tags=True)
        self.assertEqual(find(docs, 'Deployment', 'dev', 'order')['spec']['template']['spec']['containers'][0]['image'], local['order'])
        for invalid in ['liveprobe/order:latest', 'untrusted/order:samsung', 'liveprobe/order@sha256:bad', 'liveprobe/order']:
            with self.subTest(image=invalid), self.assertRaises(ValueError):
                module.render(dict(local, order=invalid), allow_local_tags=True)
        with self.assertRaises(ValueError):
            module.render(images(), app_version='')

    def test_catalog_refuses_ambiguous_or_cross_system_dependencies(self):
        module = renderer()
        mutations = []
        cross = copy.deepcopy(CATALOG)
        cross['dependencies']['payment']['MEMBER_URL'] = 'delivery-member'
        mutations.append(cross)
        missing = copy.deepcopy(CATALOG)
        missing['dependencies']['payment']['MEMBER_URL'] = 'member'
        mutations.append(missing)
        duplicate = copy.deepcopy(CATALOG)
        duplicate['systems'][1]['services'].append('order')
        mutations.append(duplicate)
        owner = copy.deepcopy(CATALOG)
        owner['service_to_system']['order'] = 'samsung-business'
        mutations.append(owner)
        for catalog in mutations:
            with self.subTest(catalog=catalog), patch.object(module, 'CATALOG', catalog):
                with self.assertRaises(ValueError):
                    module.render(images())

    def test_validate_refuses_ingress_conflicts_missing_backends_and_misownership(self):
        module = renderer()
        good = module.render(images())
        self.assertIsNone(module.validate(good))
        mutations = []
        duplicated = copy.deepcopy(good)
        duplicate = copy.deepcopy(find(duplicated, 'Ingress', 'dev', 'order'))
        duplicate['metadata']['name'] = 'order-duplicate'
        duplicated.append(duplicate)
        mutations.append(duplicated)
        missing = [d for d in copy.deepcopy(good) if not (d['kind'] == 'Service' and d['metadata'].get('namespace') == 'dev' and d['metadata']['name'] == 'order')]
        mutations.append(missing)
        for backend, port in [('notification', 8080), ('inventory', 8080), ('order', 8443)]:
            docs = copy.deepcopy(good)
            find(docs, 'Ingress', 'dev', 'order')['spec']['rules'][0]['http']['paths'][0]['backend']['service'] = {'name': backend, 'port': {'number': port}}
            mutations.append(docs)
        owner = copy.deepcopy(good)
        find(owner, 'Service', 'dev', 'order')['metadata']['labels']['app.kubernetes.io/part-of'] = 'samsung-delivery'
        mutations.append(owner)
        template = copy.deepcopy(good)
        find(template, 'Deployment', 'dev', 'order')['spec']['template']['metadata']['labels']['app.kubernetes.io/part-of'] = 'samsung-delivery'
        mutations.append(template)
        dependency = copy.deepcopy(good)
        find(dependency, 'ConfigMap', 'dev', 'payment-config')['data']['MEMBER_URL'] = 'http://delivery-member.dev.svc:8080'
        mutations.append(dependency)
        selector = copy.deepcopy(good)
        find(selector, 'Service', 'dev', 'order')['spec']['selector'] = {'app': 'notification'}
        mutations.append(selector)
        target_port = copy.deepcopy(good)
        find(target_port, 'Service', 'dev', 'order')['spec']['ports'][0]['targetPort'] = 8443
        mutations.append(target_port)
        empty = copy.deepcopy(good)
        find(empty, 'Ingress', 'dev', 'order')['spec']['rules'] = []
        mutations.append(empty)
        default_backend = copy.deepcopy(good)
        find(default_backend, 'Ingress', 'dev', 'order')['spec']['defaultBackend'] = {'service': {'name': 'business-api', 'port': {'number': 8443}}}
        mutations.append(default_backend)
        for docs in mutations:
            with self.subTest(), self.assertRaises(ValueError):
                module.validate(docs)
        existing = copy.deepcopy(find(good, 'Ingress', 'dev', 'order'))
        existing['metadata']['name'] = 'legacy-order'
        with self.assertRaises(ValueError):
            module.render(images(), existing_resources=[existing])
        existing['spec']['rules'][0]['host'] = 'legacy-order.dev.localhost'
        module.render(images(), existing_resources=[existing])

    def test_existing_snapshot_does_not_count_same_resource_update_as_collision(self):
        module = renderer()
        docs = module.render(images())
        previous = copy.deepcopy(find(docs, 'Ingress', 'dev', 'order'))
        self.assertEqual(len(module.render(images(), existing_resources=[previous])), len(docs))

    def test_cli_generates_valid_list_and_explicit_migration_report(self):
        scratch_root = Path.home() / '.hermes/cache/scratch'
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch_root) as scratch:
            folder = Path(scratch)
            input_path = folder / 'images.json'
            output = folder / 'apps.json'
            report = folder / 'changes.json'
            input_path.write_text(json.dumps(images()))
            args = [sys.executable, '-B', str(SCRIPT), '--images', str(input_path), '--output', str(output), '--changes-output', str(report)]
            result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = json.loads(output.read_text())
            self.assertEqual((rendered['apiVersion'], rendered['kind']), ('v1', 'List'))
            renderer().validate(rendered['items'])
            changes = json.loads(report.read_text())
            self.assertEqual(changes['state'], 'not_applied')
            self.assertEqual(changes['image_policy'], 'digest')
            self.assertEqual(changes['retired_labels'], {'liveprobe.io/lifecycle': 'retired'})
            required = {(ns, 'allow-same-namespace') for ns in CATALOG['environments']}
            deletion = {(d['namespace'], d['name']) for d in changes['policies_requiring_explicit_removal']}
            self.assertTrue(required <= deletion)
            self.assertTrue(all(d['kind'] != 'Secret' for d in rendered['items']))
            input_path.write_text(json.dumps({'order': 'liveprobe/order:bad'}))
            rejected_output = folder / 'rejected.json'
            result = subprocess.run(args[:args.index('--output') + 1] + [str(rejected_output)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(rejected_output.exists())

    def test_liveprobe_dependencies_change_without_other_configuration_changes(self):
        text = (ROOT / 'deploy/environments/liveprobe-config.yaml').read_text()
        expected = 'dependencies:\n'
        for source, mapping in CATALOG['dependencies'].items():
            expected += f'  {source}:\n' + ''.join(f'  - {target}\n' for target in mapping.values())
        self.assertEqual(text[:text.index('llm:')], expected)
        # 2026-11-13 dev/prd 전환으로 'llm:' 이후를 의도적으로 바꿨다: target_namespaces [dev,stg,prd]→[dev,prd],
        # historical_namespaces [stg] 추가, recovery.simple_reanalysis_level NORMAL 추가(+설명 주석).
        # llm/change_auto_create 값은 그대로이며 아래 시험이 그 의미를 따로 고정한다. 이전 잠금 값: 613942fd…b6e584.
        self.assertEqual(hashlib.sha256(text[text.index('llm:'):].encode()).hexdigest(), '0e2e7892217b5d3c36ec181099622617992ad0e3e69aa0284afd44a9e3200562')

    def test_liveprobe_config_active_dev_prd_and_stg_history_only(self):
        # 활성 감시·조치 대상은 dev/prd뿐이고 stg는 이력 조회 전용이다. 모델·자동 변경 생성 설정은 바뀌지 않는다.
        import yaml
        config = yaml.safe_load((ROOT / 'deploy/environments/liveprobe-config.yaml').read_text())
        self.assertEqual(config['target_namespaces'], ['dev', 'prd'])
        self.assertEqual(config['historical_namespaces'], ['stg'])
        self.assertEqual(config['recovery'], {'simple_reanalysis_level': 'NORMAL'})
        self.assertIs(config['change_auto_create'], False)
        self.assertEqual((config['llm']['provider'], config['llm']['timeout_s']), ('codex_cli', 180))
        self.assertEqual({role: (v['model'], v['effort']) for role, v in config['llm']['roles'].items()}, {
            'router': ('gpt-6-luna', 'low'), 'signal_analyst': ('gpt-6-luna', 'low'),
            'result_verifier': ('gpt-6.1-sol', 'low'), 'evidence_curator': ('gpt-6.1-sol', 'medium'),
            'safety_verifier': ('gpt-6-astra', 'xhigh')})
        self.assertFalse(set(config['target_namespaces']) & set(config['historical_namespaces']))

    def test_original_p18_campaign_inputs_are_untouched(self):
        for relative, digest in {
            'scripts/render-p18-environments.py': '9b03a62f76de9b3b3cab91a852492710983c479bd35b67d29ffa6062ab06b1a7',
            'scripts/test_p18_environments_render.py': '719238fb3269b1b23369b5836281708258e304445a3e485d72871db13cc334a0',
            'deploy/environments/apps.json': '1713c87cfc1f713854c40da540d2d2e94b06e967a2175fe82a585b88f85de6f7',
        }.items():
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), digest, relative)


if __name__ == '__main__':
    unittest.main()
