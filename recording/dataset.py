"""Read, validate and export recorded demonstration sessions (v0.5).

Offline tooling for ``recordings/<session>/`` folders written by
``SessionWriter``. Nothing here records, sends input, or deletes data: the
only file this module ever writes is the exported dataset, and it refuses to
write over the recording's own files.

Timing model: ``frame``/``state``/``marker`` events come from the sampler
thread and key/mouse/scroll/focus events from the input listeners. ``t`` is
non-decreasing within each of those two sources, but the two streams may
interleave out of order by a few milliseconds in ``events.jsonl``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from recording.schema import (
    Event,
    FocusEvent,
    FrameEvent,
    KeyEvent,
    MarkerEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    SchemaError,
    ScrollEvent,
    SessionInfo,
    StateEvent,
    event_from_dict,
    event_to_dict,
    session_from_dict,
)
from recording.session_writer import EVENTS_FILE, FRAMES_DIR, SESSION_FILE


DATASET_FILE = "dataset.jsonl"

SOURCE_SAMPLER = "sampler"
SOURCE_INPUT = "input"
_SAMPLER_EVENTS = (FrameEvent, StateEvent, MarkerEvent)
_INPUT_EVENTS = (KeyEvent, MouseButtonEvent, MouseMoveEvent, ScrollEvent, FocusEvent)

STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_INVALID = "invalid"

DEFAULT_MAX_ISSUES_PER_KIND = 10

# What decoding/parsing hostile or corrupt JSON can raise. ValueError covers
# UnicodeDecodeError, JSONDecodeError, SchemaError and huge-integer errors.
_BAD_DATA_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RecursionError)


class DatasetError(Exception):
    """Raised when a session cannot be exported or reviewed."""


def event_source(event: Event) -> str:
    """Return which thread produced ``event``: ``"sampler"`` or ``"input"``."""
    if isinstance(event, _SAMPLER_EVENTS):
        return SOURCE_SAMPLER
    if isinstance(event, _INPUT_EVENTS):
        return SOURCE_INPUT
    raise TypeError(f"Unsupported event type: {type(event).__name__}.")


def expected_frame_file(index: int) -> str:
    return f"{FRAMES_DIR}/{index:06d}.jpg"


def read_session_info(session_dir: Path) -> SessionInfo:
    """Parse ``session.json``; raises ``DatasetError`` with a readable message."""
    path = Path(session_dir) / SESSION_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"{SESSION_FILE} is missing.") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasetError(f"Cannot read {SESSION_FILE}: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError, plus huge integers and absurd nesting.
        raise DatasetError(f"{SESSION_FILE} is not valid JSON: {exc}") from exc
    try:
        return session_from_dict(data)
    except _BAD_DATA_ERRORS as exc:
        raise DatasetError(f"{SESSION_FILE} does not match the schema: {exc}") from exc


# --------------------------------------------------------------------------- list


@dataclass(frozen=True, slots=True)
class SessionSummary:
    name: str
    path: Path
    info: SessionInfo | None
    error: str | None = None

    @property
    def status(self) -> str:
        if self.info is None:
            return STATUS_INVALID
        return STATUS_COMPLETE if self.info.ended_at is not None else STATUS_INCOMPLETE

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock duration from ``session.json`` (whole seconds), if finished."""
        if self.info is None or self.info.ended_at is None:
            return None
        try:
            started = datetime.fromisoformat(self.info.started_at)
            ended = datetime.fromisoformat(self.info.ended_at)
            return max(0.0, (ended - started).total_seconds())
        except (TypeError, ValueError):
            # e.g. one timestamp is timezone-aware and the other is not.
            return None


def list_sessions(root_dir: Path) -> list[SessionSummary]:
    """Summarise every session folder under ``root_dir`` (oldest first)."""
    root = Path(root_dir)
    if not root.is_dir():
        return []
    summaries: list[SessionSummary] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        try:
            info = read_session_info(child)
        except DatasetError as exc:
            summaries.append(SessionSummary(child.name, child, None, str(exc)))
            continue
        summaries.append(SessionSummary(child.name, child, info))
    return summaries


# ------------------------------------------------------------------- load/validate


@dataclass(frozen=True, slots=True)
class EventLine:
    line: int
    event: Event


