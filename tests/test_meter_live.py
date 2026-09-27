"""v1.1 live meter helpers: measure, fail closed, format."""

from __future__ import annotations

from dataclasses import dataclass
import unittest

import numpy as np

from agent.game_state import GameState
from agent.llm_planner import _format_observations
from agent.meter_live import (
    METER_SOURCE,
    measure_meters,
    meter_percent_text,
    meter_validity_changes,
    meters_status_parts,
    meters_summary,
)
from agent.resource_state_bridge import apply_resource_measurements
from agent.rule_engine import RuleEngine, VisibilityRule
from agent.skills import ClickSkill, _click_intent
from vision.resource_bar import HSVRange, ResourceBarSpec


GREEN = (HSVRange((50, 180, 180), (70, 255, 255)),)


@dataclass(frozen=True)
class _Meter:
    name: str
    roi: tuple[int, int, int, int] = (10, 10, 100, 10)
    min_confidence: float = 0.8

    def spec(self) -> ResourceBarSpec:
        return ResourceBarSpec(self.name, self.roi, GREEN, max_gap_slices=0)


class _BrokenMeter:
    name = "broken"
    min_confidence = 0.8

    def spec(self) -> ResourceBarSpec:
        raise RuntimeError("bad spec")


def _frame(fraction: float) -> np.ndarray:
    frame = np.zeros((40, 140, 3), dtype=np.uint8)
    frame[10:20, 10:10 + int(round(100 * fraction))] = (0, 255, 0)
    return frame


class MeasureMetersTests(unittest.TestCase):
    def test_measures_a_valid_bar(self) -> None:
        measurements, errors = measure_meters(_frame(0.42), [_Meter("hp")])
        self.assertEqual(errors, ())
        self.assertTrue(measurements[0].valid)
        self.assertAlmostEqual(measurements[0].fraction, 0.42, delta=0.01)

    def test_clipped_roi_is_invalid(self) -> None:
        measurements, errors = measure_meters(_frame(0.5), [_Meter("hp", roi=(100, 10, 100, 10))])
        self.assertEqual(errors, ())
        self.assertFalse(measurements[0].valid)

    def test_one_broken_meter_does_not_stop_the_others(self) -> None:
        measurements, errors = measure_meters(_frame(0.5), [_BrokenMeter(), _Meter("hp")])
        self.assertEqual([m.valid for m in measurements], [False, True])
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])

    def test_empty_frame_makes_every_meter_invalid(self) -> None:
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        measurements, errors = measure_meters(empty, [_Meter("hp"), _Meter("mp")])
        self.assertEqual([m.valid for m in measurements], [False, False])
        self.assertEqual(errors, ("empty frame",))
        self.assertEqual(measure_meters(None, []), ((), ()))


class FormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.meters = (_Meter("hp"), _Meter("mp"))
        measurements, _ = measure_meters(_frame(0.42), [self.meters[0]])
        apply_resource_measurements(self.state, measurements, observed_at=100.0)

    def test_percent_text(self) -> None:
        self.assertEqual(meter_percent_text(0.424), "42%")
        self.assertEqual(meter_percent_text(1.0), "100%")

    def test_status_parts(self) -> None:
        parts = meters_status_parts(self.state, self.meters)
        self.assertRegex(parts[0], r"^hp=42%\(\d\.\d\d\)$")
        self.assertEqual(parts[1], "mp=?")

    def test_summary_uses_fresh_accepted_values_only(self) -> None:
        self.assertEqual(
            meters_summary(self.state, self.meters, now=100.5, max_age_seconds=1.0),
            "hp 42%, mp unknown",
        )
        self.assertEqual(
            meters_summary(self.state, self.meters, now=105.0, max_age_seconds=1.0),
            "hp unknown, mp unknown",
        )
        self.assertEqual(meters_summary(self.state, (), now=100.5, max_age_seconds=1.0), "")

    def test_low_confidence_is_unknown_in_summary(self) -> None:
        strict = (_Meter("hp", min_confidence=1.0),)
        self.state.update_detector(
            "hp", visible=True, confidence=0.9, value=0.5, observed_at=100.0, source=METER_SOURCE
        )
        self.assertEqual(
            meters_summary(self.state, strict, now=100.5, max_age_seconds=1.0), "hp unknown"
        )

    def test_validity_changes(self) -> None:
        measurements, _ = measure_meters(_frame(0.5), [_Meter("hp"), _Meter("mp", roi=(200, 0, 5, 5))])
        self.assertEqual(meter_validity_changes({}, measurements), [("hp", True), ("mp", False)])
        self.assertEqual(meter_validity_changes({"hp": True, "mp": False}, measurements), [])
        self.assertEqual(meter_validity_changes({"hp": False}, measurements), [("hp", True), ("mp", False)])

    def test_status_marks_low_confidence_readings_unknown(self) -> None:
        self.state.update_detector(
            "mp", visible=True, confidence=0.31, value=0.12, observed_at=100.0, source=METER_SOURCE
        )
        self.assertEqual(meters_status_parts(self.state, self.meters)[1], "mp=?(0.31)")

    def test_planner_prompt_shows_meters_as_percentages(self) -> None:
        self.state.update_detector("mp", visible=False, observed_at=100.0, source=METER_SOURCE)
        self.state.update_detector("button", visible=True, confidence=0.9)
        lines = _format_observations(self.state.snapshot(), now=100.5)
        self.assertRegex(lines[0], r"^- hp: 42% \(meter, confidence \d\.\d{3}\)$")
        self.assertEqual(lines[1], "- mp: unknown (meter)")
        self.assertIn("visible=True", lines[2])

    def test_planner_prompt_hides_stale_or_unconfident_meters(self) -> None:
        stale = _format_observations(self.state.snapshot(), now=105.0)
        self.assertEqual(stale[0], "- hp: unknown (meter)")
        self.state.update_detector(
            "hp", visible=True, confidence=0.85, value=0.42, observed_at=100.0, source=METER_SOURCE
        )
        lenient = _format_observations(self.state.snapshot(), now=100.5)
        self.assertEqual(lenient[0], "- hp: 42% (meter, confidence 0.850)")
        strict = _format_observations(
            self.state.snapshot(), now=100.5, meter_min_confidence={"hp": 0.9}
        )
        self.assertEqual(strict[0], "- hp: unknown (meter)")


class MeterIsNeverAClickTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        measurements, _ = measure_meters(_frame(0.42), [_Meter("hp")])
        apply_resource_measurements(self.state, measurements, observed_at=100.0)

    def test_visibility_rule_ignores_a_meter_reading(self) -> None:
        engine = RuleEngine([VisibilityRule("click_hp", "hp", "click", min_confidence=0.0)])
        self.assertEqual(engine.evaluate(self.state, now=100.1), [])

    def test_click_skill_refuses_a_meter(self) -> None:
        skill = ClickSkill("poke", detector="hp", min_confidence=0.0)
        result = _click_intent(skill, self.state, "test", 100.1, None)
        self.assertIsNone(result.intent)
        self.assertIn("meter", result.reason)


if __name__ == "__main__":
    unittest.main()
