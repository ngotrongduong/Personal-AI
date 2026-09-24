"""Bounded, user-editable notes carried across planner sessions (v0.8).

Notes are plain hints for the planner prompt. Nothing but the prompt, the
Memory panel and ``scripts/memory.py`` reads them, so they can never widen
permissions: they cannot enable a skill, add a key, change a rule or arm auto
mode.

The user can add, edit and delete any note. The LLM can only add notes, within
its own cap and rate. At that cap its oldest note is replaced, and it can never
touch a user note.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal, TypeAlias


FORMAT_VERSION = 1
MAX_NOTES = 20
MAX_LLM_NOTES = 10
MAX_NOTE_CHARS = 200
LLM_NOTE_INTERVAL_SECONDS = 30.0

NoteSource: TypeAlias = Literal["user", "llm"]
NOTE_SOURCES: frozenset[str] = frozenset({"user", "llm"})
_TOP_FIELDS = {"format_version", "notes"}
_NOTE_FIELDS = {"text", "source", "updated"}


class NotesError(ValueError):
    """A note or a notes file is invalid, or a user edit breaks a limit."""


@dataclass(frozen=True, slots=True)
class Note:
    text: str
    source: NoteSource
    updated: str


@dataclass(frozen=True, slots=True)
class NoteResult:
    """What ``add_llm`` did: ``added``, ``replaced`` or ``skipped``."""

    action: Literal["added", "replaced", "skipped"]
    text: str
    reason: str = ""

    @property
    def stored(self) -> bool:
        return self.action != "skipped"

    @property
    def message(self) -> str:
        if self.action == "skipped":
            return f"note skipped: {self.reason}"
        if self.action == "replaced":
            return f"noted (replaced the oldest planner note): {self.text}"
        return f"noted: {self.text}"


def clean_note(text: object) -> str:
    """A note's text: printable characters only, stripped. Raise if empty or too long."""

    if not isinstance(text, str):
        raise NotesError("A note must be a string.")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    if not cleaned:
        raise NotesError("A note cannot be empty.")
    if len(cleaned) > MAX_NOTE_CHARS:
        raise NotesError(f"A note must be at most {MAX_NOTE_CHARS} characters.")
    return cleaned


class NoteBook:
    """Thread-safe, bounded list of notes, oldest first."""

    def __init__(
        self,
        notes: tuple[Note, ...] | list[Note] = (),
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = datetime.now,
    ) -> None:
        notes = list(notes)
        if len(notes) > MAX_NOTES:
            raise NotesError(f"At most {MAX_NOTES} notes are allowed.")
        if sum(note.source == "llm" for note in notes) > MAX_LLM_NOTES:
            raise NotesError(f"At most {MAX_LLM_NOTES} planner notes are allowed.")
        self._notes = notes
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._revision = 0
        self._last_llm_at: float | None = None

    @property
    def revision(self) -> int:
        """Bumped on every change, so the Tk thread knows when to save."""

        with self._lock:
            return self._revision

    def notes(self) -> tuple[Note, ...]:
        with self._lock:
            return tuple(self._notes)

    def prompt_lines(self) -> list[str]:
        notes = self.notes()
        if not notes:
            return ["- none"]
        return [f"- [{note.source}] {note.text}" for note in notes]

    # ------------------------------------------------------------------ user

    def add_user(self, text: str) -> Note:
        cleaned = clean_note(text)
        with self._lock:
            if len(self._notes) >= MAX_NOTES:
                raise NotesError(f"The notes are full ({MAX_NOTES}); delete one first.")
            if self._duplicate_index(cleaned) is not None:
                raise NotesError("That note already exists.")
            note = Note(cleaned, "user", self._stamp())
            self._notes.append(note)
            self._revision += 1
            return note

    def edit(self, index: int, text: str, *, expected: Note | None = None) -> Note:
        """Replace a note's text. The note becomes a user note.

        With ``expected``, refuse unless the note at ``index`` is still that
        note: a planner note may have shifted the list since it was shown.
        """

        cleaned = clean_note(text)
        with self._lock:
            self._check_index(index, expected)
            duplicate = self._duplicate_index(cleaned)
            if duplicate is not None and duplicate != index:
                raise NotesError("That note already exists.")
            note = Note(cleaned, "user", self._stamp())
            self._notes[index] = note
            self._revision += 1
            return note

    def delete(self, index: int, *, expected: Note | None = None) -> Note:
        with self._lock:
            self._check_index(index, expected)
            note = self._notes.pop(index)
            self._revision += 1
            return note

    def inherit_rate_limit(self, other: NoteBook) -> None:
        """Keep ``other``'s last planner-note time, e.g. after re-reading the file."""

        with other._lock:
            last = other._last_llm_at
        with self._lock:
            self._last_llm_at = last

    # ------------------------------------------------------------------- llm

    def add_llm(self, text: object) -> NoteResult:
        """Add a planner note within the LLM's cap and rate; never touches user notes."""

        try:
            cleaned = clean_note(text)
        except NotesError as error:
            return NoteResult("skipped", "", str(error))
        with self._lock:
            now = self._clock()
            if (
                self._last_llm_at is not None
                and now - self._last_llm_at < LLM_NOTE_INTERVAL_SECONDS
            ):
                return NoteResult(
                    "skipped",
                    cleaned,
                    f"at most one planner note every {LLM_NOTE_INTERVAL_SECONDS:g} s",
                )
            if self._duplicate_index(cleaned) is not None:
                return NoteResult("skipped", cleaned, "that note already exists")

            note = Note(cleaned, "llm", self._stamp())
            llm_indexes = [i for i, existing in enumerate(self._notes) if existing.source == "llm"]
            if len(llm_indexes) >= MAX_LLM_NOTES:
                del self._notes[llm_indexes[0]]
                action = "replaced"
            elif len(self._notes) >= MAX_NOTES:
                return NoteResult("skipped", cleaned, "the notes are full")
            else:
                action = "added"
            self._notes.append(note)
            self._last_llm_at = now
            self._revision += 1
            return NoteResult(action, cleaned)

    # --------------------------------------------------------------- helpers

    def _check_index(self, index: int, expected: Note | None = None) -> None:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(self._notes):
            raise NotesError(f"No note at position {index!r}.")
        if expected is not None and self._notes[index] != expected:
            raise NotesError("The notes changed meanwhile; select the note again.")

    def _duplicate_index(self, cleaned: str) -> int | None:
        key = cleaned.casefold()
        for index, note in enumerate(self._notes):
            if note.text.casefold() == key:
                return index
        return None

    def _stamp(self) -> str:
        return self._wall().isoformat(timespec="seconds")


