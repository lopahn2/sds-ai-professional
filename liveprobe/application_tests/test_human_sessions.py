"""human-session/v1와 local-read/v1: 실제 HTTP 경계, 합성 identity만 사용."""
import json
import importlib.util
from pathlib import Path
import threading
import urllib.request
import pytest
from app import human_sessions as H
from test_core_support import Harness, PREFIX, HUMAN, MACHINE

BROWSER = {'X-LiveProbe-Client': 'dashboard', 'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin'}

@pytest.fixture
def h():
    h = Harness()
    yield h
    h.close()


def login(h, scope='tester', headers=None):
    response = h.client.post(PREFIX + '/human-sessions', headers=BROWSER, json={'scope': scope})
    assert response.status_code == 201
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie'].lower().replace('samesite', 'SameSite')
    pending = response.json()
    result = h.client.post(PREFIX + '/human-sessions/approve', headers=headers or HUMAN,
                          json={'scope': scope, 'user_code': pending['user_code']})
    return pending, result


def test_role_click_is_pending_runner_cannot_approve_and_replay_fails(h):
    pending, result = login(h, headers=MACHINE)
    assert result.status_code == 403 and result.json()['code'] == 'identity_kind_forbidden'
    assert h.app.state.human_sessions.view(h.client.cookies.get(H.COOKIE))['state'] == 'pending'
    body = {'scope': 'tester', 'user_code': pending['user_code']}
    assert h.client.post(PREFIX + '/human-sessions/approve', headers={**HUMAN, **BROWSER}, json=body).status_code == 403
    assert h.client.post(PREFIX + '/human-sessions/approve', headers=HUMAN, json=body).status_code == 200
    assert h.client.post(PREFIX + '/human-sessions/approve', headers=HUMAN, json=body).status_code == 404
    assert not h.app.state.human_sessions.actor(h.client.cookies.get(H.COOKIE), 'tester')['subject'].startswith('local-')


def test_cookie_csrf_scopes_expiry_revoke_and_machine_separation(h):
    iid = h.create()
    _, response = login(h)
    assert response.status_code == 200
    view = h.client.get(PREFIX + '/human-sessions/current').json()
    csrf = view['csrf_token']
    assert view['scopes'] == ['incident:read', 'tester']
    assert h.client.get(PREFIX + '/incidents/' + iid).status_code == 200
    create = {'environment': 'dev', 'service': 'order', 'reason': '합성'}
    assert h.client.post(PREFIX + '/incidents', json=create, headers={**BROWSER, 'X-CSRF-Token': csrf}).status_code == 403
    # 세션과 CSRF가 있어도 실행기 API를 호출할 수 없다.
    assert h.client.post(PREFIX + '/runner/claim', json={'worker_id': 'synthetic', 'capabilities': ['collect']}, headers={**BROWSER, 'X-CSRF-Token': csrf}).status_code == 401
    assert h.client.delete(PREFIX + '/human-sessions/current', headers=BROWSER).status_code == 403
    assert h.client.delete(PREFIX + '/human-sessions/current', headers={**BROWSER, 'X-CSRF-Token': csrf, 'Origin': 'http://evil.invalid'}).status_code == 403
    assert h.client.post(PREFIX + '/human-sessions/revoke', headers=HUMAN).json()['revoked'] == 1
    assert h.client.get(PREFIX + '/incidents/' + iid).status_code == 401
    login(h)
    h.now += 901
    assert h.client.get(PREFIX + '/human-sessions/current').json()['state'] == 'expired'


def test_operator_login_keeps_credential_scope_and_one_shot_approval(h):
    current = PREFIX + '/human-sessions/current'
    assert 'operator' in h.client.get(current).json()['session_modes']
    _, rejected = login(h, 'operator', MACHINE)
    assert rejected.status_code == 403 and rejected.json()['code'] == 'identity_kind_forbidden'
    _, approved = login(h, 'operator')
    assert approved.status_code == 200
    session = h.client.get(current).json()
    assert session['scope'] == 'operator' and set(session['scopes']) == set(H.SCOPES)
    assert session['expires_at'] == h.now + H.APPROVER_SECONDS
    cookie = h.client.cookies.get(H.COOKIE)
    body = {'environment':'dev','service':'order','reason':'폐기 DB의 운영자 세션 시험'}
    assert h.client.post(PREFIX + '/incidents', json=body, headers=BROWSER).status_code == 403
    headers = {**BROWSER,'X-CSRF-Token':session['csrf_token']}
    assert h.client.post(PREFIX + '/incidents', json=body, headers=headers).is_success
    store = h.app.state.human_sessions
    for scope in ('tester','prd-approver'):
        assert store.actor(cookie, scope, csrf=session['csrf_token'], mutation=True)['subject'] == session['subject']
    with pytest.raises(H.SessionError):
        store.actor(cookie, 'runner')
    store.once(cookie, lambda: store.actor(cookie, 'prd-approver', csrf=session['csrf_token'], mutation=True))
    assert h.client.get(current).json()['state'] == 'expired'


