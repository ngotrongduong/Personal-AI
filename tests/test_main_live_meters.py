"""v1.1 task 4: profile meters are measured on every live vision tick.

Observation only: nothing here enables input or reaches InputController.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

import numpy as np

from agent.profile import load_profile
from agent.skill_effects import Expectation, MeterExpectation, parse_expectation
from main import PersonalGameAIApp


EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "examples" / "meter_demo_profile.json"


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _bar_frame(fraction: float, width: int = 560, height: int = 280) -> np.ndarray:
    """A frame with the demo's pure-green bar filled to `fraction` (ROI 60,80,400,32)."""
    frame = np.full((height, width, 3), 22, dtype=np.uint8)
    filled = int(round(400 * fraction))
    frame[80:112, 60:60 + filled] = (0, 255, 0)
    return frame


class LiveMeterWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.app = PersonalGameAIApp(self.root)
        self.tmp = Path(tempfile.mkdtemp())
        folder = self.tmp / "meter_demo"
        folder.mkdir()
        shutil.copyfile(EXAMPLE, folder / "profile.json")
        profile = load_profile(folder)
        self.app.profile = profile
        self.app.skill_book = profile.skill_book()
        self.app.rule_engine = profile.rule_engine()
        self.app.vision_enabled_var.set(True)
        self.logs: list[str] = []
        self.app.log = self.logs.append

    def tearDown(self) -> None:
        self.app.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tick(self, frame: np.ndarray) -> None:
        self.app._last_vision_time = 0.0
        self.app._run_vision_if_due(frame)

    def test_tick_writes_meter_to_game_state_and_status(self) -> None:
        self._tick(_bar_frame(0.5))

        observation = self.app.game_state.get("hp")
        self.assertIsNotNone(observation)
        self.assertTrue(observation.visible)
        self.assertEqual(observation.source, "vision:resource_bar")
        self.assertAlmostEqual(observation.value, 0.5, delta=0.02)
        self.assertIn("hp=50%", self.app.detectors_var.get())
        self.assertTrue(any("Meter 'hp': reading" in line for line in self.logs))

    def test_roi_outside_frame_is_unknown_and_logged_once(self) -> None:
        self._tick(_bar_frame(0.5))
        small = np.zeros((60, 60, 3), dtype=np.uint8)
        self._tick(small)
        self._tick(small)

        observation = self.app.game_state.get("hp")
        self.assertFalse(observation.visible)
        self.assertIsNone(observation.value)
        self.assertIn("hp=?", self.app.detectors_var.get())
        lost = [line for line in self.logs if "no valid reading" in line]
        self.assertEqual(len(lost), 1)

    def test_no_input_is_sent_by_a_meter_rule_with_input_off(self) -> None:
        # heal_low_hp fires on hp below 25%; input control is off by default.
        self.assertFalse(self.app.input.enabled)
        self._tick(_bar_frame(0.1))
        self.assertFalse(self.app.executor.busy)
        self.assertTrue(any("heal_low_hp" in line and "BLOCKED" in line for line in self.logs))

    def test_effect_baseline_uses_a_fresh_valid_reading_only(self) -> None:
        expectation = parse_expectation({"meter": "hp", "rises": 0.15}, (), ("hp",))
        self.assertIsInstance(expectation, MeterExpectation)

        self.assertIsNone(self.app._effect_baseline(expectation, time.monotonic()))

        self._tick(_bar_frame(0.4))
        now = time.monotonic()
        self.assertAlmostEqual(self.app._effect_baseline(expectation, now), 0.4, delta=0.02)
        # Stale: more than GOAL_FRESH_SECONDS after the reading.
        self.assertIsNone(self.app._effect_baseline(expectation, now + 5.0))

        self._tick(np.zeros((60, 60, 3), dtype=np.uint8))
        self.assertIsNone(self.app._effect_baseline(expectation, time.monotonic()))

    def test_threshold_and_detector_expectations_need_no_baseline(self) -> None:
        self._tick(_bar_frame(0.4))
        below = parse_expectation({"meter": "hp", "below": 0.5}, (), ("hp",))
        self.assertIsNone(self.app._effect_baseline(below, time.monotonic()))
        self.assertIsNone(
            self.app._effect_baseline(Expectation("button"), time.monotonic())
        )

    def test_ui_click_rule_on_a_meter_is_rejected(self) -> None:
        self.app.rule_name_var.set("click_hp")
        self.app.rule_detector_var.set("hp")
        before = len(self.app.rule_engine.rules)
        with mock.patch("main.messagebox.showerror") as showerror:
            self.app.add_rule()
        showerror.assert_called_once()
        self.assertIn("meter", showerror.call_args.args[1])
        self.assertEqual(len(self.app.rule_engine.rules), before)

    def test_frozen_frame_after_a_capture_error_is_not_measured(self) -> None:
        self.app.capture = mock.Mock(actual_fps=0.0, last_error="DXGI access lost", hwnd=None)
        self.app.capture.latest_frame.return_value = _bar_frame(0.5)
        self.app._last_vision_time = 0.0
        self.app._poll_preview()
        self.assertIsNone(self.app.game_state.get("hp"))

        self.app.capture = mock.Mock(actual_fps=30.0, last_error=None, running=False, hwnd=None)
        self.app.capture.latest_frame.return_value = _bar_frame(0.5)
        self.app._poll_preview()
        self.assertIsNone(self.app.game_state.get("hp"))

        self.app.capture = mock.Mock(actual_fps=30.0, last_error=None, running=True, hwnd=None)
        self.app.capture.latest_frame.return_value = _bar_frame(0.5)
        self.app._poll_preview()
        self.assertIsNotNone(self.app.game_state.get("hp"))
        self.app.capture = None

    def test_preflight_facts_show_meters(self) -> None:
        facts = self.app._preflight_facts(None, False, "no planner settings")
        self.assertEqual(facts.meters, "hp unknown")
        self.assertFalse(facts.meters_all_known)

        self._tick(_bar_frame(0.8))
        facts = self.app._preflight_facts(None, False, "no planner settings")
        self.assertEqual(facts.meters, "hp 80%")
        self.assertTrue(facts.meters_all_known)


if __name__ == "__main__":
    unittest.main()
