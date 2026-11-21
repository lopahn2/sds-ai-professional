"""Isolated HTTP intake through browser QA, approval and recovery.

From the repository root with the project Python environment and Playwright Chromium installed:
    PYTHONPATH=liveprobe:liveprobe/tests:liveprobe/application_tests python -m pytest liveprobe/application_tests/test_console_release_acceptance.py

The browser opens only a disposable catalog server on loopback. Publication, GitHub and Kubernetes
are in-memory transports. The build executes the registered Python tests against the actual source.
"""

import ast
import base64
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import BigInteger, Integer, JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.application.merge_policy import GitHubMergeObserver
from app.application.safety import text_sha
from app.change_models import ChangeRequest
from app.models import Incident, CodeActionLink, Evidence, AgentRun, Action, OperatorCommand, WorkflowRun, AuditLog
from app.unified import UnifiedService
from app.unified_api import unified_router
from application_runner.dev_ui_qa import CATALOG_TEST
from application_runner.executor import Executor
from application_runner.profiles import Registry
from application_runner.reports import junit_rows, totals
from application_runner.source import source_files
from application_runner.worker import durable_write
from test_core_analysis import SyntheticTransport
from test_core_support import Harness, PREFIX, HUMAN
from test_runner_recovery_probe import Observer, stamp, access

ROOT = Path(__file__).resolve().parents[2]
SERVICE = 'catalog'
OLD = 'liveprobe/catalog@sha256:' + '1' * 64
CAPABILITY = {
    'collect': 'cluster',
    'source': 'source',
    'test_build': 'source',
    'deploy': 'cluster',
    'verify': 'cluster',
    'qa': 'qa',
    'recovery': 'cluster',
}


def bearer(role):
    return {'Authorization': 'Bearer synthetic-console-' + role}


class CatalogProvider(SyntheticTransport):
    """A deterministic model proposes one actual bounded failing regression and fix."""

    snapshot_id = None
    pinned_versions = 0
    repair_controller = False
    repair_request = None

    def complete(self, role, *args, **kwargs):
        result = super().complete(role, *args, **kwargs)
        if role == 'patch':
            patch = result.data
            patch['source_snapshot_id'] = self.snapshot_id
            patch['diff'] = patch['diff'].replace(
                '@@ -0,0 +1,2 @@\n', '@@ -0,0 +1,4 @@\n+from src.main import run\n+\n'
            )
            if self.pinned_versions > 1:
                patch['diff'] = (
                    '--- a/src/main.py\n+++ b/src/main.py\n@@ -4,2 +4,2 @@\n def available():\n'
                    '-    return False\n+    return True\n--- /dev/null\n+++ b/tests/test_available.py\n'
                    '@@ -0,0 +1,4 @@\n+from src.main import available\n+\n+def test_available():\n+    assert available()\n'
                )
                patch['tests_added'] = ['tests/test_available.py::test_available']
                if self.repair_controller:
                    # Respond to the application worker's actual requirement. The registered source
                    # supplies the test implementation, just as the deterministic provider supplies the patch.
                    start = kwargs['prompt'].split('\nRole: patch\n', 1)[1]
                    context, _ = json.JSONDecoder().raw_decode(start)
                    self.repair_request = context['round_request']
                    requirement = context['qa_requirement']
                    file = requirement['test_file']
                    assert requirement == self.repair_request['qa_requirement']
                    assert requirement['service'] == SERVICE and requirement['profile_id'] == 'samsung-catalog-v1'
                    assert requirement['reason'] == 'qa_tests_missing' and requirement['instruction']
                    assert file == CATALOG_TEST and file not in context['source_snapshot']['files']
                    assert self.repair_request['qa']['reason'] == 'required_controller_test_missing'
                    assert all(word in str(requirement['expectations_ko']) for word in ('가격', '설명', '재고'))
                    [required_case] = requirement['test_cases']
                    cls, method = required_case.split('.')
                    source = (ROOT / 'services' / SERVICE / file).read_text()
                    tree = ast.parse(source)
                    definition = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
                    check = next(n for n in definition.body if isinstance(n, ast.FunctionDef) and n.name == method)
                    imports = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
                    # Keep the exact developer method and its base test class, selected by the requested case.
                    source_lines = source.splitlines()
                    text = (
                        '\n'.join(
                            [
                                *imports,
                                '',
                                source_lines[definition.lineno - 1],
                                *source_lines[check.lineno - 1 : check.end_lineno],
                            ]
                        )
                        + '\n'
                    )
                    lines = text.splitlines(keepends=True)
                    patch['diff'] += (
                        '--- /dev/null\n+++ b/'
                        + file
                        + '\n@@ -0,0 +1,'
                        + str(len(lines))
                        + ' @@\n'
                        + ''.join('+' + line for line in lines)
                    )
                    patch['tests_added'].append(file + '::' + cls + '::' + method)
        else:
            result.data['citations'] = [
                {'ref': 'source:src/main.py', 'quote': 'def run():'},
                {'ref': 'E1-catalog', 'quote': 'dev-catalog'},
            ]
        result.text = json.dumps(result.data)
        return result


