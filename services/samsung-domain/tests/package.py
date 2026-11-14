"""arm64 이미지 build·import·의존성 확인을 실제 종료코드와 기록한다."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from manifest import source_manifest

root = Path(__file__).resolve().parents[1]
repo = root.parents[1]
run_id = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
evidence = repo / 'docs/evidence/samsung/domain' / ('package-' + run_id)
evidence.mkdir(parents=True)
tag = 'liveprobe/samsung-domain:domain-' + run_id.lower()
receipt = {'image': tag, 'source_sha256': source_manifest(root)['source_sha256'], 'platform': 'linux/arm64'}
(evidence / 'source-manifest.json').write_text(json.dumps(source_manifest(root), ensure_ascii=False, indent=2) + '\n')
result = subprocess.run(['docker', 'build', '--platform', 'linux/arm64', '--pull=false', '-t', tag, str(root)], capture_output=True, text=True, timeout=300)
(evidence / 'build.log').write_text(result.stdout + result.stderr)
receipt['build_exit_code'] = result.returncode
if result.returncode == 0:
    inspection = subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True, text=True)
    receipt['inspect_exit_code'] = inspection.returncode
    image = json.loads(inspection.stdout)[0]
    receipt.update(repo_digests=image.get('RepoDigests', []), image_id=image['Id'], architecture=image['Architecture'], runtime_user=image['Config']['User'])
    receipt['deployment_digest_observed'] = bool(receipt['repo_digests'])
    # RepoDigest가 비어 있으면 image_id를 배포 digest로 바꾸지 않는다.
    check = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--memory', '128m', '--entrypoint', 'python', tag,
        '-B', '-c', "import samsung_domain.main, confluent_kafka, psycopg; from pathlib import Path; import samsung_domain; p=Path(samsung_domain.__file__).parent; assert (p/'schema.sql').is_file() and (p/'index.html').is_file(); print('arm64 런타임 import 및 정적 자산 확인 성공')"], capture_output=True, text=True, timeout=30)
    receipt['image_import_exit_code'] = check.returncode
    (evidence / 'image-check.log').write_text(check.stdout + check.stderr)
    dependency = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--memory', '128m', '--entrypoint', 'python', tag, '-B', '-m', 'pip', 'check'], capture_output=True, text=True, timeout=30)
    receipt['image_dependency_exit_code'] = dependency.returncode
    (evidence / 'dependencies.log').write_text(dependency.stdout + dependency.stderr)
(evidence / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(receipt, ensure_ascii=False, indent=2))
print('이미지 검증 보고서:', evidence / 'receipt.json')
sys.exit(0 if result.returncode == 0 and receipt.get('image_import_exit_code') == 0 and receipt.get('image_dependency_exit_code') == 0 else 1)
