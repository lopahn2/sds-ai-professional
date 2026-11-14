"""서비스 소스의 정렬된 파일별 SHA256과 집계 지문."""
import hashlib
from pathlib import Path


def source_manifest(root):
    root = Path(root)
    entries = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or any(part in ('__pycache__', '.venv', 'build', 'dist') or part.endswith('.egg-info') for part in path.relative_to(root).parts):
            continue
        if path.suffix in ('.pyc', '.pyo'):
            continue
        entries.append({'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    aggregate = ''.join(f'{row["sha256"]}  {row["path"]}\n' for row in entries).encode()
    return {'algorithm': 'SHA256', 'source_sha256': hashlib.sha256(aggregate).hexdigest(), 'files': entries}
