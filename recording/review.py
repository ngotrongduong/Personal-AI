"""Offline viewer for a recorded session (v0.5).

Shows each frame with the user's recorded input for that frame's interval
drawn on top (clicks, mouse path, scrolls, keys) plus the recorded state
bounding boxes. Read-only: it opens a local OpenCV window and never sends
input or modifies the session.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from recording.dataset import DatasetError, iter_dataset_rows, load_session


WINDOW_NAME = "Recording review"
IDLE_POLL_MS = 50
PAGE_STEP = 10

# cv2.waitKeyEx codes: Windows (Win32 backend) and GTK/Qt on Linux.
_KEYS_NEXT = {2555904, 65363, ord("d"), ord("D"), ord("l")}
_KEYS_PREV = {2424832, 65361, ord("a"), ord("A"), ord("h")}
_KEYS_FIRST = {2359296, 65360, ord("g")}
_KEYS_LAST = {2293760, 65367, ord("G")}
_KEYS_PAGE_DOWN = {2228224, 65366, ord("]")}
_KEYS_PAGE_UP = {2162688, 65365, ord("[")}
_KEYS_PLAY = {ord(" ")}
_KEYS_QUIT = {27, ord("q"), ord("Q")}

HELP_TEXT = "<-/-> step  [ ] x10  Home/End  space play/pause  q quit"

# BGR colours.
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)
_YELLOW = (0, 220, 255)
_GREEN = (80, 200, 80)
_BUTTON_COLOURS = {"left": (40, 40, 230), "right": (230, 120, 40), "middle": (200, 60, 200)}
_OTHER_BUTTON = (200, 200, 200)


@dataclass(slots=True)
class ReviewNavigator:
    """Frame position and play state, driven by cv2 key codes."""

    count: int
    index: int = 0
    playing: bool = False

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("count must be at least 1.")
        self.index = min(max(0, self.index), self.count - 1)

    def handle_key(self, code: int) -> bool:
        """Apply one ``waitKeyEx`` result. Returns False when the user quits."""
        if code == -1:
            if self.playing:
                if self.index + 1 < self.count:
                    self.index += 1
                else:
                    self.playing = False
            return True
        if code in _KEYS_QUIT:
            return False
        if code in _KEYS_PLAY:
            self.playing = not self.playing
            if self.playing and self.index == self.count - 1:
                self.index = 0
            return True
        moves = {
            **{key: self.index + 1 for key in _KEYS_NEXT},
            **{key: self.index - 1 for key in _KEYS_PREV},
            **{key: 0 for key in _KEYS_FIRST},
            **{key: self.count - 1 for key in _KEYS_LAST},
            **{key: self.index + PAGE_STEP for key in _KEYS_PAGE_DOWN},
            **{key: self.index - PAGE_STEP for key in _KEYS_PAGE_UP},
        }
        if code in moves:
            self.playing = False
            self.index = min(max(0, moves[code]), self.count - 1)
        return True


def _ascii(text: str) -> str:
    # cv2.putText only renders ASCII.
    return text.encode("ascii", "replace").decode("ascii")


def _put_text(cv2: Any, image: Any, text: str, origin: tuple[int, int], colour=_WHITE) -> None:
    # A dark box behind the text keeps it readable on any frame. (A thicker
    # outline pass would drift: putText's glyph advance grows with thickness.)
    text = _ascii(text)
    x, y = origin
    (width, height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    cv2.rectangle(image, (x - 2, y - height - 2), (x + width + 2, y + baseline), _BLACK, -1)
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)


def _key_summary(actions: list[dict[str, Any]]) -> str:
    parts = []
    for action in actions:
        if action["type"] == "key":
            parts.append(("+" if action["action"] == "down" else "-") + action["key"])
    return " ".join(parts)


def _point(canvas: Any, x: float, y: float) -> tuple[int, int]:
    # Keep absurd recorded coordinates from overflowing cv2's drawing calls;
    # anything this far off the frame is drawn (clipped) at the edge region.
    height, width = canvas.shape[:2]
    return (
        int(min(max(x, -2 * width), 3 * width)),
        int(min(max(y, -2 * height), 3 * height)),
    )


def render_overlay(frame: Any, row: dict[str, Any], position: int, total: int, cv2: Any) -> Any:
    """Return a copy of ``frame`` with ``row``'s state and input drawn on it."""
    canvas = frame.copy()
    for observation in row["state"] or ():
        bbox = observation.get("bbox")
        if bbox is None or not observation.get("visible"):
            continue
        x, y, w, h = bbox
        top_left = _point(canvas, x, y)
        cv2.rectangle(canvas, top_left, _point(canvas, x + w, y + h), _GREEN, 1)
        _put_text(cv2, canvas, observation["name"], (top_left[0], max(12, top_left[1] - 4)), _GREEN)

    path = []
    for action in row["actions"]:
        kind = action["type"]
        if kind == "mouse_move":
            path.append(_point(canvas, action["x"], action["y"]))
        elif kind == "mouse_button":
            colour = _BUTTON_COLOURS.get(action["button"], _OTHER_BUTTON)
            thickness = -1 if action["action"] == "down" else 2
            cv2.circle(canvas, _point(canvas, action["x"], action["y"]), 8, colour, thickness)
        elif kind == "scroll":
            start = _point(canvas, action["x"], action["y"])
            end = _point(
                canvas, action["x"] + 12 * action["dx"], action["y"] - 12 * action["dy"]
            )
            cv2.arrowedLine(canvas, start, end, _YELLOW, 2)
    for start, end in zip(path, path[1:]):
        cv2.line(canvas, start, end, _YELLOW, 1)
    if path:
        cv2.circle(canvas, path[-1], 3, _YELLOW, -1)

    focused = {True: "yes", False: "NO", None: "?"}[row["focused"]]
    state_count = len(row["state"] or ())
    header = (
        f"frame {position + 1}/{total}  #{row['index']}  t={row['t']:.2f}s  "
        f"focus={focused}  state={state_count}  actions={len(row['actions'])}"
    )
    _put_text(cv2, canvas, header, (8, 18))
    keys = _key_summary(row["actions"])
    if keys:
        _put_text(cv2, canvas, "keys: " + keys, (8, 38), _YELLOW)
    height = canvas.shape[0]
    _put_text(cv2, canvas, HELP_TEXT, (8, height - 10))
    return canvas


