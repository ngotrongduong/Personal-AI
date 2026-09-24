"""v0.8 memory invariant: notes and session logs stay out of the input path.

The memory modules may import only plain standard-library modules and each
other, so they can never produce input or change permissions. The permission
and input modules may not reach memory, not even through another module.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

MEMORY_MODULES = ("agent/notes.py", "agent/session_log.py")
MEMORY_NAMES = frozenset({"agent.notes", "agent.session_log", "agent.memory_store"})
ALLOWED_FOR_MEMORY = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "json",
        "os",
        "pathlib",
        "re",
        "threading",
        "time",
        "typing",
    }
)

INPUT_PATH_MODULES = (
    "agent/skills.py",
    "agent/rule_engine.py",
    "agent/autopilot.py",
    "agent/skill_executor.py",
    "agent/action_dispatcher.py",
    "agent/profile.py",
    "agent/planner_config.py",
    "agent/proposal_mailbox.py",
    "agent/llm_planner.py",
    "agent/llm_planner_schema.py",
    "core/input_controller.py",
)
INTERNAL_PACKAGES = ("agent", "core", "vision", "recording")


def module_name(relative_path: str) -> str:
    return ".".join(Path(relative_path).with_suffix("").parts)


def imported_modules(relative_path: str) -> set[str]:
    """Absolute names of every module a source file imports (relative ones resolved)."""

    package_parts = module_name(relative_path).split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(package_parts):
                    raise ValueError(f"{relative_path}: relative import above the top package")
                base = ".".join(package_parts[: len(package_parts) - node.level + 1])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names.add(module)
            # `from . import skills` imports a submodule too.
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def dynamic_imports(relative_path: str) -> list[str]:
    """Calls such as ``__import__(...)`` that the static check cannot follow."""

    found: list[str] = []
    for node in ast.walk(ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"__import__", "import_module", "exec", "eval"}:
                found.append(name)
    return found


def source_path(module: str) -> str | None:
    for candidate in (Path(*module.split(".")).with_suffix(".py"), Path(*module.split("."), "__init__.py")):
        if (ROOT / candidate).exists():
            return candidate.as_posix()
    return None


def reachable_modules(relative_path: str) -> set[str]:
    """Every internal module a file imports, directly or through other internal modules."""

    seen: set[str] = set()
    pending = [relative_path]
    while pending:
        current = pending.pop()
        for name in imported_modules(current):
            if name in seen or name.split(".")[0] not in INTERNAL_PACKAGES:
                continue
            seen.add(name)
            path = source_path(name)
            if path is not None:
                pending.append(path)
    return seen


class MemoryBoundaryTests(unittest.TestCase):
    def memory_modules(self) -> list[str]:
        modules = list(MEMORY_MODULES)
        if (ROOT / "agent/memory_store.py").exists():
            modules.append("agent/memory_store.py")
        return modules

    def test_memory_modules_import_only_plain_stdlib_and_each_other(self) -> None:
        for module in self.memory_modules():
            with self.subTest(module=module):
                self.assertTrue((ROOT / module).exists())
                extra = imported_modules(module) - ALLOWED_FOR_MEMORY
                # `from pathlib import Path` also yields "pathlib.Path"; keep only real modules.
                extra = {
                    name
                    for name in extra
                    if name not in MEMORY_NAMES and name.rsplit(".", 1)[0] not in ALLOWED_FOR_MEMORY
                }
                self.assertEqual(extra, set())
                self.assertEqual(dynamic_imports(module), [])

    def test_input_path_modules_never_reach_memory(self) -> None:
        for module in INPUT_PATH_MODULES:
            with self.subTest(module=module):
                self.assertEqual(reachable_modules(module) & MEMORY_NAMES, set())

    def test_the_checker_resolves_relative_and_transitive_imports(self) -> None:
        names = imported_modules("agent/action_dispatcher.py")
        self.assertIn("agent.skills", names)
        self.assertIn("core.input_controller", names)
        self.assertIn("pydirectinput", names)
        self.assertIn("agent.llm_planner", reachable_modules("agent/profile.py"))
        self.assertIn("agent.notes", reachable_modules("agent/planner_controller.py"))


if __name__ == "__main__":
    unittest.main()
