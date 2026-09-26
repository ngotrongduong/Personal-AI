from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from agent.profile import load_profile
from scripts.meter_demo import (
    BAR_HEIGHT,
    BAR_WIDTH,
    CLIENT_HEIGHT,
    CLIENT_WIDTH,
    HSV_LOWER,
    HSV_UPPER,
    METER_ROI,
    MeterDemoState,
    STEP_FRACTION,
    clamp_fraction,
)
from vision.resource_bar import measure_resource_bar


class MeterDemoStateTests(unittest.TestCase):
    def test_default_and_adjustments_are_bounded(self) -> None:
        state = MeterDemoState()
        self.assertEqual(state.fraction, 0.5)
        self.assertEqual(state.percent, 50)

        self.assertEqual(state.adjust(STEP_FRACTION), 0.7)
        self.assertEqual(state.adjust(1.0), 1.0)
        self.assertEqual(state.adjust(-2.0), 0.0)

    def test_clamp_rejects_non_finite_and_boolean_values(self) -> None:
        for value in (True, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clamp_fraction(value)  # type: ignore[arg-type]


class MeterDemoProfileTests(unittest.TestCase):
    def _load_example(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "examples"
            / "meter_demo_profile.json"
        )
        data = json.loads(source.read_text(encoding="utf-8"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        (folder / "profile.json").write_text(json.dumps(data), encoding="utf-8")
        return load_profile(folder)

    def test_profile_matches_demo_geometry_and_color(self) -> None:
        profile = self._load_example()
        self.assertEqual(len(profile.meters), 1)
        meter = profile.meters[0]

        self.assertEqual(meter.roi, METER_ROI)
        self.assertEqual(meter.hsv_ranges[0].lower, HSV_LOWER)
        self.assertEqual(meter.hsv_ranges[0].upper, HSV_UPPER)
        self.assertEqual([skill.name for skill in profile.skills], ["heal", "damage"])
        self.assertEqual(profile.rules[0].rule.skill, "heal")

    def test_synthetic_demo_frame_measures_quarter_full(self) -> None:
        profile = self._load_example()
        meter = profile.meters[0]

        frame = np.zeros((CLIENT_HEIGHT, CLIENT_WIDTH, 3), dtype=np.uint8)
        x, y, width, height = METER_ROI
        self.assertEqual((width, height), (BAR_WIDTH, BAR_HEIGHT))
        frame[y : y + height, x : x + width // 4] = (0, 255, 0)

        result = measure_resource_bar(frame, meter.spec())

        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.fraction, 0.25)
        self.assertGreater(result.confidence, 0.99)


if __name__ == "__main__":
    unittest.main()
