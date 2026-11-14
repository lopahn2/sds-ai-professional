"""새 폐기 전용 PostgreSQL에서 실행하는 합성 원장 계약 시험."""
import importlib.util
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql


class DomainTest(unittest.TestCase):
    def setUp(self):
        self.databases = []
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        with psycopg.connect(os.environ['TEST_DOMAIN_DSN'], autocommit=True) as conn:
            for name in self.databases:
                conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))

    def store(self, mode='business'):
        if importlib.util.find_spec('samsung_domain') is None:
            self.fail('신규 원장 런타임이 아직 구현되지 않았습니다')
        from samsung_domain.store import Store
        name = 'domain_test_' + uuid.uuid4().hex
        with psycopg.connect(os.environ['TEST_DOMAIN_DSN'], autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        self.databases.append(name)
        dsn = psycopg.conninfo.make_conninfo(os.environ['TEST_DOMAIN_DSN'], dbname=name)
        store = Store(dsn, mode=mode, environment='dev')
        store.initialize()
        self.stores.append(store)
        return store

    def test_atomic_versions_target_sequence_and_transfer(self):
        store = self.store()
        employee = store.write('employee', 'synthetic-e1', {'name': '합성 직원', 'target_systems': ['samsung-trade']}, create=True)
        self.assertEqual(employee['entity_version'], 1)
        def change(name):
            try:
                return store.write('employee', 'synthetic-e1', {'name': name}, expected_version=1)['entity_version']
            except Exception as exc:
                return getattr(exc, 'status', 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(change, ['합성 수정가', '합성 수정나']))
        self.assertCountEqual(outcomes, [2, 409])
        store.write('employee', 'synthetic-e1', {'target_systems': ['samsung-delivery']}, expected_version=2)
        trade = store.pending('samsung-trade')
        delivery = store.pending('samsung-delivery')
        self.assertEqual([e['event']['target_seq'] for e in trade], [1, 2, 3])
        self.assertEqual(trade[-1]['event']['operation'], 'revoke')
        self.assertEqual(delivery[0]['event']['entity_version'], 3)
        self.assertEqual(delivery[0]['event']['payload']['name'][:2], '합성')
        store.ack(trade[0]['event']['event_id'])
        self.assertEqual(len(store.pending('samsung-trade')), 2)
        self.assertEqual(len(store.pending('samsung-delivery')), 1)

    def test_rollback_includes_entity_projection_outbox_and_sequence(self):
        store = self.store()
        with store.tx() as conn:
            conn.execute("CREATE FUNCTION samsung_domain.reject_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '합성 주입 실패'; END $$")
            conn.execute('CREATE TRIGGER synthetic_fail BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION samsung_domain.reject_outbox()')
        with self.assertRaises(psycopg.Error):
            store.write('employee', 'synthetic-rollback', {'name': '합성', 'target_systems': ['samsung-trade']}, create=True)
        with store.tx() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM entities').fetchone()['n'], 0)
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM projections').fetchone()['n'], 0)
            self.assertEqual(conn.execute('SELECT max(last_committed_seq) AS n FROM target_state').fetchone()['n'], 0)

    def test_immutable_snapshot_and_late_preboundary_event_cannot_resurrect(self):
        business, trade = self.store(), self.store('trade')
        business.write('employee', 'synthetic-e1', {'name': '합성', 'target_systems': ['samsung-trade']}, create=True)
        old_event = business.pending('samsung-trade')[0]['wire']
        business.write('employee', 'synthetic-e1', {'active': False}, expected_version=1)
        self.assertTrue(callable(getattr(business, 'create_export', None)), '일관된 불변 export가 아직 없습니다')
        export = business.create_export('samsung-trade')
        self.assertEqual(export['boundary_seq'], 2)
        business.write('employee', 'synthetic-e2', {'name': '합성 신규', 'target_systems': ['samsung-trade']}, create=True)
        self.assertEqual(business.export_page(export['export_id'], 'samsung-trade')['items'], [])
        generation = trade.begin_rebuild(start_offset=0, log_start=0)
        trade.load_snapshot(generation, export)
        trade.consume(old_event, offset=0, generation=generation)
        trade.activate(generation, end_offset=1)
        self.assertEqual(trade.list_entities('employee'), [])
        status = trade.sync_status()
        self.assertEqual(status['state'], 'degraded')
        self.assertEqual(status['checkpoint'], 1)

    def test_inbox_reverse_duplicate_revoke_and_hash_conflict(self):
        business, trade = self.store(), self.store('trade')
        business.write('employee', 'synthetic-e1', {'name': '합성v1', 'target_systems': ['samsung-trade']}, create=True)
        business.write('employee', 'synthetic-e1', {'name': '합성v2'}, expected_version=1)
        business.write('employee', 'synthetic-e1', {'active': False}, expected_version=2)
        events = business.pending('samsung-trade')
        self.assertTrue(callable(getattr(trade, 'consume', None)), '멱등 소비가 아직 없습니다')
        generation = trade.begin_rebuild(start_offset=0, log_start=0)
        empty = dict(environment='dev', target_system='samsung-trade', boundary_seq=0, items=[], item_count=0)
        from samsung_domain.store import canonical
        import hashlib
        empty['content_hash'] = hashlib.sha256(canonical([]).encode()).hexdigest()
        empty['expires_at'] = '2099-01-01T00:00:00+00:00'
        trade.load_snapshot(generation, empty)
        for offset, event in enumerate([events[2], events[1], events[2], events[0]]):
            trade.consume(event['wire'], offset=offset, generation=generation)
        trade.activate(generation, end_offset=4)
        self.assertEqual(trade.list_entities('employee'), [])
        modified = dict(events[2]['event'], payload={'revoked': False})
        trade.consume(canonical(modified), offset=4)
        self.assertEqual(trade.sync_status()['unresolved_quarantine'], 1)
        self.assertEqual(trade.sync_status()['state'], 'degraded')
        trade.consume('not-json', offset=5)
        self.assertEqual(trade.sync_status()['checkpoint'], 6)
        self.assertEqual(trade.sync_status()['unresolved_quarantine'], 2)

    def test_public_tls_authorization_members_and_read_only(self):
        business, trade = self.store(), self.store('trade')
        self.assertIsNotNone(importlib.util.find_spec('samsung_domain.api'), '분리 HTTP/TLS API가 아직 없습니다')
        from samsung_domain.api import create_apps
        from samsung_domain.config import Settings
        from fastapi.testclient import TestClient
        principals = [{'token': 'synthetic-admin-only', 'role': 'business-admin', 'environment': 'dev', 'target_system': None},
            {'token': 'synthetic-trade-only', 'role': 'trade-reader', 'environment': 'dev', 'target_system': 'samsung-trade'},
            {'token': 'synthetic-delivery-only', 'role': 'delivery-reader', 'environment': 'dev', 'target_system': 'samsung-delivery'},
            {'token': 'synthetic-other-env', 'role': 'business-admin', 'environment': 'prd', 'target_system': None}]
        config = Settings(mode='business', environment='dev', read_only=False, principals=principals)
        public, protected = create_apps(config, business)
        http, tls = TestClient(public), TestClient(protected, base_url='https://testserver')
        admin = {'Authorization': 'Bearer synthetic-admin-only'}
        reader = {'Authorization': 'Bearer synthetic-trade-only'}
        self.assertEqual(http.get('/api/info').json()['display_name'], '삼성 비즈니스 시스템')
        self.assertIn('합성', http.get('/').text)
        self.assertEqual(http.get('/api/v1/employees', headers=admin).status_code, 404)
        self.assertEqual(tls.get('/api/v1/employees').status_code, 401)
        body = {'id': 'synthetic-e1', 'name': '합성 직원', 'target_systems': ['samsung-trade']}
        self.assertEqual(tls.post('/api/v1/employees', json=body, headers=reader).status_code, 403)
        self.assertEqual(tls.post('/api/v1/employees', json=body, headers=admin).status_code, 201)
        self.assertEqual(tls.patch('/api/v1/employees/synthetic-e1', json={'name': '합성 수정'}, headers=admin).status_code, 422)
        self.assertEqual(tls.get('/api/v1/employees', headers={'Authorization': 'Bearer synthetic-other-env'}).status_code, 403)
        self.assertEqual(tls.post('/api/v1/sync/exports?target_system=samsung-delivery', json={}, headers=reader).status_code, 403)
        contract = {'id': 'synthetic-c1', 'title': '합성 계약', 'content': '합성 계약서 원문', 'target_systems': ['samsung-trade']}
        self.assertEqual(tls.post('/api/v1/contracts', json=contract, headers=admin).status_code, 201)
        self.assertEqual(tls.get('/api/v1/contracts/synthetic-c1/content', headers=reader).json()['content'], '합성 계약서 원문')
        self.assertEqual(tls.get('/api/v1/contracts/synthetic-c1/content', headers={'Authorization': 'Bearer synthetic-delivery-only'}).status_code, 403)
        self.assertNotIn('합성 계약서 원문', str(business.pending('samsung-trade')))
        wrong_transport = TestClient(protected, base_url='http://testserver')
        self.assertEqual(wrong_transport.get('/api/v1/employees', headers=admin).status_code, 403)
        member_cfg = Settings(mode='trade', environment='dev', read_only=True, principals=principals)
        member_public, member_tls = create_apps(member_cfg, trade)
        customer = TestClient(member_public)
        self.assertEqual(customer.post('/api/members', json={'name': '합성 고객'}).status_code, 503)
        member_cfg.read_only = False
        self.assertEqual(customer.post('/api/members', json={'name': '합성 고객'}).status_code, 201)
        self.assertEqual(customer.get('/api/members?search=고객').json()['total'], 1)
        self.assertEqual(TestClient(member_tls, base_url='https://testserver').post('/api/v1/employees', json=body, headers=reader).status_code, 403)
        self.assertEqual(TestClient(member_tls, base_url='https://testserver').get('/api/v1/contracts/synthetic-c1/content', headers=reader).status_code, 503)
        self.assertTrue(customer.get('/healthz/ready').json()['ready'])

    def test_outbox_ack_is_target_local_and_watermark_cannot_overtake(self):
        business = self.store()
        business.write('employee', 'synthetic-e1', {'name': '합성', 'target_systems': ['samsung-trade', 'samsung-delivery']}, create=True)
        self.assertIsNotNone(importlib.util.find_spec('samsung_domain.workers'), '실제 발행 루프가 아직 없습니다')
        from samsung_domain.workers import OutboxPublisher
        class Transport:
            def __init__(self):
                self.records = []
                self.fail_delivery = True
            def send(self, topic, key, wire):
                self.records.append((topic, key, wire))
                if '.delivery.' in topic and self.fail_delivery:
                    raise RuntimeError('합성 delivery ACK 실패')
        transport = Transport()
        publisher = OutboxPublisher(business, transport)
        self.assertEqual(publisher.publish_target('samsung-trade'), 1)
        with self.assertRaises(RuntimeError):
            publisher.publish_target('samsung-delivery')
        self.assertEqual(len(business.pending('samsung-trade')), 0)
        self.assertEqual(len(business.pending('samsung-delivery')), 1)
        self.assertFalse(any('.delivery.' in topic and key == 'control:watermark' for topic, key, _ in transport.records))
        delivery_first = next(wire for topic, key, wire in transport.records if '.delivery.' in topic)
        transport.fail_delivery = False
        publisher.publish_target('samsung-delivery')
        self.assertEqual(delivery_first, [wire for topic, key, wire in transport.records if '.delivery.' in topic and key != 'control:watermark'][-1])
        self.assertTrue(any('.delivery.' in topic and key == 'control:watermark' for topic, key, _ in transport.records))
        self.assertEqual(len(business.pending('samsung-delivery')), 0)

    def test_snapshot_validation_atomic_switch_and_freshness(self):
        business, trade = self.store(), self.store('trade')
        self.assertTrue(callable(getattr(business, 'watermark', None)), '원장 barrier watermark가 아직 없습니다')
        export = business.create_export('samsung-trade')
        generation = trade.begin_rebuild(start_offset=10, log_start=3)
        from samsung_domain.store import DomainError, canonical
        bad = dict(export, content_hash='wrong')
        with self.assertRaises(DomainError):
            trade.load_snapshot(generation, bad)
        trade.load_snapshot(generation, export)
        with self.assertRaises(DomainError):
            trade.activate(generation, end_offset=11)
        self.assertIsNone(trade.sync_status()['generation'])
        watermark = business.watermark('samsung-trade')
        trade.consume(canonical(watermark), offset=10, generation=generation)
        trade.activate(generation, end_offset=11)
        self.assertEqual(trade.sync_status()['state'], 'fresh')
        trade.consume('{invalid', offset=11)
        self.assertEqual(trade.sync_status()['state'], 'degraded')
        self.assertEqual(trade.sync_status()['unresolved_quarantine'], 1)
        trade.dependency_failure(True)
        self.assertTrue(trade.sync_status()['dependency_error'])
        with self.assertRaises(DomainError):
            trade.begin_rebuild(start_offset=1, log_start=2)

    def test_real_listener_entrypoint_and_synthetic_tls_fixture(self):
        self.assertIsNotNone(importlib.util.find_spec('samsung_domain.main'), '실제 두 리스너 진입점이 아직 없습니다')
        import json
        from pathlib import Path
        import socket
        import ssl
        import subprocess
        import sys
        import tempfile
        import time
        import httpx
        store = self.store('trade')
        with store.pool.connection() as conn:
            dsn = conn.info.dsn
        def free_port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]
        http_port, tls_port = free_port(), free_port()
        with tempfile.TemporaryDirectory(prefix='samsung-synthetic-tls-', dir='/Users/hwany/.hermes/cache/scratch') as directory:
            root = Path(directory)
            cert, key = root / 'synthetic.crt', root / 'synthetic.key'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1', '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
            files = {'DATABASE_DSN_FILE': dsn, 'API_CREDENTIALS_FILE': json.dumps({'principals': [{'token': 'synthetic-trade-only', 'role': 'trade-reader', 'environment': 'dev', 'target_system': 'samsung-trade'}]}),
                'BUSINESS_TOKEN_FILE': 'synthetic-trade-only', 'KAFKA_CONFIG_FILE': json.dumps({'bootstrap.servers': '127.0.0.1:1', 'security.protocol': 'SASL_SSL', 'sasl.mechanism': 'PLAIN', 'sasl.username': 'synthetic-unused', 'sasl.password': 'synthetic-unused'})}
            env = {**os.environ, 'DOMAIN_MODE': 'trade', 'APP_ENV': 'dev', 'SYSTEM_NAME': 'samsung-trade', 'SERVICE_NAME': 'trade-member',
                'HTTP_PORT': str(http_port), 'TLS_PORT': str(tls_port), 'TLS_CERT_FILE': str(cert), 'TLS_KEY_FILE': str(key),
                'KAFKA_CA_FILE': str(cert), 'BUSINESS_CA_FILE': str(cert), 'READ_ONLY_MODE': '1', 'BUSINESS_API_URL': f'https://localhost:{tls_port}', 'PYTHONDONTWRITEBYTECODE': '1'}
            for name, value in files.items():
                path = root / name
                path.write_text(value)
                env[name] = str(path)
            log = root / 'runtime.log'
            with log.open('w') as output:
                process = subprocess.Popen([sys.executable, '-B', '-m', 'samsung_domain.main'], env=env, stdout=output, stderr=output)
                try:
                    with httpx.Client(trust_env=False) as client:
                        deadline = time.monotonic() + 20
                        while True:
                            try:
                                info = client.get(f'http://127.0.0.1:{http_port}/api/info', timeout=1)
                                if info.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            if process.poll() is not None or time.monotonic() > deadline:
                                self.fail('실제 HTTP/TLS 리스너 준비 실패')
                            time.sleep(0.1)
                        self.assertEqual(info.json()['display_name'], '삼성 트레이드 시스템')
                        self.assertEqual(client.get(f'http://127.0.0.1:{http_port}/api/v1/employees').status_code, 404)
                        self.assertEqual(client.post(f'http://127.0.0.1:{http_port}/api/members', json={'name': '합성'}).status_code, 503)
                    with httpx.Client(verify=ssl.create_default_context(cafile=str(cert)), trust_env=False) as client:
                        endpoint = f'https://localhost:{tls_port}/api/v1/employees'
                        self.assertEqual(client.get(endpoint).status_code, 401)
                        self.assertEqual(client.get(endpoint, headers={'Authorization': 'Bearer synthetic-trade-only'}).status_code, 200)
                    with httpx.Client(trust_env=False) as client:
                        with self.assertRaises(httpx.ConnectError):
                            client.get(endpoint)
                finally:
                    process.terminate()
                    process.wait(timeout=20)
            self.assertEqual(process.returncode, 0)
            logs = log.read_text()
            self.assertNotIn('synthetic-trade-only', logs)
            self.assertNotIn(dsn, logs)
            self.assertNotIn('sasl.password', logs)

    def test_export_owner_body_scope_and_authoritative_sensitive_recheck(self):
        business, trade = self.store(), self.store('trade')
        from samsung_domain.api import create_apps
        from samsung_domain.config import Settings
        from samsung_domain.store import DomainError, canonical
        from fastapi.testclient import TestClient
        principals = [{'token': 'synthetic-admin', 'role': 'business-admin', 'environment': 'dev', 'target_system': None},
            {'token': 'synthetic-trade', 'role': 'trade-reader', 'environment': 'dev', 'target_system': 'samsung-trade'},
            {'token': 'synthetic-delivery', 'role': 'delivery-reader', 'environment': 'dev', 'target_system': 'samsung-delivery'}]
        _, source_api = create_apps(Settings(mode='business', read_only=False, principals=principals), business)
        source = TestClient(source_api, base_url='https://testserver')
        reader = {'Authorization': 'Bearer synthetic-trade'}
        self.assertEqual(source.post('/api/v1/sync/exports', json={'environment': 'prd'}, headers=reader).status_code, 403)
        self.assertEqual(source.post('/api/v1/sync/exports', json={'target_system': 'samsung-delivery'}, headers=reader).status_code, 403)
        business.write('contract', 'synthetic-sensitive', {'title': '합성 현행 확인', 'target_systems': ['samsung-trade']}, create=True, content='합성 민감 원문')
        export = business.create_export('samsung-trade')
        self.assertEqual(source.get('/api/v1/sync/exports/' + export['export_id'], headers={'Authorization': 'Bearer synthetic-delivery'}).status_code, 404)
        generation = trade.begin_rebuild(start_offset=0, log_start=0)
        trade.load_snapshot(generation, export)
        for row in business.pending('samsung-trade'):
            business.ack(row['event_id'])
        trade.consume(canonical(business.watermark('samsung-trade')), offset=0, generation=generation)
        trade.activate(generation, end_offset=1)
        class SourceFixture:
            available = True
            def get(self, path):
                if not self.available:
                    raise DomainError(503, '합성 원장 중단')
                response = source.get(path, headers=reader)
                if response.status_code != 200:
                    raise DomainError(response.status_code, '원장 현행 거절')
                return response.json()
        fixture = SourceFixture()
        _, replica_api = create_apps(Settings(mode='trade', principals=principals), trade, fixture)
        replica = TestClient(replica_api, base_url='https://testserver')
        path = '/api/v1/contracts/synthetic-sensitive/content'
        self.assertEqual(replica.get(path, headers=reader).json()['content'], '합성 민감 원문')
        business.write('contract', 'synthetic-sensitive', {'status': 'revoked'}, expected_version=1)
        self.assertEqual(trade.sync_status()['state'], 'fresh')
        self.assertEqual(replica.get(path, headers=reader).status_code, 403)
        fixture.available = False
        self.assertEqual(replica.get(path, headers=reader).status_code, 503)

    def test_recovery_watermark_after_activation_resolves_only_prior_generation(self):
        business, trade = self.store(), self.store('trade')
        from samsung_domain.store import canonical
        export = business.create_export('samsung-trade')
        first = trade.begin_rebuild(start_offset=0, log_start=0)
        trade.load_snapshot(first, export)
        trade.activate(first, end_offset=0)
        trade.consume('corrupt', offset=0)
        second = trade.begin_rebuild(start_offset=1, log_start=0)
        trade.load_snapshot(second, export)
        trade.activate(second, end_offset=1)
        self.assertEqual(trade.sync_status()['unresolved_quarantine'], 1)
        trade.consume(canonical(business.watermark('samsung-trade')), offset=1)
        self.assertEqual(trade.sync_status()['unresolved_quarantine'], 0)
        self.assertEqual(trade.sync_status()['state'], 'fresh')
        trade.consume('corrupt-current-generation', offset=2)
        self.assertEqual(trade.sync_status()['unresolved_quarantine'], 1)
        self.assertEqual(trade.sync_status()['state'], 'degraded')

    def test_directory_rebuild_replay_and_restart_after_kafka_commit_loss(self):
        from samsung_domain.workers import DirectoryConsumer
        import threading
        business, trade = self.store(), self.store('trade')
        business.write('employee', 'synthetic-stream', {'name': '합성 stream', 'target_systems': ['samsung-trade']}, create=True)
        class SourceFixture:
            def __init__(self):
                self.cursor = 0
                self.fail_commit = True
                self.records = []
            def bounds(self):
                return 0, len(self.records)
            def seek(self, offset):
                self.cursor = offset
            def poll(self):
                if self.cursor >= len(self.records):
                    return None
                message = dict(wire=self.records[self.cursor], offset=self.cursor)
                self.cursor += 1
                return message
            def commit(self, offset):
                if self.fail_commit:
                    self.fail_commit = False
                    raise RuntimeError('합성 Kafka commit 응답 유실')
        source = SourceFixture()
        class BusinessFixture:
            def snapshot(self):
                snapshot = business.create_export('samsung-trade')
                business.write('employee', 'synthetic-stream', {'name': 'snapshot 이후 합성'}, expected_version=1)
                source.records.extend(row['wire'] for row in business.pending('samsung-trade'))
                return snapshot
        worker = DirectoryConsumer(trade, source, BusinessFixture())
        with self.assertRaises(RuntimeError):
            worker.cycle(threading.Event())
        active = trade.sync_status()['generation']
        self.assertEqual(trade.sync_status()['checkpoint'], 2)
        source.fail_commit = False
        worker.cycle(threading.Event())
        self.assertEqual(trade.sync_status()['generation'], active)
        self.assertEqual(trade.list_entities('employee')[0]['name'], 'snapshot 이후 합성')
        self.assertEqual(trade.list_entities('employee')[0]['entity_version'], 2)
        trade.dependency_failure(True)
        self.assertTrue(trade.sync_status()['dependency_error'])
        worker.cycle(threading.Event())
        self.assertFalse(trade.sync_status()['dependency_error'])

    def test_1000_synthetic_source_changes_and_two_independent_snapshots(self):
        business, trade, delivery = self.store(), self.store('trade'), self.store('delivery')
        for index in range(1000):
            business.write('employee', f'synthetic-load-{index}', {'name': f'합성 부하 {index}', 'target_systems': ['samsung-trade', 'samsung-delivery']}, create=True)
        self.assertEqual(len(business.pending('samsung-trade', 2000)), 1000)
        self.assertEqual(len(business.pending('samsung-delivery', 2000)), 1000)
        for replica in (trade, delivery):
            export = business.create_export(replica.system)
            self.assertEqual(export['boundary_seq'], 1000)
            self.assertEqual(len(business.export_page(export['export_id'], replica.system)['items']), 500)
            self.assertEqual(len(business.export_page(export['export_id'], replica.system, page=1)['items']), 500)
            generation = replica.begin_rebuild(start_offset=0, log_start=0)
            replica.load_snapshot(generation, export)
            replica.activate(generation, end_offset=0)
            self.assertEqual(len(replica.list_entities('employee')), 1000)
        trade.create_member('합성 트레이드 고객')
        delivery.create_member('합성 딜리버리 고객')
        self.assertEqual(trade.members()['members'][0]['name'], '합성 트레이드 고객')
        self.assertEqual(delivery.members()['members'][0]['name'], '합성 딜리버리 고객')

    def test_inbox_capacity_requires_rebuild_instead_of_silent_discard(self):
        business, trade = self.store(), self.store('trade')
        generation = trade.begin_rebuild(start_offset=0, log_start=0)
        trade.load_snapshot(generation, business.create_export('samsung-trade'))
        trade.activate(generation, end_offset=0)
        with trade.tx() as conn:
            conn.execute("INSERT INTO inbox SELECT %s,'synthetic-capacity-'||i::text,'synthetic-hash' FROM generate_series(1,10000) i", (generation,))
        self.assertTrue(trade.sync_status().get('storage_rebuild_required', False), 'inbox 용량 재구축 차단이 없습니다')
        from samsung_domain.store import DomainError
        with self.assertRaises(DomainError):
            trade.consume('corrupt-but-must-not-advance', offset=0)
        self.assertEqual(trade.sync_status()['checkpoint'], 0)

    def test_observed_kafka_backlog_prevents_fresh_even_with_recent_watermark(self):
        business, trade = self.store(), self.store('trade')
        from samsung_domain.store import canonical
        generation = trade.begin_rebuild(start_offset=0, log_start=0)
        trade.load_snapshot(generation, business.create_export('samsung-trade'))
        trade.consume(canonical(business.watermark('samsung-trade')), offset=0, generation=generation)
        trade.activate(generation, end_offset=1)
        self.assertEqual(trade.sync_status()['state'], 'fresh')
        self.assertTrue(callable(getattr(trade, 'observe_end_offset', None)), '관측 backlog가 신선도에 반영되지 않습니다')
        trade.observe_end_offset(5)
        self.assertEqual(trade.sync_status()['state'], 'degraded')
        self.assertEqual(trade.sync_status()['observed_offset_lag'], 4)
        trade.advance_checkpoint(None, 5)
        self.assertEqual(trade.sync_status()['state'], 'fresh')

    def test_concurrent_exports_respect_target_storage_limit(self):
        from samsung_domain.store import DomainError
        import threading
        business = self.store()
        for _ in range(7):
            business.create_export('samsung-trade')
        from contextlib import contextmanager
        original_connection = business.pool.connection
        count_barrier = threading.Barrier(2)
        class ConnectionProbe:
            def __init__(self, connection):
                self.connection = connection
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, query, params=None):
                cursor = self.connection.execute(query, params)
                if str(query).startswith('SELECT count(*) AS n FROM exports'):
                    try:
                        count_barrier.wait(timeout=0.5)
                    except threading.BrokenBarrierError:
                        # 올바른 직렬화가 있으면 두 번째 TX는 첫 count에 도달할 수 없다.
                        pass
                return cursor
        @contextmanager
        def connection_probe(*args, **kwargs):
            with original_connection(*args, **kwargs) as connection:
                yield ConnectionProbe(connection)
        business.pool.connection = connection_probe
        start = threading.Barrier(2)
        def export(_):
            start.wait()
            try:
                business.create_export('samsung-trade')
                return 201
            except DomainError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(export, range(2)))
        self.assertCountEqual(result, [201, 503])
        with business.tx() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM exports').fetchone()['n'], 8)


if __name__ == '__main__':
    unittest.main()
