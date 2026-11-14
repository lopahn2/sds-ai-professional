"""서명된 값이라고 가정하지 않고 등록된 안전 메타데이터만 읽는 카탈로그."""
import hashlib
import json
from pathlib import Path
from .safety import sha

SERVICE_SYSTEMS = {
    'order': 'samsung-trade', 'inventory': 'samsung-trade', 'catalog': 'samsung-trade',
    'payment': 'samsung-trade', 'trade-member': 'samsung-trade',
    'delivery': 'samsung-delivery', 'notification': 'samsung-delivery',
    'delivery-member': 'samsung-delivery', 'business-api': 'samsung-business',
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

    def public(self):
        return {'systems': self.systems, 'services': [{'service': service, 'system': system,
            'profile_id': self.profiles.get(service, {}).get('id'),
            'affected_services': self.profiles.get(service, {}).get('affected_services', [])}
            for service, system in SERVICE_SYSTEMS.items()], 'environments': ['dev', 'prd'], 'historical_environments': ['dev', 'stg', 'prd'],
            'catalog_sha256': self.sha256, 'profiles_configured': self.configured}
