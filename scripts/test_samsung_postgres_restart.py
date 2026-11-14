#!/usr/bin/env python3
"""새 폐기 볼륨에서 실제 초기 SQL 실패·같은 PGDATA 재기동의 준비성 경계를 확인한다."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(args, data=None, timeout=90):
    return subprocess.run(args, input=data, capture_output=True, timeout=timeout)


@unittest.skipUnless(os.environ.get('SAMSUNG_INFRA_DOCKER_TEST') == '1', '자체 Docker 폐기 볼륨 시험은 명시적으로 실행한다')
class PostgresRestartTests(unittest.TestCase):
    def test_partial_bootstrap_restart_stays_unready_and_complete_restart_is_ready(self):
        spec = importlib.util.spec_from_file_location('samsung_infra_restart', ROOT / 'scripts/samsung_infra.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bundle = module.render()
        pg = next(o for o in bundle['manifest']['items'] if o['kind'] == 'StatefulSet' and o['metadata']['name'] == 'trade-db')
        container_spec = pg['spec']['template']['spec']['containers'][0]
        identifier = uuid.uuid4().hex[:12]
        label = 'hermes.samsung.restart=' + identifier
        names, volumes = [], []
        with tempfile.TemporaryDirectory(dir=Path.home() / '.hermes/cache/scratch') as temp:
            private = Path(temp) / 'private'
            module.prepare_private(private, bundle)
            items = json.loads((private / 'secrets.json').read_bytes())['items']
            secret = next(o for o in items if o['metadata']['name'] == 'samsung-trade-bootstrap')
            original = {k: base64.b64decode(v) for k, v in secret['data'].items()}

            def execute(name, args):
                return run(['docker', 'exec', '-i', name, *args])

            def state(name):
                result = run(['docker', 'inspect', '--format', '{{json .State}}', name])
                self.assertEqual(result.returncode, 0, '자체 컨테이너 상태 조회 실패')
                return json.loads(result.stdout)

            def wait_for(name, predicate, maximum=75):
                deadline = time.monotonic() + maximum
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.25)
                self.fail('폐기 DB 경계 조건 대기 시간 초과: ' + name)

            def archive(name, values):
                files = {'var/run/samsung/bootstrap/' + k: v for k, v in values.items()}
                files['docker-entrypoint-initdb.d/10-init.sh'] = values['10-init.sh']
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w') as tar:
                    for directory in ('var/run/samsung/bootstrap', 'docker-entrypoint-initdb.d', 'var/lib/postgresql/data'):
                        item = tarfile.TarInfo(directory)
                        item.type, item.mode, item.uid, item.gid = tarfile.DIRTYPE, 0o700, 70, 70
                        tar.addfile(item)
                    for path, value in files.items():
                        item = tarfile.TarInfo(path)
                        item.mode, item.uid, item.gid, item.size = 0o440, 70, 70, len(value)
                        tar.addfile(item, io.BytesIO(value))
                self.assertEqual(run(['docker', 'cp', '-', name + ':/'], stream.getvalue()).returncode, 0,
                                 '폐기 fixture 내부 전송 실패')

            try:
                for interrupted in (True, False):
                    suffix = 'partial' if interrupted else 'complete'
                    name = 'samsung-pg-restart-' + identifier + '-' + suffix
                    volume = name + '-data'
                    self.assertEqual(run(['docker', 'volume', 'create', '--label', label, volume]).returncode, 0)
                    volumes.append(volume)
                    args = ['docker', 'create', '--name', name, '--label', label, '--network=none',
                            '--memory=256m', '--memory-swap=256m', '--cpus=0.5', '--log-driver=none', '--user=70:70',
                            '--mount', 'type=volume,source=' + volume + ',target=/var/lib/postgresql/data']
                    for env in container_spec['env']:
                        args += ['--env', env['name'] + '=' + env['value']]
                    args += [container_spec['image'], *container_spec['args']]
                    self.assertEqual(run(args).returncode, 0, '자체 컨테이너 생성 실패')
                    names.append(name)
                    values = dict(original)
                    if interrupted:
                        self.assertTrue(b'CREATE ROLE trade_stg' in values['bootstrap.sql'])
                        values['bootstrap.sql'] = values['bootstrap.sql'].replace(
                            b'CREATE ROLE trade_stg', b'SELECT 1/0;\nCREATE ROLE trade_stg', 1)
                        values['bootstrap.contract'] = ('samsung-postgres-bootstrap-v1:' +
                            hashlib.sha256(values['bootstrap.sql']).hexdigest() + '\n').encode()
                    archive(name, values)
                    self.assertEqual(run(['docker', 'start', name]).returncode, 0)
                    if interrupted:
                        wait_for(name, lambda: not state(name)['Running'])
                        self.assertNotEqual(state(name)['ExitCode'], 0, 'SQL 중간 실패가 실제 발생해야 한다')
                        self.assertFalse(state(name)['OOMKilled'], '의도한 SQL 실패를 OOM으로 혼동하면 안 된다')
                        self.assertEqual(run(['docker', 'start', name]).returncode, 0)
                        wait_for(name, lambda: execute(name, ['pg_isready', '-h', '127.0.0.1', '-U', 'postgres']).returncode == 0)
                        first = execute(name, ['psql', '-U', 'postgres', '-Atc', "SELECT count(*) FROM pg_roles WHERE rolname='trade_dev'"])
                        missing = execute(name, ['psql', '-U', 'postgres', '-Atc', "SELECT count(*) FROM pg_roles WHERE rolname='trade_stg'"])
                        self.assertEqual(first.stdout.strip(), b'1', '중간까지 생성된 원본 상태가 남아야 한다')
                        self.assertEqual(missing.stdout.strip(), b'0', '재시작이 불완전 초기화를 자동 완성하면 안 된다')
                        for probe in ('startupProbe', 'readinessProbe'):
                            self.assertNotEqual(execute(name, container_spec[probe]['exec']['command']).returncode, 0,
                                                '불완전 PGDATA 재기동은 TCP가 열려도 준비됨이 아니어야 한다')
                        print('실제 PostgreSQL: SQL 중간 실패 → 동일 볼륨 재시작 → TCP 수락 중에도 startup/readiness 거절', flush=True)
                    else:
                        wait_for(name, lambda: execute(name, container_spec['readinessProbe']['exec']['command']).returncode == 0)
                        roles = execute(name, ['psql', '-U', 'postgres', '-Atc',
                            "SELECT count(*) FROM pg_roles WHERE rolname IN ('trade_dev','trade_stg','trade_prd','inventory_dev','inventory_stg','inventory_prd')"])
                        self.assertEqual(roles.stdout.strip(), b'6')
                        self.assertEqual(run(['docker', 'restart', '-t', '10', name]).returncode, 0)
                        wait_for(name, lambda: execute(name, container_spec['readinessProbe']['exec']['command']).returncode == 0)
                        self.assertFalse(state(name)['OOMKilled'])
                        print('실제 PostgreSQL: 전체 6개 역할 초기화 완료 → 동일 볼륨 재시작 → 현재 계약 표식과 readiness 유지', flush=True)
            finally:
                for name in names:
                    owned = run(['docker', 'inspect', '--format', '{{index .Config.Labels "hermes.samsung.restart"}}', name])
                    if owned.returncode == 0:
                        self.assertEqual(owned.stdout.strip(), identifier.encode(), '자체 소유권 불일치: 삭제 거절')
                        self.assertEqual(run(['docker', 'rm', '-f', '-v', name]).returncode, 0)
                for volume in volumes:
                    owned = run(['docker', 'volume', 'inspect', '--format', '{{index .Labels "hermes.samsung.restart"}}', volume])
                    if owned.returncode == 0:
                        self.assertEqual(owned.stdout.strip(), identifier.encode(), '자체 볼륨 소유권 불일치: 삭제 거절')
                        self.assertEqual(run(['docker', 'volume', 'rm', volume]).returncode, 0)
                for name in names:
                    self.assertNotEqual(run(['docker', 'container', 'inspect', name]).returncode, 0)
                for volume in volumes:
                    self.assertNotEqual(run(['docker', 'volume', 'inspect', volume]).returncode, 0)
                print('정리 확인: 자체 컨테이너와 폐기 볼륨 잔존 없음', flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