def load_notes(
    path: str | Path,
    *,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = datetime.now,
) -> NoteBook:
    """Read ``notes.json`` strictly. A missing file gives an empty book."""

    path = Path(path)
    if not path.exists():
        return NoteBook(clock=clock, wall=wall)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise NotesError(f"Could not read {path}: {error}") from error

    if not isinstance(data, dict):
        raise NotesError(f"{path}: the top level must be a JSON object.")
    unknown = set(data) - _TOP_FIELDS
    if unknown:
        raise NotesError(f"{path}: unknown field(s) {sorted(unknown)!r}.")
    version = data.get("format_version")
    if version != FORMAT_VERSION or isinstance(version, bool):
        raise NotesError(f"{path}: unsupported format_version {version!r}.")
    items = data.get("notes")
    if not isinstance(items, list):
        raise NotesError(f"{path}: 'notes' must be a list.")

    notes: list[Note] = []
    for position, item in enumerate(items):
        label = f"{path}: note {position}"
        if not isinstance(item, dict):
            raise NotesError(f"{label} must be a JSON object.")
        if set(item) != _NOTE_FIELDS:
            raise NotesError(f"{label} must have exactly the fields {sorted(_NOTE_FIELDS)!r}.")
        try:
            text = clean_note(item["text"])
        except NotesError as error:
            raise NotesError(f"{label}: {error}") from error
        if text != item["text"]:
            raise NotesError(f"{label}: text has control characters or surrounding spaces.")
        if not isinstance(item["source"], str) or item["source"] not in NOTE_SOURCES:
            raise NotesError(f"{label}: source must be 'user' or 'llm'.")
        if not isinstance(item["updated"], str) or len(item["updated"]) > 64:
            raise NotesError(f"{label}: updated must be a short string.")
        notes.append(Note(text, item["source"], item["updated"]))

    keys = [note.text.casefold() for note in notes]
    if len(set(keys)) != len(keys):
        raise NotesError(f"{path}: duplicate notes.")
    try:
        return NoteBook(notes, clock=clock, wall=wall)
    except NotesError as error:
        raise NotesError(f"{path}: {error}") from error


def save_notes(path: str | Path, book: NoteBook) -> None:
    """Write ``notes.json`` atomically (a temp file, then ``os.replace``)."""

    path = Path(path)
    data = {
        "format_version": FORMAT_VERSION,
        "notes": [
            {"text": note.text, "source": note.source, "updated": note.updated}
            for note in book.notes()
        ],
    }
    temp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError as error:
        raise NotesError(f"Could not save {path}: {error}") from error
