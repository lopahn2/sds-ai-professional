"""실행 영향 코드·설정·입력·의존 지문의 불변 계약 시험."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evaluation.harness import manifest as manifests


class ManifestTests(unittest.TestCase):
    def test_all_execution_code_and_dependency_changes_invalidate_fingerprint(self):
        self.assertTrue(hasattr(manifests, "execution_fingerprint"), "실행 영향 전체 파일 지문이 없다")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("liveprobe/app/agent_groups.py", "liveprobe/app/rag.py", "liveprobe/app/llm/provider.py",
                         "liveprobe/requirements.txt", "evaluation/metrics/compute.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("원본\n")
            args = dict(config_sha256="a" * 64, input_sha256="b" * 64, image_digest="sha256:" + "c" * 64,
                        dependencies={"합성의존성": "1"})
            before = manifests.execution_fingerprint(root, **args)
            self.assertTrue(before["complete"])
            for name in before["files"]:
                path = root / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                self.assertNotEqual(before["sha256"], manifests.execution_fingerprint(root, **args)["sha256"])
                path.write_bytes(original)
            self.assertNotEqual(before["sha256"], manifests.execution_fingerprint(root, **dict(args, config_sha256="d" * 64))["sha256"])
            self.assertNotEqual(before["sha256"], manifests.execution_fingerprint(root, **dict(args, dependencies={"합성의존성": "2"}))["sha256"])
            self.assertEqual(before, manifests.execution_fingerprint(root, **args))

    def test_missing_config_input_image_is_incomplete_not_fabricated(self):
        self.assertTrue(hasattr(manifests, "execution_fingerprint"), "입력 고정 완전성 계약이 없다")
        with tempfile.TemporaryDirectory() as tmp:
            result = manifests.execution_fingerprint(Path(tmp), dependencies={})
        self.assertFalse(result["complete"])
        self.assertTrue(result["missing"])

    def test_credentials_are_excluded_and_source_symlink_is_refused(self):
        self.assertTrue(hasattr(manifests, "execution_fingerprint"), "안전한 실행 지문 계약이 없다")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "liveprobe/app"
            app.mkdir(parents=True)
            # 내용 접근 여부가 관측되도록 자격 파일 대신 읽으면 실패하는 디렉터리를 둔다.
            (app / "google_oauth.json").mkdir()
            (app / ".env").mkdir()
            (app / "normal.py").write_text("pass")
            result = manifests.execution_fingerprint(root, dependencies={})
            self.assertEqual(set(result["files"]), {"liveprobe/app/normal.py"})
            (app / "alias.py").symlink_to(app / "normal.py")
            with self.assertRaisesRegex(ValueError, "링크"):
                manifests.execution_fingerprint(root, dependencies={})

    def test_resume_refuses_tampered_body_even_if_manifest_id_is_unchanged(self):
        self.assertTrue(hasattr(manifests, "hash_manifest"), "manifest 본문 검증 계약이 없다")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            original = {"mode": "dry_run"}
            original["manifest_id"] = manifests.hash_manifest(original)
            path.write_text(json.dumps(dict(original, mode="real")))
            with self.assertRaises(manifests.ManifestMismatch):
                manifests.load_or_init(path, original)


if __name__ == "__main__":
    unittest.main()
