from __future__ import annotations

import unittest

import numpy as np

from vision.template_matcher import TemplateMatcher


def _solid_frame(width: int, height: int, color: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = color
    return frame


def _noise_frame(width: int, height: int, seed: int) -> np.ndarray:
    # cv2.matchTemplate's TM_CCOEFF_NORMED is undefined/degenerate for flat-color
    # (zero-variance) regions, so tests that need a real, unique match location use
    # textured noise instead of solid colors.
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


class TemplateMatcherTests(unittest.TestCase):
    def test_not_loaded_by_default(self) -> None:
        matcher = TemplateMatcher()
        self.assertFalse(matcher.loaded)
        self.assertEqual(matcher.size, (0, 0))

    def test_find_best_without_template_returns_none(self) -> None:
        matcher = TemplateMatcher()
        frame = _solid_frame(64, 64, (10, 10, 10))
        self.assertIsNone(matcher.find_best(frame))

    def test_load_array_rejects_tiny_template(self) -> None:
        matcher = TemplateMatcher()
        tiny = np.zeros((4, 4, 3), dtype=np.uint8)
        with self.assertRaises(ValueError):
            matcher.load_array(tiny)

    def test_load_array_rejects_empty_template(self) -> None:
        matcher = TemplateMatcher()
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        with self.assertRaises(ValueError):
            matcher.load_array(empty)

    def test_finds_exact_patch_at_expected_coordinates(self) -> None:
        frame = _noise_frame(200, 150, seed=1)
        patch_x, patch_y, patch_w, patch_h = 60, 40, 30, 20

        template = frame[patch_y : patch_y + patch_h, patch_x : patch_x + patch_w].copy()

        matcher = TemplateMatcher(threshold=0.82)
        matcher.load_array(template)
        self.assertTrue(matcher.loaded)
        self.assertEqual(matcher.size, (patch_w, patch_h))

        match = matcher.find_best(frame)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertGreaterEqual(match.score, 0.82)
        self.assertEqual((match.x, match.y, match.w, match.h), (patch_x, patch_y, patch_w, patch_h))

    def test_no_match_below_threshold_returns_none(self) -> None:
        # Two independent noise images: real-world uncorrelated patterns typically
        # score well below a high threshold.
        frame = _noise_frame(120, 90, seed=2)
        template = _noise_frame(16, 16, seed=3)

        matcher = TemplateMatcher(threshold=0.95)
        matcher.load_array(template)

        self.assertIsNone(matcher.find_best(frame))

    def test_template_larger_than_frame_returns_none(self) -> None:
        frame = _solid_frame(32, 32, (5, 5, 5))
        template = _solid_frame(64, 64, (5, 5, 5))

        matcher = TemplateMatcher(threshold=0.5)
        matcher.load_array(template)

        self.assertIsNone(matcher.find_best(frame))

    def test_clear_resets_state(self) -> None:
        frame = _solid_frame(40, 40, (1, 2, 3))
        matcher = TemplateMatcher()
        matcher.load_array(frame[:16, :16])
        self.assertTrue(matcher.loaded)

        matcher.clear()
        self.assertFalse(matcher.loaded)
        self.assertEqual(matcher.size, (0, 0))
        self.assertIsNone(matcher.find_best(frame))


if __name__ == "__main__":
    unittest.main()
