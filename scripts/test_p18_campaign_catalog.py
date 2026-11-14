"""Fixed catalog contract. Catalog expansion is not execution evidence."""
import importlib.util
from pathlib import Path
import unittest
from collections import Counter

SCRIPT = Path(__file__).with_name('p18-campaign-catalog.py')


class CatalogContract(unittest.TestCase):
    def test_catalog_exists_and_has_180_unique_cases(self):
        self.assertTrue(SCRIPT.exists(), 'fixed campaign catalog is not implemented')
        spec = importlib.util.spec_from_file_location('catalog', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rows = mod.cases()
        self.assertEqual(len(rows), 180)
        self.assertEqual(len({r['id'] for r in rows}), 180)
        self.assertEqual(Counter(r['system'] for r in rows), {n: 30 for n in mod.SYSTEMS})
        for n in mod.SYSTEMS:
            own = [r for r in rows if r['system'] == n]
            self.assertEqual(Counter(r['planned_group'] for r in own), {'SIMPLE':10,'NORMAL':10,'COMPLEX':10})
        for r in rows:
            self.assertEqual(r['execution_state'], 'not_run')
            self.assertTrue(r['injections'])
            self.assertIn(r['restore'], ('clear_and_restart', 'baseline_image_and_restart'))
            self.assertFalse(r.get('observed_group'))


if __name__ == '__main__':
    unittest.main()
