from __future__ import annotations

import tkinter as tk
import unittest

import numpy as np

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


class LiveDetectorWiringTests(unittest.TestCase):
    """
    Regression test for task 11 (docs/PLAN.md): DetectorRegistry results must
    reach GameState and the GUI status text on every live-loop tick, without
    touching input/F8 behavior (no InputController calls anywhere here).
    """

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.app = PersonalGameAIApp(self.root)

    def tearDown(self) -> None:
        self.app.close()

    def test_detect_all_updates_game_state_and_status_text(self) -> None:
        frame = _noise_frame(200, 150, seed=1)
        template_a = frame[10:40, 10:40].copy()
        template_b = frame[80:110, 60:100].copy()

        self.app.registry.register_array(DetectorSpec(name="button_a", threshold=0.9), template_a)
        self.app.registry.register_array(DetectorSpec(name="button_b", threshold=0.9), template_b)
        self.app.vision_enabled_var.set(True)

        self.app._run_vision_if_due(frame)

        state_a = self.app.game_state.get("button_a")
        state_b = self.app.game_state.get("button_b")
        self.assertIsNotNone(state_a)
        self.assertIsNotNone(state_b)
        self.assertTrue(state_a.visible)
        self.assertTrue(state_b.visible)
        self.assertEqual(state_a.source, "vision:template")

        status = self.app.detectors_var.get()
        self.assertIn("button_a=FOUND", status)
        self.assertIn("button_b=FOUND", status)

    def test_detector_not_found_is_reflected_in_game_state(self) -> None:
        frame = _noise_frame(200, 150, seed=2)
        unrelated_template = _noise_frame(30, 30, seed=99)

        self.app.registry.register_array(
            DetectorSpec(name="missing_button", threshold=0.95), unrelated_template
        )
        self.app.vision_enabled_var.set(True)

        self.app._run_vision_if_due(frame)

        observation = self.app.game_state.get("missing_button")
        self.assertIsNotNone(observation)
        self.assertFalse(observation.visible)
        self.assertIn("missing_button=not found", self.app.detectors_var.get())

    def test_clear_detectors_resets_registry_and_state(self) -> None:
        frame = _noise_frame(120, 90, seed=3)
        template = frame[5:25, 5:25].copy()
        self.app.registry.register_array(DetectorSpec(name="temp"), template)
        self.app.vision_enabled_var.set(True)
        self.app._run_vision_if_due(frame)

        self.app.clear_detectors()

        self.assertEqual(self.app.registry.names, ())
        self.assertIsNone(self.app.game_state.get("temp"))
        self.assertEqual(self.app.detectors_var.get(), "Detectors: none registered")


if __name__ == "__main__":
    unittest.main()
