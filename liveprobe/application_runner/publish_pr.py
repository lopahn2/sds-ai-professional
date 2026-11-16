"""Source-account-only PR publication of an already tested artifact.

No Kubernetes, incident API, model, merge or main push transport exists here.
An uncertain remote write is reconciled by reads, never sent again automatically.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .profiles import Registry, digest
from .source import safe_path, source_files
from .worker import durable_write

HEX = re.compile(r'^[a-f0-9]{64}$')
COMMIT = re.compile(r'^[a-f0-9]{40}$')


class Publisher:
    def __init__(self, registry, state_root, provenance_root, *, execute=subprocess.run):
        self.registry = registry
        self.root = Path(state_root).resolve()
        self.provenance = Path(provenance_root).resolve()
        self.execute = execute

    @staticmethod
    def trusted_json(path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('publication_receipt_unsafe')
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o022 or stat.st_size > 4_000_000:
            raise ValueError('publication_receipt_unsafe')
        return json.loads(path.read_text())

    def run(self, args, cwd=None):
        # No inherited Git directory/config/exec redirection or alternate GitHub host.
        inherited = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env = {**inherited, 'GIT_TERMINAL_PROMPT': '0', 'GH_PROMPT_DISABLED': '1', 'GH_HOST': 'github.com',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_ATTR_NOSYSTEM': '1', 'GIT_CONFIG_COUNT': '3',
               'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/dev/null',
               'GIT_CONFIG_KEY_1': 'credential.helper', 'GIT_CONFIG_VALUE_1': '!gh auth git-credential',
               'GIT_CONFIG_KEY_2': 'commit.gpgsign', 'GIT_CONFIG_VALUE_2': 'false'}
        result = self.execute(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise OSError('publication_transport_failed')
        return result.stdout or ''

    def registered(self, service):
        records = json.loads((self.registry.root / 'repositories.json').read_text())
        policy = records.get('pull_request_policy', {})
        if (policy.get('auto_merge') is not False or policy.get('human_merge_required_before_prd') is not True or
                policy.get('each_promotion_requires_confirmation') is not True):
            raise ValueError('publication_policy_invalid')
        matches = [(name, record) for name, record in records.get('systems', {}).items()
                   if service in record.get('services', {})]
        if len(matches) != 1:
            raise ValueError('publication_service_unregistered')
        _, record = matches[0]
        repo = record.get('repository', '')
        subdir = record['services'][service].get('source_subdir', '.')
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or
                record.get('url') != 'https://github.com/' + repo or record.get('base_branch') != 'main' or
                (subdir != '.' and (subdir.startswith('/') or any(p in ('', '.', '..') or p.startswith('.')
                                                                          for p in subdir.split('/'))))):
            raise ValueError('publication_repository_invalid')
        return repo, subdir

    def prepared(self, service, artifact_key):
        if not HEX.fullmatch(artifact_key):
            raise ValueError('publication_artifact_invalid')
        profile = self.registry.get(service)
        result = self.trusted_json(self.root / 'artifacts' / (artifact_key + '.json'))
        artifact, tests = result.get('artifact', {}), result.get('tests', {})
        fields = ('image', 'source_snapshot_id', 'diff_sha256', 'source_tree_sha256', 'build_receipt_sha256')
        if (result.get('ok') is not True or set(artifact) != set(fields) or
                artifact_key != digest({key: artifact[key] for key in fields}) or
                any(not HEX.fullmatch(str(artifact[key])) for key in fields if key != 'image') or
                not all(tests.get(k) is True for k in ('before_failed', 'after_passed', 'regression_executed')) or
                type(tests.get('total')) is not int or tests['total'] < 1 or type(tests.get('failed')) is not int or tests['failed'] != 0 or
                not HEX.fullmatch(artifact['source_snapshot_id'])):
            raise ValueError('publication_tested_artifact_required')
        source = self.trusted_json(self.root / 'snapshots' / (artifact['source_snapshot_id'] + '.json'))
        if (source.get('id') != artifact['source_snapshot_id'] or source.get('profile_id') != profile['id'] or
                source.get('provenance_verified') is not True or not COMMIT.fullmatch(str(source.get('base_commit'))) or
                digest({k: v for k, v in source.items() if k != 'id'}) != source['id'] or
                {name: digest(body.encode()) for name, body in source['files'].items()} != source['file_sha256']):
            raise ValueError('publication_git_source_proof_required')
        image = artifact['image']
        if not re.fullmatch(re.escape(profile['image_repository']) + r'@sha256:[a-f0-9]{64}', image):
            raise ValueError('publication_image_mismatch')
        proof = self.trusted_json(self.provenance / (service + '-' + image.rsplit(':', 1)[1] + '.json'))
        workspace = Path(proof['source_root'])
        if (not workspace.is_absolute() or workspace.is_symlink() or
                not workspace.resolve().is_relative_to(self.root / 'workspaces') or
                self.trusted_owner(workspace) is False or proof.get('image') != image or
                proof.get('profile_id') != profile['id'] or proof.get('provenance_kind') != 'local_build_receipt'):
            raise ValueError('publication_workspace_unsafe')
        files = source_files(workspace)
        hashes = {name: digest(body.encode()) for name, body in files.items()}
        receipt = self.trusted_json(workspace / '.application-receipt.json')
        # Builder runs added/changed regression tests against original production code first.
        # Its 'before' tree therefore includes the final test files, not just the original snapshot.
        test_prefix = profile['test_root'] + '/'
        expected_before = {**source['file_sha256'], **{name: value for name, value in hashes.items()
                                                      if name.startswith(test_prefix)}}
        if (hashes != proof.get('file_sha256') or digest(hashes) != artifact['source_tree_sha256'] or
                digest(receipt.get('build')) != artifact['build_receipt_sha256'] or
                digest(receipt.get('tests')) != tests.get('report_sha256') or
                receipt.get('source_integrity', {}).get('before_file_sha256') != expected_before or
                receipt.get('source_integrity', {}).get('after_file_sha256') != hashes or
                receipt.get('build', {}).get('manifest_observed') is not True or
                receipt['build'].get('image') != image or receipt['build'].get('source_tree_sha256') != artifact['source_tree_sha256']):
            raise ValueError('publication_receipt_conflict')
        return artifact, source, files

    @staticmethod
    def trusted_owner(workspace):
        owner = workspace / '.application-owner'
        if owner.is_symlink() or not owner.is_file():
            return False
        return owner.read_text() == workspace.name

    def observe(self, record):
        """Read-only reconciliation. No absent-branch/PR result authorizes another write."""
        repo, branch = record['repository'], record['branch']
        remote = self.run(['git', 'ls-remote', '--heads', 'https://github.com/' + repo,
                           'refs/heads/' + branch]).strip().split()
        if len(remote) != 2 or remote[0] != record['commit'] or remote[1] != 'refs/heads/' + branch:
            raise ValueError('publication_outcome_unknown')
        rows = json.loads(self.run(['gh', 'pr', 'list', '--repo', repo, '--head', branch, '--base', 'main',
                                   '--state', 'all', '--json', 'number,url,headRefOid,baseRefName,state']))
        if len(rows) != 1:
            raise ValueError('publication_outcome_unknown')
        pr = rows[0]
        if (pr.get('headRefOid') != record['commit'] or pr.get('baseRefName') != 'main' or
                pr.get('state') not in ('OPEN', 'MERGED') or type(pr.get('number')) is not int or pr['number'] < 1 or
                pr.get('url') != f'https://github.com/{repo}/pull/{pr["number"]}'):
            raise ValueError('publication_pr_head_unconfirmed')
        return {**record, 'state': 'published', 'pr_number': pr['number'], 'pr_url': pr['url']}

    def publish(self, service, artifact_key, action_id):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', action_id):
            raise ValueError('publication_action_id_invalid')
        repo, subdir = self.registered(service)
        artifact, source, files = self.prepared(service, artifact_key)
        fingerprint = digest({'service': service, 'artifact': artifact, 'action_id': action_id, 'repository': repo,
                              'source_subdir': subdir, 'base_commit': source['base_commit']})
        directory = self.root / 'publications'
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        receipt = directory / (action_id + '.json')
        descriptor = os.open(directory / (action_id + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if receipt.exists():
                saved = self.trusted_json(receipt)
                if saved.get('input_sha256') != fingerprint:
                    raise ValueError('publication_request_conflict')
                observed = self.observe(saved)
                durable_write(receipt, observed)
                return observed
            branch = 'codex/liveprobe-' + action_id
            work = directory / (action_id + '-repository')
            if work.exists() or work.is_symlink():
                raise ValueError('publication_workspace_exists')
            work.mkdir(mode=0o700)
            self.run(['git', 'init', '--quiet', '--template='], work)
            # Repository-controlled attributes must never invoke host filter programs.
            info = work / '.git' / 'info'
            info.mkdir(parents=True, exist_ok=True)
            (info / 'attributes').write_text('* -filter -diff -merge -text\n')
            self.run(['git', 'remote', 'add', 'origin', 'https://github.com/' + repo], work)
            self.run(['git', 'fetch', '--no-tags', 'origin', 'main'], work)
            current = self.run(['git', 'rev-parse', 'FETCH_HEAD'], work).strip()
            if current != source['base_commit']:
                raise ValueError('publication_base_main_changed')
            self.run(['git', 'checkout', '--detach', current], work)
            target = work if subdir == '.' else work / subdir
            baseline = source_files(target)
            if {name: digest(body.encode()) for name, body in baseline.items()} != source['file_sha256']:
                raise ValueError('publication_repository_source_mismatch')
            for name, body in files.items():
                path = safe_path(target, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            if source_files(target) != files:
                raise ValueError('publication_source_tree_changed')
            changed = sorted(name for name in files if files[name] != baseline.get(name))
            if not changed:
                raise ValueError('publication_no_change')
            paths = [name if subdir == '.' else subdir + '/' + name for name in changed]
            self.run(['git', 'add', '--', *paths], work)
            self.run(['git', '-c', 'user.name=LiveProbe Source Runner', '-c', 'user.email=runner@liveprobe.local',
                      'commit', '--quiet', '-m', 'LiveProbe tested code action ' + action_id], work)
            commit = self.run(['git', 'rev-parse', 'HEAD'], work).strip()
            if not COMMIT.fullmatch(commit):
                raise ValueError('publication_commit_invalid')
            for name, body in files.items():
                git_path = name if subdir == '.' else subdir + '/' + name
                if self.run(['git', 'show', 'HEAD:' + git_path], work) != body:
                    raise ValueError('publication_commit_tree_changed')
            if self.run(['git', 'ls-remote', '--heads', 'https://github.com/' + repo, 'refs/heads/' + branch]).strip():
                raise ValueError('publication_branch_already_exists')
            record = {'input_sha256': fingerprint, 'state': 'write_intent', 'repository': repo,
                      'branch': branch, 'commit': commit, 'artifact_key': artifact_key,
                      'source_tree_sha256': artifact['source_tree_sha256']}
            # Persist before the first remote write; never replay it after an uncertain response.
            durable_write(receipt, record)
            try:
                self.run(['git', 'push', 'origin', 'HEAD:refs/heads/' + branch], work)
                self.run(['gh', 'pr', 'create', '--repo', repo, '--base', 'main', '--head', branch,
                          '--title', 'LiveProbe 코드 조치 ' + action_id,
                          '--body', '고정 산출물의 재현·회귀 시험을 통과했습니다. 사람이 검토·병합해야 합니다. 자동 병합하지 않습니다.'], work)
            except (OSError, ValueError):
                # A read may prove both writes completed. Otherwise leave the intent intact.
                pass
            observed = self.observe(record)
            durable_write(receipt, observed)
            return observed


def main(argv=None):
    parser = argparse.ArgumentParser(description='시험한 코드 조치의 PR만 게시 (병합·클러스터 변경 없음)')
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True, help='source runner의 state/execution')
    parser.add_argument('--provenance-dir', type=Path, required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--artifact-key', required=True)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--role', choices=('source',), required=True)
    args = parser.parse_args(argv)
    try:
        result = Publisher(Registry(args.repo_root), args.state_dir, args.provenance_dir).publish(
            args.service, args.artifact_key, args.action_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'[a-z][a-z0-9_]{1,100}', str(exc)) else 'publication_failed'
        print(json.dumps({'ok': False, 'error': code}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
