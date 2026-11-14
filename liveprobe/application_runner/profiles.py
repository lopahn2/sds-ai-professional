"""저장소에 등록된 삼성 프로필만 실행 대상으로 허용한다."""
import hashlib
import json
import re
from pathlib import Path


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
        self.images = json.loads((self.root / 'deploy/samsung/images.json').read_bytes())
        records = json.loads((self.root / 'deploy/samsung/application-profiles.json').read_bytes())['profiles']
        self.profiles = {p['service']: p for p in records}
        if set(self.profiles) != set(self.catalog['service_to_system']) or len(records) != len(self.profiles):
            raise ValueError('profile_catalog_mismatch')
        for service, p in self.profiles.items():
            if p['system'] != self.catalog['service_to_system'][service]:
                raise ValueError('profile_system_mismatch')
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
        if target['environment'] not in self.catalog['environments'] or target.get('system', p['system']) != p['system']:
            raise ValueError('target_group_mismatch')
        return p
