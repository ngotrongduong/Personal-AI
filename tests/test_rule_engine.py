from __future__ import annotations

import unittest

from agent.game_state import GameState
from agent.rule_engine import RuleEngine, VisibilityRule


class RuleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.rule = VisibilityRule(
            name="click_collect",
            detector_name="collect_button",
            action="click_detector_center",
            min_confidence=0.90,
            max_observation_age_seconds=0.75,
            cooldown_seconds=1.0,
        )
        self.engine = RuleEngine([self.rule])

    def test_hidden_detector_does_not_fire(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=False,
            confidence=0.99,
            observed_at=10.0,
        )
        self.assertEqual(self.engine.evaluate(self.state, now=10.1), [])

    def test_visible_detector_fires_once(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            bbox=(100, 200, 50, 20),
            observed_at=10.0,
        )

        intents = self.engine.evaluate(self.state, now=10.1)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].rule_name, "click_collect")
        self.assertEqual(intents[0].target_bbox, (100, 200, 50, 20))

    def test_cooldown_blocks_repeat_then_expires(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            observed_at=10.0,
        )

        self.assertEqual(len(self.engine.evaluate(self.state, now=10.1)), 1)
        self.assertEqual(self.engine.evaluate(self.state, now=10.5), [])

        # Refresh observation so only cooldown, not staleness, controls this check.
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            observed_at=11.2,
        )
        self.assertEqual(len(self.engine.evaluate(self.state, now=11.2)), 1)

    def test_low_confidence_does_not_fire(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.85,
            observed_at=10.0,
        )
        self.assertEqual(self.engine.evaluate(self.state, now=10.1), [])

    def test_stale_observation_does_not_fire(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.99,
            observed_at=10.0,
        )
        self.assertEqual(self.engine.evaluate(self.state, now=11.0), [])

    def test_disable_then_enable_rule_controls_evaluation(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            observed_at=10.0,
        )

        self.engine.disable_rule("click_collect")
        self.assertFalse(self.engine.is_rule_enabled("click_collect"))
        self.assertEqual(self.engine.evaluate(self.state, now=10.1), [])

        self.engine.enable_rule("click_collect")
        self.assertTrue(self.engine.is_rule_enabled("click_collect"))
        self.assertEqual(len(self.engine.evaluate(self.state, now=10.1)), 1)

    def test_enable_and_disable_unknown_rule_raise_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.disable_rule("unknown")
        with self.assertRaises(ValueError):
            self.engine.enable_rule("unknown")

    def test_disabling_rule_preserves_existing_cooldown(self) -> None:
        self.state.update_detector(
            "collect_button",
            visible=True,
            confidence=0.95,
            observed_at=10.0,
        )
        self.assertEqual(len(self.engine.evaluate(self.state, now=10.1)), 1)

        self.engine.disable_rule("click_collect")
        self.assertEqual(self.engine.evaluate(self.state, now=10.2), [])
        self.engine.enable_rule("click_collect")
        self.assertEqual(self.engine.evaluate(self.state, now=10.5), [])


if __name__ == "__main__":
    unittest.main()
