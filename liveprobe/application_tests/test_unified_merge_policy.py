"""실제 네트워크 없이 PR/사람/시험 소스 지문과 경합을 검증한다."""
import base64
import copy
import hashlib
import json

import pytest

from app.application.merge_policy import GitHubMergeObserver, MergeBlocked
from app.application.safety import sha


@pytest.fixture
def observer(tmp_path):
    config = {'pull_request_policy': {'auto_merge': False, 'human_merge_required_before_prd': True,
                                     'each_promotion_requires_confirmation': True},
              'systems': {'x': {'repository': 'owner/source', 'base_branch': 'main',
                                  'services': {'order': {'source_subdir': 'service'}}}}}
    file = tmp_path / 'repositories.json'
    file.write_text(json.dumps(config))
    body = b'def run():\n    return 1\n'
    blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
    commit = 'a' * 40
    responses = {
        '/repos/owner/source/pulls/1': {'merged': True, 'state': 'closed', 'merged_at': '2026-11-16T00:00:00Z',
            'merged_by': {'type': 'User', 'login': 'human'}, 'merge_commit_sha': commit,
            'base': {'ref': 'main', 'repo': {'full_name': 'owner/source'}},
            'head': {'repo': {'full_name': 'owner/source'}}},
        '/repos/owner/source/git/ref/heads/main': {'object': {'sha': commit}},
        f'/repos/owner/source/git/trees/{commit}?recursive=1': {'truncated': False, 'tree': [
            {'path': 'service/main.py', 'type': 'blob', 'mode': '100644', 'size': len(body), 'sha': blob}]},
        f'/repos/owner/source/git/blobs/{blob}': {'encoding': 'base64', 'size': len(body),
                                                'content': base64.b64encode(body).decode()}}
    transport = lambda path: copy.deepcopy(responses[path])
    obj = GitHubMergeObserver(str(file), get_json=transport)
    obj.expected = sha({'main.py': hashlib.sha256(body).hexdigest()})
    obj.responses = responses
    return obj


def test_human_merge_of_exact_tested_source(observer):
    result = observer.observe(service='order', pr_number=1, source_tree_sha256=observer.expected)
    assert result['human_merge_observed'] is True
    assert result['source_tree_sha256'] == observer.expected


@pytest.mark.parametrize('change,code', [
    ({'merged': False}, 'human_merge_required'),
    ({'merged_by': {'type': 'Bot', 'login': 'robot'}}, 'human_merge_required'),
    ({'base': {'ref': 'other', 'repo': {'full_name': 'owner/source'}}}, 'human_merge_required'),
    ({'head': {'repo': {'full_name': 'attacker/fork'}}}, 'human_merge_required')])
def test_pr_not_human_registered_merge(observer, change, code):
    observer.responses['/repos/owner/source/pulls/1'].update(change)
    with pytest.raises(MergeBlocked, match=code):
        observer.observe(service='order', pr_number=1, source_tree_sha256=observer.expected)


def test_source_mismatch_blocks(observer):
    with pytest.raises(MergeBlocked, match='merged_source_differs'):
        observer.observe(service='order', pr_number=1, source_tree_sha256='b' * 64)


def test_changed_main_blocks(observer):
    observer.responses['/repos/owner/source/git/ref/heads/main']['object']['sha'] = 'b' * 40
    with pytest.raises(MergeBlocked, match='merged_main_changed'):
        observer.observe(service='order', pr_number=1, source_tree_sha256=observer.expected)


def test_truncated_tree_blocks(observer):
    observer.responses[f'/repos/owner/source/git/trees/{"a" * 40}?recursive=1']['truncated'] = True
    with pytest.raises(MergeBlocked, match='merge_tree_incomplete'):
        observer.observe(service='order', pr_number=1, source_tree_sha256=observer.expected)


def test_blob_corruption_blocks(observer):
    key = next(k for k in observer.responses if '/blobs/' in k)
    observer.responses[key]['content'] = base64.b64encode(b'evil').decode()
    with pytest.raises(MergeBlocked):
        observer.observe(service='order', pr_number=1, source_tree_sha256=observer.expected)


