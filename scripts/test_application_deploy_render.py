"""Offline deploy contracts; never import application conftest or contact a cluster."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'liveprobe/application@sha256:' + '0' * 64
SCRATCH = Path(os.environ.get('TMPDIR', '/Users/hwany/.hermes/cache/scratch'))


def load_script(test, name):
    path = ROOT / 'scripts' / name
    test.assertTrue(path.is_file(), 'required deployment script is not implemented')
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    test.assertTrue(spec is not None and spec.loader is not None)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script(self, 'render-application-deploy.py')

    def test_render_deterministic_and_preserves_catalog_bytes(self):
        first = self.module.render(IMAGE)
        self.assertEqual(first, self.module.render(IMAGE))
        # 2026-11-13 dev/prd 전환: 퇴역한 stg의 runner Role/RoleBinding 2개(52/53)를 더 이상 렌더하지 않아 17→15개다.
        self.assertEqual(len(first), 15)
        docs = [json.loads(text) for text in first.values()]
        self.assertEqual(len({(d['kind'], d['metadata']['namespace'], d['metadata']['name']) for d in docs}), len(docs))
        for doc in docs:
            self.assertTrue(all((doc.get('apiVersion'), doc.get('kind'), doc.get('metadata', {}).get('name'), doc.get('metadata', {}).get('namespace'))))
            self.assertNotIn(doc['kind'], ('Namespace', 'Secret', 'ClusterRole', 'ClusterRoleBinding'))
            self.assertEqual(doc['metadata']['labels']['app.kubernetes.io/part-of'], 'liveprobe-application')
        # 직접 선언하는 PVC는 2026-11-07 위임 결정의 새 서비스 전용 인증 저장소 하나뿐이다(DB PVC는 StatefulSet 템플릿).
        self.assertEqual([d['metadata']['name'] for d in docs if d['kind'] == 'PersistentVolumeClaim'], ['liveprobe-application-codex'])
        config = next(d for d in docs if d['kind'] == 'ConfigMap')
        for name in ('application-profiles.json', 'system-catalog.json'):
            self.assertEqual(config['data'][name], (ROOT / 'deploy/samsung' / name).read_text())

    def test_digest_only_and_no_credentials_in_image(self):
        for invalid in ('liveprobe/application:latest', 'sha256:' + '0' * 64,
                        'liveprobe/application@sha256:abc', IMAGE + 'x', IMAGE + '\n',
                        'user:password@registry/app@sha256:' + '0' * 64,
                        'https://registry/app@sha256:' + '0' * 64):
            with self.subTest(case='invalid_image'):
                with self.assertRaises(ValueError):
                    self.module.render(invalid)
        self.module.render('localhost:5000/team/app@sha256:' + 'a' * 64)

    def test_check_mode_detects_missing_changed_and_extra_yaml_without_writing(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            out = Path(td) / 'rendered'
            self.assertEqual(self.module.main(['--image', IMAGE, '--out', str(out), '--check']), 1)
            self.assertFalse(out.exists())
            self.assertEqual(self.module.main(['--image', IMAGE, '--out', str(out)]), 0)
            self.assertEqual(self.module.main(['--image', IMAGE, '--out', str(out), '--check']), 0)
            path = next(out.glob('*.yaml'))
            path.write_text('{}\n')
            self.assertEqual(self.module.main(['--image', IMAGE, '--out', str(out), '--check']), 1)
            self.assertEqual(path.read_text(), '{}\n')
            self.assertEqual(self.module.main(['--image', IMAGE, '--out', str(out)]), 0)
            (out / 'unexpected.yaml').write_text('{}\n')
            self.assertEqual(self.module.main(['--image', IMAGE, '--out', str(out), '--check']), 1)

    def test_api_identity_pvc_and_health_boundary(self):
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        api = next(d for d in docs if d['kind'] == 'Deployment')
        self.assertEqual(api['metadata']['namespace'], 'liveprobe-system')
        pod = api['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['serviceAccountName'], 'liveprobe-application-api')
        self.assertNotIn('initContainers', pod)
        self.assertNotIn('fsGroup', pod.get('securityContext', {}))
        container = pod['containers'][0]
        self.assertEqual(container['image'], IMAGE)
        # 인증 준비 실패는 비root 조건·기존 공유 저장소 보호를 완화하는 근거가 아니다.
        security = container['securityContext']
        self.assertTrue(security['runAsNonRoot'])
        self.assertEqual(security['runAsUser'], 65532)
        self.assertFalse(security['allowPrivilegeEscalation'])
        self.assertTrue(security['readOnlyRootFilesystem'])
        self.assertEqual(security['capabilities'], {'drop': ['ALL']})
        self.assertEqual(security['seccompProfile'], {'type': 'RuntimeDefault'})
        self.assertNotIn('privileged', security)
        self.assertEqual(container['readinessProbe']['httpGet']['path'], '/api/application/v1/healthz')
        env = {e['name']: e.get('value') for e in container['env']}
        expected = {'APPLICATION_DATABASE_URL_FILE': '/run/application/database/url',
                    'APPLICATION_IDENTITY_FILE': '/run/application/identity/human.json',
                    'APPLICATION_MACHINE_IDENTITY_FILE': '/run/application/identity/machine.json',
                    'APPLICATION_PROFILES_FILE': '/etc/application/application-profiles.json',
                    'APPLICATION_CATALOG_FILE': '/etc/application/system-catalog.json',
                    'APPLICATION_CODEX_HOME': '/var/lib/liveprobe/application-codex',
                    'APPLICATION_DATABASE_ISOLATED': 'true', 'APPLICATION_WORKER_ENABLED': 'true',
                    'APPLICATION_MODEL': 'gpt-6.2-sol',
                    'APPLICATION_CLASSIFIER_MODEL': 'gpt-6-luna', 'APPLICATION_CRITIC_MODEL': 'gpt-6.2-astra'}
        self.assertEqual(env, expected)
        volumes = {v['name']: v for v in pod['volumes']}
        # 2026-11-07 위임 결정(권장안, decision-log): 기존 관제 공유 인증 PVC를 쓰지 않고 새 서비스 전용 인증 저장소를 둔다.
        self.assertNotIn('liveprobe-codex-spare', json.dumps(docs))
        self.assertEqual(volumes['codex-home']['persistentVolumeClaim'], {'claimName': 'liveprobe-application-codex'})
        mount = next(m for m in container['volumeMounts'] if m['name'] == 'codex-home')
        self.assertEqual(mount['mountPath'], env['APPLICATION_CODEX_HOME'])
        self.assertFalse(mount.get('readOnly', False), '전용 CLI 홈은 로그인·토큰 갱신을 위해 쓰기 가능해야 한다')
        pvc = next(d for d in docs if d['kind'] == 'PersistentVolumeClaim')
        self.assertEqual(pvc['metadata']['name'], 'liveprobe-application-codex')
        self.assertEqual(pvc['metadata']['labels'], {'app.kubernetes.io/part-of': 'liveprobe-application'})
        self.assertEqual(pvc['spec'], {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'local-path',
                                       'resources': {'requests': {'storage': '1Gi'}}})
        self.assertEqual({v['secret']['secretName'] for v in volumes.values() if 'secret' in v},
                         {'liveprobe-application-identity', 'liveprobe-application-db-url'})
        self.assertTrue(all('hostPath' not in v for v in volumes.values()))
        self.assertTrue(all(not d['automountServiceAccountToken'] for d in docs if d['kind'] == 'ServiceAccount'))
        serialized = json.dumps(docs)
        self.assertFalse(any(key in serialized for key in ('postgresql://', '"stringData"', '"token"', '"principals"')))

    def test_role_specific_models_do_not_collapse_to_common_model(self):
        import runpy
        from unittest.mock import patch
        settings_class = runpy.run_path(str(ROOT / 'liveprobe/app/application/settings.py'))['ApplicationSettings']
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        api = next(d for d in docs if d['kind'] == 'Deployment')
        env = {e['name']: e['value'] for e in api['spec']['template']['spec']['containers'][0]['env']}
        with patch.dict(os.environ, env, clear=True):
            settings = settings_class.from_env()
        self.assertEqual(settings.model_for('classifier'), 'gpt-6-luna')
        self.assertEqual(settings.model_for('critic'), 'gpt-6.2-astra')
        self.assertEqual(settings.model_for('analyst'), 'gpt-6.2-sol')

    def test_previous_lineup_env_runs_on_current_ids(self):
        import runpy
        from unittest.mock import patch
        settings_class = runpy.run_path(str(ROOT / 'liveprobe/app/application/settings.py'))['ApplicationSettings']
        old = {'APPLICATION_MODEL': 'gpt-6.1-sol', 'APPLICATION_CLASSIFIER_MODEL': 'gpt-6-luna',
               'APPLICATION_CRITIC_MODEL': 'gpt-6-astra'}
        with patch.dict(os.environ, old, clear=True):
            settings = settings_class.from_env()
        self.assertEqual([settings.model_for(r) for r in ('classifier', 'analyst', 'critic', 'patch')],
                         ['gpt-6-luna', 'gpt-6.2-sol', 'gpt-6.2-astra', 'gpt-6.2-sol'])
        # 직접 생성한 설정(이전 저장값)도 새 실행에서 현재 ID를 쓴다.
        self.assertEqual(settings_class(model='gpt-6.1-sol', critic_model='gpt-6-astra').model_for('critic'), 'gpt-6.2-astra')

    def test_new_database_and_routing_only(self):
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        db = next(d for d in docs if d['kind'] == 'StatefulSet')
        self.assertEqual(db['metadata']['name'], 'liveprobe-application-db')
        pod = db['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        c = pod['containers'][0]
        self.assertEqual(c['image'], 'pgvector/pgvector:0.8.6-pg17-bookworm')
        self.assertEqual(c['imagePullPolicy'], 'IfNotPresent')
        password = next(e for e in c['env'] if e['name'] == 'POSTGRES_PASSWORD')
        self.assertEqual(password['valueFrom']['secretKeyRef'], {'name': 'liveprobe-application-db', 'key': 'password'})
        pvc = db['spec']['volumeClaimTemplates'][0]
        self.assertEqual(pvc['spec']['storageClassName'], 'local-path')
        self.assertEqual(pvc['spec']['resources']['requests']['storage'], '1Gi')
        ingress = next(d for d in docs if d['kind'] == 'Ingress')
        self.assertEqual(ingress['spec']['ingressClassName'], 'traefik')
        self.assertEqual(ingress['spec']['rules'][0]['host'], 'application.liveprobe.localhost')
        path = ingress['spec']['rules'][0]['http']['paths'][0]
        self.assertEqual(path['backend']['service'], {'name': 'liveprobe-application-api', 'port': {'number': 8080}})

    def test_rbac_denies_secret_config_exec_delete_and_cluster_scope(self):
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        roles = [d for d in docs if d['kind'] == 'Role']
        # 2026-11-13 dev/prd 전환: runner 권한은 활성 환경 dev/prd에만 둔다. stg는 퇴역(이력 전용)이라 권한을 렌더하지 않는다.
        self.assertEqual({r['metadata']['namespace'] for r in roles}, {'dev', 'prd'})
        for role in roles:
            for rule in role['rules']:
                if rule['resources'] == ['services/proxy']:
                    continue  # 업무 확인 전용 규칙은 아래 시험이 이름·동사를 정확히 검사한다.
                self.assertTrue(set(rule['resources']) <= {'pods', 'pods/log', 'events', 'replicasets', 'deployments'})
                self.assertTrue(set(rule['verbs']) <= {'get', 'list', 'watch', 'patch'})
                if 'patch' in rule['verbs']:
                    self.assertEqual(rule['resources'], ['deployments'])
        bindings = [d for d in docs if d['kind'] == 'RoleBinding']
        # 같은 이유로 바인딩도 dev/prd 2개뿐이다(이전 3개).
        self.assertEqual(len(bindings), 2)
        self.assertEqual({b['metadata']['namespace'] for b in bindings}, {'dev', 'prd'})
        self.assertTrue(all(b['subjects'] == [{'kind': 'ServiceAccount', 'name': 'liveprobe-application-runner', 'namespace': 'liveprobe-system'}] for b in bindings))

    def test_retired_stg_gets_no_runner_rbac_and_file_numbers_are_stable(self):
        # 퇴역 환경 stg에는 어떤 runner 객체도 렌더하지 않는다. 기존 번호 체계를 유지해 prd는 54/55, stg 번호 52/53은 비운다.
        rendered = self.module.render(IMAGE)
        docs = [json.loads(t) for t in rendered.values()]
        self.assertFalse([d for d in docs if d['metadata']['namespace'] == 'stg'])
        self.assertEqual(sorted(name for name in rendered if name.startswith('5')),
                         ['50-runner-dev-role.yaml', '51-runner-dev-binding.yaml',
                          '54-runner-prd-role.yaml', '55-runner-prd-binding.yaml'])
        self.assertFalse(any('stg' in name for name in rendered))

    def test_runner_namespaces_must_be_active_non_retired_catalog_environments(self):
        # 실패 폐쇄: catalog가 runner 대상 환경을 퇴역/비활성으로 표시하면 권한 선언을 렌더하지 않는다.
        import shutil
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            root = Path(td)
            (root / 'deploy/samsung').mkdir(parents=True)
            shutil.copyfile(ROOT / 'deploy/samsung/application-profiles.json', root / 'deploy/samsung/application-profiles.json')
            catalog = json.loads((ROOT / 'deploy/samsung/system-catalog.json').read_text())
            original_root = self.module.ROOT
            self.addCleanup(setattr, self.module, 'ROOT', original_root)
            self.module.ROOT = root
            for environments, retired in ((['dev', 'prd'], ['prd']), (['dev'], ['stg']), (['dev', 'prd'], 'stg')):
                with self.subTest(environments=environments, retired=retired):
                    (root / 'deploy/samsung/system-catalog.json').write_text(
                        json.dumps(dict(catalog, environments=environments, retired_environments=retired)))
                    with self.assertRaises(ValueError):
                        self.module.render(IMAGE)
            (root / 'deploy/samsung/system-catalog.json').write_text(
                json.dumps(dict(catalog, environments=['dev', 'prd'], retired_environments=['stg'])))
            self.assertEqual(len(self.module.render(IMAGE)), 15)

    def test_committed_render_matches_current_inputs_without_retired_files(self):
        # 커밋된 산출물은 현재 catalog/profile 바이트와 기존 digest로 재현되어야 하며 퇴역한 stg 파일이 남아 있지 않아야 한다.
        out = ROOT / 'deploy/application/rendered'
        image = json.loads((out / '30-api.yaml').read_text())['spec']['template']['spec']['containers'][0]['image']
        self.assertFalse(sorted(p.name for p in out.glob('*stg*')))
        self.assertEqual(self.module.main(['--image', image, '--out', str(out), '--check']), 0)

    def test_runner_patch_is_limited_to_catalogued_deployments(self):
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        services = set(json.loads((ROOT / 'deploy/samsung/system-catalog.json').read_text())['service_to_system'])
        for role in (doc for doc in docs if doc['kind'] == 'Role'):
            writes = [rule for rule in role['rules'] if 'patch' in rule['verbs']]
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0]['verbs'], ['patch'])
            self.assertEqual(set(writes[0]['resourceNames']), services)

    def test_runner_business_proxy_is_get_only_and_name_limited(self):
        # RUNNER-04: 업무 확인은 등록 서비스의 http 8080 프록시 GET만 허용한다. 서비스 목록·생성·수정·https 내부 포트는 없다.
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        services = sorted(json.loads((ROOT / 'deploy/samsung/system-catalog.json').read_text())['service_to_system'])
        for role in (doc for doc in docs if doc['kind'] == 'Role'):
            proxies = [rule for rule in role['rules'] if any(r.startswith('services') for r in rule['resources'])]
            self.assertEqual(len(proxies), 1, role['metadata']['namespace'])
            self.assertEqual(proxies[0], {'apiGroups': [''], 'resources': ['services/proxy'], 'verbs': ['get'],
                                          'resourceNames': [f'http:{name}:8080' for name in services]})

    def test_network_policy_selectors_are_narrow(self):
        docs = [json.loads(t) for t in self.module.render(IMAGE).values()]
        api = next(d for d in docs if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == 'liveprobe-application-api')
        peer = api['spec']['ingress'][0]['from'][0]
        self.assertEqual(peer['namespaceSelector']['matchLabels'], {'kubernetes.io/metadata.name': 'kube-system'})
        self.assertEqual(peer['podSelector']['matchLabels'], {'app.kubernetes.io/name': 'traefik'})
        self.assertEqual(api['spec']['ingress'][0]['ports'], [{'protocol': 'TCP', 'port': 8080}])
        public = next(e for e in api['spec']['egress'] if any('ipBlock' in p for p in e['to']))
        block = public['to'][0]['ipBlock']
        self.assertEqual(block['cidr'], '0.0.0.0/0')
        self.assertTrue({'10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10', '127.0.0.0/8'} <= set(block['except']))
        self.assertEqual(public['ports'], [{'protocol': 'TCP', 'port': 443}])
        db = next(d for d in docs if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == 'liveprobe-application-db')
        self.assertEqual(db['spec']['ingress'][0]['from'], [{'podSelector': {'matchLabels': {'app': 'liveprobe-application-api'}}}])
        self.assertEqual(db['spec']['egress'], [])

    def test_application_image_nonroot_no_dependency_reinstall(self):
        path = ROOT / 'liveprobe/Dockerfile.application'
        self.assertTrue(path.is_file(), 'application Dockerfile is not implemented')
        text = path.read_text()
        self.assertIn('FROM liveprobe/liveprobe@sha256:f05d276f1efa308bd5258ffabe6a57b564abec50814b57b956fdaf7f34811b9c', text)
        self.assertIn('COPY --chown=65532:65532 app/ /app/app/', text)
        self.assertIn('USER 65532:65532', text)
        self.assertIn('ENV HOME=/tmp', text)
        self.assertIn('"app.application.main:app"', text)
        self.assertIn('"8080"', text)
        self.assertFalse(any(word in text for word in ('pip install', 'npm install', 'auth.json', 'chown -R /var/lib')))


class FakeKubectl:
    """Only this transport is substituted; all generation and file I/O is real."""
    def __init__(self, *, existing=None, forbidden=False, create_failure=False):
        self.calls = []
        self.existing = existing
        self.forbidden = forbidden
        self.create_failure = create_failure
        self.documents = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get('input')))
        if 'get' in argv:
            name = argv[argv.index('secret') + 1]
            if self.forbidden:
                return subprocess.CompletedProcess(argv, 1, '', 'Error from server (Forbidden): request refused')
            if name == self.existing:
                return subprocess.CompletedProcess(argv, 0, 'secret/' + name + '\n', '')
            return subprocess.CompletedProcess(argv, 1, '', f'Error from server (NotFound): secrets "{name}" not found\n')
        if 'create' in argv:
            doc = json.loads(kwargs['input'])
            self.documents.append(doc)
            # A failed kubectl/plugin may echo the whole input. It must never escape main().
            if self.create_failure:
                return subprocess.CompletedProcess(argv, 1, kwargs['input'], kwargs['input'])
            return subprocess.CompletedProcess(argv, 0, 'secret/' + doc['metadata']['name'] + '\n', '')
        raise AssertionError('unexpected kubectl operation')


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script(self, 'provision-application-secrets.py')
        self.directory = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def invoke(self, fake, args=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.module.main(args or [], run=fake, repo_root=self.root)
        return rc, out.getvalue() + err.getvalue()

    def test_dry_run_has_zero_kubectl_calls_and_no_files(self):
        fake = FakeKubectl()
        rc, output = self.invoke(fake, ['--dry-run'])
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 0)
        self.assertFalse(any(self.root.iterdir()))

    def test_existing_secret_stops_without_create_or_local_tokens(self):
        for name in ('liveprobe-application-db', 'liveprobe-application-db-url', 'liveprobe-application-identity'):
            fake = FakeKubectl(existing=name)
            rc, output = self.invoke(fake)
            self.assertEqual(rc, 1)
            self.assertTrue(all('get' in argv for argv, _ in fake.calls))
            self.assertFalse((self.root / '.e2e-tmp/application').exists())

    def test_not_found_is_distinguished_from_permission_error(self):
        fake = FakeKubectl(forbidden=True)
        rc, output = self.invoke(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(any('create' in argv for argv, _ in fake.calls))
        self.assertFalse(any(self.root.iterdir()))

    def test_only_new_secret_creates_and_values_go_exclusively_to_stdin(self):
        import base64
        import urllib.parse
        fake = FakeKubectl()
        rc, output = self.invoke(fake, ['--context', 'rancher-desktop'])
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 6)
        docs = {doc['metadata']['name']: doc for doc in fake.documents}
        self.assertEqual(len(docs), 3)
        self.assertTrue(all(doc['kind'] == 'Secret' and doc['metadata']['namespace'] == 'liveprobe-system' for doc in docs.values()))
        password = base64.b64decode(docs['liveprobe-application-db']['data']['password']).decode()
        url = base64.b64decode(docs['liveprobe-application-db-url']['data']['url']).decode()
        parsed = urllib.parse.urlsplit(url)
        self.assertTrue(urllib.parse.unquote(parsed.password or '') == password)
        self.assertTrue(parsed.hostname == 'liveprobe-application-db.liveprobe-system.svc.cluster.local')
        self.assertTrue(parsed.path == '/liveprobe_application')
        human = json.loads(base64.b64decode(docs['liveprobe-application-identity']['data']['human.json']))
        machine = json.loads(base64.b64decode(docs['liveprobe-application-identity']['data']['machine.json']))
        self.assertEqual(human['principals'], [])
        self.assertEqual([p['subject'] for p in machine['principals']], ['application-runner'])
        self.assertTrue(set(machine['principals'][0]['scopes']) == {'runner', 'incident:create', 'incident:read'})
        principals = human['principals'] + machine['principals']
        self.assertTrue(len({p['token'] for p in principals}) == 1)
        directory = self.root / '.e2e-tmp/application'
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for principal in principals:
            token_path = directory / (principal['subject'] + '.token')
            self.assertTrue(token_path.read_text().strip() == principal['token'])
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
            self.assertTrue(str(token_path) in output)
        private_values = [password, url] + [p['token'] for p in principals]
        self.assertTrue(all(len(value) >= 16 for value in private_values))
        self.assertFalse(any(value in output for value in private_values))
        for argv, stdin in fake.calls:
            self.assertTrue(argv[:3] == ['kubectl', '--context', 'rancher-desktop'])
            self.assertTrue('-n' in argv and argv[argv.index('-n') + 1] == 'liveprobe-system')
            self.assertFalse(any(value in ' '.join(argv) for value in private_values))
            self.assertFalse(any(arg in argv for arg in ('apply', 'replace', 'patch', 'delete', '--from-literal')))
            if 'get' in argv:
                self.assertEqual(argv[-2:], ['-o', 'name'])
                self.assertIsNone(stdin)
            else:
                self.assertTrue(argv[-5:] == ['create', '-f', '-', '-o', 'name'])
                self.assertTrue(bool(stdin))

    def test_create_race_or_failure_has_no_retry_no_secret_output_and_keeps_recovery_tokens(self):
        fake = FakeKubectl(create_failure=True)
        rc, output = self.invoke(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(len(fake.documents), 1)
        import base64
        private_password = base64.b64decode(fake.documents[0]['data']['password']).decode()
        self.assertFalse(private_password in output)
        self.assertFalse(fake.calls[-1][1] in output)
        self.assertEqual(len(list((self.root / '.e2e-tmp/application').glob('*.token'))), 1)
        self.assertTrue(all('delete' not in argv for argv, _ in fake.calls))

    def test_existing_or_symlink_token_path_never_overwritten(self):
        base = self.root / '.e2e-tmp'
        base.mkdir()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (base / 'application').symlink_to(elsewhere, target_is_directory=True)
        fake = FakeKubectl()
        rc, output = self.invoke(fake)
        self.assertEqual(rc, 1)
        self.assertFalse(any('create' in argv for argv, _ in fake.calls))
        self.assertFalse(any(elsewhere.iterdir()))
        (base / 'application').unlink()
        (base / 'application').mkdir(mode=0o700)
        token = base / 'application/application-runner.token'
        token.write_bytes(b'existing-test-marker')
        fake = FakeKubectl()
        rc, output = self.invoke(fake)
        self.assertEqual(rc, 1)
        self.assertTrue(token.read_bytes() == b'existing-test-marker')
        self.assertFalse(any('create' in argv for argv, _ in fake.calls))

    def test_transport_exception_is_sanitized(self):
        def unavailable(argv, **kwargs):
            raise RuntimeError('synthetic-private-marker-do-not-print')
        rc, output = self.invoke(unavailable)
        self.assertEqual(rc, 1)
        self.assertFalse('synthetic-private-marker-do-not-print' in output)


if __name__ == '__main__':
    unittest.main()
