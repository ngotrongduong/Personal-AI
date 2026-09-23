from __future__ import annotations

import tkinter as tk
import unittest

import numpy as np

from agent.action_dispatcher import DispatchResult
from agent.rule_engine import VisibilityRule
from main import PersonalGameAIApp
from vision.detector_registry import DetectorSpec


def _noise_frame(width: int, height: int, seed: int) -> np.ndarray:
    # cv2.matchTemplate's TM_CCOEFF_NORMED is degenerate on flat-color (zero
    # variance) regions, so use textured noise for deterministic matches.
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


class _FakeCapture:
    """Stands in for core.capture.WindowCapture: only .hwnd is read here."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd


CAPTURE_HWND = 111
COMBOBOX_HWND = 222


class MainActionDispatchTests(unittest.TestCase):
    """
    Regression test for task 12 (docs/PLAN.md): the live loop must dispatch
    ActionIntents against the window that is actually being captured, not
    whatever the window-picker combobox happens to show -- the two can
    diverge if the user reselects the combobox while a capture session keeps
    running against the original window. Dispatching against the wrong hwnd
    would translate a bbox from one window's frame into screen coordinates
    on a different window (a real, unintended click).
    """

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.app = PersonalGameAIApp(self.root)

    def tearDown(self) -> None:
        self.app.capture = None
        self.app.close()

    def test_dispatch_uses_capture_hwnd_not_combobox_selection(self) -> None:
        frame = _noise_frame(200, 150, seed=7)
        template = frame[10:40, 10:40].copy()
        self.app.registry.register_array(
            DetectorSpec(name="button_a", threshold=0.9), template
        )
        self.app.rule_engine.add_rule(
            VisibilityRule(name="click_button_a", detector_name="button_a", action="click")
        )
        self.app.vision_enabled_var.set(True)

        # Simulate a capture session already running against CAPTURE_HWND,
        # while the combobox (e.g. from a stale refresh_windows() call) is
        # currently pointing at a different window.
        self.app.capture = _FakeCapture(CAPTURE_HWND)

        seen_hwnds: list[int | None] = []

        def fake_dispatch(intent, *, hwnd, now=None):
            seen_hwnds.append(hwnd)
            return DispatchResult(intent, False, "test stub: not actually dispatched")

        self.app.dispatcher.dispatch = fake_dispatch

        self.app._run_vision_if_due(frame)

        self.assertEqual(seen_hwnds, [CAPTURE_HWND])
        self.assertNotIn(COMBOBOX_HWND, seen_hwnds)

    def test_dispatch_hwnd_is_none_when_capture_not_running(self) -> None:
        frame = _noise_frame(200, 150, seed=8)
        template = frame[10:40, 10:40].copy()
        self.app.registry.register_array(
            DetectorSpec(name="button_a", threshold=0.9), template
        )
        self.app.rule_engine.add_rule(
            VisibilityRule(name="click_button_a", detector_name="button_a", action="click")
        )
        self.app.vision_enabled_var.set(True)
        self.app.capture = None

        seen_hwnds: list[int | None] = []

        def fake_dispatch(intent, *, hwnd, now=None):
            seen_hwnds.append(hwnd)
            return DispatchResult(intent, False, "test stub: not actually dispatched")

        self.app.dispatcher.dispatch = fake_dispatch

        self.app._run_vision_if_due(frame)

        self.assertEqual(seen_hwnds, [None])


if __name__ == "__main__":
    unittest.main()
