"""자체 컨테이너에서만 시험하고 결과·정리 영수증을 기록한다."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
run_id = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
evidence = REPO / 'docs/evidence/samsung/domain' / run_id
evidence.mkdir(parents=True)
name = 'samsung-domain-it-' + run_id.lower()
from manifest import source_manifest
receipt = {'run_id': run_id, 'synthetic_fixture_only': True, 'container': name, 'memory_limit_bytes': 268435456,
    'source_sha256': source_manifest(ROOT)['source_sha256']}
(evidence / 'source-manifest.json').write_text(json.dumps(source_manifest(ROOT), ensure_ascii=False, indent=2) + '\n')
try:
    result = subprocess.run(['docker', 'run', '-d', '--name', name, '--memory=256m', '--cpus=0.5',
        '--mount', 'type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=134217728',
        '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', '-p', '127.0.0.1::5432', 'postgres:16',
        '-c', 'shared_buffers=16MB', '-c', 'max_connections=30'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('폐기 전용 PostgreSQL 기동 실패')
    port = subprocess.check_output(['docker', 'port', name, '5432/tcp'], text=True).strip().rsplit(':', 1)[1]
    import psycopg
    dsn = f'host=127.0.0.1 port={port} user=postgres dbname=postgres connect_timeout=2'
    deadline = time.monotonic() + 45
    while True:
        try:
            with psycopg.connect(dsn):
                break
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise RuntimeError('폐기 DB 준비 시간 초과') from None
            time.sleep(0.3)
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT / 'src'), 'TEST_DOMAIN_DSN': dsn}
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'), '-p', 'test_*.py', '-v']
    tests = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=240)
    receipt.update(exit_code=tests.returncode, command='python -B -m unittest discover -s tests -p test_*.py -v')
    (evidence / 'tests.log').write_text(tests.stdout + tests.stderr)
    print(tests.stdout + tests.stderr)
finally:
    cleanup = subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True, text=True)
    verify = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True)
    receipt['cleanup_exit_code'] = cleanup.returncode
    receipt['container_absent'] = verify.returncode != 0
    (evidence / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print('검증 보고서:', evidence / 'receipt.json')
sys.exit(receipt.get('exit_code', 1))
