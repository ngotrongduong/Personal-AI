from __future__ import annotations

import unittest

import numpy as np

from vision.resource_bar import (
    HSVRange,
    ResourceBarSpec,
    measure_resource_bar,
    measure_resource_bars,
)


GREEN = HSVRange((50, 200, 200), (70, 255, 255))


def _blank(width: int = 100, height: int = 20) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


class ResourceBarTests(unittest.TestCase):
    def test_half_full_left_to_right(self) -> None:
        frame = _blank()
        frame[:, :50] = (0, 255, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec("player_hp", (0, 0, 100, 20), (GREEN,), max_gap_slices=0),
        )

        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.fraction, 0.50)
        self.assertAlmostEqual(result.percent, 50.0)
        self.assertGreater(result.confidence, 0.99)
        self.assertEqual(result.bbox, (0, 0, 100, 20))

    def test_quarter_full_right_to_left(self) -> None:
        frame = _blank()
        frame[:, 75:] = (0, 255, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec(
                "mana",
                (0, 0, 100, 20),
                (GREEN,),
                direction="right_to_left",
                max_gap_slices=0,
            ),
        )

        self.assertAlmostEqual(result.fraction, 0.25)

    def test_vertical_bottom_to_top(self) -> None:
        frame = _blank(width=20, height=100)
        frame[60:, :] = (0, 255, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec(
                "energy",
                (0, 0, 20, 100),
                (GREEN,),
                direction="bottom_to_top",
                max_gap_slices=0,
            ),
        )

        self.assertAlmostEqual(result.fraction, 0.40)

    def test_short_internal_gap_is_tolerated(self) -> None:
        frame = _blank()
        frame[:, :50] = (0, 255, 0)
        frame[:, 24] = (0, 0, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec("hp", (0, 0, 100, 20), (GREEN,), max_gap_slices=1),
        )

        self.assertAlmostEqual(result.fraction, 0.50)
        self.assertGreater(result.confidence, 0.95)

    def test_large_gap_stops_extent_and_ignores_later_noise(self) -> None:
        frame = _blank()
        frame[:, :30] = (0, 255, 0)
        frame[:, 60] = (0, 255, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec("hp", (0, 0, 100, 20), (GREEN,), max_gap_slices=1),
        )

        self.assertAlmostEqual(result.fraction, 0.30)

    def test_empty_bar_is_valid_zero_measurement(self) -> None:
        result = measure_resource_bar(
            _blank(),
            ResourceBarSpec("hp", (0, 0, 100, 20), (GREEN,), max_gap_slices=0),
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.fraction, 0.0)
        self.assertGreater(result.confidence, 0.99)

    def test_outside_frame_roi_is_invalid_not_empty(self) -> None:
        result = measure_resource_bar(
            _blank(),
            ResourceBarSpec("hp", (500, 500, 100, 20), (GREEN,)),
        )

        self.assertFalse(result.valid)
        self.assertIsNone(result.bbox)
        self.assertEqual(result.confidence, 0.0)

    def test_partially_clipped_roi_returns_actual_bbox(self) -> None:
        frame = _blank(width=80, height=20)
        frame[:, 60:] = (0, 255, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec("hp", (60, 0, 40, 20), (GREEN,), max_gap_slices=0),
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.bbox, (60, 0, 20, 20))
        self.assertEqual(result.fraction, 1.0)

    def test_multiple_hsv_ranges_are_combined(self) -> None:
        red_low = HSVRange((0, 200, 200), (10, 255, 255))
        frame = _blank()
        frame[:, :20] = (0, 0, 255)
        frame[:, 20:40] = (0, 255, 0)

        result = measure_resource_bar(
            frame,
            ResourceBarSpec(
                "mixed_resource",
                (0, 0, 100, 20),
                (red_low, GREEN),
                max_gap_slices=0,
            ),
        )

        self.assertAlmostEqual(result.fraction, 0.40)

    def test_measure_many_rejects_duplicate_names(self) -> None:
        spec = ResourceBarSpec("hp", (0, 0, 100, 20), (GREEN,))
        with self.assertRaisesRegex(ValueError, "duplicate names"):
            measure_resource_bars(_blank(), [spec, spec])


if __name__ == "__main__":
    unittest.main()
