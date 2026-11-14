#!/usr/bin/env python3
"""Provision isolated P18 inventory DB identities and Java chaos tokens without exposing their values.

The operator explicitly authorizes provisioning; this script never prints passwords/tokens and never rotates existing ones.
No existing database is dropped or reset. Namespaces must already exist. Runtime values only travel over subprocess stdin.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess

ENVIRONMENTS = ('dev', 'stg', 'prd')


def call(cmd, *, payload=None):
    r = subprocess.run(cmd, input=payload, text=True, capture_output=True, timeout=90)
    if r.returncode:
        # psql/Kubernetes diagnostics might quote input values. Static error only.
        raise RuntimeError('provisioning command failed; input and diagnostics withheld')
    return r.stdout


def provision(context):
    k = ['kubectl', '--context', context]
    schema = (Path(__file__).resolve().parents[1] / 'services/inventory/db/schema.sql').read_text()
    for ns in ENVIRONMENTS:
        call(k + ['get', 'namespace', ns, '-o', 'name'])
        for service in ('order', 'inventory'):
            name = service + '-chaos-operator'
            found = call(k + ['-n', ns, 'get', 'secret', name, '--ignore-not-found', '-o', 'name']).strip()
            if not found:
                body = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': ns},
                        'type': 'Opaque', 'stringData': {'token': secrets.token_urlsafe(48)}}
                call(k + ['create', '-f', '-'], payload=json.dumps(body))
            print(f'{ns}/{name}: 존재 확인(값 비출력)', flush=True)
        role = f'inventory_{ns}'
        role_exists = call(k + ['-n', 'liveprobe-system', 'exec', 'postgres-0', '--', 'psql', '-U', 'liveprobe', '-d', 'postgres',
            '-Atc', f"select count(*) from pg_roles where rolname='{role}'"]).strip() == '1'
        secret_exists = bool(call(k + ['-n', ns, 'get', 'secret', 'inventory-db', '--ignore-not-found', '-o', 'name']).strip())
        if role_exists or secret_exists:
            if not (role_exists and secret_exists):
                raise RuntimeError(f'{ns}: role/Secret partial provisioning; no credential rotation attempted')
            print(f'{ns}: 기존 역할·Secret 보존, 재설정 없음', flush=True)
            continue
        password = secrets.token_urlsafe(48)
        # Generated URL-safe password has no quote/backslash; values never enter argv or logs.
        assert "'" not in password and '\\' not in password
        sql = f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 10 PASSWORD '{password}';\n"
        sql += f'CREATE DATABASE {role} OWNER {role};\nREVOKE ALL ON DATABASE {role} FROM PUBLIC;\n'
        call(k + ['-n', 'liveprobe-system', 'exec', '-i', 'postgres-0', '--', 'psql', '-v', 'ON_ERROR_STOP=1', '-q', '-U', 'liveprobe', '-d', 'postgres'], payload=sql)
        call(k + ['-n', 'liveprobe-system', 'exec', '-i', 'postgres-0', '--', 'psql', '-v', 'ON_ERROR_STOP=1', '-q', '-U', 'liveprobe', '-d', role],
             payload=f'SET ROLE {role};\n{schema}')
        body = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'inventory-db', 'namespace': ns},
                'type': 'Opaque', 'stringData': {'password': password}}
        call(k + ['create', '-f', '-'], payload=json.dumps(body))
        sql, password, body = None, None, None
        print(f'{ns}: 전용 {role} DB·역할·Secret 생성, 기존 inventory DB 변경 없음', flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--context', default='rancher-desktop')
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    if not args.execute:
        p.error('explicit --execute required')
    provision(args.context)
