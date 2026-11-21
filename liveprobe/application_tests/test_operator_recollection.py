import copy

import pytest

from test_core_analysis import SyntheticTransport
from test_core_support import Harness, PREFIX, HUMAN, digest


@pytest.fixture
def case():
    h = Harness(SyntheticTransport())
    try:
        yield h
    finally:
        h.close()


def request_collection(h):
    sample = 'original request returned 500'
    signal = {'observed_at': '2020-02-03T10:20:30Z', 'sample': sample,
              'sha256': digest(sample), 'source': 'pod_log'}
    created = h.client.post(PREFIX + '/incidents', headers=HUMAN, json={
        'environment': 'dev', 'service': 'order', 'reason': '주문 조회 실패', 'signal': signal})
    assert created.status_code == 201
    incident = created.json()['id']
    assert h.report(h.claim(['collect']), h.collect()).status_code == 200
    assert h.report(h.claim(['source']), h.source()).status_code == 200
    assert h.app.state.worker.run_once()
    assert h.detail(incident)['state'] == 'test_build_queued'
    prior = h.detail(incident)['data']['collection']
    payload = {'request_id': 'recollect-one', 'note_ko': '현재 버전의 근거를 다시 수집합니다.', 'kind': 'revise'}
    response = h.client.post(PREFIX + '/incidents/' + incident + '/supplements', headers=HUMAN, json=payload)
    assert response.status_code == 200 and response.json()['state'] == 'collecting'
    job = h.claim(['collect'])
    quiet = h.collect()
    quiet['logs'] = []
    quiet['evidence'][0].update(source='deployment', text='Deployment ready replicas 1/1')
    return incident, job, quiet, prior


def test_quiet_recollection_preserves_historical_evidence_and_pins_current_source(case):
    h = case
    incident, job, result, previous = request_collection(h)
    assert h.report(job, result).status_code == 200
    assert h.report(job, result).status_code == 200
    saved = h.detail(incident)
    assert saved['state'] == 'source_pinning'
    collection = saved['data']['collection']
    assert collection['log_window'] == {'since': job['payload']['since'], 'log_batches': 0}
    assert not collection['logs']
    assert saved['data']['collection_history'] == [previous]
    source = h.claim(['source'])
    assert source['payload']['target'] == result['target']
    assert h.report(source, h.source()).status_code == 200
    assert h.claim(['source']) is None
    document = h.detail(incident)
    grounded = h.app.state.worker._evidence(document)
    assert grounded['E1-order'] == 'Deployment ready replicas 1/1'
    assert grounded['original:signal'] == 'original request returned 500'
    original = h.app.state.worker._original_signal(document)
    assert original['observed_at'].startswith('2020-02-03')
    changed = copy.deepcopy(document)
    changed['data']['signal']['sample'] = 'tampered'
    assert 'original:signal' not in h.app.state.worker._evidence(changed)


@pytest.mark.parametrize('mutation,reason', [
    ('missing_logs', 'collect_evidence_missing'), ('missing_evidence', 'collect_evidence_missing'),
    ('other_environment', 'collect_evidence_scope_invalid'), ('unanchored', 'collect_evidence_missing'),
])
def test_incomplete_or_unbound_quiet_report_does_not_queue_source(case, mutation, reason):
    h = case
    incident, job, report, previous = request_collection(h)
    if mutation == 'missing_logs':
        del report['logs']
    if mutation == 'missing_evidence':
        report['evidence'] = []
    if mutation == 'other_environment':
        report['evidence'][0]['environment'] = 'prd'
    if mutation == 'unanchored':
        report['evidence'][0]['source'] = 'pod_log'
    assert h.report(job, report).status_code == 200
    document = h.detail(incident)
    assert document['state'] == 'handoff'
    assert document['error']['code'] == reason
    assert document['data']['collection'] == previous
    assert h.claim(['source']) is None


def test_initial_quiet_collection_retains_previous_gate(case):
    incident = case.create()
    quiet = case.collect()
    quiet['logs'] = []
    assert case.report(case.claim(['collect']), quiet).status_code == 200
    assert case.detail(incident)['error']['code'] == 'collect_evidence_missing'


def test_existing_collect_handoff_can_be_retried_by_operator_without_erasing_it(case):
    h = case
    incident, job, quiet, previous = request_collection(h)
    incomplete = dict(quiet, evidence=[])
    assert h.report(job, incomplete).json()['state'] == 'handoff'
    detail_path = PREFIX + '/incidents/' + incident
    body = {'request_id': 'retry-after-handoff', 'kind': 'revise', 'note_ko': '현재 배포 관측을 다시 대조합니다.'}
    for _ in range(2):
        response = h.client.post(detail_path + '/supplements', headers=HUMAN, json=body)
        assert response.json()['supplement']['applied_round'] == 3
    retry = h.claim(['collect'])
    assert retry['id'] != job['id']
    assert h.claim(['collect']) is None
    assert h.report(retry, quiet).json()['state'] == 'source_pinning'
    saved = h.detail(incident)
    assert saved['data']['collection_history'] == [previous]
    assert [stage['details'].get('code') for stage in saved['stages'] if stage['state'] == 'handoff'] == ['collect_evidence_missing']
    assert not h.claim(['deploy'])


def test_original_request_attribution_uses_historical_collection_after_quiet_window():
    from app.application.current_version import original_check
    from app.application.qa_release import original_operation

    raw = '{"service":"catalog","environment":"dev","event":"http_access","method":"GET","uri":"/api/products","status":500}'
    older = {'logs': [{'service': 'catalog', 'environment': 'dev', 'text': raw}]}
    data = {'collection_history': [older], 'collection': {'logs': [], 'evidence': []}}
    document = {'data': data, 'service': 'catalog', 'environment': 'dev'}
    profile = {'verification': {'path': '/api/products', 'kind': 'catalog'}}
    op, error = original_check(document, profile)
    assert error is None and op['source'] == 'collection_history[0].logs'
    assert original_operation(document, profile)['source'] == op['source']
    newer = dict(document, environment='prd')
    assert original_check(newer, profile)[0] is None
    assert original_operation(newer, profile) is None
