"""오프라인 CLI가 실제 저장 파일을 읽고 원본을 보존하는 시험."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from liveprobe.tests.test_application_evaluation_selection import observed, truth
from liveprobe.tests.test_application_evaluation_shadow import saved


def offline():
    if importlib.util.find_spec("evaluation.offline") is None:
        raise AssertionError("저장 출력 평가·shadow 재생 CLI가 없다")
    from evaluation import offline as module
    return module


class OfflineCliTests(unittest.TestCase):
    def test_evaluate_writes_reproducible_report_without_modifying_input(self):
        api = offline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records.json"
            labels = root / "labels.json"
            output = root / "report.json"
            records.write_text(json.dumps([observed()]))
            labels.write_text(json.dumps({"합성-1": truth()}))
            before = records.read_bytes(), labels.read_bytes()
            self.assertEqual(api.main(["evaluate", "--records", str(records), "--truth", str(labels), "--out", str(output)]), 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["runs"][0]["selection_scores"]["level"]["verdict"], "pass")
            self.assertEqual(report["runs"][0]["selection_scores"]["models"]["verdict"], "unknown")
            self.assertEqual((records.read_bytes(), labels.read_bytes()), before)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(api.main(["evaluate", "--records", str(records), "--truth", str(labels), "--out", str(records)]), 2)
            self.assertEqual(records.read_bytes(), before[0])

    def test_shadow_cli_replays_only_saved_rows(self):
        api = offline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "roles.json"
            output = root / "shadow.json"
            records.write_text(json.dumps([saved(), saved("저비용", "low", 0.2)]))
            rc = api.main(["shadow", "--records", str(records), "--role", "challenger",
                           "--baseline", '{"model":"고성능","effort":"high"}',
                           "--recommendation", '{"model":"저비용","effort":"low"}', "--out", str(output)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(output.read_text())["verdict"], "pass")

    def test_equivalent_output_path_cannot_overwrite_input(self):
        api = offline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records.json"
            records.write_text("[]")
            (root / "subdir").mkdir()
            output_alias = root / "subdir" / ".." / "records.json"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(api.main(["utility", "--records", str(records), "--out", str(output_alias)]), 2)
            self.assertEqual(records.read_text(), "[]")

    def test_root_system_links_are_allowed_but_not_user_links(self):
        api = offline()
        # Real root aliases on macOS, without opening /etc or creating system files.
        for alias in ("/var", "/tmp", "/etc"):
            path = Path(alias) / "liveprobe-public-evaluation" / "records.json"
            self.assertEqual(api._safe_path(path), path)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "records.json"
            original.write_text("[]")
            file_alias = root / "alias.json"
            file_alias.symlink_to(original)
            parent_alias = root / "alias-dir"
            parent_alias.symlink_to(root, target_is_directory=True)
            dangling = root / "missing-alias.json"
            dangling.symlink_to(root / "missing.json")
            for path in (file_alias, parent_alias / "records.json", dangling):
                with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "링크"):
                    api._safe_path(path)

    def test_nonstandard_system_link_target_is_not_trusted(self):
        from unittest.mock import patch
        api = offline()
        # A root alias is allowed only for the fixed OS destination, not by name alone.
        with patch.object(Path, "readlink", return_value=Path("unexpected-target")):
            with self.assertRaisesRegex(ValueError, "링크"):
                api._safe_path(Path("/var/liveprobe-public-evaluation/records.json"))

    def test_credential_file_paths_are_rejected_before_read(self):
        api = offline()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "google_oauth.json"
            credential.mkdir()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(api.main(["utility", "--records", str(credential)]), 2)


if __name__ == "__main__":
    unittest.main()
