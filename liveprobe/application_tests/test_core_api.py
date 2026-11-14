"""기존 conftest/DB/Engine을 불러오지 않는 독립 API 시험."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient


class CoreApiTests(unittest.TestCase):
    def load_core(self):
        try:
            return importlib.import_module('app.application.main')
        except ModuleNotFoundError:
            self.fail('독립 Application ASGI API가 구현되어야 한다')

    def test_independent_sqlite_instances_and_catalog(self):
        core = self.load_core()
        from app.application.settings import ApplicationSettings
        with tempfile.TemporaryDirectory(prefix='lp-app-core-', dir=os.environ.get('TMPDIR')) as td:
            def client(name):
                settings = ApplicationSettings(database_url=f'sqlite:///{td}/{name}.db', testing=True)
                app = core.create_app(settings)
                app.state.store.initialize()
                return TestClient(app)
            with client('one') as first, client('two') as second:
                self.assertEqual(first.get('/api/application/v1/healthz').status_code, 200)
                catalog = first.get('/api/application/v1/catalog').json()
                self.assertEqual(len(catalog['services']), 9)
                self.assertEqual(len(catalog['systems']), 3)
                # 무인증 쓰기는 기본 폐쇄한다.
                self.assertEqual(first.post('/api/application/v1/incidents', json={
                    'environment': 'dev', 'service': 'order', 'reason': '주문 실패'
                }).status_code, 503)
                self.assertEqual(second.get('/api/application/v1/readyz').status_code, 503)
                self.assertNotEqual(first.app.state.store.engine.url.database, second.app.state.store.engine.url.database)
                self.assertTrue(all(t.name.startswith('application_') for t in first.app.state.store.base.metadata.sorted_tables))

    def test_default_import_does_not_connect_or_import_old_engine(self):
        core = self.load_core()
        from app.application.settings import ApplicationSettings
        with TestClient(core.create_app(ApplicationSettings())) as client:
            result = client.get('/api/application/v1/readyz')
            self.assertEqual(result.status_code, 503)
            self.assertFalse(result.json()['ready'])
            self.assertEqual(client.get('/api/application/v1/healthz').status_code, 200)
        # Run the import check in a fresh interpreter even if the K8s suite ran first.
        import subprocess
        import sys
        result = subprocess.run([sys.executable, '-c',
            'import sys; import app.application.main; '
            'assert "app.engine" not in sys.modules and "app.db" not in sys.modules'],
            cwd=Path(__file__).resolve().parents[1], check=False)
        self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
