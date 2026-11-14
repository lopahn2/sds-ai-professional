#!/usr/bin/env python3
"""Create three NEW Application Secrets without revealing credentials.

Only name-only existence reads and create -f - are permitted. No apply, rotation,
Secret body reads, retries, cleanup deletes, or credential values in argv/output.
Run only from a trusted operator shell; do not enable kubectl debug/HTTP tracing.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'liveprobe-system'
SECRET_NAMES = ('liveprobe-application-db', 'liveprobe-application-db-url', 'liveprobe-application-identity')
SUBJECTS = ('hwany-tester', 'hwany-approver', 'application-runner')


class ProvisionRefused(Exception):
    """Static failure only; never attach input, response or transport exceptions."""


def public_command(context):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]*', context):
        raise ProvisionRefused()
    return ['kubectl', '--context', context, '--request-timeout=20s', '-n', NAMESPACE]


def invoke(run, argv, *, payload=None):
    # Capture BOTH streams: even kubectl/plugin error output may include Secret input.
    # Force predictable NotFound diagnostics. Do not copy raw output into exceptions.
    return run(argv, input=payload, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               timeout=30, check=False, env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})


def require_absent(run, command):
    for name in SECRET_NAMES:
        result = invoke(run, command + ['get', 'secret', name, '-o', 'name'])
        missing = re.fullmatch(r'Error from server \(NotFound\): secrets? "' + re.escape(name) + r'" not found\s*', result.stderr or '')
        if result.returncode != 1 or not missing:
            # A present Secret, Forbidden, connectivity failure, or unknown output all stop.
            raise ProvisionRefused()


def open_token_directory(root):
    """Pin dirfds and reject symlinks/races; never read existing credential files."""
    base = root / '.e2e-tmp'
    try:
        base.mkdir(mode=0o700)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    base_fd = os.open(base, flags)
    try:
        info = os.fstat(base_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ProvisionRefused()
        try:
            os.mkdir('application', 0o700, dir_fd=base_fd)
        except FileExistsError:
            pass
        directory_fd = os.open('application', flags, dir_fd=base_fd)
    finally:
        os.close(base_fd)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ProvisionRefused()
        for subject in SUBJECTS:
            try:
                os.stat(subject + '.token', dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ProvisionRefused()
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def secret_document(name, values):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': name, 'namespace': NAMESPACE,
                         'labels': {'app.kubernetes.io/part-of': 'liveprobe-application'}},
            'data': {key: base64.b64encode(value.encode('utf-8')).decode('ascii') for key, value in values.items()}}


def provision(*, repo_root, run, context):
    command = public_command(context)
    require_absent(run, command)  # all three must be absent before generating anything
    directory_fd = open_token_directory(repo_root)
    try:
        tokens = {subject: secrets.token_urlsafe(48) for subject in SUBJECTS}
        password = secrets.token_urlsafe(48)
        url = ('postgresql://liveprobe_application:' + quote(password, safe='') +
               '@liveprobe-application-db.liveprobe-system.svc.cluster.local:5432/liveprobe_application')
        human = {'principals': [
            {'subject': SUBJECTS[0], 'token': tokens[SUBJECTS[0]], 'scopes': ['incident:read', 'incident:create', 'tester']},
            {'subject': SUBJECTS[1], 'token': tokens[SUBJECTS[1]], 'scopes': ['incident:read', 'prd-approver']}]}
        machine = {'principals': [
            {'subject': SUBJECTS[2], 'token': tokens[SUBJECTS[2]], 'scopes': ['runner', 'incident:create', 'incident:read']}]}
        # Raw bearer files only; human/machine identity JSON never goes to disk.
        # Keep tokens on partial/unknown create failures for operator-led recovery.
        for subject in SUBJECTS:
            name = subject + '.token'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(tokens[subject] + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(directory_fd)
        for subject in SUBJECTS:
            print(repo_root / '.e2e-tmp/application' / (subject + '.token'), flush=True)
        docs = [secret_document(SECRET_NAMES[0], {'password': password}),
                secret_document(SECRET_NAMES[1], {'url': url}),
                secret_document(SECRET_NAMES[2], {'human.json': json.dumps(human), 'machine.json': json.dumps(machine)})]
        for doc in docs:
            result = invoke(run, command + ['create', '-f', '-', '-o', 'name'], payload=json.dumps(doc))
            if result.returncode != 0:
                raise ProvisionRefused()  # no retry, replace, rollback or automatic delete
    finally:
        os.close(directory_fd)


def main(argv=None, *, run=subprocess.run, repo_root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', default='rancher-desktop')
    parser.add_argument('--dry-run', action='store_true', help='zero subprocesses, credentials, or file writes')
    args = parser.parse_args(argv)
    if args.dry_run:
        print('dry-run: three create-only Secrets planned; zero kubectl calls; zero files written')
        return 0
    try:
        provision(repo_root=Path(repo_root), run=run, context=args.context)
        return 0
    except Exception:
        # Never interpolate exceptions, subprocess output, response bodies or documents.
        print('provisioning refused or create result unknown; no automatic retry/overwrite/delete; preserve any token files and inspect names only', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
