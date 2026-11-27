"""저장소에 등록된 삼성 프로필만 실행 대상으로 허용한다."""
import hashlib
import json
import re
from pathlib import Path

if __package__.startswith('liveprobe.'):
    from ..app.application.catalog import DEV_RESERVATION_PROFILE, catalog_services
else:
    from app.application.catalog import DEV_RESERVATION_PROFILE, catalog_services


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def manifest_image(image, repository=None):
    if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}', image):
        raise ValueError('manifest_digest_required')
    if repository and image.split('@')[0] != repository:
        raise ValueError('image_repository_mismatch')
    return image


class Registry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        raw = (self.root / 'deploy/samsung/system-catalog.json').read_bytes()
        self.catalog_sha256 = hashlib.sha256(raw).hexdigest()
        self.catalog = json.loads(raw)
        services, self.dev_only = catalog_services(self.catalog)
        self.images = json.loads((self.root / 'deploy/samsung/images.json').read_bytes())
        records = json.loads((self.root / 'deploy/samsung/application-profiles.json').read_bytes())['profiles']
        self.profiles = {p['service']: p for p in records}
        if set(self.profiles) != set(services) or len(records) != len(self.profiles):
            raise ValueError('profile_catalog_mismatch')
        for service, p in self.profiles.items():
            if p['system'] != services[service]:
                raise ValueError('profile_system_mismatch')
            if service in self.dev_only and p != DEV_RESERVATION_PROFILE:
                raise ValueError('dev_profile_mismatch')
            manifest_image(self.images[service], p['image_repository'])

    def get(self, service, profile_id=None):
        if service not in self.profiles:
            raise ValueError('unregistered_service')
        p = self.profiles[service]
        if profile_id is not None and profile_id != p['id']:
            raise ValueError('unregistered_profile')
        return p

    def target(self, target):
        p = self.get(target['service'])
        if (target['environment'] not in self.catalog['environments'] or target.get('system', p['system']) != p['system'] or
                (p['service'] in self.dev_only and target['environment'] != 'dev')):
            raise ValueError('target_group_mismatch')
        return p