class IsolatedHost:
    """Run local pytest with real JUnit files. Publish image manifests only into this transport's memory."""

    def __init__(self, work):
        self.work = work
        self.images = {OLD}
        self.built = []

    def observe_manifest(self, image):
        assert image in self.images
        return image

    def tests(self, workspace, profile, phase, added):
        report = self.work / (phase + '.xml')
        proc = subprocess.run(
            [
                sys.executable,
                '-B',
                '-m',
                'pytest',
                '-q',
                '-p',
                'no:cacheprovider',
                '-o',
                'junit_family=xunit1',
                '--junitxml=' + str(report),
                'tests',
            ],
            cwd=workspace,
            env={'PATH': os.defpath, 'PYTHONPATH': str(workspace), 'PYTHONDONTWRITEBYTECODE': '1', 'SELF_TRAFFIC': '0'},
            capture_output=True,
            timeout=75,
        )
        assert report.exists(), proc.stdout[-2000:]
        return {'exit_code': proc.returncode, **totals(junit_rows(report.read_text(), 'python', phase))}

    def build(self, workspace, profile, tree):
        image = 'liveprobe/catalog@sha256:' + tree
        self.images.add(image)
        self.built.append((workspace, image))
        return {'image': image, 'source_tree_sha256': tree, 'manifest_observed': True}


class LocalCluster(Observer):
    """All prd calls terminate in this object. Dev UI requests use only a disposable loopback server."""

    def __init__(self):
        super().__init__()
        self.docs = {}
        self.writes = []
        self.now = time.time()
        self.started = self.now - 3600
        self.open_ui = None
        self.incident_log = None

    def read(self, env, service):
        assert env in ('dev', 'prd') and service in ('catalog', 'inventory')
        key = env, service
        if key not in self.docs:
            self.docs[key] = {
                'metadata': {
                    'name': service,
                    'namespace': env,
                    'uid': env + '-' + service,
                    'generation': 1,
                    'annotations': {},
                    'resourceVersion': '1',
                },
                'spec': {
                    'replicas': 1,
                    'template': {
                        'metadata': {'labels': {'app': service}},
                        'spec': {
                            'containers': [{'name': service, 'image': 'liveprobe/' + service + '@sha256:' + '1' * 64}]
                        },
                    },
                },
                'status': {'observedGeneration': 1, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1},
            }
        return deepcopy(self.docs[key])

    def patch(self, env, service, ops):
        doc = self.read(env, service)
        for op in ops:
            parts = [part.replace('~1', '/').replace('~0', '~') for part in op['path'].split('/')[1:]]
            obj = doc
            for part in parts[:-1]:
                obj = obj[int(part)] if isinstance(obj, list) else obj[part]
            field = parts[-1]
            if op['op'] == 'test':
                assert obj[field] == op['value']
            elif op['op'] == 'remove':
                del obj[field]
            else:
                assert op['op'] in ('replace', 'add')
                obj[field] = op['value']
                if op['path'].endswith('/image'):
                    self.writes.append((env, service, op['value']))
                    if env == 'dev':
                        self.open_ui()
        doc['metadata']['resourceVersion'] = str(int(doc['metadata']['resourceVersion']) + 1)
        self.docs[env, service] = doc
        return deepcopy(doc)

    def pods(self, env, service):
        image = self.read(env, service)['spec']['template']['spec']['containers'][0]['image']
        return [{'image': image, 'image_id': image, 'owned': True, 'ready': True}]

    def logs(self, env, service, since):
        if env != 'dev' or service != SERVICE or self.incident_log is None:
            return []
        return [{'text': json.dumps(self.incident_log), 'observed_at': self.incident_log['timestamp']}]

    def business(self, env, service, path):
        assert service == SERVICE and path == '/api/products'
        if env == 'dev':
            with build_opener(ProxyHandler({})).open(self.origin + path, timeout=5) as response:
                return {'status': response.status, 'body': json.load(response)}
        assert env == 'prd'
        return {'status': 200, 'body': {'products': [{'name': '수정 시험 상품', 'price': 79000}]}}


