from __future__ import annotations

import unittest

from agent.game_state import GameState
from agent.vision_state_bridge import apply_detections
from vision.detector_registry import Detection


class VisionStateBridgeTests(unittest.TestCase):
    def test_multiple_detections_update_independent_game_state_entries(self) -> None:
        state = GameState()
        detections = [
            Detection("collect_button", True, 0.96, (10, 20, 30, 40)),
            Detection("inventory_full", False, 0.0, None),
        ]

        observations = apply_detections(state, detections, observed_at=123.0)

        self.assertEqual(len(observations), 2)
        collect = state.get("collect_button")
        inventory = state.get("inventory_full")
        self.assertIsNotNone(collect)
        self.assertIsNotNone(inventory)
        assert collect is not None
        assert inventory is not None
        self.assertTrue(collect.visible)
        self.assertEqual(collect.bbox, (10, 20, 30, 40))
        self.assertEqual(collect.observed_at, 123.0)
        self.assertEqual(collect.source, "vision:template")
        self.assertFalse(inventory.visible)
        self.assertEqual(inventory.observed_at, 123.0)

    def test_new_cycle_can_turn_previous_visible_state_off(self) -> None:
        state = GameState()
        apply_detections(
            state,
            [Detection("collect_button", True, 0.98, (1, 2, 3, 4))],
            observed_at=10.0,
        )
        apply_detections(
            state,
            [Detection("collect_button", False, 0.0, None)],
            observed_at=11.0,
        )

        observation = state.get("collect_button")
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertFalse(observation.visible)
        self.assertIsNone(observation.bbox)

    def test_duplicate_names_in_one_cycle_are_rejected_before_update(self) -> None:
        state = GameState()
        detections = [
            Detection("collect_button", True, 0.9, (1, 2, 3, 4)),
            Detection("collect_button", False, 0.0, None),
        ]

        with self.assertRaisesRegex(ValueError, "duplicate detector names"):
            apply_detections(state, detections, observed_at=10.0)

        self.assertIsNone(state.get("collect_button"))


if __name__ == "__main__":
    unittest.main()
