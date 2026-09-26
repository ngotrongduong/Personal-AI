from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agent.agent_session import (
    GOAL_FRESH_SECONDS,
    STOP_GOAL,
    AgentRun,
    GoalCondition,
    GoalConditionError,
    MeterGoalCondition,
    RunBudget,
    parse_goal_condition,
)
from agent.game_state import GameState
from agent.meter_conditions import MeterCondition
from agent.ollama_client import OllamaClientConfig
from agent.planner_config import PlannerConfig
from agent.profile import (
    PROFILE_FILENAME,
    MeterDefinition,
    ProfileError,
    load_profile,
    save_profile,
)
from vision.resource_bar import HSVRange


def _meter_block() -> dict[str, object]:
    return {
        "name": "hp",
        "roi": [0, 0, 100, 10],
        "hsv_ranges": [
            {"lower": [50, 100, 100], "upper": [80, 255, 255]}
        ],
        "min_confidence": 0.8,
    }


def _profile(stop_when: object) -> dict[str, object]:
    return {
        "format_version": 1,
        "name": "Meter goal",
        "permissions": {"allowed_keys": []},
        "detectors": [],
        "meters": [_meter_block()],
        "skills": [],
        "rules": [],
        "planner": {
            "enabled": False,
            "stop_when": stop_when,
        },
    }


class MeterGoalParseTests(unittest.TestCase):
    def test_detector_goal_is_unchanged(self) -> None:
        self.assertEqual(
            parse_goal_condition({"detector": "done", "visible": False}),
            GoalCondition("done", False),
        )

    def test_meter_goal_parses(self) -> None:
        self.assertEqual(
            parse_goal_condition(
                {"meter": "hp", "below": 0.2, "min_confidence": 0.9}
            ),
            MeterGoalCondition(MeterCondition("hp", "below", 0.2, 0.9)),
        )

    def test_ambiguous_and_invalid_meter_goal_is_rejected(self) -> None:
        cases = [
            ({"meter": "hp"}, "exactly one"),
            ({"meter": "hp", "below": 0.2, "above": 0.8}, "exactly one"),
            (
                {"meter": "hp", "detector": "done", "below": 0.2},
                "exactly one",
            ),
            ({"meter": "hp", "below": 0.2, "extra": 1}, "unknown field"),
        ]
        for block, fragment in cases:
            with self.subTest(block=block), self.assertRaisesRegex(
                GoalConditionError, fragment
            ):
                parse_goal_condition(block)


class MeterAgentRunTests(unittest.TestCase):
    def update(
        self,
        state: GameState,
        value: float,
        at: float,
        *,
        confidence: float = 0.95,
        visible: bool = True,
    ) -> None:
        state.update_detector(
            "hp",
            visible=visible,
            confidence=confidence,
            value=value,
            observed_at=at,
            source="vision:resource_bar",
        )

    def test_threshold_goal_needs_fresh_post_start_reading(self) -> None:
        state = GameState()
        run = AgentRun(
            RunBudget(60.0, 100.0),
            MeterGoalCondition(MeterCondition("hp", "below", 0.3)),
        )

        self.update(state, 0.2, 99.9)
        self.assertIsNone(run.stop_reason(state, 100.2))

        self.update(state, 0.2, 100.5)
        self.assertEqual(run.stop_reason(state, 100.6), STOP_GOAL)

    def test_stale_or_low_confidence_goal_fails_closed(self) -> None:
        state = GameState()
        run = AgentRun(
            RunBudget(60.0, 100.0),
            MeterGoalCondition(
                MeterCondition("hp", "below", 0.3, min_confidence=0.9)
            ),
        )

        self.update(state, 0.2, 100.2, confidence=0.4)
        self.assertIsNone(run.stop_reason(state, 100.3))

        self.update(state, 0.2, 100.4)
        self.assertIsNone(
            run.stop_reason(state, 100.4 + GOAL_FRESH_SECONDS + 0.1)
        )

    def test_change_goal_captures_first_valid_post_start_baseline(self) -> None:
        state = GameState()
        run = AgentRun(
            RunBudget(60.0, 100.0),
            MeterGoalCondition(MeterCondition("hp", "rises", 0.2)),
        )

        self.update(state, 0.4, 100.2)
        self.assertIsNone(run.stop_reason(state, 100.3))

        self.update(state, 0.55, 100.5)
        self.assertIsNone(run.stop_reason(state, 100.6))

        self.update(state, 0.61, 100.8)
        self.assertEqual(run.stop_reason(state, 100.9), STOP_GOAL)

    def test_invalid_sample_never_becomes_change_baseline(self) -> None:
        state = GameState()
        run = AgentRun(
            RunBudget(60.0, 100.0),
            MeterGoalCondition(
                MeterCondition("hp", "falls", 0.2, min_confidence=0.9)
            ),
        )

        self.update(state, 0.8, 100.2, confidence=0.2)
        self.assertIsNone(run.stop_reason(state, 100.3))

        self.update(state, 0.7, 100.4)
        self.assertIsNone(run.stop_reason(state, 100.5))

        self.update(state, 0.49, 100.7)
        self.assertEqual(run.stop_reason(state, 100.8), STOP_GOAL)


class MeterGoalProfileTests(unittest.TestCase):
    def test_profile_accepts_declared_meter_stop_when(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / PROFILE_FILENAME).write_text(
                json.dumps(_profile({"meter": "hp", "below": 0.25})),
                encoding="utf-8",
            )
            profile = load_profile(folder)

        self.assertEqual(
            profile.planner.stop_when,
            MeterGoalCondition(MeterCondition("hp", "below", 0.25)),
        )

    def test_profile_rejects_unknown_meter_stop_when(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / PROFILE_FILENAME).write_text(
                json.dumps(_profile({"meter": "mana", "below": 0.25})),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfileError, "unknown meter"):
                load_profile(folder)

    def test_save_round_trip_meter_stop_when(self) -> None:
        meter = MeterDefinition(
            name="hp",
            roi=(0, 0, 100, 10),
            hsv_ranges=(HSVRange((50, 100, 100), (80, 255, 255)),),
        )
        goal = MeterGoalCondition(MeterCondition("hp", "above", 0.9, 0.85))
        planner = PlannerConfig(
            enabled=False,
            ollama=OllamaClientConfig(model="qwen3.5:9b"),
            stop_when=goal,
        )

        with tempfile.TemporaryDirectory() as tmp:
            folder = save_profile(
                tmp,
                "Meter Goal",
                meters=[meter],
                planner=planner,
            )
            loaded = load_profile(folder)
            raw = json.loads(
                (folder / PROFILE_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(loaded.planner.stop_when, goal)
        self.assertEqual(raw["planner"]["stop_when"], goal.to_block())


if __name__ == "__main__":
    unittest.main()
