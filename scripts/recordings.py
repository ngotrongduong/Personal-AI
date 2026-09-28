"""Review, validate, export and label recorded demonstrations.

Usage (from the repo root, with the venv's python):

    python scripts/recordings.py list
    python scripts/recordings.py validate <session|path> [--all]
    python scripts/recordings.py export <session|path> [--out FILE] [--overwrite]
    python scripts/recordings.py review <session|path> [--start N]
    python scripts/recordings.py label <session|path> <profile> [--radius R]
                                      [--out FILE] [--overwrite]

``<session>`` is a folder name under ``recordings/`` (or ``--root``) or a path
to a session folder. ``<profile>`` is a folder under ``profiles/`` or a path
to a profile folder. Read-only except ``export`` and ``label --out``, which
write new data files; nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import json
import math
import os
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
    load_session,
    validate_session,
)
from recording.labels import label_events, label_to_dict  # noqa: E402
from recording.session_writer import EVENTS_FILE, FRAMES_DIR, SESSION_FILE  # noqa: E402
from agent.profile import ProfileError, load_profile  # noqa: E402

DEFAULT_ROOT = REPO_ROOT / "recordings"
PROFILES_ROOT = REPO_ROOT / "profiles"


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


def resolve_profile(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_dir():
        return candidate
    under_root = PROFILES_ROOT / value
    if under_root.is_dir():
        return under_root
    raise ProfileError(
        f"No profile folder {value!r} (looked in {PROFILES_ROOT})."
    )


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


def _require_valid_for_label(session: object) -> None:
    if not session.report.ok:
        raise DatasetError(
            f"{session.session_dir.name} failed validation "
            f"({len(session.report.errors)} error(s)); run 'validate' for details."
        )


def _check_label_out_path(
    session_dir: Path, profile_dir: Path, out_path: Path
) -> None:
    resolved = out_path.resolve()
    if out_path.is_dir():
        raise DatasetError(f"Output {out_path} is a folder; give a file path.")

    for profiles in {PROFILES_ROOT.resolve(), profile_dir.resolve()}:
        if resolved == profiles or resolved.is_relative_to(profiles):
            raise DatasetError(f"Refusing to write labels inside {profiles}.")

    session = session_dir.resolve()
    protected = {
        (session / EVENTS_FILE).resolve(),
        (session / SESSION_FILE).resolve(),
        (session / (SESSION_FILE + ".tmp")).resolve(),
    }
    if resolved in protected:
        raise DatasetError(f"Refusing to overwrite recording file {out_path}.")
    frames = (session / FRAMES_DIR).resolve()
    if resolved == frames or resolved.is_relative_to(frames):
        raise DatasetError(
            f"Refusing to write into the recording's {FRAMES_DIR}/ folder."
        )


def _write_labels(
    session_dir: Path,
    profile_dir: Path,
    out_path: Path,
    labels: object,
    *,
    overwrite: bool,
) -> None:
    _check_label_out_path(session_dir, profile_dir, out_path)
    if out_path.exists() and not overwrite:
        raise DatasetError(
            f"{out_path} already exists; pass --overwrite to replace it."
        )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DatasetError(f"Could not create output folder: {error}") from error

    temp = out_path.with_name(out_path.name + ".tmp")
    try:
        handle = open(temp, "x", encoding="utf-8", newline="\n")
    except FileExistsError:
        raise DatasetError(f"{temp} already exists; move it away first.") from None
    except OSError as error:
        raise DatasetError(f"Could not create {temp}: {error}") from error
    try:
        with handle:
            for label in labels:
                handle.write(json.dumps(label_to_dict(label), ensure_ascii=False) + "\n")
        os.replace(temp, out_path)
    except BaseException as error:
        try:
            temp.unlink()
        except OSError:
            pass
        if isinstance(error, OSError):
            raise DatasetError(f"Could not write {out_path}: {error}") from error
        raise


def cmd_label(args: argparse.Namespace) -> int:
    session_dir = resolve_session(args.session, args.root)
    session = load_session(session_dir)
    _require_valid_for_label(session)
    if session.info is None:  # Defensive: a valid session always has metadata.
        raise DatasetError(f"{session_dir.name} has no session metadata.")
    profile_dir = resolve_profile(args.profile)
    profile = load_profile(profile_dir)
    result = label_events(
        session.events,
        profile.skills,
        client_width=session.info.client_width,
        client_height=session.info.client_height,
        radius=args.radius,
    )
    unlabeled = result.counts["unlabeled"]
    print(
        f"{session_dir.name}: {result.total_inputs} inputs, "
        f"{result.total_inputs - unlabeled} labeled, {unlabeled} unlabeled"
    )
    for skill, count in result.counts.items():
        if skill != "unlabeled":
            print(f"  {skill}: {count}")
    if args.out is not None:
        _write_labels(
            session_dir,
            profile_dir,
            args.out,
            result.labels,
            overwrite=args.overwrite,
        )
    return 0


def _radius(value: str) -> float:
    try:
        radius = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("radius must be a number") from None
    if not math.isfinite(radius) or not 0.0 < radius <= 0.5:
        raise argparse.ArgumentTypeError(
            "radius must be greater than 0 and at most 0.5"
        )
    return radius


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

    label = commands.add_parser("label", help="label recorded input with profile skills")
    label.add_argument("session")
    label.add_argument("profile")
    label.add_argument(
        "--radius",
        type=_radius,
        default=0.03,
        help="tap-match radius as a fraction of the client diagonal (default 0.03)",
    )
    label.add_argument("--out", type=Path, help="optional JSONL output file")
    label.add_argument("--overwrite", action="store_true", help="replace an existing output")
    label.set_defaults(func=cmd_label)
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
    except (DatasetError, ProfileError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
