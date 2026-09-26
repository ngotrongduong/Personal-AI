from __future__ import annotations

import unittest

from agent.game_state import GameState, Observation
from agent.meter_conditions import (
    MeterCondition,
    MeterConditionError,
    accepted_meter_value,
    condition_matches_value,
    current_meter_value,
    meter_condition_met,
    parse_meter_condition,
)


class MeterConditionParseTests(unittest.TestCase):
    def test_parses_each_operator(self) -> None:
        for operator in ("below", "above", "rises", "falls"):
            with self.subTest(operator=operator):
                condition = parse_meter_condition(
                    {"meter": "hp", operator: 0.25, "min_confidence": 0.9},
                    {"hp"},
                )
                self.assertEqual(condition.meter, "hp")
                self.assertEqual(condition.operator, operator)
                self.assertEqual(condition.amount, 0.25)
                self.assertEqual(condition.min_confidence, 0.9)
                self.assertEqual(condition.to_block()[operator], 0.25)

    def test_requires_exactly_one_operator(self) -> None:
        for block in (
            {"meter": "hp"},
            {"meter": "hp", "below": 0.2, "above": 0.8},
        ):
            with self.subTest(block=block), self.assertRaisesRegex(
                MeterConditionError, "exactly one"
            ):
                parse_meter_condition(block, {"hp"})

    def test_rejects_unknown_meter_and_fields(self) -> None:
        with self.assertRaisesRegex(MeterConditionError, "unknown meter"):
            parse_meter_condition({"meter": "mana", "below": 0.2}, {"hp"})
        with self.assertRaisesRegex(MeterConditionError, "unknown field"):
            parse_meter_condition(
                {"meter": "hp", "below": 0.2, "retry": True},
                {"hp"},
            )

    def test_amount_and_confidence_are_bounded(self) -> None:
        for block in (
            {"meter": "hp", "below": -0.1},
            {"meter": "hp", "above": 1.1},
            {"meter": "hp", "rises": True},
            {"meter": "hp", "falls": float("inf")},
            {"meter": "hp", "below": 0.2, "min_confidence": -0.1},
            {"meter": "hp", "below": 0.2, "min_confidence": 1.1},
        ):
            with self.subTest(block=block), self.assertRaises(MeterConditionError):
                parse_meter_condition(block, {"hp"})


class AcceptedMeterValueTests(unittest.TestCase):
    def observation(
        self,
        *,
        value: object = 0.5,
        visible: bool = True,
        confidence: float = 0.9,
        observed_at: float = 10.0,
    ) -> Observation:
        return Observation(
            name="hp",
            visible=visible,
            confidence=confidence,
            value=value,  # type: ignore[arg-type]
            observed_at=observed_at,
            source="vision:resource_bar",
        )

    def test_accepts_valid_normalized_value(self) -> None:
        self.assertEqual(
            accepted_meter_value(
                self.observation(),
                min_confidence=0.8,
                now=10.5,
                max_age_seconds=1.0,
            ),
            0.5,
        )

    def test_invalid_observations_fail_closed(self) -> None:
        cases = [
            None,
            self.observation(visible=False),
            self.observation(confidence=0.7),
            self.observation(value=None),
            self.observation(value=True),
            self.observation(value=-0.1),
            self.observation(value=1.1),
            self.observation(value=float("nan")),
        ]
        for observation in cases:
            with self.subTest(observation=observation):
                self.assertIsNone(
                    accepted_meter_value(
                        observation,
                        min_confidence=0.8,
                        now=10.5,
                        max_age_seconds=1.0,
                    )
                )

    def test_stale_future_and_pre_boundary_samples_fail_closed(self) -> None:
        self.assertIsNone(
            accepted_meter_value(
                self.observation(observed_at=8.0),
                min_confidence=0.8,
                now=10.0,
                max_age_seconds=1.0,
            )
        )
        self.assertIsNone(
            accepted_meter_value(
                self.observation(observed_at=11.0),
                min_confidence=0.8,
                now=10.0,
                max_age_seconds=1.0,
            )
        )
        self.assertIsNone(
            accepted_meter_value(
                self.observation(observed_at=10.0),
                min_confidence=0.8,
                after=10.0,
            )
        )


class MeterConditionEvaluationTests(unittest.TestCase):
    def test_thresholds_are_strict(self) -> None:
        self.assertTrue(
            condition_matches_value(MeterCondition("hp", "below", 0.3), 0.29)
        )
        self.assertFalse(
            condition_matches_value(MeterCondition("hp", "below", 0.3), 0.3)
        )
        self.assertTrue(
            condition_matches_value(MeterCondition("hp", "above", 0.7), 0.71)
        )
        self.assertFalse(
            condition_matches_value(MeterCondition("hp", "above", 0.7), 0.7)
        )

    def test_change_conditions_require_a_valid_baseline(self) -> None:
        rise = MeterCondition("hp", "rises", 0.2)
        fall = MeterCondition("hp", "falls", 0.2)
        self.assertFalse(condition_matches_value(rise, 0.8))
        self.assertFalse(condition_matches_value(rise, 0.8, baseline=-0.1))
        self.assertFalse(condition_matches_value(rise, 1.1, baseline=0.5))
        self.assertTrue(condition_matches_value(rise, 0.8, baseline=0.5))
        self.assertFalse(condition_matches_value(rise, 0.69, baseline=0.5))
        self.assertTrue(condition_matches_value(fall, 0.3, baseline=0.6))
        self.assertFalse(condition_matches_value(fall, 0.41, baseline=0.6))

    def test_game_state_helper_fails_closed_and_matches(self) -> None:
        state = GameState()
        condition = MeterCondition("hp", "below", 0.3, min_confidence=0.8)

        self.assertFalse(
            meter_condition_met(
                state,
                condition,
                now=10.0,
                max_age_seconds=1.0,
            )
        )

        state.update_detector(
            "hp",
            visible=True,
            confidence=0.95,
            value=0.25,
            observed_at=9.5,
            source="vision:resource_bar",
        )
        self.assertTrue(
            meter_condition_met(
                state,
                condition,
                now=10.0,
                max_age_seconds=1.0,
            )
        )
        self.assertEqual(
            current_meter_value(
                state,
                condition,
                now=10.0,
                max_age_seconds=1.0,
            ),
            0.25,
        )


if __name__ == "__main__":
    unittest.main()
