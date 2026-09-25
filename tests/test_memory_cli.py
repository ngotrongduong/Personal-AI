from __future__ import annotations

import ast
import contextlib
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent.memory_store import new_session_path, notes_path
from agent.notes import NoteBook, save_notes
from agent.session_log import SessionLogWriter
from tests.test_recording_input_recorder import FORBIDDEN_MODULE_PARTS, FORBIDDEN_NAMES

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memory.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("memory_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def wall() -> datetime:
    return datetime(2026, 9, 25, 10, 0, 0)


class MemoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cli = load_cli()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def make_notes(self, slug: str = "notepad") -> Path:
        book = NoteBook(clock=lambda: 1000.0, wall=wall)
        book.add_user("The status bar shows the line")
        book.add_llm("type_x works only when focused")
        path = notes_path(self.root, slug)
        save_notes(path, book)
        return path

    def make_session(self, slug: str = "notepad", *, end: bool = True) -> Path:
        path = new_session_path(self.root, slug, now=wall())
        log = SessionLogWriter(path, clock=lambda: 0.0, wall=wall)
        log.write("session_start", app_version="0.8.0", profile="Notepad", model="qwen3.5:9b",
                  goal="type hello", auto_max_steps=3, llm_notes=True)
        log.write("cycle", status="proposal", message="type_x", latency_s=1.25)
        log.write("step", skill="type_x", reason="goal says type", decision="approved",
                  outcome="done", ok=True)
        log.write("auto", on=True, reason="confirmed by the user", max_steps=3)
        log.write("note", action="add", source="llm", text="type_x works")
        if end:
            log.close("emergency stop")
        return path

    def test_list(self) -> None:
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("No memory", out)
        self.make_notes()
        self.make_session()
        self.make_session()
        self.make_session("other")
        bad = notes_path(self.root, "broken")
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json", encoding="utf-8")
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        lines = {line.split()[0]: line for line in out.splitlines()[1:]}
        self.assertEqual(sorted(lines), ["broken", "notepad", "other"])
        self.assertIn("2/20 notes (1/10 from the planner)", lines["notepad"])
        self.assertIn("20260925-100000-2.jsonl", lines["notepad"])
        self.assertIn("no notes", lines["other"])
        self.assertIn("INVALID", lines["broken"])

    def test_show_notes(self) -> None:
        path = self.make_notes()
        code, out, _ = self.run_cli("show", str(path))
        self.assertEqual(code, 0)
        self.assertIn("2/20 notes", out)
        self.assertIn("1. [user] The status bar shows the line", out)
        self.assertIn("2. [llm] type_x works only when focused", out)

    def test_show_session(self) -> None:
        code, out, _ = self.run_cli("show", str(self.make_session()))
        self.assertEqual(code, 0)
        self.assertIn("6 records", out)
        self.assertIn("start: model qwen3.5:9b, goal 'type hello'", out)
        self.assertIn("cycle: proposal (1.25s) - type_x", out)
        self.assertIn("step: type_x approved -> ok, done (reason: goal says type)", out)
        self.assertIn("auto on, max 3: confirmed by the user", out)
        self.assertIn("note add [llm]: type_x works", out)
        self.assertIn("end: emergency stop", out)
        self.assertNotIn("no session_end", out)

        code, out, _ = self.run_cli("show", str(self.make_session(end=False)))
        self.assertEqual(code, 0)
        self.assertIn("no session_end", out)

    def test_show_session_with_effects(self) -> None:
        path = new_session_path(self.root, "notepad", now=wall())
        log = SessionLogWriter(path, clock=lambda: 0.0, wall=wall)
        log.write("session_start", app_version="1.0.0", profile="Notepad", model="qwen3.5:9b",
                  goal="type x", auto_max_steps=3, llm_notes=False)
        log.write("step", skill="type_x", reason="goal", decision="approved", outcome="done", ok=True)
        log.write("effect", skill="type_x", effect="confirmed", detector="x_glyph", waited_s=0.4)
        log.write("step", skill="type_x", reason="goal", decision="auto", outcome="done", ok=True)
        log.write("effect", skill="type_x", effect="not_seen", detector="x_glyph", waited_s=2)
        log.close("goal reached")
        code, out, _ = self.run_cli("show", str(path))
        self.assertEqual(code, 0)
        self.assertIn("effects: 1 confirmed / 1 not seen", out)
        self.assertIn("effect: type_x confirmed (x_glyph, 0.4s)", out)
        self.assertIn("effect: type_x not seen (x_glyph, 2s)", out)
        self.assertIn("end: goal reached", out)

    def test_session_without_effects_has_no_effect_line(self) -> None:
        code, out, _ = self.run_cli("show", str(self.make_session()))
        self.assertEqual(code, 0)
        self.assertNotIn("effects:", out)

    def test_show_broken_session_prints_problems(self) -> None:
        path = self.make_session()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        code, out, _ = self.run_cli("show", str(path))
        self.assertEqual(code, 1)
        self.assertIn("problem: line 7: invalid JSON", out)
        self.assertIn("end: emergency stop", out)

    def test_validate_one_file_and_all(self) -> None:
        notes = self.make_notes()
        session = self.make_session()
        self.make_session(end=False)  # a running session is still valid
        self.assertEqual(self.run_cli("validate", str(notes))[0], 0)
        code, out, _ = self.run_cli("validate", str(session))
        self.assertEqual(code, 0)
        self.assertIn("OK", out)
        code, out, _ = self.run_cli("validate", "--all")
        self.assertEqual(code, 0)
        self.assertIn("3/3 file(s) OK.", out)

        record = {"v": 1, "type": "step", "t": 0, "wall": "x", "skill": "s", "reason": "r",
                  "decision": "approved", "outcome": "o", "ok": True, "keys": ["space"]}
        with session.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        notes.write_text(json.dumps({"format_version": 1, "notes": [1]}), encoding="utf-8")
        code, out, _ = self.run_cli("validate", "--all")
        self.assertEqual(code, 1)
        self.assertIn("1/3 file(s) OK.", out)
        self.assertIn("unknown field(s) ['keys']", out)
        self.assertIn("records after session_end", out)
        self.assertIn("note 0 must be a JSON object", out)

    def test_validate_with_nothing_to_check(self) -> None:
        code, out, _ = self.run_cli("validate", "--all")
        self.assertEqual(code, 0)
        self.assertIn("No memory", out)

    def test_errors_exit_2(self) -> None:
        code, _, err = self.run_cli("show", str(self.root / "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("No file", err)
        other = self.root / "other.txt"
        other.write_text("x", encoding="utf-8")
        code, _, err = self.run_cli("validate", str(other))
        self.assertEqual(code, 2)
        self.assertIn("neither", err)
        self.assertEqual(self.run_cli("validate")[0], 2)
        broken = notes_path(self.root, "demo")
        broken.parent.mkdir(parents=True)
        broken.write_text("[]", encoding="utf-8")
        code, _, err = self.run_cli("show", str(broken))
        self.assertEqual(code, 2)
        self.assertIn("JSON object", err)

    def test_cli_is_read_only(self) -> None:
        notes = self.make_notes()
        session = self.make_session()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for argv in (("list",), ("show", str(notes)), ("show", str(session)), ("validate", "--all")):
            self.run_cli(*argv)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_cli_never_imports_input_senders(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            names: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            for module in modules:
                self.assertFalse(any(part in module for part in FORBIDDEN_MODULE_PARTS), module)
            for name in names:
                self.assertNotIn(name, FORBIDDEN_NAMES)


if __name__ == "__main__":
    unittest.main()