class _IssueLog:
    """Collects messages, keeping at most ``limit`` per kind."""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._messages: list[str] = []
        self._counts: dict[str, int] = {}

    def add(self, kind: str, message: str) -> None:
        count = self._counts.get(kind, 0) + 1
        self._counts[kind] = count
        if count <= self._limit:
            self._messages.append(message)

    def result(self) -> list[str]:
        messages = list(self._messages)
        for kind, count in self._counts.items():
            if count > self._limit:
                messages.append(f"... and {count - self._limit} more {kind} problem(s).")
        return messages


@dataclass(slots=True)
class ValidationReport:
    session_dir: Path
    info: SessionInfo | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: int = 0
    counts_by_type: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def frames(self) -> int:
        return self.counts_by_type.get("frame", 0)


@dataclass(slots=True)
class RecordingSession:
    """A session read from disk; ``report`` lists everything wrong with it."""

    session_dir: Path
    info: SessionInfo | None
    events: list[EventLine]
    report: ValidationReport


def _iter_event_lines(path: Path) -> Iterator[tuple[int, bytes, bool]]:
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, start=1):
            yield number, raw, raw.endswith(b"\n")


def load_session(
    session_dir: Path, *, max_issues_per_kind: int = DEFAULT_MAX_ISSUES_PER_KIND
) -> RecordingSession:
    """Read and check a session folder. Never raises for bad data."""
    session_dir = Path(session_dir)
    report = ValidationReport(session_dir=session_dir)
    session = RecordingSession(session_dir, None, [], report)
    errors = _IssueLog(max_issues_per_kind)
    warnings = _IssueLog(max_issues_per_kind)
    try:
        _load_into(session, errors, warnings)
    finally:
        report.errors = errors.result()
        report.warnings = warnings.result()
    return session


def _load_into(session: RecordingSession, errors: _IssueLog, warnings: _IssueLog) -> None:
    session_dir = session.session_dir
    report = session.report
    if not session_dir.is_dir():
        errors.add("folder", f"{session_dir} is not a folder.")
        return

    try:
        session.info = report.info = read_session_info(session_dir)
    except DatasetError as exc:
        errors.add("session.json", str(exc))
    info = session.info
    finished = info is not None and info.ended_at is not None

    events_path = session_dir / EVENTS_FILE
    if not events_path.is_file():
        errors.add(EVENTS_FILE, f"{EVENTS_FILE} is missing.")
        return

    base = session_dir.resolve()
    last_t: dict[str, tuple[float, int]] = {}
    expected_index = 1
    referenced: set[str] = set()
    markers: list[MarkerEvent] = []
    try:
        for number, raw, has_newline in _iter_event_lines(events_path):
            report.lines += 1
            try:
                event = event_from_dict(json.loads(raw.decode("utf-8")))
            except _BAD_DATA_ERRORS as exc:
                if not has_newline and not finished:
                    warnings.add(
                        "truncated",
                        f"line {number}: last line is incomplete (the session did not "
                        f"finish cleanly); ignored.",
                    )
                else:
                    errors.add("event", f"line {number}: {exc}")
                continue
            entry = EventLine(number, event)
            session.events.append(entry)
            type_name = event_to_dict(event)["type"]
            report.counts_by_type[type_name] = report.counts_by_type.get(type_name, 0) + 1

            source = event_source(event)
            previous = last_t.get(source)
            if previous is not None and event.t < previous[0]:
                errors.add(
                    "time",
                    f"line {number}: t={event.t:.6f} goes backwards from t={previous[0]:.6f} "
                    f"(line {previous[1]}) within {source} events.",
                )
            else:
                last_t[source] = (event.t, number)

            if isinstance(event, FrameEvent):
                if event.index != expected_index:
                    errors.add(
                        "frame",
                        f"line {number}: frame index {event.index}, expected {expected_index}.",
                    )
                expected_index = event.index + 1
                if event.file != expected_frame_file(event.index):
                    # Never follow an unexpected path out of the session folder.
                    errors.add(
                        "frame",
                        f"line {number}: frame file {event.file!r} should be "
                        f"{expected_frame_file(event.index)!r}.",
                    )
                elif not (session_dir / event.file).is_file():
                    errors.add("frame", f"line {number}: frame file {event.file} is missing.")
                elif not (session_dir / event.file).resolve().is_relative_to(base):
                    # A symlink/junction pointing out of the session folder.
                    errors.add(
                        "frame",
                        f"line {number}: frame file {event.file} links outside the session.",
                    )
                else:
                    referenced.add(event.file)
            elif isinstance(event, MarkerEvent):
                markers.append(event)
    except OSError as exc:
        errors.add(EVENTS_FILE, f"Cannot read {EVENTS_FILE}: {exc}")
        return

    _check_markers(markers, info, finished, warnings)
    _check_frame_folder(session_dir, referenced, report, errors, warnings)
    if info is not None:
        if finished:
            if report.frames != info.frames:
                errors.add(
                    "count",
                    f"{report.frames} frame events, but {SESSION_FILE} says {info.frames}.",
                )
            if report.lines != info.events:
                errors.add(
                    "count",
                    f"{report.lines} lines in {EVENTS_FILE}, but {SESSION_FILE} says "
                    f"{info.events} events.",
                )
        else:
            warnings.add(
                "unfinished",
                f"ended_at is null: the session did not finish cleanly (app crash or still "
                f"recording), so counts in {SESSION_FILE} were not checked.",
            )


