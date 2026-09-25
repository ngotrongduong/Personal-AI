"""Structured, append-only JSONL log of one planner session (v0.8).

The log is an audit trail for the user and data for offline analysis. Nothing
in the app reads it back to decide anything: it never widens permissions.

The writer fails soft. An I/O error turns it off for the rest of the session
and is kept in ``error``; it never raises into the planner, autopilot,
executor or F8 paths. A record that does not match the schema is a
programming error and raises ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import json
from pathlib import Path
import threading
import time


LOG_VERSION = 1
MAX_TEXT_CHARS = 500
MAX_SESSION_BYTES = 5 * 1024 * 1024

_COMMON_FIELDS = frozenset({"v", "type", "t", "wall"})
_DECISIONS = frozenset({"approved", "auto", "rejected", "expired", "refused"})
_NOTE_ACTIONS = frozenset({"add", "edit", "delete", "skip"})
_NOTE_SOURCES = frozenset({"user", "llm"})
_EFFECTS = frozenset({"confirmed", "not_seen"})


def _text(value: object) -> bool:
    return isinstance(value, str)


def _optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _optional_number(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _optional_bool(value: object) -> bool:
    return value is None or isinstance(value, bool)


def _optional_int(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _bool(value: object) -> bool:
    return isinstance(value, bool)


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _one_of(allowed: frozenset[str]) -> Callable[[object], bool]:
    return lambda value: isinstance(value, str) and value in allowed


# type -> {field: validator}. Every field is required.
EVENT_FIELDS: Mapping[str, Mapping[str, Callable[[object], bool]]] = {
    "session_start": {
        "app_version": _text,
        "profile": _optional_text,
        "model": _text,
        "goal": _text,
        "auto_max_steps": _int,
        "llm_notes": _bool,
    },
    "cycle": {"status": _text, "message": _text, "latency_s": _optional_number},
    "step": {
        "skill": _text,
        "reason": _text,
        "decision": _one_of(_DECISIONS),
        "outcome": _text,
        "ok": _optional_bool,
    },
    # v1.0: the observed effect of the previous step, written when it resolves.
    "effect": {
        "skill": _text,
        "effect": _one_of(_EFFECTS),
        "detector": _text,
        "waited_s": _number,
    },
    "auto": {"on": _bool, "reason": _text, "max_steps": _optional_int},
    "note": {"action": _one_of(_NOTE_ACTIONS), "source": _one_of(_NOTE_SOURCES), "text": _text},
    "truncated": {"limit_bytes": _int},
    "session_end": {"reason": _text},
}
EVENT_TYPES = frozenset(EVENT_FIELDS)


def clean_text(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Printable characters only, stripped and cut to ``limit``."""

    return "".join(ch if ch.isprintable() else " " for ch in value).strip()[:limit]


def record_problems(record: object) -> list[str]:
    """Schema problems of one decoded record; empty when it is valid."""

    if not isinstance(record, dict):
        return ["record is not a JSON object"]
    event_type = record.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_FIELDS:
        return [f"unknown record type {event_type!r}"]
    problems: list[str] = []
    if record.get("v") != LOG_VERSION or isinstance(record.get("v"), bool):
        problems.append(f"unsupported version {record.get('v')!r}")
    t = record.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0:
        problems.append("'t' must be a non-negative number")
    if not isinstance(record.get("wall"), str):
        problems.append("'wall' must be a string")
    fields = EVENT_FIELDS[event_type]
    for name, valid in fields.items():
        if name not in record:
            problems.append(f"{event_type}: missing field {name!r}")
        elif not valid(record[name]):
            problems.append(f"{event_type}: invalid value for {name!r}: {record[name]!r}")
    unknown = set(record) - _COMMON_FIELDS - set(fields)
    if unknown:
        problems.append(f"{event_type}: unknown field(s) {sorted(unknown)!r}")
    return problems


class SessionLogWriter:
    """Append records of one session to ``path``; thread-safe and fail-soft."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = datetime.now,
        max_bytes: int = MAX_SESSION_BYTES,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._wall = wall
        self._max_bytes = max_bytes
        self._started_at = clock()
        self._lock = threading.Lock()
        self._bytes = 0
        self._truncated = False
        self._closed = False
        self.error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def truncated(self) -> bool:
        return self._truncated

    def write(self, event_type: str, **fields: object) -> bool:
        """Append one record. False when it was not written (off, capped or closed)."""

        record = self._build(event_type, fields)
        with self._lock:
            if self._closed:
                return False
            if event_type == "session_end":
                self._closed = True
            return self._append(record)

    def close(self, reason: str) -> bool:
        """Write ``session_end`` once; later calls do nothing."""

        return self.write("session_end", reason=reason)

    def _build(self, event_type: str, fields: dict[str, object]) -> dict[str, object]:
        cleaned = {
            name: clean_text(value) if isinstance(value, str) else value
            for name, value in fields.items()
        }
        record: dict[str, object] = {
            "v": LOG_VERSION,
            "type": event_type,
            "t": round(max(0.0, self._clock() - self._started_at), 3),
            "wall": self._wall().isoformat(timespec="seconds"),
            **cleaned,
        }
        problems = record_problems(record)
        if problems:
            raise ValueError("; ".join(problems))
        return record

    def _append(self, record: dict[str, object]) -> bool:
        if self.failed or self._truncated:
            return False
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        if self._bytes + len(line) > self._max_bytes:
            self._truncated = True
            marker = self._build("truncated", {"limit_bytes": self._max_bytes})
            self._raw_append((json.dumps(marker) + "\n").encode("utf-8"))
            return False
        return self._raw_append(line)

    def _raw_append(self, data: bytes) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(data)
        except OSError as error:
            self.error = f"{type(error).__name__}: {error}"
            return False
        self._bytes += len(data)
        return True


def read_session(path: str | Path) -> list[dict[str, object]]:
    """Decode and validate a session file strictly; raise ``ValueError`` on any problem."""

    records, problems = _read(Path(path))
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return records


def validate_session(path: str | Path) -> list[str]:
    """Every problem found in a session file; empty when it is valid."""

    return _read(Path(path))[1]


def inspect_session(path: str | Path) -> tuple[list[dict[str, object]], list[str]]:
    """The records that decode plus every problem found; never raises."""

    return _read(Path(path))


def _read(path: Path) -> tuple[list[dict[str, object]], list[str]]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        return [], [f"cannot read: {error}"]

    records: list[dict[str, object]] = []
    problems: list[str] = []
    for number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            problems.append(f"line {number}: blank line")
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            problems.append(f"line {number}: invalid JSON ({error.msg})")
            continue
        except RecursionError:
            problems.append(f"line {number}: invalid JSON (nested too deeply)")
            continue
        problems.extend(f"line {number}: {problem}" for problem in record_problems(record))
        if isinstance(record, dict):
            records.append(record)

    types = [record.get("type") for record in records]
    if not types:
        problems.append("empty session")
    elif types[0] != "session_start":
        problems.append("the first record must be session_start")
    if types.count("session_start") > 1:
        problems.append("more than one session_start")
    if types.count("session_end") > 1:
        problems.append("more than one session_end")
    if "session_end" in types and types[-1] != "session_end":
        problems.append("records after session_end")
    if types.count("truncated") > 1:
        problems.append("more than one truncated record")
    return records, problems
