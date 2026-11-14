"""경로, 이미지와 소스 대조, 엄격 patch 회귀."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from liveprobe.application_runner.profiles import Registry, digest, manifest_image


class SourceTests(unittest.TestCase):
    def test_source_snapshot_matches_manifest_and_stack_paths(self):
        try:
            from liveprobe.application_runner.source import safe_path, snapshot, map_stack
        except ImportError:
            self.fail('소스 대조 구현이 없습니다')
        p = Registry(ROOT).get('trade-member')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root/'src/samsung_domain').mkdir(parents=True)
            (root/'src/samsung_domain/api.py').write_text('answer = 1\n')
            files = {'src/samsung_domain/api.py': digest(b'answer = 1\n')}
            image = 'liveprobe/samsung-domain@sha256:'+'a'*64
            proof = {'image':image, 'profile_id':p['id'], 'file_sha256':files, 'base_commit':None}
            actual = snapshot(root, p, image, proof)
            self.assertTrue(actual['provenance_verified'])
            self.assertEqual(map_stack('/opt/samsung-domain/src/samsung_domain/api.py:99', p, actual), 'src/samsung_domain/api.py')
            with self.assertRaises(ValueError):
                map_stack('/app/src/samsung_domain/api.py:99', p, actual)
            (root/'src/samsung_domain/api.py').write_text('answer = 2\n')
            with self.assertRaisesRegex(ValueError, 'source_mismatch'):
                snapshot(root, p, image, proof)
            for path in ('../x.py','/x.py','a/../../x.py','x\\y.py','.env','x.env','google_oauth.json','auth.py','secret/a.sql','private/key.toml','conftest.py','x.pem','dump.sql'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    safe_path(root, path)
            (root/'link.py').symlink_to(root/'src/samsung_domain/api.py')
            with self.assertRaises(ValueError):
                safe_path(root, 'link.py')
            for path in ('schema.sql','index.html','requirements.lock','pyproject.toml'):
                self.assertEqual(safe_path(root,path), root/path)
        with self.assertRaises(ValueError):
            manifest_image('sha256:'+'a'*64)

    def test_patch_adds_test_then_code_without_shell_or_fuzzy_apply(self):
        try:
            from liveprobe.application_runner.source import parse_patch, apply_changes
        except ImportError:
            self.fail('격리 patch 구현이 없습니다')
        diff = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n--- /dev/null\n+++ b/tests/test_fix.py\n@@ -0,0 +1 @@\n+assert value == 2\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root/'app.py').write_text('value = 1\n')
            changes = parse_patch(diff, root, 'tests')
            self.assertEqual(set(changes), {'app.py','tests/test_fix.py'})
            apply_changes(root, changes, tests_only=True, test_root='tests')
            self.assertEqual((root/'app.py').read_text(), 'value = 1\n')
            self.assertTrue((root/'tests/test_fix.py').exists())
            apply_changes(root, changes, tests_only=False, test_root='tests')
            self.assertEqual((root/'app.py').read_text(), 'value = 2\n')
            with self.assertRaises(ValueError):
                parse_patch(diff.replace('value = 1','value = 9'),root,'tests')
            with self.assertRaises(ValueError):
                parse_patch(diff.replace('app.py','../app.py'),root,'tests')

if __name__ == '__main__':
    unittest.main()
