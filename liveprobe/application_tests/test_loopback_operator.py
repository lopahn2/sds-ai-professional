from dataclasses import replace
from copy import deepcopy
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from app import local_operator as operator
from app.auth import require_user
from app.human_sessions import local_read_key
import test_core_release
from test_core_analysis import SyntheticTransport
from test_core_support import Harness, PREFIX


def credentials(tmp_path):
    read = tmp_path / 'read'
    write = tmp_path / 'write'
    for path, value in ((read, 'r'*64), (write, 'w'*64)):
        path.write_text(value)
        path.chmod(0o600)
    return read, write


def signed(method, path, token, host, key=b'w'*64):
    return {'Host': host, 'Origin': 'http://'+host, 'X-LiveProbe-Client': 'dashboard',
            'X-LiveProbe-Local-Read': 'r'*64, 'Cookie': operator.COOKIE+'='+token,
            operator.CSRF: token, operator.HEADER: operator.proof(key, method, path, host, token)}


def test_gateway_bound_human_approves_exactly_tested_release_without_identity_file(tmp_path):
    with_human = Harness(SyntheticTransport())
    try:
        flow = test_core_release.CoreReleaseTests()
        flow.h = with_human
        incident = flow.awaiting()
        release = flow.release(incident)
        read, write = credentials(tmp_path)
        with_human.app.state.settings = replace(with_human.settings, local_read_key_file=str(read),
                                                local_operator_key_file=str(write), identity_file=None)
        with_human.app.state.local_read_key = local_read_key(str(read))
        body = {k: release[k] for k in ('release_id', 'artifact_sha256', 'plan_sha256')}
        body['image'] = release['artifact']['image']
        token = operator.new_token(b'w'*64)
        base = PREFIX + '/incidents/' + incident
        def post(route, payload, override=None):
            headers = signed('POST', base+route, token, 'testserver')
            return with_human.client.post(base+route, headers={**headers, **(override or {})}, json=payload)
        test_data = {**body, 'passed': True, 'notes_ko': 'dev 주문 검증 통과'}
        assert post('/user-test', test_data, {operator.CSRF: 'wrong'}).status_code == 403
        assert post('/user-test', test_data, {'Origin':'http://different.localhost'}).status_code == 403
        assert post('/user-test', test_data, {operator.HEADER:'invalid'}).status_code == 403
        recorded = post('/user-test', test_data)
        assert recorded.status_code == 200, recorded.text
        assert recorded.json()['user_test']['subject'] == operator.SUBJECT
        body['user_test_id'] = recorded.json()['user_test']['id']
        mismatch = deepcopy(body)
        mismatch['plan_sha256'] = '9'*64
        assert post('/approve-prd', mismatch).status_code == 409
        accepted = post('/approve-prd', body)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['approval']['subject'] == operator.SUBJECT
        assert post('/approve-prd', body).status_code == 409
        # A stored local approval must stop authorizing its queued release if the local boundary is disabled.
        write.unlink()
        assert with_human.claim(['deploy']) is None
    finally:
        with_human.close()


def test_k8s_read_key_cannot_authorize_writes_or_relabel_proof(tmp_path, monkeypatch):
    read, write = credentials(tmp_path)
    monkeypatch.setenv('LIVEPROBE_LOCAL_OPERATOR_KEY_FILE', str(write))
    monkeypatch.setenv('LIVEPROBE_LOCAL_READ_KEY_FILE', str(read))
    app = FastAPI()
    @app.post('/api/settings')
    def example(actor=Depends(require_user)):
        return actor
    client = TestClient(app, base_url='http://liveprobe.localhost')
    key = write.read_bytes()
    token = operator.new_token(key)
    path = '/api/settings'
    headers = signed('POST', path, token, 'liveprobe.localhost')
    assert client.post(path, headers=headers).status_code == 200
    for change in ({operator.HEADER: ''}, {operator.CSRF: ''}, {'Origin': ''}, {'Host':'other.localhost'},
                   {'Sec-Fetch-Site':'cross-site'}, {operator.HEADER:operator.proof(key,'GET',path,'liveprobe.localhost',token)},
                   {operator.HEADER:operator.proof(key,'POST','/api/settings/else','liveprobe.localhost',token)}):
        assert client.post(path, headers={**headers,**change}).status_code == 403
    write.write_bytes(read.read_bytes())
    assert client.post(path, headers=headers).status_code == 503
    for path in ('/api/alerts', '/api/hooks/alertmanager', '/api/runner/claim', PREFIX+'/runner/jobs/a/result',
                 PREFIX+'/incidents/i/review-dev-metadata', PREFIX+'/human-sessions/approve'):
        assert not operator.human_path(path)
