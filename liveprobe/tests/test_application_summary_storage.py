from sqlalchemy.orm import sessionmaker
from sqlalchemy import event

from app import db
from app.application.browser_access import summary
from app.application.control import ControlPlane
from app.application.settings import ApplicationSettings
from app.application.storage import Incident, Store
from conftest import needs_db


@needs_db
def test_postgres_summary_preserves_release_and_link_without_reading_evidence(db_url):
    settings = ApplicationSettings()
    store = Store(settings)
    store.engine = db.engine()
    store.factory = sessionmaker(store.engine, expire_on_commit=False)
    store.initialize()
    control = ControlPlane(store, settings)

    origin = {'installation_id': 'fixture', 'incident_id': 7, 'action_id': 'change', 'request_sha256': 'a' * 64}
    raw = 'stored diagnostic evidence'
    with store.transaction() as session:
        session.add_all([
            Incident(id='linked', state='reconciliation_required', created_at=12, document={
                'environment': 'dev', 'service': 'order', 'system': 'samsung-trade', 'reason': '요청 실패',
                'data': {'round': 2, 'unified_origin': origin, 'collection': {'logs': [raw]},
                         'display_readings': {'signal/reason': {'ko': '주문 오류', 'source': raw}},
                         'followups': [{'note_ko': '복구 확인'}],
                         'release': {'protocol': 'dev-qa-prd/v3', 'approval': {'consumed_by_job_id': 'job'},
                                     'qa': {'outcome': 'passed', 'accepted': True, 'observed_at': 11, 'proof': [raw]},
                                     'recovery': {'outcome': 'passed', 'accepted': True, 'observed_at': 12, 'proof': [raw]}}},
            }),
            Incident(id='legacy', state='completed', created_at=10, document={
                'environment': 'stg', 'service': 'payment', 'created_at': 9, 'stages': [raw],
                'data': {'release': {'qa': 'legacy receipt'}, 'followups': []},
            }),
        ])

    for filters in ({}, {'group': 'automatic'}, {'environment': 'stg'},
                    {'service': 'order', 'search': '요청'}, {'limit': 1, 'offset': 1, 'as_of': 12}):
        full = control.list_incidents(**filters)
        projected = control.list_incidents(summary_only=True, **filters)
        full_records = full.pop('incidents')
        page = projected.pop('incidents')
        assert projected == full
        assert list(map(summary, page)) == list(map(summary, full_records))
        assert raw not in str(page)

    page = control.list_incidents(summary_only=True)['incidents']
    assert page[0]['data']['unified_origin'] == origin
    assert summary(page[0])['mega_stage'] == 'recovery'
    assert summary(page[1])['round'] == 1
    assert summary(page[1])['reason'] == ''
    assert control.detail('linked')['data']['collection']['logs'] == [raw]

    # Older JSON rows can retain a NUL in stored raw logs. Their summaries are still readable.
    with store.transaction() as session:
        session.add(Incident(id='legacy-log', state='completed', created_at=8, document={
            'environment': 'stg', 'service': 'payment', 'data': {'collection': {'logs': ['\x00']}},
        }))
    projected = control.list_incidents(summary_only=True)['incidents']
    assert list(map(summary, projected)) == list(map(summary, control.list_incidents()['incidents']))
    assert summary(projected[-1])['id'] == 'legacy-log'
    assert control.detail('legacy-log')['data']['collection']['logs'] == ['\x00']

    statements = []
    def capture(_connection, _cursor, statement, *_):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    event.listen(store.engine, 'before_cursor_execute', capture)
    try:
        control.list_incidents(group='automatic', summary_only=True)
        control.list_incidents(group='waiting_people', summary_only=True)
        assert len(statements) == 14  # Each call also checks two readiness conditions.
        statements.clear()
        page = control.list_live_incidents(limit=200, offset=0, as_of=None)
        assert len(statements) == 5 and page['total'] == 1 and page['live']
        assert page['incidents'][0]['data']['unified_origin'] == origin
        assert raw not in str(page) and page['incidents'][0]['id'] == 'linked'
    finally:
        event.remove(store.engine, 'before_cursor_execute', capture)
