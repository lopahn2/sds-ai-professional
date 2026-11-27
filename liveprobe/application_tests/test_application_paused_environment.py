from sqlalchemy import select

from app.application.storage import Incident, Job, SourceInquiry
from app.config import AppConfig
from app.source_investigation import claim as claim_source
from test_core_support import Harness, HUMAN, PREFIX


def test_paused_prd_is_readable_and_excluded_from_new_intake_and_claims(monkeypatch):
    harness = Harness()
    try:
        response = harness.client.post(PREFIX + '/incidents', headers=HUMAN,
            json={'environment': 'prd', 'service': 'order', 'reason': 'old PRD incident'})
        assert response.status_code == 201
        prd = response.json()['id']
        dev = harness.create()
        config = AppConfig(target_namespaces=['dev'], historical_namespaces=['stg', 'prd'])
        monkeypatch.setattr('app.config.get_app_config', lambda: config)

        catalog = harness.client.get(PREFIX + '/catalog').json()
        assert catalog['environments'] == ['dev']
        assert catalog['historical_environments'] == ['dev', 'stg', 'prd']
        refused = harness.client.post(PREFIX + '/incidents', headers=HUMAN,
            json={'environment': 'prd', 'service': 'order', 'reason': 'new PRD incident'})
        assert refused.status_code == 409 and refused.json()['code'] == 'environment_paused'
        job = harness.claim(['collect'])
        assert job['incident_id'] == dev
        assert harness.claim(['collect']) is None
        assert harness.detail(prd)['state'] == 'collecting'

        with harness.app.state.store.transaction() as session:
            assert session.scalar(select(Job).where(Job.incident_id == prd)).status == 'queued'
            session.get(Incident, prd).state = 'analysis_queued'
            session.add(Job(id='queued-prd-promotion', incident_id=dev, kind='deploy', payload={'stage': 'prd'},
                            created_at=harness.now, status='queued', idempotency_key='queued-prd-promotion'))
            for index, environment in enumerate(('prd', 'dev'), 1):
                session.add(SourceInquiry(id='probe_' + environment, incident_id=index, created_at=harness.now + index,
                                          expires_at=harness.now + 60, payload={'environment': environment}, state='queued'))
        assert harness.app.state.worker._claim() is None
        assert harness.detail(prd)['state'] == 'analysis_queued'
        assert harness.claim(['deploy']) is None
        assert claim_source(harness.app.state.control, {'subject': 'fixture', 'runner_role': 'source'})['id'] == 'probe_dev'
        approval = harness.client.post(PREFIX + '/incidents/' + dev + '/approve-prd', headers=HUMAN,
            json={'release_id': 'fixture', 'artifact_sha256': 'fixture', 'plan_sha256': 'fixture', 'image': 'fixture'})
        assert approval.status_code == 409 and approval.json()['code'] == 'environment_paused'
        with harness.app.state.store.read() as session:
            assert session.get(Job, 'queued-prd-promotion').status == 'queued'
            assert session.get(SourceInquiry, 'probe_prd').state == 'queued'
    finally:
        harness.close()


def test_claim_skips_paused_and_owned_rows_and_still_closes_expired_reviews(monkeypatch):
    harness = Harness()
    try:
        cases = []
        for label in ('paused', 'owned', 'expired', 'current', 'later'):
            response = harness.client.post(PREFIX + '/incidents', headers=HUMAN,
                json={'environment': 'dev', 'service': 'order', 'reason': f'analysis queue {label}'})
            assert response.status_code == 201
            cases.append(response.json()['id'])
            assert harness.report(harness.claim(['collect']), harness.collect()).status_code == 200
            assert harness.report(harness.claim(['source']), harness.source()).status_code == 200
        paused, owned, expired, current, later = cases
        store, worker = harness.app.state.store, harness.app.state.worker
        with store.transaction() as session:
            for index, incident_id in enumerate((paused, owned, expired, current, later)):
                session.get(Incident, incident_id).created_at = harness.now + index
            row = session.get(Incident, paused)
            row.document = {**row.document, 'environment': 'prd'}
            row = session.get(Incident, owned)
            row.state = 'analyzing'
            worker.control.data(row, 'analysis_lease', {'owner': 'existing', 'until': harness.now + 1})
            row = session.get(Incident, expired)
            worker.control.data(row, 'evidence_inquiry', {'state': 'reviewing', 'deadline': harness.now - 1,
                                                         'job_id': 'historic-inquiry'})
        config = AppConfig(target_namespaces=['dev'], historical_namespaces=['stg', 'prd'])
        monkeypatch.setattr('app.config.get_app_config', lambda: config)

        incident_id, owner, document = worker._claim()
        assert incident_id == current
        assert document['data']['analysis_lease']['owner'] == owner
        assert document['data']['source_snapshot']['id'] == 'snapshot-one'
        assert harness.detail(expired)['error']['code'] == 'additional_observation_exhausted'
        assert harness.detail(owned)['data']['analysis_lease']['owner'] == 'existing'
        assert harness.detail(paused)['state'] == 'analysis_queued'
        assert worker._claim()[0] == later
    finally:
        harness.close()
