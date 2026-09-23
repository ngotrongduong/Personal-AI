from __future__ import annotations

import unittest

from agent.game_state import GameState, Observation


class GameStateTests(unittest.TestCase):
    def test_visible_observation_meets_threshold(self) -> None:
        state = GameState()
        state.update(
            Observation(
                name="collect_button",
                visible=True,
                confidence=0.93,
                bbox=(10, 20, 30, 40),
                observed_at=100.0,
            )
        )

        self.assertTrue(
            state.is_visible(
                "collect_button",
                min_confidence=0.90,
                max_age_seconds=1.0,
                now=100.5,
            )
        )

    def test_low_confidence_is_not_visible_for_rule_purposes(self) -> None:
        state = GameState()
        state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.60,
            observed_at=100.0,
        )

        self.assertFalse(
            state.is_visible(
                "collect_button",
                min_confidence=0.82,
                max_age_seconds=1.0,
                now=100.1,
            )
        )

    def test_stale_observation_is_rejected(self) -> None:
        state = GameState()
        state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            observed_at=100.0,
        )

        self.assertFalse(
            state.is_visible(
                "collect_button",
                min_confidence=0.82,
                max_age_seconds=0.75,
                now=101.0,
            )
        )

    def test_older_out_of_order_update_is_ignored(self) -> None:
        state = GameState()
        state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            observed_at=200.0,
        )
        state.update_detector(
            "collect_button",
            visible=False,
            confidence=0.10,
            observed_at=199.0,
        )

        observation = state.get("collect_button")
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertTrue(observation.visible)
        self.assertEqual(observation.observed_at, 200.0)


if __name__ == "__main__":
    unittest.main()
