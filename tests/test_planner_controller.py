from __future__ import annotations

import unittest

from agent.game_state import GameState
from agent.llm_planner import PlannerCancelledError
from agent.ollama_client import OllamaClientConfig
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.rule_engine import RuleEngine, VisibilityRule


class FakeClient:
    def __init__(self, config: OllamaClientConfig) -> None:
        self.config = config


class FakeScheduler:
    def __init__(self, planner: object, state: GameState, *, interval_seconds: float) -> None:
        self.planner = planner
        self.state = state
        self.interval_seconds = interval_seconds
        self.started = False
        self.stopped = False

    @property
    def is_running(self) -> bool:
        return self.started and not self.stopped

    def start(self) -> bool:
        self.started = True
        return True

    def stop(self, *, join_timeout: float = 1.0) -> None:
        self.stopped = True
        self.join_timeout = join_timeout


class PlannerControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.schedulers: list[FakeScheduler] = []
        self.controller = PlannerController(
            self.state,
            client_factory=FakeClient,
            scheduler_factory=self._make_scheduler,
        )
        self.engine = RuleEngine(
            [
                VisibilityRule(
                    name="click_collect",
                    detector_name="collect_button",
                    action="click_detector_center",
                )
            ]
        )
        self.config = PlannerConfig(
            enabled=True,
            ollama=OllamaClientConfig(model="test-model"),
            interval_seconds=2.5,
        )

    def _make_scheduler(
        self,
        planner: object,
        state: GameState,
        *,
        interval_seconds: float,
    ) -> FakeScheduler:
        scheduler = FakeScheduler(planner, state, interval_seconds=interval_seconds)
        self.schedulers.append(scheduler)
        return scheduler

    def test_disabled_config_does_not_create_scheduler(self) -> None:
        disabled = PlannerConfig()

        self.assertFalse(self.controller.start(self.engine, disabled))

        self.assertEqual(self.schedulers, [])
        self.assertFalse(self.controller.is_running)

    def test_enabled_config_creates_and_starts_scheduler(self) -> None:
        self.assertTrue(self.controller.start(self.engine, self.config))

        scheduler = self.schedulers[0]
        self.assertIs(scheduler.planner._rule_engine._rule_engine, self.engine)
        self.assertIs(scheduler.state, self.state)
        self.assertEqual(scheduler.interval_seconds, 2.5)
        self.assertTrue(scheduler.started)
        self.assertTrue(self.controller.is_running)

    def test_start_twice_stops_previous_scheduler(self) -> None:
        self.controller.start(self.engine, self.config)
        first = self.schedulers[0]

        self.controller.start(self.engine, self.config)

        self.assertTrue(first.stopped)
        self.assertEqual(len(self.schedulers), 2)
        self.assertTrue(self.controller.is_running)

    def test_stop_is_idempotent_and_updates_running_state(self) -> None:
        self.controller.stop()
        self.controller.start(self.engine, self.config)
        scheduler = self.schedulers[0]

        self.controller.stop()
        self.controller.stop()

        self.assertTrue(scheduler.stopped)
        self.assertFalse(self.controller.is_running)

    def test_stop_requests_non_blocking_scheduler_shutdown(self) -> None:
        self.controller.start(self.engine, self.config)
        scheduler = self.schedulers[0]

        self.controller.stop()

        self.assertEqual(scheduler.join_timeout, 0.0)

    def test_cancelled_old_rule_control_cannot_change_rules_after_stop(self) -> None:
        self.controller.start(self.engine, self.config)
        old_rule_control = self.schedulers[0].planner._rule_engine

        self.controller.stop()
        with self.assertRaises(PlannerCancelledError):
            old_rule_control.disable_rule("click_collect")

        self.assertTrue(self.engine.is_rule_enabled("click_collect"))

        self.engine.disable_rule("click_collect")
        with self.assertRaises(PlannerCancelledError):
            old_rule_control.enable_rule("click_collect")

        self.assertFalse(self.engine.is_rule_enabled("click_collect"))

    def test_restart_uses_a_fresh_uncancelled_rule_control(self) -> None:
        self.controller.start(self.engine, self.config)
        old_rule_control = self.schedulers[0].planner._rule_engine
        self.controller.stop()

        self.controller.start(self.engine, self.config)
        new_rule_control = self.schedulers[1].planner._rule_engine
        with self.assertRaises(PlannerCancelledError):
            old_rule_control.disable_rule("click_collect")
        new_rule_control.disable_rule("click_collect")

        self.assertIsNot(old_rule_control, new_rule_control)
        self.assertFalse(self.engine.is_rule_enabled("click_collect"))


if __name__ == "__main__":
    unittest.main()
