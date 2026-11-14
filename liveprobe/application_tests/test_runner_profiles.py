"""외부 연결 없는 실행기 첫 계약 시험."""
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class ProfileTests(unittest.TestCase):
    def test_catalog_nine_services_and_domain_impact(self):
        try:
            from liveprobe.application_runner.profiles import Registry
        except ModuleNotFoundError:
            self.fail('등록 프로필 구현이 없습니다')
        registry = Registry(ROOT)
        self.assertEqual(len(registry.profiles), 9)
        self.assertEqual(registry.get('delivery')['test_root'], 'test')
        self.assertEqual(registry.get('order')['test_root'], 'src/test')
        for service in ('business-api', 'trade-member', 'delivery-member'):
            p = registry.get(service)
            self.assertEqual(p['source_root'], 'services/samsung-domain')
            self.assertEqual(p['runtime_root'], '/opt/samsung-domain')
            self.assertEqual(set(p['affected_services']), {'business-api', 'trade-member', 'delivery-member'})
        with self.assertRaises(ValueError):
            registry.target({'environment': 'dev', 'service': 'payment', 'system': 'samsung-delivery'})


if __name__ == '__main__':
    unittest.main()
