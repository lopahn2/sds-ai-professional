"""Check the immutable source provenance for the product's reservation trial."""

from hashlib import sha256
import json
from pathlib import Path

from evaluation.jdbc_dev_manifest import IMAGE
from evaluation.matched_recovery_campaign import canonical_digest, write_new
from evaluation.prepare_jdbc_fixture import VARIANTS
from liveprobe.app.application.catalog import DEV_RESERVATION, DEV_RESERVATION_PROFILE, catalog_services
from liveprobe.application_runner.profiles import digest, manifest_image
from liveprobe.application_runner.source import snapshot, source_files


def write_local_registry(repository, destination, image):
    """Keep the optional registration out of the standard deployment catalog."""
    manifest_image(image, DEV_RESERVATION_PROFILE['image_repository'])
    files = ('system-catalog.json', 'application-profiles.json', 'images.json')
    base = repository / 'deploy/samsung'
    catalog, profiles, images = (json.loads((base / name).read_bytes()) for name in files)
    if catalog_services(catalog)[1]:
        raise ValueError('expected the unmodified product catalog')
    catalog['dev_only_services'] = [DEV_RESERVATION]
    system = DEV_RESERVATION_PROFILE['system']
    catalog['service_to_system'][DEV_RESERVATION] = system
    catalog['systems'].append({'id': system, 'display_name': 'LiveProbe 검증', 'services': [DEV_RESERVATION]})
    profiles['profiles'].append(DEV_RESERVATION_PROFILE)
    images[DEV_RESERVATION] = image
    output = destination / 'deploy/samsung'
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, document in zip(files, (catalog, profiles, images), strict=True):
        write_new(output / name, document)
    return output


def dev_runner_roles():
    api = 'rbac.authorization.k8s.io'
    items = []
    for role, account, verbs in (
        ('source', 'liveprobe-application-source', ['get']),
        ('qa', 'liveprobe-application-qa', ['get']),
        ('cluster', 'liveprobe-application-runner', ['get', 'patch']),
    ):
        name = 'lp-eval-reservation-' + role
        metadata = {'name': name, 'namespace': 'dev'}
        rules = [{'apiGroups': ['apps'], 'resources': ['deployments'],
                  'resourceNames': [DEV_RESERVATION], 'verbs': verbs}]
        if role == 'cluster':
            rules.append({'apiGroups': [''], 'resources': ['services/proxy'],
                          'resourceNames': [f'http:{DEV_RESERVATION}:8080'], 'verbs': ['get']})
        items.extend((
            {'apiVersion': api+'/v1', 'kind': 'Role', 'metadata': metadata, 'rules': rules},
            {'apiVersion': api+'/v1', 'kind': 'RoleBinding', 'metadata': metadata,
             'roleRef': {'apiGroup': api, 'kind': 'Role', 'name': name},
             'subjects': [{'kind': 'ServiceAccount', 'name': account, 'namespace': 'liveprobe-system'}]},
        ))
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}


def source_proof(compiled, variant, artifact):
    if variant not in VARIANTS or not IMAGE.fullmatch(artifact['image']):
        raise ValueError('expected a sealed reservation variant and local image')
    root = Path(compiled['root']) / variant
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('the original source tree is unavailable')
    files = source_files(root)
    archive = root / 'target/reservation-1.0.0.jar'
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('the compiled reservation archive is unavailable')
    if (canonical_digest(files) != compiled['source_sha256'][variant]
            or sha256(archive.read_bytes()).hexdigest() != compiled['jar_sha256'][variant]
            or artifact['jar_sha256'] != compiled['jar_sha256'][variant]):
        raise ValueError('the original source or archive differs from the sealed build')
    proof = {
        'image': artifact['image'], 'profile_id': DEV_RESERVATION_PROFILE['id'], 'base_commit': None,
        'file_sha256': {name: digest(body.encode()) for name, body in files.items()},
        'source_root': str(root), 'provenance_kind': 'local_build_receipt',
    }
    return proof, snapshot(root, DEV_RESERVATION_PROFILE, artifact['image'], proof)['id']
