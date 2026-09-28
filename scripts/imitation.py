"""Inspect and evaluate retrieval-based imitation over recorded demos.

This tool is read-only except for an explicit ``eval --out`` report. It never
captures a window, sends input, edits a profile, or changes a recording.
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

from agent.profile import ProfileError, load_profile  # noqa: E402
from imitation.demo_bank import DemoBank, select_sessions  # noqa: E402
from imitation.evaluate import EvalReport, evaluate  # noqa: E402
from imitation.policy import PolicyConfig  # noqa: E402
from recording.session_writer import EVENTS_FILE, FRAMES_DIR, SESSION_FILE  # noqa: E402


class ImitationCliError(ValueError):
    pass


def _session_names(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    names = tuple(part.strip() for part in value.split(","))
    for name in names:
        if (
            not name
            or Path(name).name != name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise argparse.ArgumentTypeError(
                "sessions must be comma-separated plain folder names"
            )
    return names


def _build_bank(root: Path, window: str, names: tuple[str, ...]) -> DemoBank:
    sessions = select_sessions(root, window, names)
    return DemoBank.build(sessions)


def cmd_bank(args: argparse.Namespace) -> int:
    session_dirs = select_sessions(args.recordings_root, args.window, args.sessions)
    bank = DemoBank.build(session_dirs)
    print(
        f"{len(bank.clicks)} demo clicks from {len(bank.session_counts)} session(s) "
        f"matching {args.window!r}."
    )
    if not session_dirs:
        print("  no matching sessions")
    for session_dir in session_dirs:
        print(f"  {session_dir.name}: {bank.session_counts.get(session_dir.name, 0)} clicks")
    if bank.skipped:
        print("Skipped:")
        for reason, count in sorted(bank.skipped.items()):
            print(f"  {reason}: {count}")
    else:
        print("Skipped: none")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    window = args.window
    sessions = args.sessions
    config = PolicyConfig()
    if args.profile is not None:
        profile = load_profile(args.profile)
        if profile.imitation is None:
            raise ImitationCliError(
                f"Profile {profile.name!r} has no imitation block."
            )
        imitation = profile.imitation
        window = imitation.window_title
        sessions = imitation.sessions
        config = imitation.policy_config()
    elif not window:
        raise ImitationCliError("eval needs --window or --profile.")

    bank = _build_bank(args.recordings_root, window, sessions)
    report = evaluate(bank, config, mode=args.mode, gap_seconds=args.gap_seconds)
    _print_report(report, len(bank.session_counts))
    if args.out is not None:
        _write_report(
            args.out,
            report,
            args.recordings_root,
            overwrite=args.overwrite,
        )
        print(f"Wrote report to {args.out}.")
    return 0


def _print_report(report: EvalReport, session_count: int) -> None:
    print(
        f"{report.mode}: {report.total} clicks from {session_count} session(s); "
        f"{report.hits} hits, {report.misses} misses, {report.abstains} abstains"
    )
    print(
        f"Precision {report.precision * 100.0:.1f}% | "
        f"Coverage {report.coverage * 100.0:.1f}%"
    )
    if report.abstain_reasons:
        print("Abstain reasons:")
        for reason, count in report.abstain_reasons.items():
            print(f"  {reason}: {count}")
    else:
        print("Abstain reasons: none")


def _write_report(
    out_path: Path,
    report: EvalReport,
    recordings_root: Path,
    *,
    overwrite: bool,
) -> None:
    _check_out_path(out_path, recordings_root)
    if out_path.exists() and not overwrite:
        raise ImitationCliError(
            f"{out_path} already exists; pass --overwrite to replace it."
        )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ImitationCliError(f"Could not create output folder: {error}") from error

    temp = out_path.with_name(out_path.name + ".tmp")
    try:
        handle = open(temp, "x", encoding="utf-8", newline="\n")
    except FileExistsError:
        raise ImitationCliError(f"{temp} already exists; move it away first.") from None
    except OSError as error:
        raise ImitationCliError(f"Could not create {temp}: {error}") from error
    try:
        with handle:
            json.dump(report.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, out_path)
    except BaseException as error:
        try:
            temp.unlink()
        except OSError:
            pass
        if isinstance(error, OSError):
            raise ImitationCliError(f"Could not write {out_path}: {error}") from error
        raise


def _check_out_path(out_path: Path, recordings_root: Path) -> None:
    if out_path.is_dir():
        raise ImitationCliError(f"Output {out_path} is a folder; give a file path.")
    lower_name = out_path.name.casefold()
    if lower_name in {EVENTS_FILE.casefold(), SESSION_FILE.casefold()} or lower_name.endswith(
        ".jpg"
    ):
        raise ImitationCliError(f"Refusing recording-style output name {out_path.name!r}.")

    resolved = out_path.resolve()
    root = recordings_root.resolve()
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or not _looks_like_session(child):
                continue
            session = child.resolve()
            if resolved == session or resolved.is_relative_to(session):
                raise ImitationCliError(
                    f"Refusing to write an eval report inside recording session {child.name!r}."
                )


def _looks_like_session(path: Path) -> bool:
    return (
        (path / SESSION_FILE).exists()
        or (path / EVENTS_FILE).exists()
        or (path / FRAMES_DIR).is_dir()
    )


def _gap_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("gap seconds must be a number") from None
    if not math.isfinite(number) or number < 0.0:
        raise argparse.ArgumentTypeError("gap seconds must be non-negative")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imitation.py",
        description="Build and evaluate a read-only bank of recorded demo clicks.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    bank = commands.add_parser("bank", help="summarise matching demo clicks")
    bank.add_argument("recordings_root", type=Path)
    bank.add_argument("--window", required=True, help="case-insensitive window-title substring")
    bank.add_argument("--sessions", type=_session_names, default=())
    bank.set_defaults(func=cmd_bank)

    eval_command = commands.add_parser("eval", help="run leave-out offline evaluation")
    eval_command.add_argument("recordings_root", type=Path)
    eval_command.add_argument(
        "--window",
        help="case-insensitive window-title substring (required without --profile)",
    )
    eval_command.add_argument("--sessions", type=_session_names, default=())
    eval_command.add_argument("--profile", type=Path, help="profile folder with imitation settings")
    eval_command.add_argument("--mode", choices=("loso", "loco"), default="loso")
    eval_command.add_argument("--gap-seconds", type=_gap_seconds, default=10.0)
    eval_command.add_argument("--out", type=Path, help="optional JSON report output")
    eval_command.add_argument("--overwrite", action="store_true")
    eval_command.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ImitationCliError, ProfileError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
