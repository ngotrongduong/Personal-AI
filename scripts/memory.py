"""List, show and validate planner memory: notes and session logs (v0.8, effects v1.0).

Usage (from the repo root, with the venv's python):

    python scripts/memory.py list
    python scripts/memory.py show <file>
    python scripts/memory.py validate <file>
    python scripts/memory.py validate --all

``<file>`` is a ``notes.json`` or a session ``.jsonl`` path. ``--root`` is the
folder that holds ``memory/`` (default: the repo root). Read-only: nothing is
ever written or deleted.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_store import (  # noqa: E402
    NOTES_FILENAME,
    SESSION_SUFFIX,
    list_memory_slugs,
    list_sessions,
    memory_root,
    notes_path,
)
from agent.notes import MAX_LLM_NOTES, MAX_NOTES, NotesError, load_notes  # noqa: E402
from agent.session_log import inspect_session, validate_session  # noqa: E402

DEFAULT_ROOT = REPO_ROOT


class MemoryCliError(Exception):
    pass


def _kind(path: Path) -> str:
    if path.name == NOTES_FILENAME:
        return "notes"
    if path.suffix == SESSION_SUFFIX:
        return "session"
    raise MemoryCliError(f"{path} is neither {NOTES_FILENAME} nor a {SESSION_SUFFIX} session log.")


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise MemoryCliError(f"No file {value!r}.")
    return path


def _notes_summary(path: Path) -> str:
    if not path.exists():
        return "no notes"
    try:
        notes = load_notes(path).notes()
    except NotesError:
        return "notes.json INVALID"
    planner = sum(note.source == "llm" for note in notes)
    return f"{len(notes)}/{MAX_NOTES} notes ({planner}/{MAX_LLM_NOTES} from the planner)"


def cmd_list(args: argparse.Namespace) -> int:
    slugs = list_memory_slugs(args.root)
    if not slugs:
        print(f"No memory in {memory_root(args.root)}.")
        return 0
    print(f"{'profile':<24} {'notes':<40} {'sessions':>8}  latest")
    for slug in slugs:
        sessions = list_sessions(args.root, slug)
        latest = sessions[-1].name if sessions else "-"
        print(f"{slug:<24} {_notes_summary(notes_path(args.root, slug)):<40} {len(sessions):>8}  {latest}")
    return 0


def _show_notes(path: Path) -> int:
    try:
        notes = load_notes(path).notes()
    except NotesError as error:
        raise MemoryCliError(str(error)) from error
    planner = sum(note.source == "llm" for note in notes)
    print(f"{path}: {len(notes)}/{MAX_NOTES} notes ({planner}/{MAX_LLM_NOTES} from the planner)")
    for number, note in enumerate(notes, start=1):
        print(f"{number:>3}. [{note.source}] {note.text}  ({note.updated})")
    return 0


def _record_line(record: dict[str, object]) -> str:
    kind = record.get("type")
    if kind == "session_start":
        return (f"start: model {record.get('model')}, goal {record.get('goal')!r}, "
                f"auto max {record.get('auto_max_steps')}, planner notes "
                f"{'on' if record.get('llm_notes') else 'off'}")
    if kind == "cycle":
        latency = record.get("latency_s")
        took = f" ({latency:g}s)" if isinstance(latency, (int, float)) else ""
        return f"cycle: {record.get('status')}{took} - {record.get('message')}"
    if kind == "step":
        result = {True: "ok", False: "failed", None: "-"}.get(record.get("ok"), "-")
        return (f"step: {record.get('skill')} {record.get('decision')} -> {result}, "
                f"{record.get('outcome')} (reason: {record.get('reason')})")
    if kind == "effect":
        waited = record.get("waited_s")
        took = f", {waited:g}s" if isinstance(waited, (int, float)) else ""
        effect = str(record.get("effect")).replace("_", " ")
        return f"effect: {record.get('skill')} {effect} ({record.get('detector')}{took})"
    if kind == "auto":
        state = "on" if record.get("on") else "off"
        limit = record.get("max_steps")
        extra = f", max {limit}" if limit is not None else ""
        return f"auto {state}{extra}: {record.get('reason')}"
    if kind == "note":
        return f"note {record.get('action')} [{record.get('source')}]: {record.get('text')}"
    if kind == "truncated":
        return f"truncated at {record.get('limit_bytes')} bytes"
    if kind == "session_end":
        return f"end: {record.get('reason')}"
    return f"{kind}: {record}"


def _show_session(path: Path) -> int:
    records, problems = inspect_session(path)
    counts = Counter(str(record.get("type")) for record in records)
    summary = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
    print(f"{path}: {len(records)} records ({summary or 'none'})")
    start = next((r for r in records if r.get("type") == "session_start"), None)
    if start is not None:
        print(f"  app {start.get('app_version')}, profile {start.get('profile') or '(none)'}")
    effects = Counter(str(r.get("effect")) for r in records if r.get("type") == "effect")
    if effects:
        print(f"  effects: {effects['confirmed']} confirmed / {effects['not_seen']} not seen")
    if records and records[-1].get("type") != "session_end":
        print("  (no session_end: still running, or the app stopped abruptly)")
    for record in records:
        print(f"  {record.get('wall', '?')}  {_record_line(record)}")
    for problem in problems:
        print(f"  problem: {problem}")
    return 1 if problems else 0


def cmd_show(args: argparse.Namespace) -> int:
    path = _existing_file(args.file)
    return _show_notes(path) if _kind(path) == "notes" else _show_session(path)


def _validate(path: Path) -> list[str]:
    if _kind(path) == "notes":
        try:
            load_notes(path)
        except NotesError as error:
            return [str(error)]
        return []
    return validate_session(path)


def cmd_validate(args: argparse.Namespace) -> int:
    if args.all:
        targets: list[Path] = []
        for slug in list_memory_slugs(args.root):
            notes = notes_path(args.root, slug)
            if notes.exists():
                targets.append(notes)
            targets.extend(list_sessions(args.root, slug))
        if not targets:
            print(f"No memory in {memory_root(args.root)}.")
            return 0
    elif args.file:
        targets = [_existing_file(args.file)]
    else:
        raise MemoryCliError("Give a file or --all.")
    failed = 0
    for target in targets:
        problems = _validate(target)
        print(f"{target}: {'FAILED' if problems else 'OK'}")
        for problem in problems:
            print(f"  error: {problem}")
        failed += 1 if problems else 0
    if len(targets) > 1:
        print(f"{len(targets) - failed}/{len(targets)} file(s) OK.")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory.py", description="List, show and validate planner notes and session logs."
    )
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT,
        help=f"folder that holds memory/ (default {DEFAULT_ROOT})",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="profiles with note counts and sessions").set_defaults(func=cmd_list)

    show = commands.add_parser("show", help="print a notes.json or a session log readably")
    show.add_argument("file")
    show.set_defaults(func=cmd_show)

    validate = commands.add_parser("validate", help="check a notes.json or a session log strictly")
    validate.add_argument("file", nargs="?")
    validate.add_argument("--all", action="store_true", help="validate every file under memory/")
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Notes and goals may not fit the console code page (e.g. cp1252).
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (MemoryCliError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
