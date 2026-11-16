"""PR publisher: fake git/gh only; fixture evidence is an owned synthetic tested artifact."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from liveprobe.application_runner.profiles import digest
from liveprobe.application_runner.publish_pr import Publisher
from liveprobe.application_runner.worker import durable_write

BASE = 'a' * 40
COMMIT = 'b' * 40


class Registry:
    def __init__(self, root):
        self.root = root
    def get(self, service):
        if service != 'order':
            raise ValueError('unregistered_service')
        return {'id': 'order', 'service': 'order', 'image_repository': 'local/order', 'language': 'python', 'test_root': 'tests'}


class Transport:
    def __init__(self):
        self.calls = []
        self.remote = None
        self.pr = None
        self.uncertain_push = False
        self.uncertain_create = False
        self.current_main = BASE
    def __call__(self, args, **kwargs):
        self.calls.append(args)
        work = kwargs.get('cwd')
        text = ''
        code = 0
        if args[:3] == ['git', 'checkout', '--detach']:
            (work / 'src').mkdir()
            (work / 'src/main.py').write_text('return 0\n')
        elif args[:3] == ['git', 'rev-parse', 'FETCH_HEAD']:
            text = self.current_main
        elif args[:3] == ['git', 'rev-parse', 'HEAD']:
            text = COMMIT
        elif args[:2] == ['git', 'show']:
            text = (work / args[2].split(':', 1)[1]).read_text()
        elif args[:3] == ['git', 'ls-remote', '--heads']:
            text = (self.remote + '\t' + args[-1] + '\n') if self.remote else ''
        elif args[:2] == ['git', 'push']:
            self.remote = COMMIT
            code = 1 if self.uncertain_push else 0
        elif args[:3] == ['gh', 'pr', 'create']:
            self.pr = {'number': 7, 'url': 'https://github.com/example/order/pull/7',
                       'headRefOid': COMMIT, 'baseRefName': 'main', 'state': 'OPEN'}
            code = 1 if self.uncertain_create else 0
        elif args[:3] == ['gh', 'pr', 'list']:
            text = json.dumps([self.pr] if self.pr else [])
        return SimpleNamespace(returncode=code, stdout=text)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'execution'
        self.provenance = self.root / 'provenance'
        self.transport = Transport()
        self.publisher = Publisher(Registry(self.root), self.state, self.provenance, execute=self.transport)
        records = {'pull_request_policy': {'auto_merge': False, 'human_merge_required_before_prd': True,
                    'each_promotion_requires_confirmation': True}, 'systems': {'order': {
                    'repository': 'example/order', 'url': 'https://github.com/example/order', 'base_branch': 'main',
                    'services': {'order': {'source_subdir': '.'}}}}}
        (self.root / 'repositories.json').write_text(json.dumps(records))
        self.source = {'base_commit': BASE, 'base_image': 'local/order@sha256:' + '1' * 64,
                       'files': {'src/main.py': 'return 0\n'},
                       'file_sha256': {'src/main.py': digest(b'return 0\n')},
                       'profile_id': 'order', 'affected_services': ['order'], 'provenance_verified': True}
        self.source['id'] = digest(self.source)
        durable_write(self.state / 'snapshots' / (self.source['id'] + '.json'), self.source)
        files = {'src/main.py': 'return 1\n', 'tests/test_main.py': 'def test_run(): pass\n'}
        self.workspace = self.state / 'workspaces' / 'application-synthetic'
        self.workspace.mkdir(parents=True)
        (self.workspace / '.application-owner').write_text(self.workspace.name)
        for name, body in files.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        hashes = {name: digest(body.encode()) for name, body in files.items()}
        image = 'local/order@sha256:' + '2' * 64
        build = {'image': image, 'source_tree_sha256': digest(hashes), 'manifest_observed': True}
        reports = {'before': [{'failed': 1}], 'after': [{'failed': 0}], 'modes': ['synthetic']}
        proof = {'image': image, 'profile_id': 'order', 'base_commit': None, 'file_sha256': hashes,
                 'source_root': str(self.workspace), 'provenance_kind': 'local_build_receipt'}
        durable_write(self.provenance / ('order-' + '2' * 64 + '.json'), proof)
        durable_write(self.workspace / '.application-receipt.json', {'tests': reports, 'build': build,
            'source_integrity': {'before_file_sha256': {**self.source['file_sha256'], 'tests/test_main.py': hashes['tests/test_main.py']}, 'after_file_sha256': hashes}})
        self.artifact = {'image': image, 'source_snapshot_id': self.source['id'], 'diff_sha256': '3' * 64,
                         'source_tree_sha256': digest(hashes), 'build_receipt_sha256': digest(build)}
        self.key = digest(self.artifact)
        self.result = {'ok': True, 'artifact': self.artifact,
                       'tests': {'before_failed': True, 'after_passed': True, 'regression_executed': True,
                                 'total': 2, 'failed': 0, 'report_sha256': digest(reports)}}
        durable_write(self.state / 'artifacts' / (self.key + '.json'), self.result)

    def publish(self):
        return self.publisher.publish('order', self.key, 'action-42')

    def test_actual_builder_artifact_with_added_regression_is_publishable(self):
        from liveprobe.application_runner.build import Builder
        source = {'base_commit': BASE, 'base_image': 'local/order@sha256:' + '1' * 64,
                  'profile_id': 'order', 'affected_services': ['order'], 'provenance_verified': True,
                  'files': {'src/main.py': 'value = 0\n'}, 'file_sha256': {'src/main.py': digest(b'value = 0\n')}}
        source['id'] = digest(source)
        diff = ('--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n'
                '--- /dev/null\n+++ b/tests/test_regression.py\n@@ -0,0 +1,2 @@\n'
                '+def test_value():\n+ assert True\n')
        patch = {'source_snapshot_id': source['id'], 'diff': diff, 'diff_sha256': digest(diff.encode()),
                 'tests_added': ['tests/test_regression.py::test_value']}
        class BuildTransport:
            def tests(self, workspace, profile, phase, added):
                return {'exit_code': 1 if phase == 'before' else 0, 'total': 1,
                        'failed': 1 if phase == 'before' else 0, 'executed_tests': [{
                            'id': added[0], 'file': 'tests/test_regression.py',
                            'status': 'failed' if phase == 'before' else 'passed', 'phase': phase}]}
            def build(self, workspace, profile, tree):
                return {'image': 'local/order@sha256:' + '4' * 64, 'source_tree_sha256': tree, 'manifest_observed': True}
        result = Builder(self.state / 'workspaces', BuildTransport(), self.provenance).run(
            source, patch, Registry(self.root).get('order'))
        key = digest(result['artifact'])
        durable_write(self.state / 'snapshots' / (source['id'] + '.json'), source)
        durable_write(self.state / 'artifacts' / (key + '.json'), result)
        artifact, actual_source, files = self.publisher.prepared('order', key)
        self.assertEqual(artifact, result['artifact'])
        self.assertEqual(actual_source, source)
        self.assertIn('tests/test_regression.py', files)
        self.assertEqual(files['src/main.py'], 'value = 1\n')
        self.assertEqual(self.transport.calls, [])

    def test_git_environment_cannot_redirect_repository_or_github_host(self):
        from unittest.mock import patch
        import os
        recorded = []
        def execute(args, **kwargs):
            recorded.append(kwargs['env'])
            return SimpleNamespace(returncode=0, stdout='')
        self.publisher.execute = execute
        with patch.dict(os.environ, {'GIT_DIR': '/foreign', 'GIT_CONFIG_COUNT': '99',
                                     'GIT_CONFIG_KEY_78': 'include.path', 'GIT_CONFIG_VALUE_78': '/foreign/config',
                                     'GIT_CONFIG_GLOBAL': '/foreign/config', 'GH_HOST': 'attacker.invalid'}):
            self.publisher.run(['git', 'status'])
        env = recorded[0]
        self.assertNotIn('GIT_DIR', env)
        self.assertNotIn('GIT_CONFIG_KEY_78', env)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')
        self.assertEqual(env['GH_HOST'], 'github.com')

    def test_only_tested_branch_and_pr_are_published_and_replay_only_reads(self):
        receipt = self.publish()
        self.assertEqual(receipt['state'], 'published')
        self.assertEqual(receipt['pr_number'], 7)
        push = next(call for call in self.transport.calls if call[:2] == ['git', 'push'])
        self.assertEqual(push, ['git', 'push', 'origin', 'HEAD:refs/heads/codex/liveprobe-action-42'])
        self.assertFalse(any('merge' in call or '--force' in call for call in self.transport.calls))
        count = len(self.transport.calls)
        self.assertEqual(self.publish(), receipt)
        self.assertTrue(all(call[:3] in (['git', 'ls-remote', '--heads'], ['gh', 'pr', 'list'])
                            for call in self.transport.calls[count:]))

    def test_uncertain_push_preserves_intent_and_never_repushes_or_creates_pr(self):
        self.transport.uncertain_push = True
        with self.assertRaisesRegex(ValueError, 'publication_outcome_unknown'):
            self.publish()
        receipt = self.publisher.trusted_json(self.state / 'publications' / 'action-42.json')
        self.assertEqual(receipt['state'], 'write_intent')
        count = len(self.transport.calls)
        with self.assertRaisesRegex(ValueError, 'publication_outcome_unknown'):
            self.publish()
        self.assertTrue(all(call[:3] in (['git', 'ls-remote', '--heads'], ['gh', 'pr', 'list'])
                            for call in self.transport.calls[count:]))

    def test_uncertain_pr_response_is_reconciled_by_head_and_pr_readback(self):
        self.transport.uncertain_create = True
        self.assertEqual(self.publish()['state'], 'published')

    def test_changed_workspace_cannot_be_published(self):
        (self.workspace / 'src/main.py').write_text('changed after tests\n')
        with self.assertRaisesRegex(ValueError, 'publication_receipt_conflict'):
            self.publish()
        self.assertEqual(self.transport.calls, [])

    def test_null_source_commit_is_blocked_even_with_good_build_artifact(self):
        source = copy.deepcopy(self.source)
        source['base_commit'] = None
        durable_write(self.state / 'snapshots' / (self.source['id'] + '.json'), source)
        with self.assertRaisesRegex(ValueError, 'publication_git_source_proof_required'):
            self.publish()
        self.assertEqual(self.transport.calls, [])

    def test_changed_main_blocks_remote_writes(self):
        self.transport.current_main = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'publication_base_main_changed'):
            self.publish()
        self.assertFalse(any(call[:2] == ['git', 'push'] for call in self.transport.calls))

    def test_existing_remote_branch_is_not_overwritten(self):
        self.transport.remote = 'd' * 40
        with self.assertRaisesRegex(ValueError, 'publication_branch_already_exists'):
            self.publish()
        self.assertFalse(any(call[:2] == ['git', 'push'] for call in self.transport.calls))
        self.assertFalse((self.state / 'publications' / 'action-42.json').exists())

    def test_other_repository_policy_cannot_enable_auto_merge(self):
        path = self.root / 'repositories.json'
        records = json.loads(path.read_text())
        records['pull_request_policy']['auto_merge'] = True
        path.write_text(json.dumps(records))
        with self.assertRaisesRegex(ValueError, 'publication_policy_invalid'):
            self.publish()
        self.assertEqual(self.transport.calls, [])

    def test_forged_pr_head_is_not_accepted_after_remote_writes(self):
        original = self.transport.__class__.__call__
        transport = self.transport
        def altered(args, **kwargs):
            result = original(transport, args, **kwargs)
            if args[:3] == ['gh', 'pr', 'list'] and transport.pr:
                result.stdout = json.dumps([dict(transport.pr, headRefOid='f' * 40)])
            return result
        self.publisher.execute = altered
        with self.assertRaisesRegex(ValueError, 'publication_pr_head_unconfirmed'):
            self.publish()
        self.assertEqual(self.publisher.trusted_json(self.state / 'publications' / 'action-42.json')['state'], 'write_intent')


if __name__ == '__main__':
    unittest.main()
