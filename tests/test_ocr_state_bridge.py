from __future__ import annotations

import unittest

from agent.game_state import GameState
from agent.ocr_state_bridge import apply_ocr_measurements
from vision.ocr import OcrMeasurement


class OcrStateBridgeTests(unittest.TestCase):
    def test_valid_ocr_measurement_updates_text_value(self) -> None:
        state = GameState()
        measurement = OcrMeasurement(
            name="gold",
            valid=True,
            text="1234",
            confidence=0.94,
            bbox=(10, 20, 50, 12),
        )

        apply_ocr_measurements(state, [measurement], observed_at=123.0)

        observation = state.get("gold")
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertTrue(observation.visible)
        self.assertEqual(observation.value, "1234")
        self.assertEqual(observation.confidence, 0.94)
        self.assertEqual(observation.source, "vision:ocr")
        self.assertEqual(observation.observed_at, 123.0)

    def test_invalid_ocr_measurement_does_not_publish_text(self) -> None:
        state = GameState()
        measurement = OcrMeasurement(
            name="gold",
            valid=False,
            text="",
            confidence=0.49,
            bbox=None,
        )

        apply_ocr_measurements(state, [measurement], observed_at=123.0)

        observation = state.get("gold")
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertFalse(observation.visible)
        self.assertIsNone(observation.value)
        self.assertEqual(observation.confidence, 0.49)

    def test_duplicate_names_are_rejected_before_state_mutation(self) -> None:
        state = GameState()
        measurement = OcrMeasurement("gold", True, "1234", 0.9, (0, 0, 10, 10))

        with self.assertRaisesRegex(ValueError, "duplicate names"):
            apply_ocr_measurements(state, [measurement, measurement], observed_at=123.0)

        self.assertIsNone(state.get("gold"))


if __name__ == "__main__":
    unittest.main()
