"""Where v0.8 session memory lives on disk.

Layout, under the app root (gitignored)::

    memory/<profile-slug>/notes.json
    memory/<profile-slug>/sessions/<YYYYmmdd-HHMMSS>.jsonl

The caller passes the profile slug (``agent.profile.profile_slug``); without a
profile the slug is ``_no_profile``, which no profile slug can equal because
profile slugs never start with ``_``. Nothing here deletes or overwrites a
file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


MEMORY_DIRNAME = "memory"
NOTES_FILENAME = "notes.json"
SESSIONS_DIRNAME = "sessions"
NO_PROFILE_SLUG = "_no_profile"
SESSION_SUFFIX = ".jsonl"
MAX_NAME_ATTEMPTS = 1000

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SESSION_NAME = re.compile(r"\d{8}-\d{6}(?:-\d{1,4})?\.jsonl")


def check_slug(slug: str | None) -> str:
    """The folder name for ``slug``; ``None`` means no profile. Raise on an unsafe slug."""

    if slug is None or slug == NO_PROFILE_SLUG:
        return NO_PROFILE_SLUG
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug.endswith(("_", "-")):
        raise ValueError(f"Unsafe memory folder name {slug!r}.")
    return slug


def memory_root(root: str | Path) -> Path:
    return Path(root) / MEMORY_DIRNAME


def memory_dir(root: str | Path, slug: str | None) -> Path:
    return memory_root(root) / check_slug(slug)


def notes_path(root: str | Path, slug: str | None) -> Path:
    return memory_dir(root, slug) / NOTES_FILENAME


def sessions_dir(root: str | Path, slug: str | None) -> Path:
    return memory_dir(root, slug) / SESSIONS_DIRNAME


def new_session_path(root: str | Path, slug: str | None, *, now: datetime | None = None) -> Path:
    """Create and return a new, empty session file; never reuses an existing name.

    Raises ``OSError`` when the folder or the file cannot be created.
    """

    folder = sessions_dir(root, slug)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        name = stamp if attempt == 1 else f"{stamp}-{attempt}"
        path = folder / f"{name}{SESSION_SUFFIX}"
        try:
            # "x" fails if the file exists, so two sessions never share a file.
            with path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"No free session file name for {stamp} in {folder}.")


def list_sessions(root: str | Path, slug: str | None) -> list[Path]:
    """Session files of one profile, oldest first."""

    folder = sessions_dir(root, slug)
    if not folder.is_dir():
        return []
    paths = [path for path in folder.iterdir() if path.is_file() and _SESSION_NAME.fullmatch(path.name)]
    return sorted(paths, key=_session_sort_key)


def list_memory_slugs(root: str | Path) -> list[str]:
    """Profile folders that hold memory, sorted."""

    folder = memory_root(root)
    if not folder.is_dir():
        return []
    slugs: list[str] = []
    for path in folder.iterdir():
        if not path.is_dir():
            continue
        try:
            slugs.append(check_slug(path.name))
        except ValueError:
            continue
    return sorted(slugs)


def _session_sort_key(path: Path) -> tuple[str, int]:
    parts = path.name[: -len(SESSION_SUFFIX)].split("-")
    return "-".join(parts[:2]), int(parts[2]) if len(parts) > 2 else 1
