"""The imitation reader and evaluator must never reach the input path directly."""

from __future__ import annotations

import ast

from tests.test_memory_boundary import ROOT, dynamic_imports, imported_modules


IMITATION_MODULES = tuple(
    path.relative_to(ROOT).as_posix()
    for path in sorted((ROOT / "imitation").glob("*.py"))
) + ("scripts/imitation.py",)
FORBIDDEN = {
    "agent.action_dispatcher",
    "agent.skill_executor",
    "pynput",
    "win32api",
    "win32con",
    "win32gui",
    "ctypes",
}


def test_imitation_modules_do_not_import_the_input_path() -> None:
    for module in IMITATION_MODULES:
        imports = imported_modules(module)
        assert imports & FORBIDDEN == set(), module
        assert {name for name in imports if name == "core" or name.startswith("core.")} == set()
        assert dynamic_imports(module) == []


def test_boundary_checks_all_source_import_nodes() -> None:
    for module in IMITATION_MODULES:
        tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
        assert any(isinstance(node, ast.Import | ast.ImportFrom) for node in ast.walk(tree))
