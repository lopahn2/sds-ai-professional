"""모델·DB·클러스터 없이 저장 평가와 역할 재생을 실행한다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from evaluation.metrics.application import compute_evaluation
from evaluation.role_replay import compare_shadow, role_utility


def _safe_path(path: Path) -> Path:
    path = path.absolute()
    denied = ("private", "credentials", "secrets", "google_oauth.json", "conftest.py", ".ssh", ".aws", ".kube")
    if any(part.lower() in denied or part.lower().startswith(".env") or any(term in part.lower() for term in ("oauth", "credential", "dbdump", "password")) for part in path.parts):
        raise ValueError("비공개·인증 경로는 평가 입력 또는 출력으로 사용할 수 없다")
    # Only macOS's fixed, root-owned aliases are trusted. Never resolve the
    # whole user path first: that would hide a file/ancestor/dangling symlink.
    system_aliases = {Path("/var"): Path("private/var"), Path("/tmp"): Path("private/tmp"),
                      Path("/etc"): Path("private/etc")}
    for part in (path, *path.parents):
        if not part.is_symlink():
            continue
        target = system_aliases.get(part)
        if (target is None or part.lstat().st_uid != 0
                or part.readlink() not in (target, Path("/") / target)):
            raise ValueError("링크 경로는 평가 입력 또는 출력으로 사용할 수 없다")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("evaluate", "shadow", "utility"):
        command = commands.add_parser(name)
        command.add_argument("--records", type=Path, required=True)
        command.add_argument("--out", type=Path)
        if name == "evaluate":
            command.add_argument("--truth", type=Path, required=True)
        if name == "shadow":
            command.add_argument("--role", required=True)
            command.add_argument("--baseline", required=True)
            command.add_argument("--recommendation", required=True)
    args = parser.parse_args(argv)
    try:
        records_path = _safe_path(args.records)
        truth_path = _safe_path(args.truth) if args.command == "evaluate" else None
        inputs = {records_path, truth_path}
        output = _safe_path(args.out) if args.out else None
        # Compare physical identities only after _safe_path inspected every
        # unresolved component; resolving earlier would conceal user symlinks.
        if output is not None and any(output.resolve() == p.resolve() for p in inputs if p is not None):
            raise ValueError("평가 출력은 불변 입력 파일을 덮어쓸 수 없다")
        records = json.loads(records_path.read_text("utf-8"))
        if args.command in ('utility', 'shadow') and isinstance(records, dict):
            import hashlib
            body = {k: v for k, v in records.items() if k not in ('exported_at', 'content_sha256')}
            canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            if hashlib.sha256(canonical.encode()).hexdigest() != records.get('content_sha256'):
                raise ValueError("저장 스냅샷 해시 불일치")
            records = body.get('records')
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError("저장 실행은 객체 배열이어야 한다")
        if args.command == "evaluate" and truth_path is not None:
            labels = json.loads(truth_path.read_text("utf-8"))
            result = compute_evaluation(records, labels)
        elif args.command == "shadow":
            result = compare_shadow(records, role=args.role, baseline=json.loads(args.baseline),
                                    recommendation=json.loads(args.recommendation))
        else:
            result = role_utility(records)
        text = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("평가 입력·지문·경로 계약을 확인해야 한다. 원본 내용은 출력하지 않는다.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
