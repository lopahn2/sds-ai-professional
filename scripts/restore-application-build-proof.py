#!/usr/bin/env python3
"""Recover a lost local build proof from an exact archived release and its observed manifest."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'liveprobe'))
from application_runner.build import final_manifest
from application_runner.profiles import Registry, digest, manifest_image
from application_runner.source import parse_patch, safe_path, source_files
from application_runner.worker import durable_write


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def restore(document, image, destination):
    registry = Registry(ROOT)
    profile = registry.get(document['service'])
    manifest_image(image, profile['image_repository'])
    records = document['data']
    rounds = [*records.get('rounds', []), records]
    release = next((r['release'] for r in rounds if (r.get('release') or {}).get('artifact', {}).get('image') == image), None)
    require(release and digest(release['artifact']) == release['artifact_sha256'], 'matching_archived_build_required')
    artifact = release['artifact']
    require(any(row.get('image') == image for row in
                release['stage_results'].get('dev', {}).get('verification', [])), 'verified_dev_image_required')
    tests = release['tests']
    require(tests['before_failed'] and tests['after_passed'] and tests['regression_executed'] and
            tests['total'] > 0 and tests['failed'] == 0, 'tested_release_required')
    source, patch = records['source_snapshot'], records['patch']
    require(source['profile_id'] == profile['id'] and source['provenance_verified'] is True and
            digest({k:v for k,v in source.items() if k != 'id'}) == source['id'] ==
            patch['source_snapshot_id'] == artifact['source_snapshot_id'], 'stored_source_mismatch')
    require(digest(patch['diff'].encode()) == patch['diff_sha256'] == artifact['diff_sha256'], 'stored_patch_mismatch')
    require(re.fullmatch('[a-f0-9]{64}', artifact['source_tree_sha256']), 'invalid_source_tree_digest')
    inspection = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    require(final_manifest(inspection, profile['image_repository']) == image, 'manifest_mismatch')
    platform = inspection['Os'] + '/' + inspection['Architecture']
    receipt = {'image':image, 'manifest_observed':True, 'platform':platform, 'source_tree_sha256':artifact['source_tree_sha256']}
    require(digest(receipt) == artifact['build_receipt_sha256'], 'build_receipt_mismatch')

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not destination.is_symlink() and destination.stat().st_uid == os.getuid() and
            not destination.stat().st_mode & 0o077, 'private_destination_required')
    target = destination / artifact['source_tree_sha256']
    proof_file = destination / (profile['service'] + '-' + image.rsplit(':',1)[1] + '.json')
    require(not target.is_symlink() and not proof_file.is_symlink(), 'symlink_forbidden')
    temp = Path(tempfile.mkdtemp(dir=destination))
    try:
        for name, content in source['files'].items():
            path = safe_path(temp, name)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(content)
        require({p:digest(t.encode()) for p,t in source_files(temp).items()} == source['file_sha256'], 'base_tree_mismatch')
        for name, content in parse_patch(patch['diff'],temp,profile['test_root']).items():
            path = safe_path(temp,name)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(content)
        hashes = {p:digest(t.encode()) for p,t in source_files(temp).items()}
        require(digest(hashes) == artifact['source_tree_sha256'], 'release_tree_mismatch')
        if target.exists():
            require(not target.is_symlink() and {p:digest(t.encode()) for p,t in source_files(target).items()} == hashes,
                    'existing_tree_mismatch')
        else:
            temp.rename(target)
        proof = {'image':image, 'profile_id':profile['id'], 'file_sha256':hashes, 'source_root':str(target),
                 'provenance_kind':'local_build_receipt', 'base_commit':None}
        if proof_file.exists():
            require(not proof_file.is_symlink() and json.loads(proof_file.read_text()) == proof, 'existing_proof_mismatch')
        else:
            durable_write(proof_file, proof)
        print(f"{profile['service']} {image} · {len(hashes)} source files verified")
    finally:
        if temp.exists():
            shutil.rmtree(temp)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incident', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    require(re.fullmatch('[a-zA-Z0-9_-]{1,128}',args.incident), 'invalid_incident_id')
    os.umask(0o077)
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    origin = 'http://127.0.0.1:4175'
    with client.open(urllib.request.Request(origin+'/', headers={'Accept':'text/html'}), timeout=15) as entry:
        require(entry.geturl() == origin+'/', 'unexpected_local_console')
    path = origin+'/api/application/v1/incidents/'+args.incident
    with client.open(urllib.request.Request(path, headers={'X-LiveProbe-Client':'dashboard'}), timeout=20) as response:
        require(response.geturl() == path, 'unexpected_incident_url')
        document = json.load(response)
    require(document['id'] == args.incident, 'incident_mismatch')
    restore(document, args.image, args.destination.absolute())
