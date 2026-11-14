"""Parent image provenance guards; Docker command fixtures are not workload evidence."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


def module():
    spec = importlib.util.spec_from_file_location('build_targets', Path(__file__).with_name('p18-build-targets.py'))
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class BuildGuards(unittest.TestCase):
    def test_pin_local_runtime_uses_verified_image_id(self):
        m = module()
        image_id = 'sha256:' + 'a' * 64
        with patch.object(m.subprocess, 'check_output', return_value=json.dumps([{'Id': image_id}]).encode()), patch.object(m.subprocess, 'run') as run:
            tag, receipt = m.pin_local_runtime('inventory', 'local/inventory@sha256:' + 'b' * 64)
        self.assertEqual(receipt['image_id'], image_id)
        self.assertEqual(tag, 'liveprobe/inventory:p18-runtime-' + 'a' * 64)
        self.assertEqual(run.call_args.args[0], ['docker', 'tag', image_id, tag])
        self.assertTrue(run.call_args.kwargs['check'])

    def test_changed_local_alias_is_rejected(self):
        m = module()
        outputs = [json.dumps([{'Id': 'sha256:' + c * 64}]).encode() for c in ('a', 'b')]
        with patch.object(m.subprocess, 'check_output', side_effect=outputs), patch.object(m.subprocess, 'run'):
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                m.pin_local_runtime('inventory', 'local/inventory:base')


if __name__ == '__main__':
    unittest.main()
