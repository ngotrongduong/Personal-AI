"""v1.1.1: the main window fits high-DPI screens and warns when it hides the game."""

from __future__ import annotations

import unittest

from main import BASE_MIN_SIZE, BASE_WINDOW_SIZE, fitted_window_size, overlap_fraction


STANDARD_SCALING = 96 / 72


class FittedWindowSizeTests(unittest.TestCase):
    def test_standard_dpi_uses_base_size(self):
        width, height, min_w, min_h = fitted_window_size(STANDARD_SCALING, 3840, 2160)
        self.assertEqual((width, height), BASE_WINDOW_SIZE)
        self.assertEqual((min_w, min_h), BASE_MIN_SIZE)

    def test_150_percent_scale_grows_window(self):
        # 150% on a 4K monitor: Tk reports scaling 2.0.
        width, height, min_w, min_h = fitted_window_size(2.0, 3840, 2160)
        self.assertEqual((width, height), (2250, 1350))
        self.assertEqual((min_w, min_h), (1170, 840))

    def test_clamped_to_screen(self):
        width, height, min_w, min_h = fitted_window_size(4.0, 1920, 1080)
        self.assertLessEqual(width, 1920)
        self.assertLessEqual(height, 1080)
        self.assertLessEqual(min_w, width)
        self.assertLessEqual(min_h, height)

    def test_small_screen_min_size_never_exceeds_window(self):
        width, height, min_w, min_h = fitted_window_size(STANDARD_SCALING, 1280, 720)
        self.assertEqual((width, height), (int(1280 * 0.92), int(720 * 0.92)))
        self.assertLessEqual(min_w, width)
        self.assertLessEqual(min_h, height)

    def test_low_scaling_never_shrinks_below_base(self):
        self.assertEqual(
            fitted_window_size(1.0, 3840, 2160)[:2], BASE_WINDOW_SIZE
        )


class OverlapFractionTests(unittest.TestCase):
    GAME = (1000, 0, 2000, 1000)

    def test_disjoint_windows_do_not_cover(self):
        self.assertEqual(overlap_fraction((0, 0, 1000, 1000), self.GAME), 0.0)
        self.assertEqual(overlap_fraction((2100, 0, 3000, 900), self.GAME), 0.0)

    def test_partial_cover_is_share_of_game(self):
        self.assertAlmostEqual(overlap_fraction((0, 0, 1250, 1000), self.GAME), 0.25)
        self.assertAlmostEqual(overlap_fraction((1500, 500, 3000, 3000), self.GAME), 0.25)

    def test_full_cover(self):
        self.assertEqual(overlap_fraction((0, 0, 4000, 4000), self.GAME), 1.0)

    def test_minimized_window_off_screen(self):
        self.assertEqual(overlap_fraction((-32000, -32000, -31840, -31972), self.GAME), 0.0)

    def test_empty_game_region(self):
        self.assertEqual(overlap_fraction((0, 0, 10, 10), (5, 5, 5, 5)), 0.0)


if __name__ == "__main__":
    unittest.main()
