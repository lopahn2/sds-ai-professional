#!/usr/bin/env python3
"""기존 백업을 폐기 전용 DB에 복원하고 COPY 행·시퀀스를 원본 아카이브와 대조한다.

원본 클러스터·Secret·DB에는 접속하지 않는다. 원문 데이터와 외부 명령 진단을 출력하지 않는다.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def command(args, *, payload=None, timeout=90, allow_failure=False):
    result = subprocess.run(args, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode and not allow_failure:
        raise RuntimeError('외부 명령 실패: 원문 진단 비출력')
    return result


def copy_blocks(sql):
    blocks = []
    lines = iter(sql.splitlines())
    for line in lines:
        match = re.fullmatch(rb'COPY (.+) FROM stdin;', line)
        if not match:
            continue
        rows = []
        for row in lines:
            if row == rb'\.':
                break
            rows.append(row)
        else:
            raise AssertionError('원본 COPY 종료 표식 누락')
        blocks.append((match[1].decode('utf-8'), rows))
    return blocks


def rows_hash(rows):
    return hashlib.sha256(b''.join(row + b'\n' for row in sorted(rows))).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', default='pgvector/pgvector:0.8.6-pg17-bookworm')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('이전 결과를 덮지 않도록 새 출력 경로가 필요합니다')
    manifest = json.loads((args.backup_root / 'backup-manifest.json').read_text())
    archives = []
    for entry in manifest['databases']:
        path = args.backup_root / entry['file']
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise AssertionError('원본 아카이브 접근 제한 확인 실패')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise AssertionError('원본 아카이브 지문 불일치')
        archives.append((entry, data))
    identifier = uuid.uuid4().hex[:12]
    name = 'samsung-restore-' + identifier
    owner = 'hermes.samsung.restore=' + identifier
    receipt = {'at': datetime.now(timezone.utc).isoformat(), 'scope': '기존 시점 백업의 실제 폐기 DB 복원·데이터 동등성',
               'source_cluster_access': False, 'source_database_mutated': False,
               'credentials_read': False, 'container': name, 'databases': [], 'exit_code': 1}
    created = False
    stage = '시작 전 이미지 검사'
    try:
        image = json.loads(command(['docker', 'image', 'inspect', args.image]).stdout)[0]
        digests = image.get('RepoDigests') or []
        if not digests:
            raise AssertionError('복원 이미지의 실제 RepoDigest 없음')
        receipt['image'] = digests[0]
        stage = '격리 DB 시작'
        command(['docker', 'run', '-d', '--name', name, '--label', owner,
                 '--network=none', '--memory=256m', '--memory-swap=256m', '--cpus=0.5',
                 '--mount', 'type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=201326592',
                 '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', receipt['image'],
                 '-c', 'shared_buffers=16MB', '-c', 'max_connections=12'])
        created = True
        deadline = time.monotonic() + 60
        while command(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'], allow_failure=True).returncode:
            if time.monotonic() >= deadline:
                raise AssertionError('격리 DB 준비 시간 초과')
            time.sleep(0.4)
        stage = '백업 복원·원본 COPY 대조'
        for entry, data in archives:
            db = entry['database']
            if not re.fullmatch('[a-z_]+', db):
                raise AssertionError('허용되지 않은 백업 DB 식별자')
            command(['docker', 'exec', name, 'createdb', '-U', 'postgres', db])
            restored = command(['docker', 'exec', '-i', name, 'pg_restore', '-U', 'postgres',
                                '-d', db, '--no-owner', '--no-acl', '--exit-on-error'], payload=data)
            expected = command(['docker', 'exec', '-i', name, 'pg_restore', '--data-only',
                                '--no-owner', '--no-acl', '-f', '-'], payload=data).stdout
            tables = []
            for target, rows in copy_blocks(expected):
                actual = command(['docker', 'exec', '-i', name, 'psql', '-X', '-U', 'postgres',
                                  '-d', db, '-v', 'ON_ERROR_STOP=1', '-q', '-c',
                                  f'COPY {target} TO STDOUT;']).stdout.splitlines()
                assert len(actual) == len(rows), '복원 행 수 불일치'
                assert rows_hash(actual) == rows_hash(rows), '복원 COPY 내용 지문 불일치'
                tables.append({'table': target.split(' (', 1)[0], 'rows': len(rows), 'sha256': rows_hash(rows), 'matches': True})
            sequences = []
            for match in re.finditer(rb"SELECT pg_catalog.setval\('([^']+)', ([0-9]+), (true|false)\);", expected):
                seq = match[1].decode('utf-8')
                assert re.fullmatch(r'[A-Za-z0-9_.\"]+', seq), '허용되지 않은 시퀀스 식별자'
                actual = command(['docker', 'exec', name, 'psql', '-X', '-U', 'postgres', '-d', db,
                                  '-Atc', f'SELECT last_value,is_called FROM {seq}']).stdout.strip()
                want = match[2] + b'|' + (b't' if match[3] == b'true' else b'f')
                assert actual == want, '복원 시퀀스 불일치'
                sequences.append({'sequence': seq, 'matches': True})
            receipt['databases'].append({'database': db, 'archive_sha256': entry['sha256'],
                                         'restore_exit_code': restored.returncode, 'tables': tables,
                                         'sequences': sequences, 'row_count': sum(x['rows'] for x in tables)})
            print(f'{db}: 실제 복원·COPY {len(tables)}개·시퀀스 {len(sequences)}개 대조 통과', flush=True)
        assert len(receipt['databases']) == len(manifest['databases'])
        state = json.loads(command(['docker', 'inspect', '--format', '{{json .State}}', name]).stdout)
        receipt['oom_killed'] = state['OOMKilled']
        assert not state['OOMKilled']
        receipt['exit_code'] = 0
    except Exception as exc:
        receipt['failure'] = {'stage': stage, 'kind': type(exc).__name__, 'diagnostics_withheld': True}
    finally:
        if created:
            cleanup = command(['docker', 'rm', '-f', '-v', name], allow_failure=True)
            absent = command(['docker', 'container', 'inspect', name], allow_failure=True).returncode != 0
            receipt['cleanup_exit_code'] = cleanup.returncode
            receipt['container_absent'] = absent
            if cleanup.returncode or not absent:
                receipt['exit_code'] = 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
        print('복원 인수 영수증:', args.output, '종료코드', receipt['exit_code'], flush=True)
    return receipt['exit_code']


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(main())