def test_unregistered_service_cannot_pick_repository(observer):
    with pytest.raises(MergeBlocked, match='repository_policy_unconfigured'):
        observer.observe(service='unregistered', pr_number=1, source_tree_sha256=observer.expected)


def test_main_changes_during_blob_observation(observer):
    original = observer.get_json
    count = 0
    def get(path):
        nonlocal count
        value = original(path)
        if '/git/ref/' in path:
            count += 1
            if count == 2:
                value['object']['sha'] = 'b' * 40
        return value
    observer.get_json = get
    with pytest.raises(MergeBlocked, match='merged_main_changed'):
        observer.observe(service='order', pr_number=1, source_tree_sha256=observer.expected)


def seeded_review():
    from test_core_support import Harness
    from app.application.storage import Incident
    h = Harness(repositories_file='synthetic-registration-only')
    iid = h.create()
    c = h.app.state.control
    artifact = {'image': 'local/order@sha256:' + '1' * 64, 'source_tree_sha256': '2' * 64}
    release = {'protocol': 'dev-prd/v2', 'release_id': 'synthetic-release', 'valid': True,
               'artifact': artifact, 'artifact_sha256': sha(artifact), 'targets': {},
               'affected_services': ['order']}
    release['plan_sha256'] = c.release_fingerprint(release)
    with h.app.state.store.transaction() as session:
        incident = session.get(Incident, iid)
        incident.state = 'awaiting_user_test'
        c.data(incident, 'release', release)
    body = {k: release[k] for k in ('release_id', 'artifact_sha256', 'plan_sha256')}
    body.update(image=artifact['image'], pr_number=1)
    return h, iid, body


def test_review_route_scopes_fingerprint_and_fresh_callback(monkeypatch):
    from test_core_support import HUMAN, MACHINE, PREFIX
    calls = []
    def observed(self, **kwargs):
        calls.append(kwargs)
        return {'repository': 'owner/source', 'pr_number': 1, 'url': 'https://github.com/owner/source/pull/1',
                'merge_commit': 'a' * 40, 'merged_by': 'human', 'source_tree_sha256': '2' * 64,
                'human_merge_observed': True}
    monkeypatch.setattr(GitHubMergeObserver, 'observe', observed)
    h, iid, body = seeded_review()
    try:
        url = PREFIX + f'/incidents/{iid}/review-merge'
        assert h.client.post(url, json=body, headers=MACHINE).status_code == 403
        assert not calls
        bad = dict(body, artifact_sha256='b' * 64)
        assert h.client.post(url, json=bad, headers=HUMAN).status_code == 409
        assert not calls
        result = h.client.post(url, json=body, headers=HUMAN)
        assert result.status_code == 200, result.text
        assert result.json()['merge_review']['human_merge_observed'] is True
        document = h.detail(iid)
        assert h.app.state.control.merge_authorized(document)
        assert len(calls) == 2  # approval-time gate does a fresh independent read
        document['data']['release']['plan_sha256'] = 'b' * 64
        assert h.app.state.control.merge_authorized(document) is False
        assert len(calls) == 2
    finally:
        h.close()


def test_review_does_not_save_proof_if_state_changes_during_remote_read(monkeypatch):
    from test_core_support import HUMAN, PREFIX
    from app.application.storage import Incident
    h, iid, body = seeded_review()
    def changed(self, **kwargs):
        with h.app.state.store.transaction() as session:
            row = session.get(Incident, iid)
            row.state = 'handoff'
        return {'human_merge_observed': True, 'merge_commit': 'a' * 40}
    monkeypatch.setattr(GitHubMergeObserver, 'observe', changed)
    try:
        assert h.client.post(PREFIX + f'/incidents/{iid}/review-merge', json=body, headers=HUMAN).status_code == 409
        assert 'merge_review' not in h.detail(iid)['data']
    finally:
        h.close()
