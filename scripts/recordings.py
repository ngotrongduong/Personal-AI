"""Review, validate and export recorded demonstrations (v0.5).

Usage (from the repo root, with the venv's python):

    python scripts/recordings.py list
    python scripts/recordings.py validate <session|path> [--all]
    python scripts/recordings.py export <session|path> [--out FILE] [--overwrite]
    python scripts/recordings.py review <session|path> [--start N]

``<session>`` is a folder name under ``recordings/`` (or ``--root``) or a path
to a session folder. Read-only except ``export``, which writes a new
``dataset.jsonl``; nothing is ever deleted.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recording.dataset import (  # noqa: E402
    DatasetError,
    ValidationReport,
    export_dataset,
    list_sessions,
    validate_session,
)

DEFAULT_ROOT = REPO_ROOT / "recordings"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def resolve_session(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_dir():
        return candidate
    under_root = root / value
    if under_root.is_dir():
        return under_root
    raise DatasetError(f"No session folder {value!r} (looked in {root}).")


def cmd_list(args: argparse.Namespace) -> int:
    sessions = list_sessions(args.root)
    if not sessions:
        print(f"No recordings in {args.root}.")
        return 0
    print(f"{'session':<20} {'status':<10} {'length':>8} {'fps':>5} {'frames':>7} "
          f"{'dropped':>7} {'events':>8}  stop / window")
    for summary in sessions:
        info = summary.info
        if info is None:
            print(f"{summary.name:<20} {summary.status:<10} {summary.error}")
            continue
        detail = f"{info.stop_reason or '-'} / {info.window_title or '(untitled)'}"
        print(
            f"{summary.name:<20} {summary.status:<10} "
            f"{_format_duration(summary.duration_seconds):>8} {info.record_fps:>5g} "
            f"{info.frames:>7} {info.dropped_frames:>7} {info.events:>8}  {detail}"
        )
    return 0


def _print_report(report: ValidationReport) -> None:
    verdict = "OK" if report.ok else "FAILED"
    counts = ", ".join(f"{name} {count}" for name, count in sorted(report.counts_by_type.items()))
    print(f"{report.session_dir.name}: {verdict} - {report.lines} lines ({counts or 'no events'})")
    for message in report.errors:
        print(f"  error: {message}")
    for message in report.warnings:
        print(f"  warning: {message}")


def cmd_validate(args: argparse.Namespace) -> int:
    if args.all:
        targets = [summary.path for summary in list_sessions(args.root)]
        if not targets:
            print(f"No recordings in {args.root}.")
            return 0
    elif args.session:
        targets = [resolve_session(args.session, args.root)]
    else:
        raise DatasetError("Give a session or --all.")
    failed = 0
    for target in targets:
        report = validate_session(target)
        _print_report(report)
        failed += 0 if report.ok else 1
    return 1 if failed else 0


def cmd_export(args: argparse.Namespace) -> int:
    session_dir = resolve_session(args.session, args.root)
    result = export_dataset(session_dir, args.out, overwrite=args.overwrite)
    print(f"Wrote {result.rows} rows ({result.actions} actions) to {result.out_path}.")
    if result.unassigned_actions:
        print(f"  note: {result.unassigned_actions} input event(s) before the first frame "
              f"are not in any row.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from recording.review import review_session

    session_dir = resolve_session(args.session, args.root)
    last = review_session(session_dir, start=args.start)
    print(f"Closed review at frame {last}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recordings.py", description="Review, validate and export recorded demonstrations."
    )
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help=f"recordings folder (default {DEFAULT_ROOT})"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="list sessions with a summary").set_defaults(func=cmd_list)

    validate = commands.add_parser("validate", help="check a session's files and timing")
    validate.add_argument("session", nargs="?")
    validate.add_argument("--all", action="store_true", help="validate every session")
    validate.set_defaults(func=cmd_validate)

    export = commands.add_parser("export", help="write dataset.jsonl (one row per frame)")
    export.add_argument("session")
    export.add_argument("--out", type=Path, help="output file (default <session>/dataset.jsonl)")
    export.add_argument("--overwrite", action="store_true", help="replace an existing output")
    export.set_defaults(func=cmd_export)

    review = commands.add_parser("review", help="step through frames with input overlaid")
    review.add_argument("session")
    review.add_argument("--start", type=int, default=1, help="first frame to show (1-based)")
    review.set_defaults(func=cmd_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Window titles may not fit the console code page (e.g. cp1252).
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
