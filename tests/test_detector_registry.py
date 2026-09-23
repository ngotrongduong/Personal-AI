from __future__ import annotations

import unittest

import numpy as np

from vision.detector_registry import DetectorRegistry, DetectorSpec


class DetectorRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = np.random.default_rng(12345)
        self.frame = self.rng.integers(0, 256, (140, 180, 3), dtype=np.uint8)

    def test_multiple_named_detectors_find_independent_targets(self) -> None:
        template_a = self.frame[20:38, 30:54].copy()
        template_b = self.frame[82:104, 112:140].copy()

        registry = DetectorRegistry()
        registry.register_array(DetectorSpec("collect_button", threshold=0.99), template_a)
        registry.register_array(DetectorSpec("continue_button", threshold=0.99), template_b)

        results = registry.detect_all(self.frame)

        self.assertEqual(set(results), {"collect_button", "continue_button"})
        self.assertTrue(results["collect_button"].visible)
        self.assertEqual(results["collect_button"].bbox, (30, 20, 24, 18))
        self.assertGreaterEqual(results["collect_button"].confidence, 0.99)
        self.assertTrue(results["continue_button"].visible)
        self.assertEqual(results["continue_button"].bbox, (112, 82, 28, 22))

    def test_missing_template_returns_invisible_result(self) -> None:
        unrelated = self.rng.integers(0, 256, (18, 24, 3), dtype=np.uint8)

        registry = DetectorRegistry()
        registry.register_array(DetectorSpec("missing", threshold=0.9999), unrelated)

        result = registry.detect_all(self.frame)["missing"]

        self.assertFalse(result.visible)
        self.assertEqual(result.confidence, 0.0)
        self.assertIsNone(result.bbox)

    def test_roi_coordinates_are_translated_to_full_frame(self) -> None:
        template = self.frame[70:90, 100:126].copy()

        registry = DetectorRegistry()
        registry.register_array(
            DetectorSpec("inside_roi", threshold=0.99, roi=(80, 55, 70, 60)),
            template,
        )

        result = registry.detect_all(self.frame)["inside_roi"]

        self.assertTrue(result.visible)
        self.assertEqual(result.bbox, (100, 70, 26, 20))

    def test_roi_that_excludes_target_does_not_match(self) -> None:
        template = self.frame[70:90, 100:126].copy()

        registry = DetectorRegistry()
        registry.register_array(
            DetectorSpec("outside_roi", threshold=0.9999, roi=(0, 0, 60, 60)),
            template,
        )

        self.assertFalse(registry.detect_all(self.frame)["outside_roi"].visible)

    def test_duplicate_detector_name_is_rejected(self) -> None:
        template = self.frame[20:38, 30:54].copy()
        registry = DetectorRegistry()
        spec = DetectorSpec("collect_button")
        registry.register_array(spec, template)

        with self.assertRaisesRegex(ValueError, "Duplicate detector name"):
            registry.register_array(spec, template)

    def test_unregister_removes_detector(self) -> None:
        template = self.frame[20:38, 30:54].copy()
        registry = DetectorRegistry()
        registry.register_array(DetectorSpec("collect_button"), template)

        self.assertTrue(registry.unregister("collect_button"))
        self.assertFalse(registry.unregister("collect_button"))
        self.assertEqual(registry.names, ())


if __name__ == "__main__":
    unittest.main()
