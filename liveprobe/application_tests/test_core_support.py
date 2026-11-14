"""합성 identity/증거만 사용하는 공통 시험 장치."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from fastapi.testclient import TestClient

PREFIX = '/api/application/v1'
MACHINE = {'Authorization': 'Bearer synthetic-machine-only'}
HUMAN = {'Authorization': 'Bearer synthetic-human-only'}
IMAGE = 'local/order@sha256:' + '1' * 64
NEW_IMAGE = 'local/order@sha256:' + '2' * 64
NOW = 1900000000.0


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def target(env='dev', service='order'):
    return {'environment': env, 'service': service, 'system': 'samsung-trade',
            'uid': 'synthetic-' + env, 'resource_version': '10', 'container': service,
            'image': IMAGE, 'template_sha256': '3' * 64}


class Harness:
    def __init__(self, provider=None, **settings_overrides):
        from app.application.main import create_app
        from app.application.settings import ApplicationSettings
        self.tmp = tempfile.TemporaryDirectory(prefix='lp-app-core-', dir=os.environ.get('TMPDIR'))
        self.path = Path(self.tmp.name)
        self.now = NOW
        identity = self.path / 'synthetic-human.json'
        machine = self.path / 'synthetic-machine.json'
        identity.write_text(json.dumps({'principals': [{'subject': 'synthetic-tester',
            'token': 'synthetic-human-only', 'scopes': ['incident:read', 'incident:create', 'tester', 'prd-approver']}]}))
        machine.write_text(json.dumps({'principals': [{'subject': 'synthetic-worker',
            'token': 'synthetic-machine-only', 'scopes': ['runner']}]}))
        self.settings = ApplicationSettings(database_url=f'sqlite:///{self.path}/only-tests.db', testing=True,
            identity_file=str(identity), machine_identity_file=str(machine), lease_seconds=30, **settings_overrides)
        self.app = create_app(self.settings, provider=provider, clock=lambda: self.now)
        self.app.state.store.initialize()
        self.client = TestClient(self.app)

    def close(self):
        self.client.close()
        self.app.state.store.engine.dispose()
        self.tmp.cleanup()

    def create(self, service='order'):
        response = self.client.post(PREFIX + '/incidents', headers=HUMAN,
            json={'environment': 'dev', 'service': service, 'reason': '주문 실패 token=synthetic-sensitive-value'})
        assert response.status_code == 201, response.status_code
        return response.json()['id']

    def claim(self, kinds=None):
        response = self.client.post(PREFIX + '/runner/claim', headers=MACHINE,
            json={'worker_id': 'synthetic-worker', 'capabilities': kinds or ['collect', 'source', 'test_build', 'deploy', 'verify', 'reconcile']})
        assert response.status_code == 200, response.status_code
        return response.json()['job']

    def report(self, job, result):
        return self.client.post(PREFIX + '/runner/jobs/' + job['id'] + '/result', headers=MACHINE,
            json={'lease_token': job['lease_token'], 'fencing_token': job['fencing_token'], 'result': result})

    def detail(self, incident):
        return self.client.get(PREFIX + '/incidents/' + incident, headers=HUMAN).json()

    def collect(self):
        return {'ok': True, 'target': target(), 'logs': [{'service': 'order', 'environment': 'dev', 'observed_at': '2030-03-17T17:46:40Z',
            'text': 'order failed token=synthetic-sensitive-value'}],
            'evidence': [{'id': 'E1-order', 'service': 'order', 'environment': 'dev',
                'observed_at': '2030-03-17T17:46:40Z', 'source': 'pod_log',
                'text': 'order failed', 'quality': 'observed'}], 'observed_at': '2030-03-17T17:46:40Z'}

    def source(self, verified=True):
        body = 'def run():\n    return 1 / 0\n'
        return {'ok': True, 'source_snapshot': {'id': 'snapshot-one', 'base_commit': 'a' * 40,
            'base_image': IMAGE, 'files': {'src/main.py': body}, 'file_sha256': {'src/main.py': digest(body)},
            'profile_id': 'order', 'affected_services': ['order'], 'provenance_verified': verified}}

    def seed_source(self, verified=True):
        incident = self.create()
        job = self.claim(['collect'])
        assert self.report(job, self.collect()).status_code == 200
        job = self.claim(['source'])
        assert self.report(job, self.source(verified)).status_code == 200
        return incident