def test_operator_never_adds_another_credential_and_obeys_rotation(h):
    path = Path(h.settings.identity_file)
    first = {'subject':'same-person','token':'synthetic-human-only','scopes':['tester','incident:read']}
    second = {'subject':'same-person','token':'synthetic-other-human','scopes':['prd-approver','incident:read']}
    path.write_text(json.dumps({'principals':[first,second]}))
    _, result = login(h,'operator')
    assert result.json()['scopes'] == ['incident:read','tester']
    store = h.app.state.human_sessions
    cookie = h.client.cookies.get(H.COOKIE)
    with pytest.raises(H.SessionError,match='scope_forbidden'):
        store.actor(cookie,'prd-approver')
    first['scopes'] = ['tester','prd-approver']
    path.write_text(json.dumps({'principals':[first,second]}))
    assert h.client.get(PREFIX + '/human-sessions/current').json()['scopes'] == ['tester']
    first['token'] = 'synthetic-replacement-human'
    path.write_text(json.dumps({'principals':[first,second]}))
    assert h.client.get(PREFIX + '/human-sessions/current').json()['state'] == 'expired'


def test_removed_permission_is_rechecked_and_one_shot_is_atomic(h):
    login(h, 'prd-approver')
    cookie = h.client.cookies.get(H.COOKIE)
    store = h.app.state.human_sessions
    csrf = store.view(cookie)['csrf_token']
    successes = []
    def apply():
        def mutation():
            store.actor(cookie, 'prd-approver', mutation=True, csrf=csrf)
            successes.append(1)
        try:
            store.once(cookie, mutation)
        except H.SessionError:
            pass
    threads = [threading.Thread(target=apply) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert successes == [1]
    login(h)
    path = Path(h.settings.identity_file)
    path.write_text(json.dumps({'principals': [{'subject': 'synthetic-tester', 'token': 'synthetic-human-only', 'scopes': ['tester']}]}))
    assert h.client.get(PREFIX + '/incidents/nonexistent').status_code == 403


def test_minimum_summary_requires_real_gateway_key_and_omits_raw_data(h):
    iid = h.create()
    path = h.path / 'local-key'
    key = 'synthetic-local-read-key-' + 'x' * 32
    path.write_text(key); path.chmod(0o600)
    h.app.state.local_read_key = H.local_read_key(str(path))
    assert h.client.get(PREFIX + '/summaries', headers={'Host': 'localhost', 'X-Forwarded-For': '127.0.0.1'}).status_code == 403
    result = h.client.get(PREFIX + '/summaries', headers={H.LOCAL_READ_HEADER: key})
    assert result.status_code == 200
    item = result.json()['incidents'][0]
    assert item['id'] == iid and item['projection'] == 'local-read/v1'
    assert 'data' not in item and 'stages' not in item and 'error' not in item
    assert 'synthetic-sensitive-value' not in json.dumps(item)
    assert h.client.get(PREFIX + '/incidents/' + iid, headers={H.LOCAL_READ_HEADER: key}).status_code == 401
    path2 = h.path / 'weak'; path2.write_text(key); path2.chmod(0o644)
    assert not H.LocalReadKey(str(path2)).verify({H.LOCAL_READ_HEADER: key})


def test_start_scope_escalation_cross_site_and_logout(h):
    assert h.client.post(PREFIX + '/human-sessions', headers={**BROWSER, 'Origin': 'http://evil.invalid'}, json={'scope': 'tester'}).status_code == 403
    assert h.client.post(PREFIX + '/human-sessions', headers=BROWSER, json={'scope': 'runner'}).status_code == 422
    login(h)
    view = h.client.get(PREFIX + '/human-sessions/current').json()
    assert h.client.delete(PREFIX + '/human-sessions/current', headers={**BROWSER, 'X-CSRF-Token': view['csrf_token']}).status_code == 200
    assert h.client.get(PREFIX + '/human-sessions/current').json()['state'] == 'none'


def test_original_html_address_redirects_only_documents(h):
    from app.application.browser_access import install_redirect
    install_redirect(h.app, 'http://127.0.0.1:9876', H.LocalReadKey(None),
                     legacy_application_host='application.liveprobe.localhost')
    response = h.client.get('/', headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 307 and response.headers['location'] == 'http://127.0.0.1:9876/'
    old = {'Accept': 'text/html', 'Host': 'application.liveprobe.localhost'}
    bookmarked = h.client.get('/?incident=native-saved', headers=old, follow_redirects=False)
    assert bookmarked.headers['location'] == 'http://127.0.0.1:9876/application?incident=native-saved'
    assert h.client.get(PREFIX + '/healthz', headers=old, follow_redirects=False).status_code == 200
    assert h.client.get(PREFIX + '/healthz', headers={'Accept': 'text/html'}, follow_redirects=False).status_code == 200
