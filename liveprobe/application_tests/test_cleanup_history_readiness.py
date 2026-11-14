"""정돈-02/04/07 Application: 조회 무잠금·무초기화, 전체 이력 서버 필터·페이지, 준비 상태 구분.

합성 identity·격리 tmp SQLite만 사용한다. 모델·실행기·클러스터 호출 없음.
"""
import contextlib
import hashlib
import sqlite3
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import select

from test_core_support import Harness, HUMAN, PREFIX


def snapshot(path):
    """Every row of every application_* table, hashed: proves a read path wrote nothing."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        names = [r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
        out = {}
        for name in names:
            rows = db.execute(f'select * from "{name}" order by 1').fetchall()
            out[name] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
        return out


class ApplicationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.db = self.h.path / 'only-tests.db'

    def seed(self, rows):
        """rows: (environment, service, state, reason). created_at increases by row order."""
        from app.application.storage import Incident
        ids = []
        with self.h.app.state.store.transaction() as session:
            for n, (env, service, state, reason) in enumerate(rows):
                system = 'samsung-delivery' if service == 'delivery' else 'samsung-trade'
                incident = Incident(id=f'inc{n:04d}', state=state, created_at=1000.0 + n, document={
                    'environment': env, 'service': service, 'system': system, 'reason': reason,
                    'stages': [], 'error': None, 'data': {}})
                session.add(incident)
                ids.append(incident.id)
        return ids

    def get(self, query=''):
        response = self.h.client.get(PREFIX + '/incidents' + query, headers=HUMAN)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ---------------------------------------------------------------- 정돈-04
    def test_calendar_end_day_and_service_scope(self):
        self.seed([('dev','order','completed','stored'), ('prd','delivery','handoff','stored')])
        rows = self.get('?start=1970-01-01&end=1970-01-01&system=samsung-delivery&service=delivery')
        self.assertEqual(rows['total'], 1)
        self.assertEqual(rows['incidents'][0]['id'], 'inc0001')
        self.assertEqual(self.get('?start=1970-01-02')['total'], 0)
        self.assertEqual(self.h.client.get(PREFIX + '/incidents?start=1970-01-02&end=1970-01-01', headers=HUMAN).status_code, 422)

    def test_filters_apply_to_the_whole_history_before_paging(self):
        rows = [('prd', 'delivery', 'completed', 'old delivery timeout')] + \
               [('dev', 'order', 'handoff', f'order failure {n}') for n in range(260)] + \
               [('stg', 'order', 'awaiting_user_test', 'needs a person')]
        ids = self.seed(rows)
        # The prd incident is older than the 200 newest rows and is still found.
        prd = self.get('?environment=prd')
        self.assertEqual((prd['total'], [i['id'] for i in prd['incidents']]), (1, [ids[0]]))
        self.assertEqual(self.get('?system=samsung-delivery')['total'], 1)
        self.assertEqual(self.get('?search=timeout')['incidents'][0]['id'], ids[0])
        self.assertEqual(self.get('?group=waiting_people')['total'], 1)
        everything = self.get('?limit=200')
        self.assertEqual(everything['total'], 262)
        self.assertEqual(everything['by_group'], {'automatic': 0, 'waiting_people': 1, 'handoff': 260, 'closed': 1})
        # Same filter: the union of the pages is exactly `total`, no duplicates.
        seen, offset = [], 0
        first = self.get('?environment=dev&limit=100')
        while offset < first['total']:
            page = self.get(f'?environment=dev&limit=100&offset={offset}&as_of={first["as_of"]}')
            self.assertEqual(page['total'], 260)
            seen += [i['id'] for i in page['incidents']]
            offset += 100
        self.assertEqual((len(seen), len(set(seen))), (260, 260))
        # Empty, unknown and invalid filters.
        self.assertEqual(self.get('?environment=qa')['incidents'], [])
        self.assertEqual(self.get('?environment=prd&state=handoff')['total'], 0)
        for bad in ('?limit=0', '?limit=201', '?offset=-1', '?group=guess', '?as_of=-1'):
            self.assertEqual(self.h.client.get(PREFIX + '/incidents' + bad, headers=HUMAN).status_code, 422, bad)

    def test_as_of_keeps_later_pages_stable_while_new_incidents_arrive(self):
        self.seed([('dev', 'order', 'handoff', f'r{n}') for n in range(5)])
        first = self.get('?limit=2')
        from app.application.storage import Incident
        with self.h.app.state.store.transaction() as session:
            session.add(Incident(id='new', state='collecting', created_at=5000.0, document={
                'environment': 'dev', 'service': 'order', 'system': 'samsung-trade', 'reason': 'new',
                'stages': [], 'error': None, 'data': {}}))
        later = [self.get(f'?limit=2&offset={o}&as_of={first["as_of"]}') for o in (2, 4)]
        ids = [i['id'] for page in [first] + later for i in page['incidents']]
        self.assertEqual((len(ids), len(set(ids))), (5, 5))
        self.assertNotIn('new', ids)
        self.assertEqual(later[0]['newer_since_as_of'], 1)

    # ---------------------------------------------------------------- 정돈-02
    def test_reads_take_no_write_lock_and_write_nothing(self):
        incident = self.h.create()
        before = snapshot(self.db)
        store = self.h.app.state.store
        with patch.object(store, 'transaction', side_effect=AssertionError('read path took the write lock')), \
             patch.object(store, 'bootstrap', side_effect=AssertionError('read path initialized')), \
             patch.object(store, 'initialize', side_effect=AssertionError('read path initialized')):
            for _ in range(3):
                self.assertEqual(self.h.client.get(PREFIX + '/readyz').status_code, 503)
                self.get()
                self.assertEqual(self.h.client.get(PREFIX + f'/incidents/{incident}', headers=HUMAN).status_code, 200)
                self.assertEqual(self.h.client.get(PREFIX + f'/incidents/{incident}/events', headers=HUMAN).status_code, 200)
                self.assertEqual(self.h.client.get(PREFIX + f'/incidents/{incident}/release', headers=HUMAN).status_code, 200)
        self.assertEqual(snapshot(self.db), before)

    def test_reads_do_not_wait_for_an_open_write_transaction(self):
        incident = self.h.create()
        store = self.h.app.state.store
        holding, release = threading.Event(), threading.Event()

        def writer():
            with store.transaction():
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(holding.wait(5))
            # The writer holds BEGIN IMMEDIATE; a read must still answer instead of queuing behind it.
            with store.read() as session:
                from app.application.storage import Incident
                self.assertEqual(session.scalar(select(Incident.id).where(Incident.id == incident)), incident)
            self.assertEqual(self.h.client.get(PREFIX + f'/incidents/{incident}', headers=HUMAN).status_code, 200)
        finally:
            release.set()
            thread.join(10)

    # ---------------------------------------------------------------- 정돈-07
    def test_model_readiness_separates_configured_from_observed_success(self):
        ready = self.h.client.get(PREFIX + '/readyz').json()
        self.assertFalse(ready['checks']['model'])
        self.assertEqual(ready['model_status']['state'], 'unconfigured')
        self.assertIsNone(ready['model_status']['last_success_at'])
        self.assertEqual(ready['waiting_people'], {'awaiting_user_test': 0, 'awaiting_prd_approval': 0})
        c = self.h.app.state.control
        incident = self.h.create()
        from app.application.storage import Incident
        with self.h.app.state.store.transaction() as session:
            row = session.get(Incident, incident)
            c.event(session, row, 'role_completed', {'role': 'classifier'})
        self.assertEqual(c.model_status(True)['state'], 'recent_success')
        self.h.now += 10
        with self.h.app.state.store.transaction() as session:
            c.stop(session, session.get(Incident, incident), 'model_transport_failed')
        status = c.model_status(True)
        self.assertEqual((status['state'], status['last_failure_code']), ('recent_failure', 'model_transport_failed'))
        # A configured runtime with no recorded call is "configured, success not observed", never "connected".
        empty = Harness()
        self.addCleanup(empty.close)
        self.assertEqual(empty.app.state.control.model_status(True)['state'], 'configured_unverified')


if __name__ == '__main__':
    unittest.main()
