"""Build a read-only bank of the user's recorded left clicks."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np

from imitation.features import patch_at, read_frame, screen_feature
from recording.dataset import list_sessions, load_session
from recording.schema import FrameEvent, MouseButtonEvent


@dataclass(frozen=True, slots=True)
class DemoClick:
    session: str
    t: float
    frame_index: int
    frame_path: Path
    fx: float
    fy: float
    screen: np.ndarray
    patch: np.ndarray


@dataclass(frozen=True, slots=True)
class ExtractResult:
    clicks: tuple[DemoClick, ...]
    skipped: dict[str, int]


def extract_demo_clicks(
    session_dir: str | Path,
    *,
    lead_seconds: float = 0.05,
    max_frame_age: float = 0.5,
    drag_fraction: float = 0.02,
    patch_fraction: float = 0.08,
) -> ExtractResult:
    """Extract click features from one validated recording session."""

    _validate_extract_options(lead_seconds, max_frame_age, drag_fraction, patch_fraction)
    folder = Path(session_dir)
    session = load_session(folder)
    if not session.report.ok or session.info is None:
        return ExtractResult((), {"invalid_session": 1})

    ordered = [line.event for line in sorted(session.events, key=lambda line: line.event.t)]
    frames = [event for event in ordered if isinstance(event, FrameEvent)]
    mouse = [event for event in ordered if isinstance(event, MouseButtonEvent)]
    width = session.info.client_width
    height = session.info.client_height
    diagonal = math.hypot(width, height)
    skipped: Counter[str] = Counter()
    clicks: list[DemoClick] = []
    used_releases: set[int] = set()

    for mouse_index, down in enumerate(mouse):
        if down.action != "down":
            continue
        if down.button != "left":
            skipped["non_left"] += 1
            continue

        release_index, release = _next_left_release(mouse, mouse_index + 1, used_releases)
        if release is None:
            skipped["no_release"] += 1
            continue
        used_releases.add(release_index)
        if math.hypot(release.x - down.x, release.y - down.y) > drag_fraction * diagonal:
            skipped["drag"] += 1
            continue

        cutoff = down.t - lead_seconds
        frame = _last_frame_at_or_before(frames, cutoff)
        if frame is None or down.t - frame.t > max_frame_age:
            skipped["no_frame"] += 1
            continue
        frame_path = folder / frame.file
        image = read_frame(frame_path)
        if image is None:
            skipped["unreadable_frame"] += 1
            continue

        fx = min(1.0, max(0.0, down.x / width))
        fy = min(1.0, max(0.0, down.y / height))
        clicks.append(
            DemoClick(
                session=folder.name,
                t=down.t,
                frame_index=frame.index,
                frame_path=frame_path,
                fx=fx,
                fy=fy,
                screen=screen_feature(image),
                patch=patch_at(image, fx, fy, fraction=patch_fraction),
            )
        )

    return ExtractResult(tuple(clicks), dict(skipped))


def _validate_extract_options(
    lead_seconds: object,
    max_frame_age: object,
    drag_fraction: object,
    patch_fraction: object,
) -> None:
    for value, name, allow_zero in (
        (lead_seconds, "lead_seconds", True),
        (max_frame_age, "max_frame_age", False),
        (drag_fraction, "drag_fraction", True),
        (patch_fraction, "patch_fraction", False),
    ):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{name} must be a finite number")
        number = float(value)
        if not math.isfinite(number) or number < 0.0 or (not allow_zero and number == 0.0):
            qualifier = "non-negative" if allow_zero else "positive"
            raise ValueError(f"{name} must be a {qualifier} finite number")


def _next_left_release(
    events: list[MouseButtonEvent],
    start: int,
    used: set[int],
) -> tuple[int, MouseButtonEvent | None]:
    for index in range(start, len(events)):
        event = events[index]
        if index not in used and event.button == "left" and event.action == "up":
            return index, event
    return -1, None


def _last_frame_at_or_before(frames: list[FrameEvent], cutoff: float) -> FrameEvent | None:
    selected: FrameEvent | None = None
    for frame in frames:
        if frame.t > cutoff:
            break
        selected = frame
    return selected


def select_sessions(
    root: str | Path,
    window_title: str,
    names: tuple[str, ...] | list[str] = (),
) -> list[Path]:
    """Select valid complete or incomplete sessions by title and optional name."""

    wanted = set(names)
    needle = window_title.casefold()
    selected = []
    for summary in list_sessions(Path(root)):
        if summary.info is None:
            continue
        if wanted and summary.name not in wanted:
            continue
        if needle in summary.info.window_title.casefold():
            selected.append(summary.path)
    return sorted(selected, key=lambda path: path.name)


@dataclass(frozen=True, slots=True)
class DemoBank:
    clicks: tuple[DemoClick, ...]
    session_counts: dict[str, int]
    skipped: dict[str, int]
    screens: np.ndarray

    @property
    def counts_by_session(self) -> dict[str, int]:
        """Compatibility-friendly name for the per-session click counts."""

        return dict(self.session_counts)

    @property
    def per_session(self) -> dict[str, int]:
        return dict(self.session_counts)

    @property
    def skipped_totals(self) -> dict[str, int]:
        return dict(self.skipped)

    @classmethod
    def build(cls, session_dirs: object, **extract_kwargs: float) -> DemoBank:
        clicks: list[DemoClick] = []
        skipped: Counter[str] = Counter()
        directories = sorted(session_dirs, key=lambda path: Path(path).name)  # type: ignore[arg-type]
        for session_dir in directories:
            result = extract_demo_clicks(session_dir, **extract_kwargs)
            clicks.extend(result.clicks)
            skipped.update(result.skipped)
        return cls._from(clicks, dict(skipped))

    @classmethod
    def from_clicks(cls, clicks: object) -> DemoBank:
        return cls._from(tuple(clicks), {})  # type: ignore[arg-type]

    @classmethod
    def _from(cls, clicks: object, skipped: dict[str, int]) -> DemoBank:
        ordered = tuple(clicks)  # type: ignore[arg-type]
        counts = Counter(click.session for click in ordered)
        if ordered:
            dimensions = {click.screen.size for click in ordered}
            if len(dimensions) != 1:
                raise ValueError("all demo screen features must have the same size")
            screens = np.stack(
                [np.asarray(click.screen, dtype=np.float32).reshape(-1) for click in ordered]
            )
        else:
            screens = np.empty((0, 48 * 48), dtype=np.float32)
        return cls(ordered, dict(counts), dict(skipped), screens)
