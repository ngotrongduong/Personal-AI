from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from recording.dataset import DatasetError, iter_dataset_rows, load_session
from recording.review import (
    ReviewNavigator,
    _play_delay_ms,
    read_frame,
    render_overlay,
    review_session,
)
from tests.test_recording_dataset import edit_session_json, make_session
from tests.test_recording_input_recorder import FORBIDDEN_MODULE_PARTS, FORBIDDEN_NAMES


RIGHT = 2555904
LEFT = 2424832
HOME = 2359296
END = 2293760
PAGE_DOWN = 2228224
PAGE_UP = 2162688
SPACE = ord(" ")
QUIT = ord("q")
TIMEOUT = -1

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recordings.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("recordings_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReviewNavigatorTests(unittest.TestCase):
    def test_step_and_clamp(self) -> None:
        nav = ReviewNavigator(count=3)
        for code, expected in ((LEFT, 0), (RIGHT, 1), (RIGHT, 2), (RIGHT, 2), (ord("a"), 1)):
            self.assertTrue(nav.handle_key(code))
            self.assertEqual(nav.index, expected)

    def test_jumps(self) -> None:
        nav = ReviewNavigator(count=25, index=5)
        nav.handle_key(PAGE_DOWN)
        self.assertEqual(nav.index, 15)
        nav.handle_key(PAGE_DOWN)
        self.assertEqual(nav.index, 24)
        nav.handle_key(PAGE_UP)
        self.assertEqual(nav.index, 14)
        nav.handle_key(HOME)
        self.assertEqual(nav.index, 0)
        nav.handle_key(END)
        self.assertEqual(nav.index, 24)

    def test_play_advances_on_timeout_and_stops_at_the_end(self) -> None:
        nav = ReviewNavigator(count=3)
        nav.handle_key(TIMEOUT)
        self.assertEqual(nav.index, 0)  # Paused: a timeout does nothing.
        nav.handle_key(SPACE)
        self.assertTrue(nav.playing)
        nav.handle_key(TIMEOUT)
        nav.handle_key(TIMEOUT)
        self.assertEqual(nav.index, 2)
        nav.handle_key(TIMEOUT)
        self.assertEqual((nav.index, nav.playing), (2, False))
        nav.handle_key(SPACE)  # Play again from the start.
        self.assertEqual((nav.index, nav.playing), (0, True))
        nav.handle_key(RIGHT)  # Stepping pauses.
        self.assertEqual((nav.index, nav.playing), (1, False))

    def test_quit_and_bounds(self) -> None:
        self.assertFalse(ReviewNavigator(count=1).handle_key(QUIT))
        self.assertFalse(ReviewNavigator(count=1).handle_key(27))
        self.assertEqual(ReviewNavigator(count=4, index=99).index, 3)
        with self.assertRaises(ValueError):
            ReviewNavigator(count=0)


class FakeCv2:
    """Real cv2 drawing/decoding, fake window calls."""

    def __init__(self, keys: list[int], *, visible_after: int | None = None) -> None:
        self._keys = list(keys)
        self._visible_after = visible_after
        self.shown: list[tuple[str, tuple[int, ...]]] = []
        self.waits: list[int] = []
        self.destroyed: list[str] = []
        self.windows: list[str] = []

    def __getattr__(self, name: str):
        return getattr(cv2, name)

    def namedWindow(self, title: str, flags: int) -> None:
        self.windows.append(title)

    def imshow(self, title: str, image: np.ndarray) -> None:
        self.shown.append((title, image.shape))

    def waitKeyEx(self, delay: int) -> int:
        self.waits.append(delay)
        return self._keys.pop(0) if self._keys else QUIT

    def getWindowProperty(self, title: str, prop: int) -> float:
        if self._visible_after is not None and len(self.waits) > self._visible_after:
            return 0.0
        return 1.0

    def destroyWindow(self, title: str) -> None:
        self.destroyed.append(title)


class ReviewSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session_dir = make_session(self.root)

    def test_render_overlay_draws_on_a_copy(self) -> None:
        rows = list(iter_dataset_rows(load_session(self.session_dir)))
        row = dict(rows[0])
        row["state"] = [{"name": "enemy", "visible": True, "bbox": [100, 100, 20, 20]}]
        row["actions"] = [
            {"type": "mouse_button", "t": 0.1, "button": "left", "action": "down", "x": 150, "y": 120},
            {"type": "key", "t": 0.1, "key": "w", "action": "down"},
        ]
        frame = np.zeros((240, 320, 3), dtype=np.uint8)

        canvas = render_overlay(frame, row, 0, len(rows), cv2)

        self.assertEqual(canvas.shape, frame.shape)
        self.assertFalse(frame.any())
        self.assertEqual(tuple(canvas[120, 150]), (40, 40, 230))  # Left click, filled.
        self.assertEqual(tuple(canvas[110, 100]), (80, 200, 80))  # State bbox edge.
        self.assertFalse(canvas[200, 250].any())  # Untouched background.

    def test_render_overlay_survives_absurd_coordinates(self) -> None:
        rows = list(iter_dataset_rows(load_session(self.session_dir)))
        row = dict(rows[0])
        big = 10**12
        row["state"] = [{"name": "far", "visible": True, "bbox": [big, -big, big, big]}]
        row["actions"] = [
            {"type": "mouse_move", "t": 0.1, "x": big, "y": big},
            {"type": "mouse_move", "t": 0.1, "x": -big, "y": 5},
            {"type": "mouse_button", "t": 0.1, "button": "x1", "action": "up", "x": -big, "y": big},
            {"type": "scroll", "t": 0.1, "dx": big, "dy": -big, "x": 10, "y": 10},
        ]

        canvas = render_overlay(np.zeros((48, 64, 3), dtype=np.uint8), row, 0, 1, cv2)

        self.assertEqual(canvas.shape, (48, 64, 3))

    def test_play_delay_is_bounded(self) -> None:
        self.assertEqual(_play_delay_ms(10.0), 100)
        self.assertEqual(_play_delay_ms(5000.0), 1)
        for fps in (1e-310, 0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(fps=fps):
                self.assertEqual(_play_delay_ms(fps), 1000)

    def test_steps_through_frames_and_closes_the_window(self) -> None:
        fake = FakeCv2([RIGHT, TIMEOUT, RIGHT, RIGHT, QUIT])

        last = review_session(self.session_dir, cv2=fake)

        self.assertEqual(last, 3)
        self.assertEqual(len(fake.shown), 3)  # Redrawn only when the frame changes.
        self.assertEqual(fake.shown[0][1], (48, 64, 3))
        self.assertEqual(fake.destroyed, fake.windows)

    def test_play_uses_the_recorded_fps(self) -> None:
        fake = FakeCv2([SPACE, TIMEOUT, TIMEOUT, TIMEOUT, QUIT])

        self.assertEqual(review_session(self.session_dir, cv2=fake), 3)
        self.assertEqual(fake.waits[:3], [50, 100, 100])

    def test_closing_the_window_ends_review(self) -> None:
        fake = FakeCv2([TIMEOUT] * 10, visible_after=2)

        self.assertEqual(review_session(self.session_dir, start=2, cv2=fake), 2)
        self.assertEqual(len(fake.waits), 3)
        self.assertEqual(len(fake.destroyed), 1)

    def test_invalid_session_is_not_reviewed(self) -> None:
        edit_session_json(self.session_dir, frames=99)
        fake = FakeCv2([])

        with self.assertRaises(DatasetError):
            review_session(self.session_dir, cv2=fake)
        self.assertEqual(fake.windows, [])

    def test_read_frame_handles_non_ascii_paths(self) -> None:
        folder = self.root / "phiên ghi"
        folder.mkdir()
        path = folder / "khung.jpg"
        ok, data = cv2.imencode(".jpg", np.full((8, 8, 3), 200, dtype=np.uint8))
        self.assertTrue(ok)
        path.write_bytes(data.tobytes())

        self.assertEqual(read_frame(path, cv2).shape, (8, 8, 3))
        path.write_bytes(b"not a jpeg")
        with self.assertRaises(DatasetError):
            read_frame(path, cv2)


class RecordingsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session_dir = make_session(self.root)
        self.cli = load_cli()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list(self) -> None:
        code, out, _ = self.run_cli("list")

        self.assertEqual(code, 0)
        self.assertIn("20260924_120000", out)
        self.assertIn("complete", out)
        self.assertIn("user / Test Game", out)

    def test_list_empty_root(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.cli.main(["--root", str(self.root / "none"), "list"])
        self.assertEqual(code, 0)
        self.assertIn("No recordings", out.getvalue())

    def test_validate_ok_and_failed(self) -> None:
        code, out, _ = self.run_cli("validate", "20260924_120000")
        self.assertEqual(code, 0)
        self.assertIn("20260924_120000: OK - 14 lines", out)

        edit_session_json(self.session_dir, frames=7)
        code, out, _ = self.run_cli("validate", str(self.session_dir))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)
        self.assertIn("error: 3 frame events", out)

    def test_validate_all(self) -> None:
        make_session(self.root, "20260924_130000")

        code, out, _ = self.run_cli("validate", "--all")

        self.assertEqual(code, 0)
        self.assertEqual(out.count(": OK"), 2)

    def test_export(self) -> None:
        code, out, _ = self.run_cli("export", "20260924_120000")

        self.assertEqual(code, 0)
        self.assertIn("Wrote 3 rows (5 actions)", out)
        self.assertIn("1 input event(s) before the first frame", out)
        rows = (self.session_dir / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(rows[0])["index"], 1)

        code, _, err = self.run_cli("export", "20260924_120000")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        code, _, _ = self.run_cli("export", "20260924_120000", "--overwrite")
        self.assertEqual(code, 0)

    def test_errors_exit_2(self) -> None:
        code, _, err = self.run_cli("validate", "missing_session")
        self.assertEqual(code, 2)
        self.assertIn("No session folder", err)
        code, _, err = self.run_cli("validate")
        self.assertEqual(code, 2)

    def test_cli_never_imports_input_senders(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            names: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            for module in modules:
                self.assertFalse(any(part in module for part in FORBIDDEN_MODULE_PARTS), module)
            for name in names:
                self.assertNotIn(name, FORBIDDEN_NAMES)


if __name__ == "__main__":
    unittest.main()