@contextmanager
def core_sessions(work):
    engine = create_engine('sqlite:///' + str(work / 'core.sqlite'))
    meta = MetaData()
    for model in (
        Incident,
        CodeActionLink,
        Evidence,
        AgentRun,
        Action,
        OperatorCommand,
        WorkflowRun,
        AuditLog,
        ChangeRequest,
    ):
        table = model.__table__.to_metadata(meta)
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
            if col.primary_key and isinstance(col.type, BigInteger):
                col.type = Integer()
    meta.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions.begin
    finally:
        engine.dispose()


def file_map(path):
    return {name: text_sha(body) for name, body in source_files(path).items()}


@contextmanager
def dev_server(site, access_log=None):
    cmd = (
        'from app import create_server; s=create_server("127.0.0.1",0,background=False); '
        'print(s.server_port,flush=True);s.serve_forever()'
    )
    proc = subprocess.Popen(
        [sys.executable, '-B', '-c', cmd],
        cwd=site,
        env={'PATH': os.defpath, 'APP_ENV': 'dev', 'SELF_TRAFFIC': '0'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield 'http://127.0.0.1:' + str(int(proc.stdout.readline()))
    finally:
        proc.terminate()
        stdout, _ = proc.communicate(timeout=5)
        if access_log is not None:
            for line in stdout.splitlines():
                if line.startswith('{'):
                    event = json.loads(line)
                    if event.get('event') == 'http_access':
                        access_log.append(event)


class Acceptance:
    def __init__(self, stack, tmp, missing, monkeypatch):
        self.tmp = tmp
        self.registry = Registry(ROOT)
        self.host, self.kube = IsolatedHost(tmp), LocalCluster()
        self.config = {'provenance_dir': str(tmp / 'provenance'), 'qa_ui_origins': {}}
        self.profile = self.registry.get(SERVICE)
        self.provider = CatalogProvider(level=2)
        origins = {}
        self.h = Harness(
            self.provider,
            unified_mode=True,
            qa_required=True,
            qa_ui_origins=origins,
            profiles_file=str(ROOT / 'deploy/samsung/application-profiles.json'),
            catalog_file=str(ROOT / 'deploy/samsung/system-catalog.json'),
            repositories_file=str(ROOT / 'repositories.json'),
        )
        stack.callback(self.h.close)
        self.h.now = time.time()
        self.coordinator = UnifiedService(
            self.h.app, 'console-acceptance', session_factory=stack.enter_context(core_sessions(tmp))
        )
        self.coordinator.start()
        stack.callback(self.coordinator.stop)
        app = FastAPI()
        app.include_router(unified_router({'unified': self.coordinator}))
        self.ui = stack.enter_context(
            TestClient(app, base_url='http://liveprobe.localhost', headers={'X-LiveProbe-Client': 'dashboard'})
        )
        Path(self.h.settings.machine_identity_file).write_text(
            json.dumps(
                {
                    'principals': [
                        {
                            'subject': role + '-machine',
                            'token': 'synthetic-console-' + role,
                            'scopes': ['runner', 'runner:' + role],
                        }
                        for role in ('source', 'cluster', 'qa')
                    ]
                }
            )
        )
        site = tmp / 'registered-source'
        site.mkdir()
        from shutil import copytree

        src = ROOT / 'services/catalog'
        for p in ('app.py', 'runtime.py', 'static', 'tests'):
            if (src / p).is_dir():
                copytree(src / p, site / p, ignore=lambda path, items: [i for i in items if i == '__pycache__'])
            else:
                (site / p).write_bytes((src / p).read_bytes())
        if missing:
            (site / CATALOG_TEST).unlink()
        (site / 'src').mkdir()
        (site / 'src/main.py').write_text('def run():\n    return 1 / 0\n\ndef available():\n    return False\n')
        # Only the disposable source tree is faulty. The same exception must disappear on the built version.
        app_source = site / 'app.py'
        body = app_source.read_text().replace(
            '        self.searches += 1',
            '        self.searches += 1\n            from src.main import run\n            run()',
        )
        if missing:
            body = body.replace(
                "            row['price'] = self.display_price(row)",
                "            row['price'] = self.display_price(row)\n            from src.main import available\n"
                "            if term == '키보드' and not available():\n                row['price'] = 1",
            )
        app_source.write_text(body)
        before_log = []
        with dev_server(site, before_log) as faulty:
            with pytest.raises(HTTPError) as observed:
                build_opener(ProxyHandler({})).open(faulty + '/api/products', timeout=5)
            assert observed.value.code == 500
            observed.value.close()
            # Runtime emits the access log after flushing the HTTP response.
            time.sleep(0.02)
        self.kube.incident_log = next(
            row for row in before_log if row['uri'] == '/api/products' and row['status'] == 500
        )
        durable_write(
            Path(self.config['provenance_dir']) / ('catalog-' + '1' * 64 + '.json'),
            {
                'provenance_kind': 'local_build_receipt',
                'image': OLD,
                'profile_id': self.profile['id'],
                'source_root': str(site),
                'file_sha256': file_map(site),
                'base_commit': 'a' * 40,
            },
        )
        self.job_ids = []

        def open_ui():
            self.kube.origin = stack.enter_context(dev_server(self.host.built[-1][0]))
            self.config['qa_ui_origins'][SERVICE] = origins[SERVICE] = self.kube.origin

        self.kube.open_ui = open_ui
        self.merged = False

        def local_github(_observer, path):
            repo = '/repos/live-probe/catalog'
            commit = 'c' * 40
            if path == repo + '/pulls/37':
                return {
                    'merged': self.merged,
                    'state': 'closed',
                    'merged_at': stamp(self.h.now),
                    'merged_by': {'type': 'User', 'login': 'operator'},
                    'head': {'repo': {'full_name': 'live-probe/catalog'}},
                    'base': {'ref': 'main', 'repo': {'full_name': 'live-probe/catalog'}},
                    'merge_commit_sha': commit,
                }
            if path == repo + '/git/ref/heads/main':
                return {'object': {'sha': commit}}
            files = source_files(self.host.built[-1][0])
            blobs = {
                name: (
                    hashlib.sha1(b'blob ' + str(len(body.encode())).encode() + b'\0' + body.encode()).hexdigest(),
                    body.encode(),
                )
                for name, body in files.items()
            }
            if path == repo + '/git/trees/' + commit + '?recursive=1':
                return {
                    'truncated': False,
                    'tree': [
                        {'path': name, 'mode': '100644', 'type': 'blob', 'sha': b[0], 'size': len(b[1])}
                        for name, b in blobs.items()
                    ],
                }
            for blob, body in blobs.values():
                if path == repo + '/git/blobs/' + blob:
                    return {'encoding': 'base64', 'size': len(body), 'content': base64.b64encode(body).decode()}
            raise AssertionError('unknown simulated GitHub request')

        monkeypatch.setattr(GitHubMergeObserver, '_get', local_github)

    def intake(self):
        req = {
            'environment': 'dev',
            'service': SERVICE,
            'request_id': 'isolated-case-one',
            'reason': '상품 조회 오류가 관측되었습니다.',
        }
        result = self.ui.post('/api/unified/incidents', headers=HUMAN, json=req)
        assert result.status_code == 201, result.text
        self.iid = result.json()['incident_id']
        self.child = result.json()['code_action']['application_id']
        again = self.ui.post('/api/unified/incidents', headers=HUMAN, json=req)
        assert again.json()['code_action']['application_id'] == self.child

    def detail(self):
        r = self.h.client.get(PREFIX + '/incidents/' + self.child, headers=HUMAN)
        assert r.status_code == 200, r.text
        return r.json()

    def claim(self, role, kind):
        r = self.h.client.post(
            PREFIX + '/runner/claim', headers=bearer(role), json={'worker_id': 'same-label', 'capabilities': [kind]}
        )
        assert r.status_code == 200, r.text
        return r.json()['job']

    def run_job(self, kind):
        # Browser receipts and recovery windows use wall time, including across QA rounds.
        self.h.now = time.time()
        role = CAPABILITY[kind]
        job = self.claim(role, kind)
        assert job and job['incident_id'] == self.child, (
            kind,
            self.detail()['state'],
            self.detail()['data'].get('handoff'),
        )
        self.job_ids.append(job['id'])
        executor = Executor(
            self.registry, self.kube, self.host, self.tmp / 'state', {**self.config, 'runner_role': role}
        )
        result = executor.execute(job)
        self.h.now = time.time()
        if kind == 'source':
            self.provider.snapshot_id = result['source_snapshot']['id']
            self.provider.pinned_versions += 1
        report = {'lease_token': job['lease_token'], 'fencing_token': job['fencing_token'], 'result': result}
        endpoint = PREFIX + '/runner/jobs/' + job['id'] + '/result'
        if kind == 'qa':
            assert self.h.client.post(endpoint, headers=bearer('cluster'), json=report).status_code == 403
        returned = self.h.client.post(endpoint, headers=bearer(role), json=report)
        assert returned.status_code == 200, (kind, returned.text)
        return result

    def to_build(self):
        self.intake()
        self.run_job('collect')
        self.run_job('source')

    def to_qa(self):
        assert self.h.app.state.worker.run_once()
        assert self.detail()['state'] == 'test_build_queued', (
            self.detail()['stages'][-2:],
            self.detail()['data'].get('stop'),
            self.provider.calls[-1]['role'] if self.provider.calls else None,
        )
        built = self.run_job('test_build')
        assert built['ok'], built
        self.run_job('collect')
        assert self.run_job('deploy')['ok']
        assert self.run_job('verify')['ok']
        if self.detail()['state'] == 'dev_infra_pending':
            # Use the public coordinator as the periodic projection worker does; no direct state mutations.
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and self.detail()['state'] == 'dev_infra_pending':
                time.sleep(0.08)
        assert self.detail()['state'] == 'prd_targets_collecting', self.detail()['state']
        self.run_job('collect')
        assert self.detail()['state'] == 'qa_queued'
        assert self.claim('cluster', 'qa') is None and self.claim('source', 'qa') is None
        assert self.claim('qa', 'deploy') is None
        return self.run_job('qa')

    def human(self, action, body):
        return self.ui.post(f'/api/incidents/{self.iid}/code-action/' + action, headers=HUMAN, json=body)


def test_real_browser_qa_release_approval_and_recovery(tmp_path, monkeypatch):
    with ExitStack() as stack:
        case = Acceptance(stack, tmp_path, False, monkeypatch)
        case.to_build()
        qa = case.to_qa()['qa']
        assert qa['outcome'] == 'passed', qa.get('reason')
        record = case.detail()
        release = record['data']['release']
        assert record['state'] == 'awaiting_prd_approval' and release['qa']['accepted'], release['qa'].get(
            'rejection_code'
        )
        assert len(qa['steps']) >= 5 and qa['developer_tests'][0]['file'] == CATALOG_TEST
        assert release['risk']['assessment'] == 'complete'
        finger = {k: release[k] for k in ('release_id', 'artifact_sha256', 'plan_sha256')}
        finger.update(image=release['artifact']['image'])
        approve = {**finger, 'risk_sha256': release['risk_sha256'], 'qa_receipt_id': qa['receipt_id']}
        assert case.human('approve-prd', approve).status_code == 409
        assert case.human('review-merge', {**finger, 'pr_number': 37}).status_code == 409
        case.merged = True
        checked = case.human('review-merge', {**finger, 'pr_number': 37})
        assert checked.status_code == 200, checked.text
        assert case.human('approve-prd', {**approve, 'risk_sha256': 'e' * 64}).status_code == 409
        approved = case.human('approve-prd', approve)
        assert approved.status_code == 200, approved.text
        assert case.run_job('deploy')['ok'] and case.run_job('verify')['ok']
        assert case.detail()['state'] == 'awaiting_recovery'
        case.kube.image = finger['image']
        case.kube.roll_uid = 'prd-catalog'
        case.kube.lines = [access(case.h.now - 1, 200), access(case.h.now + 0.03, 200)]
        time.sleep(0.08)
        case.h.now = time.time()
        recovered = case.run_job('recovery')['recovery']
        assert (recovered['outcome'], recovered['method']) == ('passed', 'original_error_absent'), recovered
        assert case.detail()['state'] == 'completed'
        previous = case.detail()['data']['release']
        note = {
            'request_id': 'completed-note-01',
            'kind': 'revise',
            'note_ko': '다음 점검에서 상품 검색 응답 시간을 다시 살핍니다.',
        }
        submitted = case.human('supplements', note)
        assert submitted.status_code == 200, submitted.text
        repeated = case.h.client.post(PREFIX + '/incidents/' + case.child + '/supplements', headers=HUMAN, json=note)
        assert repeated.status_code == 200 and repeated.json()['followup']['id'] == submitted.json()['followup']['id']
        conflict = case.human('supplements', {**note, 'note_ko': '다른 내용의 점검 메모'})
        assert conflict.status_code == 409
        document = case.detail()
        assert document['state'] == 'completed' and len(document['data']['followups']) == 1
        assert (
            document['data']['release'] == previous
            and case.claim('qa', 'qa') is None
            and case.claim('source', 'test_build') is None
        )
        assert [row[0] for row in case.kube.writes] == ['dev', 'prd']
        assert len(set(case.job_ids)) == len(case.job_ids)


@pytest.mark.parametrize('repair_controller', [False, True], ids=['still-missing', 'registered-test-added'])
def test_missing_registered_scenario_starts_one_round(tmp_path, monkeypatch, repair_controller):
    with ExitStack() as stack:
        case = Acceptance(stack, tmp_path, True, monkeypatch)
        case.provider.repair_controller = repair_controller
        case.to_build()
        first = case.to_qa()['qa']
        assert first['outcome'] == 'tests_missing'
        middle = case.detail()
        assert middle['state'] == 'collecting' and middle['data']['round'] == 2
        assert middle['data']['rounds'][0]['qa_history'][0]['receipt_id'] == first['receipt_id']
        case.run_job('collect')
        case.run_job('source')
        second = case.to_qa()['qa']
        last = case.detail()
        if repair_controller:
            assert case.provider.repair_request['qa']['receipt_id'] == first['receipt_id']
            requirement = case.provider.repair_request['qa_requirement']
            assert requirement['test_file'] == CATALOG_TEST and requirement['test_cases'] == [
                'CatalogTests.test_search_detail_and_prices'
            ]
            assert requirement == middle['data']['round_request']['qa_requirement']
            assert second['outcome'] == 'passed' and last['state'] == 'awaiting_prd_approval'
            assert first['release_id'] != second['release_id'] and first['image'] != second['image']
            assert second['developer_tests'][0]['file'] == CATALOG_TEST
            expected = CATALOG_TEST + '::CatalogTests::test_search_detail_and_prices'
            executions = last['data']['release']['tests']['executed_tests']
            assert {(r['phase'], r['status']) for r in executions if r['id'] == expected} == {
                ('before', 'failed'),
                ('after', 'passed'),
            }
            assert last['data']['release']['qa']['accepted'] is True
            assert all(row['status'] == 'passed' for row in second['steps'])
        else:
            assert second['outcome'] == 'tests_missing' and last['state'] == 'handoff'
            assert last['stages'][-1]['details']['code'] == 'qa_required_test_missing'
        assert len(last['data']['qa_history']) == 1 and last['data']['round'] == 2
        assert not any(row[0] == 'prd' for row in case.kube.writes)


@pytest.mark.parametrize(
    ('capability', 'stage', 'state'), [('qa', 'dev', 'qa_queued'), ('recovery', 'prd', 'awaiting_recovery')]
)
def test_standalone_v3_requires_a_scoped_machine(capability, stage, state):
    from test_core_support import MACHINE

    h = Harness(qa_required=True)
    try:
        child = h.create()
        control = h.app.state.control
        with control.store.transaction() as session:
            incident = control.get(session, child)
            control.data(
                incident,
                'release',
                {'protocol': control.new_release_protocol(), 'release_id': 'isolated', 'valid': True},
            )
            control.transition(session, incident, state)
            control.enqueue(session, incident, capability, {'stage': stage, 'release_id': 'isolated'})
        response = h.client.post(
            PREFIX + '/runner/claim', headers=MACHINE, json={'worker_id': 'legacy', 'capabilities': [capability]}
        )
        # Servers may refuse the runner or filter unscoped capabilities, but cannot grant this real queued job.
        assert response.status_code == 403 or (response.status_code == 200 and response.json()['job'] is None)
    finally:
        h.close()


def test_unsettled_dev_pod_image_cannot_supply_qa_identity(tmp_path):
    cluster = LocalCluster()
    registry = Registry(ROOT)
    executor = Executor(registry, cluster, IsolatedHost(tmp_path), tmp_path, {'runner_role': 'qa'})
    assert executor.dev_identity(SERVICE, 'dev-catalog', OLD)
    cluster.pods = lambda env, service: [
        {'image_id': 'liveprobe/catalog@sha256:' + '2' * 64, 'ready': True, 'owned': True}
    ]
    assert executor.dev_identity(SERVICE, 'dev-catalog', OLD) is None