def read_frame(path: Path, cv2: Any) -> Any:
    """Load a JPEG (works with non-ASCII paths, unlike ``cv2.imread`` on Windows)."""
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise DatasetError(f"Cannot decode frame {path}.")
    return image


def _play_delay_ms(fps: float) -> int:
    """Frame delay for playback, kept to 1 ms .. 1 s whatever ``fps`` says."""
    if not math.isfinite(fps) or fps <= 0:
        return 1000
    return round(min(max(1000 / fps, 1), 1000))


def review_session(session_dir: Path, *, start: int = 1, cv2: Any = None) -> int:
    """Open the review window for ``session_dir``. Returns the last frame shown (1-based)."""
    session_dir = Path(session_dir)
    session = load_session(session_dir)
    rows = list(iter_dataset_rows(session))  # Raises DatasetError if invalid.
    if not rows:
        raise DatasetError(f"{session_dir.name} has no frames to review.")
    if cv2 is None:
        import cv2 as cv2_module

        cv2 = cv2_module
    fps = session.info.record_fps if session.info is not None else 10.0
    play_delay = _play_delay_ms(fps)
    navigator = ReviewNavigator(len(rows), start - 1)
    title = f"{WINDOW_NAME} - {_ascii(session_dir.name)}"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    shown: int | None = None
    try:
        while True:
            if navigator.index != shown:
                row = rows[navigator.index]
                frame = read_frame(session_dir / row["file"], cv2)
                cv2.imshow(title, render_overlay(frame, row, navigator.index, len(rows), cv2))
                shown = navigator.index
            code = cv2.waitKeyEx(play_delay if navigator.playing else IDLE_POLL_MS)
            if not navigator.handle_key(code):
                break
            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break  # Closed with the window's X button.
    finally:
        cv2.destroyWindow(title)
    return navigator.index + 1
