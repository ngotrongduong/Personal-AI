from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from agent.memory_store import (
    NO_PROFILE_SLUG,
    check_slug,
    list_memory_slugs,
    list_sessions,
    memory_dir,
    new_session_path,
    notes_path,
)
from agent.profile import profile_slug


NOW = datetime(2026, 9, 25, 10, 30, 5)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_paths_per_slug(self) -> None:
        slug = profile_slug("Notepad demo")
        self.assertEqual(memory_dir(self.root, slug), self.root / "memory" / "notepad_demo")
        self.assertEqual(notes_path(self.root, slug), self.root / "memory" / "notepad_demo" / "notes.json")
        self.assertEqual(memory_dir(self.root, None), self.root / "memory" / NO_PROFILE_SLUG)

    def test_no_profile_slug_cannot_collide_with_a_profile(self) -> None:
        for name in ("_no_profile", "__no_profile__", " _No_Profile "):
            with self.subTest(name=name):
                self.assertNotEqual(profile_slug(name), NO_PROFILE_SLUG)

    def test_unsafe_slugs_are_rejected(self) -> None:
        for slug in ("", "..", "../x", "a/b", "a\\b", "C:", "_hidden", "UPPER", "a" * 65, "x_", 5):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    check_slug(slug)  # type: ignore[arg-type]
        self.assertEqual(check_slug("notepad_demo-2"), "notepad_demo-2")

    def test_new_session_paths_are_unique_and_never_overwrite(self) -> None:
        first = new_session_path(self.root, "demo", now=NOW)
        self.assertEqual(first.name, "20260925-103005.jsonl")
        first.write_text("keep me\n", encoding="utf-8")
        second = new_session_path(self.root, "demo", now=NOW)
        third = new_session_path(self.root, "demo", now=NOW)
        self.assertEqual([second.name, third.name], ["20260925-103005-2.jsonl", "20260925-103005-3.jsonl"])
        self.assertEqual(first.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "")

    def test_list_sessions_is_oldest_first_and_ignores_other_files(self) -> None:
        self.assertEqual(list_sessions(self.root, "demo"), [])
        paths = [new_session_path(self.root, "demo", now=NOW) for _ in range(11)]
        later = new_session_path(self.root, "demo", now=datetime(2026, 9, 25, 11, 0, 0))
        folder = paths[0].parent
        (folder / "notes.txt").write_text("x", encoding="utf-8")
        (folder / "bad.jsonl").write_text("x", encoding="utf-8")
        self.assertEqual(list_sessions(self.root, "demo"), [*paths, later])

    def test_list_memory_slugs(self) -> None:
        self.assertEqual(list_memory_slugs(self.root), [])
        new_session_path(self.root, "b_game", now=NOW)
        new_session_path(self.root, None, now=NOW)
        memory_dir(self.root, "a_game").mkdir(parents=True)
        (self.root / "memory" / "Not Safe").mkdir()
        (self.root / "memory" / "file.txt").write_text("x", encoding="utf-8")
        self.assertEqual(list_memory_slugs(self.root), [NO_PROFILE_SLUG, "a_game", "b_game"])

    def test_creation_failure_raises_os_error(self) -> None:
        (self.root / "memory").write_text("a file, not a folder", encoding="utf-8")
        with self.assertRaises(OSError):
            new_session_path(self.root, "demo", now=NOW)


if __name__ == "__main__":
    unittest.main()
