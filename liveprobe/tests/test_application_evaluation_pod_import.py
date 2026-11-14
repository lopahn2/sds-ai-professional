"""평가 계측이 운영 이미지(liveprobe/app만 포함)에서도 import되는지 확인한다."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

LIVEPROBE = Path(__file__).resolve().parents[1]


class PodImportTests(unittest.TestCase):
    def test_evalrun_imports_without_host_evaluation_package(self):
        # 이미지에는 저장소 루트의 evaluation/ 패키지가 없다. cwd=liveprobe, PYTHONPATH 없음으로 같은 조건을 만든다.
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        probe = ("import importlib.util, sys\n"
                 "assert importlib.util.find_spec('evaluation') is None, 'host evaluation package leaked'\n"
                 "import app.evalrun as e\n"
                 "print(callable(e.export_runs), callable(e.summarize_runs))\n")
        result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=LIVEPROBE, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertEqual(result.stdout.strip(), "True True")

    def test_pod_summary_without_definition_marks_high_class_unknown(self):
        from app import accounting
        rows = [{"model": "gpt-6-astra", "attempts": 2, "input_tokens": 1, "output_tokens": 1,
                 "cost": 0.1, "duration_ms": 1, "usage_summary": {}}]
        result = accounting.summarize_runs(rows)
        # 정의 없이 모델 등급을 추정하지 않는다. 합계·시도 수는 그대로 계산한다.
        self.assertIsNone(result["high_class_role_rows"])
        self.assertIsNone(result["high_class_provider_attempts"]["total"])
        self.assertIsNone(result["unmapped_model_rows"])
        self.assertEqual(result["provider_attempts"]["total"], 2)
        self.assertEqual(result["model_class_source"], None)

    def test_host_wrapper_keeps_legacy_definition_default(self):
        from evaluation import accounting as host
        from evaluation.metrics.contracts import legacy_definition
        rows = [{"model": "gpt-6-astra", "attempts": 2, "input_tokens": 1, "output_tokens": 1,
                 "cost": 0.1, "duration_ms": 1, "usage_summary": {}}]
        result = host.summarize_runs(rows)
        self.assertEqual(result["high_class_role_rows"], 1)
        self.assertEqual(result["high_class_provider_attempts"]["total"], 2)
        self.assertEqual(result["model_class_source"], "legacy_definition")
        self.assertEqual(legacy_definition()["model_class"]["gpt-6-astra"], "high")


if __name__ == "__main__":
    unittest.main()