def _check_markers(
    markers: list[MarkerEvent], info: SessionInfo | None, finished: bool, warnings: _IssueLog
) -> None:
    starts = [m for m in markers if m.action == "start"]
    stops = [m for m in markers if m.action == "stop"]
    if not starts:
        warnings.add("marker", "No start marker.")
    elif len(starts) > 1:
        warnings.add("marker", f"{len(starts)} start markers; expected 1.")
    if finished and not stops:
        warnings.add("marker", "No stop marker.")
    elif len(stops) > 1:
        warnings.add("marker", f"{len(stops)} stop markers; expected 1.")
    if stops and info is not None and info.stop_reason is not None:
        reason = stops[-1].reason
        if reason != info.stop_reason:
            warnings.add(
                "marker",
                f"stop marker reason {reason!r} differs from {SESSION_FILE} "
                f"stop_reason {info.stop_reason!r}.",
            )


def _check_frame_folder(
    session_dir: Path,
    referenced: set[str],
    report: ValidationReport,
    errors: _IssueLog,
    warnings: _IssueLog,
) -> None:
    frames_dir = session_dir / FRAMES_DIR
    if not frames_dir.is_dir():
        errors.add("frame", f"{FRAMES_DIR}/ folder is missing.")
        return
    on_disk = {f"{FRAMES_DIR}/{p.name}" for p in frames_dir.glob("*.jpg")}
    unreferenced = len(on_disk - referenced)
    if unreferenced:
        warnings.add(
            "frame",
            f"{unreferenced} frame file(s) in {FRAMES_DIR}/ are not referenced by "
            f"{EVENTS_FILE} (or failed the checks above).",
        )


def validate_session(
    session_dir: Path, *, max_issues_per_kind: int = DEFAULT_MAX_ISSUES_PER_KIND
) -> ValidationReport:
    return load_session(session_dir, max_issues_per_kind=max_issues_per_kind).report


# -------------------------------------------------------------------------- export


def _require_valid(session: RecordingSession) -> None:
    if not session.report.ok:
        raise DatasetError(
            f"{session.session_dir.name} failed validation "
            f"({len(session.report.errors)} error(s)); run 'validate' for details."
        )


def _image_path(session_dir: Path, file: str, image_base: Path | None) -> str:
    target = session_dir / file
    if image_base is None:
        return file
    try:
        return Path(os.path.relpath(target, image_base)).as_posix()
    except ValueError:
        # Different drives on Windows: fall back to an absolute path.
        return target.resolve().as_posix()


