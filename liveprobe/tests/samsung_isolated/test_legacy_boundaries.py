"""기존 시험에서 명시적으로 골라낸 순수 함수만 실행한다. conftest·API·DB fixture는 적재하지 않는다."""
from __future__ import annotations
import ast
from pathlib import Path
from typing import Any
import unittest

SOURCES = {
    "test_flows.py": {"helpers": {"rec", "pod", "svc", "_inv"}, "constants": {"INV", "FRESH"}, "imports": {"__future__", "app.flows"}},
    "test_p15_overview.py": {"helpers": {"inventory", "sample"}, "constants": set(), "imports": {"datetime"}},
}


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for filename, allowed in SOURCES.items():
        path = Path(__file__).resolve().parents[1] / filename
        original = ast.parse(path.read_text(), filename=str(path))
        body, names = [], []
        for node in original.body:
            if isinstance(node, ast.ImportFrom) and node.module in allowed["imports"]:
                body.append(node)
            elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) and t.id in allowed["constants"] for t in node.targets):
                body.append(node)
            elif isinstance(node, ast.FunctionDef):
                if node.name in allowed["helpers"]:
                    body.append(node)
                elif node.name.startswith("test_") and not node.decorator_list and not node.args.args and not node.args.kwonlyargs:
                    body.append(node); names.append(node.name)
        # 선택한 함수의 본문·assert는 그대로다. DB/API 시험을 skip으로 위장하지 않는다.
        namespace: dict[str, Any] = {"__file__": str(path), "__name__": f"samsung_legacy_{path.stem}"}
        exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
        for name in names:
            suite.addTest(unittest.FunctionTestCase(namespace[name], description=f"{filename}::{name}"))
    return suite
