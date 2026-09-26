from __future__ import annotations

import unittest

from agent.game_state import Observation
from agent.llm_planner import _format_observations


class MeterPlannerPromptTests(unittest.TestCase):
    def test_fresh_accepted_meter_is_a_percentage(self) -> None:
        observation = Observation(
            name="hp",
            visible=True,
            confidence=0.96,
            value=0.42,
            observed_at=10.0,
            source="vision:resource_bar",
        )

        [line] = _format_observations({"hp": observation}, now=10.5)

        self.assertIn("- hp: 42%", line)
        self.assertIn("confidence=0.960", line)
        self.assertNotIn("value=0.42", line)

    def test_stale_meter_is_not_presented_as_a_trusted_value(self) -> None:
        observation = Observation(
            name="hp",
            visible=True,
            confidence=0.96,
            value=0.42,
            observed_at=8.0,
            source="vision:resource_bar",
        )

        [line] = _format_observations({"hp": observation}, now=10.0)

        self.assertIn("hp: unavailable (stale", line)
        self.assertNotIn("42%", line)

    def test_rejected_meter_is_unavailable(self) -> None:
        observation = Observation(
            name="hp",
            visible=False,
            confidence=0.50,
            value=None,
            observed_at=10.0,
            source="vision:resource_bar",
        )

        [line] = _format_observations({"hp": observation}, now=10.1)

        self.assertIn("hp: unavailable", line)
        self.assertIn("confidence=0.500", line)

    def test_non_meter_observation_format_is_unchanged(self) -> None:
        observation = Observation(
            name="button",
            visible=True,
            confidence=0.90,
            value="available",
            observed_at=10.0,
            source="vision:template",
        )

        [line] = _format_observations({"button": observation}, now=10.1)

        self.assertIn("visible=True", line)
        self.assertIn("value='available'", line)
        self.assertIn("source='vision:template'", line)


if __name__ == "__main__":
    unittest.main()
