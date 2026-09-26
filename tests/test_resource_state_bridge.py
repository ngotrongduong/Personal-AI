from __future__ import annotations

import unittest

from agent.game_state import GameState
from agent.resource_state_bridge import apply_resource_measurements
from vision.resource_bar import ResourceBarMeasurement


class ResourceStateBridgeTests(unittest.TestCase):
    def test_valid_measurement_updates_fraction_value(self) -> None:
        state = GameState()
        measurement = ResourceBarMeasurement(
            name="player_hp",
            valid=True,
            fraction=0.42,
            confidence=0.97,
            bbox=(10, 20, 100, 12),
            matched_pixel_fraction=0.40,
        )

        apply_resource_measurements(state, [measurement], observed_at=123.0)

        observation = state.get("player_hp")
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertTrue(observation.visible)
        self.assertEqual(observation.value, 0.42)
        self.assertEqual(observation.confidence, 0.97)
        self.assertEqual(observation.source, "vision:resource_bar")
        self.assertEqual(observation.observed_at, 123.0)

    def test_invalid_measurement_does_not_masquerade_as_empty_bar(self) -> None:
        state = GameState()
        measurement = ResourceBarMeasurement(
            name="player_hp",
            valid=False,
            fraction=0.0,
            confidence=0.0,
            bbox=None,
            matched_pixel_fraction=0.0,
        )

        apply_resource_measurements(state, [measurement], observed_at=10.0)

        observation = state.get("player_hp")
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertFalse(observation.visible)
        self.assertIsNone(observation.value)


    def test_profile_min_confidence_rejects_weak_measurement(self) -> None:
        state = GameState()
        measurement = ResourceBarMeasurement(
            name="player_hp",
            valid=True,
            fraction=0.42,
            confidence=0.60,
            bbox=(10, 20, 100, 12),
            matched_pixel_fraction=0.40,
        )

        apply_resource_measurements(
            state,
            [measurement],
            observed_at=123.0,
            min_confidence_by_name={"player_hp": 0.80},
        )

        observation = state.get("player_hp")
        assert observation is not None
        self.assertFalse(observation.visible)
        self.assertIsNone(observation.value)
        self.assertEqual(observation.confidence, 0.60)
        self.assertEqual(observation.bbox, measurement.bbox)

    def test_profile_min_confidence_keeps_strong_measurement(self) -> None:
        state = GameState()
        measurement = ResourceBarMeasurement(
            name="player_hp",
            valid=True,
            fraction=0.42,
            confidence=0.90,
            bbox=(10, 20, 100, 12),
            matched_pixel_fraction=0.40,
        )

        apply_resource_measurements(
            state,
            [measurement],
            min_confidence_by_name={"player_hp": 0.80},
        )

        observation = state.get("player_hp")
        assert observation is not None
        self.assertTrue(observation.visible)
        self.assertEqual(observation.value, 0.42)

    def test_invalid_profile_min_confidence_is_rejected(self) -> None:
        state = GameState()
        measurement = ResourceBarMeasurement(
            name="player_hp",
            valid=True,
            fraction=0.5,
            confidence=1.0,
            bbox=(0, 0, 100, 10),
            matched_pixel_fraction=0.5,
        )

        for threshold in (-0.1, 1.1, float("nan"), True):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                apply_resource_measurements(
                    state,
                    [measurement],
                    min_confidence_by_name={"player_hp": threshold},
                )

    def test_duplicate_names_are_rejected_before_state_mutation(self) -> None:
        state = GameState()
        measurement = ResourceBarMeasurement(
            name="player_hp",
            valid=True,
            fraction=0.5,
            confidence=1.0,
            bbox=(0, 0, 100, 10),
            matched_pixel_fraction=0.5,
        )

        with self.assertRaisesRegex(ValueError, "duplicate names"):
            apply_resource_measurements(
                state,
                [measurement, measurement],
                observed_at=10.0,
            )

        self.assertIsNone(state.get("player_hp"))


if __name__ == "__main__":
    unittest.main()
