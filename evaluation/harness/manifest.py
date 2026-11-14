"""Run manifest: pins versions and input hashes; the manifest id binds a store."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import subprocess
import re
from importlib import metadata

from . import HARNESS_VERSION
from .arms import ARMS
from .cases import REPO, Catalog

# Files whose content defines behaviour of a run. Hashes, not contents, are stored.
PINNED_FILES = (
    "evaluation/harness/__init__.py", "evaluation/harness/arms.py", "evaluation/harness/cases.py",
    "evaluation/harness/manifest.py", "evaluation/harness/store.py", "evaluation/harness/scheduler.py",
    "evaluation/harness/runners.py", "chaos/validate.py", "liveprobe/app/config.py",
    "liveprobe/app/router.py", "liveprobe/app/workflows.py", "liveprobe/app/engine.py",
)


def hash_manifest(body: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in body.items() if k != "manifest_id"},
                                    sort_keys=True).encode()).hexdigest()


def execution_fingerprint(root: Path = REPO, *, config_sha256=None, input_sha256=None,
                          image_digest=None, dependencies=None) -> dict:
    """자격 파일을 탐색하지 않고 허용된 코드·의존 명세와 입력 지문만 고정한다."""
    root = Path(root).resolve()
    paths = set()
    for base in ("liveprobe/app", "evaluation", "liveprobe/alembic"):
        directory = root / base
        if directory.is_symlink():
            raise ValueError("실행 소스 디렉터리 링크는 허용하지 않는다")
        paths.update(directory.rglob("*.py"))
    for pattern in ("liveprobe/requirements*.txt", "liveprobe/pyproject.toml", "liveprobe/uv.lock",
                    "liveprobe/poetry.lock", "pyproject.toml", "uv.lock", "requirements*.txt",
                    "chaos/validate.py", "liveprobe/Dockerfile*"):
        paths.update(root.glob(pattern))
    files = {}
    for path in sorted(paths):
        rel = path.relative_to(root)
        if any(p.startswith(".") or p in ("__pycache__", "private", "credentials", "secrets") for p in rel.parts):
            continue
        if any(part.is_symlink() for part in (path, *path.parents) if part != root):
            raise ValueError("실행 소스 링크는 허용하지 않는다")
        if path.is_file():
            files[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    versions = dependencies if dependencies is not None else {
        name: metadata.version(name) for name in sorted({dist.metadata["Name"] for dist in metadata.distributions()
                                                       if dist.metadata["Name"]})}
    missing = []
    for name, value in (("config_sha256", config_sha256), ("input_sha256", input_sha256)):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
            raise ValueError("설정·입력 지문 형식이 올바르지 않다")
        if value is None:
            missing.append(name)
    if image_digest is not None and (not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)):
        raise ValueError("이미지 manifest 지문 형식이 올바르지 않다")
    if image_digest is None:
        missing.append("image_digest")
    if not files:
        missing.append("files")
    if not versions:
        missing.append("dependencies")
    body = {"schema": "liveprobe-execution-manifest/v2", "files": files, "dependencies": versions,
            "python": platform.python_version(), "config_sha256": config_sha256, "input_sha256": input_sha256,
            "image_digest": image_digest, "complete": not missing, "missing": missing}
    body["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return body


def _file_hash(rel: str) -> str | None:
    p = REPO / rel
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _git_head() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def build_manifest(catalog: Catalog, *, mode: str, cost_ceiling_usd: float, runner_id: str,
                   fault_worker_id: str, arms=ARMS, extra: dict | None = None) -> dict:
    if mode not in ("dry_run", "real"):
        raise ValueError("mode must be dry_run or real")
    body = {
        "harness_version": HARNESS_VERSION,
        "mode": mode,
        "cost_ceiling_usd": cost_ceiling_usd,
        "runner": runner_id,
        "fault_worker": fault_worker_id,
        "python": platform.python_version(),
        "git_head": _git_head(),  # None when the repo has no commit yet; file hashes still pin code.
        "files": {rel: _file_hash(rel) for rel in PINNED_FILES},
        "execution": execution_fingerprint(),
        "arms": {a.arm_id: a.spec_hash() for a in arms},
        "catalog": {
            "label_version": catalog.label_version,
            "labels_manifest_sha256": catalog.labels_manifest_hash,
            "cases": {cid: {"public_sha256": catalog.public_hash(cid), "file_sha256": catalog.file_hashes[cid]}
                      for cid in catalog.case_ids},
        },
        "extra": extra or {},
    }
    body["manifest_id"] = hash_manifest(body)
    return body


def load_or_init(path: Path, manifest: dict) -> dict:
    """First run writes the manifest; a resume must present an identical one."""
    path = Path(path)
    if manifest.get("manifest_id") != hash_manifest(manifest):
        raise ManifestMismatch("요청 manifest 본문 지문이 다르다")
    if path.exists():
        existing = json.loads(path.read_text("utf-8"))
        if existing.get("manifest_id") != hash_manifest(existing):
            raise ManifestMismatch("저장 manifest 본문이 변경되었다")
        if existing.get("manifest_id") != manifest["manifest_id"]:
            diff = sorted(k for k in manifest if existing.get(k) != manifest[k])
            raise ManifestMismatch(f"store belongs to manifest {existing.get('manifest_id', '?')[:12]}, "
                                   f"current {manifest['manifest_id'][:12]}; differing: {diff}")
        return existing
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return manifest


class ManifestMismatch(RuntimeError):
    pass
