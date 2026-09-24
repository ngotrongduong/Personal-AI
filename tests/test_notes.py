from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent.notes import (
    LLM_NOTE_INTERVAL_SECONDS,
    MAX_LLM_NOTES,
    MAX_NOTE_CHARS,
    MAX_NOTES,
    Note,
    NoteBook,
    NotesError,
    load_notes,
    save_notes,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def wall() -> datetime:
    return datetime(2026, 9, 25, 10, 0, 0)


class NoteBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.book = NoteBook(clock=self.clock, wall=wall)

    def add_llm(self, text: str):
        result = self.book.add_llm(text)
        self.clock.now += LLM_NOTE_INTERVAL_SECONDS
        return result

    def test_user_add_edit_delete(self) -> None:
        note = self.book.add_user("  The status bar\nshows up  ")
        self.assertEqual(note, Note("The status bar shows up", "user", "2026-09-25T10:00:00"))
        self.book.add_user("Second")
        self.book.edit(1, "Second, edited")
        self.assertEqual([n.text for n in self.book.notes()], ["The status bar shows up", "Second, edited"])
        deleted = self.book.delete(0)
        self.assertEqual(deleted.text, "The status bar shows up")
        self.assertEqual(len(self.book.notes()), 1)
        self.assertEqual(self.book.revision, 4)

    def test_user_limits(self) -> None:
        with self.assertRaises(NotesError):
            self.book.add_user("   ")
        with self.assertRaises(NotesError):
            self.book.add_user("x" * (MAX_NOTE_CHARS + 1))
        with self.assertRaises(NotesError):
            self.book.add_user(42)  # type: ignore[arg-type]
        self.book.add_user("Same")
        with self.assertRaises(NotesError):
            self.book.add_user("SAME")
        for index in range(MAX_NOTES - 1):
            self.book.add_user(f"note {index}")
        with self.assertRaises(NotesError):
            self.book.add_user("one too many")
        with self.assertRaises(NotesError):
            self.book.edit(MAX_NOTES, "x")
        with self.assertRaises(NotesError):
            self.book.delete(-1)
        with self.assertRaises(NotesError):
            self.book.edit(0, "note 0")  # duplicate of another note
        self.book.edit(0, "same")  # same note, new casing is fine

    def test_edit_turns_an_llm_note_into_a_user_note(self) -> None:
        self.add_llm("planner fact")
        edited = self.book.edit(0, "planner fact, checked by me")
        self.assertEqual(edited.source, "user")

    def test_llm_note_added_and_rate_limited(self) -> None:
        result = self.book.add_llm("type_x works only when focused")
        self.assertEqual(result.action, "added")
        self.assertTrue(result.stored)
        self.assertEqual(result.message, "noted: type_x works only when focused")
        self.clock.now += LLM_NOTE_INTERVAL_SECONDS - 1
        result = self.book.add_llm("another")
        self.assertFalse(result.stored)
        self.assertIn("every", result.message)
        self.clock.now += 1
        self.assertTrue(self.book.add_llm("another").stored)
        self.assertEqual([n.source for n in self.book.notes()], ["llm", "llm"])

    def test_llm_invalid_and_duplicate_notes_are_skipped(self) -> None:
        self.assertEqual(self.book.add_llm("").action, "skipped")
        self.assertEqual(self.book.add_llm("x" * 201).action, "skipped")
        self.assertEqual(self.book.add_llm({"type": "run_skill"}).action, "skipped")
        self.book.add_user("Known fact")
        self.assertEqual(self.add_llm("known FACT").action, "skipped")
        self.assertEqual(self.book.revision, 1)

    def test_llm_cap_replaces_its_oldest_note_only(self) -> None:
        self.book.add_user("user first")
        for index in range(MAX_LLM_NOTES):
            self.assertEqual(self.add_llm(f"llm {index}").action, "added")
        self.book.add_user("user last")
        result = self.add_llm("llm new")
        self.assertEqual(result.action, "replaced")
        texts = [n.text for n in self.book.notes()]
        self.assertNotIn("llm 0", texts)
        self.assertIn("user first", texts)
        self.assertIn("user last", texts)
        self.assertEqual(texts[-1], "llm new")
        self.assertEqual(sum(n.source == "llm" for n in self.book.notes()), MAX_LLM_NOTES)

    def test_llm_note_skipped_when_user_notes_fill_the_book(self) -> None:
        for index in range(MAX_NOTES):
            self.book.add_user(f"user {index}")
        result = self.book.add_llm("no room")
        self.assertEqual(result.action, "skipped")
        self.assertEqual(result.reason, "the notes are full")
        self.assertTrue(all(n.source == "user" for n in self.book.notes()))

    def test_prompt_lines(self) -> None:
        self.assertEqual(self.book.prompt_lines(), ["- none"])
        self.book.add_user("u")
        self.add_llm("l")
        self.assertEqual(self.book.prompt_lines(), ["- [user] u", "- [llm] l"])

    def test_constructor_enforces_caps(self) -> None:
        with self.assertRaises(NotesError):
            NoteBook([Note(f"n{i}", "user", "") for i in range(MAX_NOTES + 1)])
        with self.assertRaises(NotesError):
            NoteBook([Note(f"n{i}", "llm", "") for i in range(MAX_LLM_NOTES + 1)])

    def test_concurrent_llm_and_user_changes_stay_bounded(self) -> None:
        book = NoteBook(clock=lambda: 0.0)  # rate limit makes most LLM notes skip

        def user() -> None:
            for index in range(40):
                try:
                    book.add_user(f"user {threading.get_ident()} {index}")
                except NotesError:
                    pass

        def llm() -> None:
            for index in range(40):
                book.add_llm(f"llm {threading.get_ident()} {index}")

        threads = [threading.Thread(target=f) for f in (user, user, llm, llm)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        notes = book.notes()
        self.assertLessEqual(len(notes), MAX_NOTES)
        # The frozen clock allows one planner note at most (none if users filled the book first).
        self.assertLessEqual(sum(n.source == "llm" for n in notes), 1)
        self.assertEqual(len({n.text.casefold() for n in notes}), len(notes))


class NotesFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "memory" / "demo" / "notes.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, data: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_missing_file_is_an_empty_book(self) -> None:
        self.assertEqual(load_notes(self.path).notes(), ())

    def test_save_and_load_round_trip(self) -> None:
        book = NoteBook(clock=FakeClock(), wall=wall)
        book.add_user("user note")
        book.add_llm("llm note")
        save_notes(self.path, book)
        self.assertFalse(self.path.with_name("notes.json.tmp").exists())
        loaded = load_notes(self.path)
        self.assertEqual(loaded.notes(), book.notes())
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["format_version"], 1)

    def test_save_replaces_the_file(self) -> None:
        book = NoteBook()
        book.add_user("one")
        save_notes(self.path, book)
        book.add_user("two")
        save_notes(self.path, book)
        self.assertEqual([n.text for n in load_notes(self.path).notes()], ["one", "two"])

    def test_strict_load_rejects_bad_files(self) -> None:
        good = {"text": "t", "source": "user", "updated": "2026-09-25T10:00:00"}
        cases = {
            "not an object": [1],
            "unknown top field": {"format_version": 1, "notes": [], "extra": 1},
            "bad version": {"format_version": 2, "notes": []},
            "bool version": {"format_version": True, "notes": []},
            "notes not a list": {"format_version": 1, "notes": {}},
            "note not an object": {"format_version": 1, "notes": ["t"]},
            "extra note field": {"format_version": 1, "notes": [{**good, "skill": "hold_space"}]},
            "missing field": {"format_version": 1, "notes": [{"text": "t", "source": "user"}]},
            "empty text": {"format_version": 1, "notes": [{**good, "text": " "}]},
            "long text": {"format_version": 1, "notes": [{**good, "text": "x" * 201}]},
            "control chars": {"format_version": 1, "notes": [{**good, "text": "a\nb"}]},
            "bad source": {"format_version": 1, "notes": [{**good, "source": "system"}]},
            "bad updated": {"format_version": 1, "notes": [{**good, "updated": 5}]},
            "duplicates": {"format_version": 1, "notes": [good, {**good, "text": "T"}]},
            "too many": {"format_version": 1, "notes": [{**good, "text": f"n{i}"} for i in range(21)]},
            "too many llm": {
                "format_version": 1,
                "notes": [{**good, "text": f"n{i}", "source": "llm"} for i in range(11)],
            },
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.write(data)
                with self.assertRaises(NotesError):
                    load_notes(self.path)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(NotesError):
            load_notes(self.path)

    def test_save_failure_raises_notes_error(self) -> None:
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("file", encoding="utf-8")
        with self.assertRaises(NotesError):
            save_notes(blocker / "notes.json", NoteBook())


if __name__ == "__main__":
    unittest.main()