def iter_dataset_rows(
    session: RecordingSession, *, image_base: Path | None = None
) -> Iterator[dict[str, Any]]:
    """Yield one row per frame: the frame, the state at that time, and the
    user's input in ``[t, next_t)``.

    ``state`` is the latest ``state`` event with ``t`` <= the frame's ``t``
    (the sampler records them together). ``focused`` is the last known focus
    state at the frame's ``t`` (``None`` before the first focus event).
    ``actions`` holds input-source events (key, mouse_button, mouse_move,
    scroll, focus); the last frame's interval is open-ended. Input before the
    first frame belongs to no row. ``image`` is the frame path relative to
    ``image_base`` (the session folder when ``image_base`` is None).
    """
    _require_valid(session)
    frames = [e.event for e in session.events if isinstance(e.event, FrameEvent)]
    states = [e.event for e in session.events if isinstance(e.event, StateEvent)]
    inputs = [e.event for e in session.events if isinstance(e.event, _INPUT_EVENTS)]
    focus = [e for e in inputs if isinstance(e, FocusEvent)]
    state_ts = [s.t for s in states]
    focus_ts = [f.t for f in focus]

    cursor = 0
    if frames:
        while cursor < len(inputs) and inputs[cursor].t < frames[0].t:
            cursor += 1

    for position, frame in enumerate(frames):
        next_t = frames[position + 1].t if position + 1 < len(frames) else None
        state_at = bisect_right(state_ts, frame.t) - 1
        state = states[state_at] if state_at >= 0 else None
        focus_at = bisect_right(focus_ts, frame.t) - 1
        focused = focus[focus_at].action == "gained" if focus_at >= 0 else None

        actions: list[dict[str, Any]] = []
        while cursor < len(inputs) and (next_t is None or inputs[cursor].t < next_t):
            actions.append(event_to_dict(inputs[cursor]))
            cursor += 1

        state_dict = event_to_dict(state) if state is not None else None
        yield {
            "index": frame.index,
            "t": frame.t,
            "next_t": next_t,
            "file": frame.file,
            "image": _image_path(session.session_dir, frame.file, image_base),
            "focused": focused,
            "state_t": None if state_dict is None else state_dict["t"],
            "state": None if state_dict is None else state_dict["observations"],
            "actions": actions,
        }


def unassigned_input_count(session: RecordingSession) -> int:
    """Input events recorded before the first frame (they belong to no row)."""
    frames = [e.event for e in session.events if isinstance(e.event, FrameEvent)]
    inputs = [e.event for e in session.events if isinstance(e.event, _INPUT_EVENTS)]
    if not frames:
        return len(inputs)
    return sum(1 for e in inputs if e.t < frames[0].t)


@dataclass(frozen=True, slots=True)
class ExportResult:
    out_path: Path
    rows: int
    actions: int
    unassigned_actions: int


# Recording files are never written, in this session or any other.
_PROTECTED_FILES = frozenset({EVENTS_FILE, SESSION_FILE, SESSION_FILE + ".tmp"})


def _check_out_path(session_dir: Path, out_path: Path) -> None:
    resolved = out_path.resolve()
    base = session_dir.resolve()
    if resolved == base or resolved.is_dir():
        raise DatasetError(f"Output {out_path} is a folder; give a file path.")
    if resolved.name in _PROTECTED_FILES or resolved.suffix.lower() == ".jpg":
        raise DatasetError(f"Refusing to write a dataset named {resolved.name}.")
    if resolved.is_relative_to(base / FRAMES_DIR):
        raise DatasetError(f"Refusing to write into the recording's {FRAMES_DIR}/ folder.")


def export_dataset(
    session_dir: Path,
    out_path: Path | None = None,
    *,
    overwrite: bool = False,
) -> ExportResult:
    """Validate a session and write ``dataset.jsonl`` (one row per frame).

    The default output is ``<session>/dataset.jsonl``. An existing output is
    only replaced with ``overwrite=True``; the recording's own files are never
    written. The file is written to a temporary name and renamed at the end.
    """
    session_dir = Path(session_dir)
    session = load_session(session_dir)
    _require_valid(session)
    out = Path(out_path) if out_path is not None else session_dir / DATASET_FILE
    _check_out_path(session_dir, out)
    if out.exists() and not overwrite:
        raise DatasetError(f"{out} already exists; pass overwrite to replace it.")

    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    rows = 0
    actions = 0
    try:
        # Exclusive create: never truncate (or later remove) a file we did not
        # make, e.g. a user's own <name>.tmp or a hardlink to events.jsonl.
        handle = open(temp, "x", encoding="utf-8", newline="\n")
    except FileExistsError:
        raise DatasetError(f"{temp} already exists; move it away first.") from None
    try:
        with handle:
            for row in iter_dataset_rows(session, image_base=out.parent):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows += 1
                actions += len(row["actions"])
        os.replace(temp, out)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return ExportResult(out, rows, actions, unassigned_input_count(session))
