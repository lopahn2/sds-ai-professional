"""서명된 값이라고 가정하지 않고 등록된 안전 메타데이터만 읽는 카탈로그."""
import hashlib
import json
from pathlib import Path
from .safety import safe_source_path, sha

SERVICE_SYSTEMS = {
    'order': 'samsung-trade', 'inventory': 'samsung-trade', 'catalog': 'samsung-trade',
    'payment': 'samsung-trade', 'trade-member': 'samsung-trade',
    'delivery': 'samsung-delivery', 'notification': 'samsung-delivery',
    'delivery-member': 'samsung-delivery', 'business-api': 'samsung-business',
}

# Business assertions consumed by the five installed dev UI adapters, relative to the pinned service source.
CONTROLLER_TESTS = {
    'order': ('java', 'src/test/java/io/liveprobe/target/order/OrderControllerTest.java',
              ('returnsInventoryData', 'notFoundIsNotRetried'),
              ('GET /api/orders/check?item=…에서 등록 상품은 200이고 orderable과 inventory.quantity가 재고 조회와 일치한다.',
               '없는 상품은 재고 조회가 한 번 호출되고 404를 반환한다.')),
    'catalog': ('python', 'tests/test_business.py', ('CatalogTests.test_search_detail_and_prices',),
                ('GET /api/products?q=…의 검색 건수와 첫 상품 가격을 시험 데이터와 대조한다.',
                 'GET /api/products/{id}의 상품 상세 설명과 재고를 확인한다. 두 요청 모두 200이다.')),
    'payment': ('python', 'tests/test_business.py', ('test_history_has_fixed_retention_and_monotonic_ids',),
                ('GET /api/payments의 payments 목록에서 최근 100건을 보존하고, 100개의 결제 ID가 모두 서로 다름을 확인한다.',)),
    'notification': ('python', 'tests/test_business.py', ('test_enqueue_and_complete_all_channels',),
                     ('격리 시험에서 세 채널 알림을 등록한 뒤 GET /api/notifications의 queue_depth가 3인지 확인한다.',)),
    'delivery': ('node', 'test/index.js', ('송장 등록·조회·순차 단계 변경·지연 목록',),
                 ('격리 시험에 지연 송장을 준비하고 GET /api/deliveries?delayed=true의 deliveries 목록에 항목이 있는지 확인한다.',)),
}


class Catalog:
    def __init__(self, settings):
        self.sha256 = sha(SERVICE_SYSTEMS)
        self.profiles = {}
        self.configured = False
        self.dependencies = {}
        self.systems = sorted(set(SERVICE_SYSTEMS.values()))
        try:
            if not settings.catalog_file or not settings.profiles_file:
                raise ValueError()
            raw = Path(settings.catalog_file).read_bytes()
            catalog = json.loads(raw)
            if catalog['service_to_system'] != SERVICE_SYSTEMS or catalog['environments'] != ['dev', 'prd']:
                raise ValueError()
            profiles = json.loads(Path(settings.profiles_file).read_text())['profiles']
            self.profiles = {profile['service']: profile for profile in profiles}
            if len(profiles) != 9 or set(self.profiles) != set(SERVICE_SYSTEMS):
                raise ValueError()
            for service, profile in self.profiles.items():
                if (profile['system'] != SERVICE_SYSTEMS[service] or not isinstance(profile['id'], str) or
                    not profile['id'] or profile['language'] not in ('java', 'node', 'python', 'domain') or
                    not profile['affected_services'] or service not in profile['affected_services'] or
                    any(item not in SERVICE_SYSTEMS for item in profile['affected_services'])):
                    raise ValueError()
            self.sha256 = hashlib.sha256(raw).hexdigest()
            self.dependencies = catalog.get('dependencies', {})
            self.configured = True
        except (OSError, ValueError, TypeError, KeyError):
            self.profiles = {}
        if settings.testing and not settings.profiles_file:
            # 합성 SQLite 장치 전용. 런타임에서 임의 프로필을 만들지 않는다.
            self.profiles = {service: {'id': service, 'service': service, 'system': system,
                'language': 'python', 'test_root': 'tests', 'affected_services': [service]}
                for service, system in SERVICE_SYSTEMS.items()}
            self.configured = True

    def missing_controller_test(self, service):
        registered = CONTROLLER_TESTS.get(service)
        profile = self.profiles.get(service)
        if not self.configured or not registered or not profile:
            return None
        language, path, cases, expectations = registered
        if (profile.get('language') != language or profile.get('service') != service or
                not safe_source_path(path) or not path.startswith((profile.get('test_root') or '\0') + '/')):
            return None
        return {'reason': 'qa_tests_missing', 'service': service, 'profile_id': profile['id'],
                'test_file': path, 'test_cases': list(cases), 'expectations_ko': list(expectations),
                'instruction': 'Add the registered controller test at test_file in the service source. '
                               'Implement the specified business assertions using isolated test fixtures.'}

    def public(self):
        return {'systems': self.systems, 'services': [{'service': service, 'system': system,
            'profile_id': self.profiles.get(service, {}).get('id'),
            'affected_services': self.profiles.get(service, {}).get('affected_services', [])}
            for service, system in SERVICE_SYSTEMS.items()], 'environments': ['dev', 'prd'], 'historical_environments': ['dev', 'stg', 'prd'],
            'catalog_sha256': self.sha256, 'profiles_configured': self.configured}
