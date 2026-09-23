from __future__ import annotations

import json
import threading
import time
import unittest

from agent.game_state import GameState
from agent.llm_planner import LlmPlanner
from agent.ollama_client import OllamaClient, OllamaClientConfig
from agent.planner_scheduler import PlannerScheduler
from agent.rule_engine import RuleEngine, VisibilityRule


class PlannerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RuleEngine(
            [
                VisibilityRule(
                    name="click_collect",
                    detector_name="collect_button",
                    action="click_detector_center",
                ),
                VisibilityRule(
                    name="heal_low_hp",
                    detector_name="heal_button",
                    action="click_detector_center",
                ),
            ]
        )
        self.state = GameState()
        self.state.update_detector("collect_button", visible=True, confidence=0.95, value="available")
        self.schedulers: list[PlannerScheduler] = []

    def tearDown(self) -> None:
        for scheduler in self.schedulers:
            scheduler.stop()

    def _scheduler(self, transport: object) -> PlannerScheduler:
        client = OllamaClient(OllamaClientConfig(model="test"), transport=transport)
        scheduler = PlannerScheduler(LlmPlanner(client, self.engine), self.state, interval_seconds=0.02)
        self.schedulers.append(scheduler)
        return scheduler

    def _rule_enabled_state(self) -> tuple[tuple[str, bool], ...]:
        return tuple((rule.name, self.engine.is_rule_enabled(rule.name)) for rule in self.engine.rules)

    def _wait_until(self, predicate: object, *, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    def test_valid_directive_changes_rule_state_and_is_logged_from_scheduler_thread(self) -> None:
        transport_called = threading.Event()

        def transport(_request: object, _timeout: float) -> bytes:
            transport_called.set()
            return json.dumps(
                {"response": '{"type":"disable_rule","rule_name":"click_collect"}'}
            ).encode("utf-8")

        scheduler = self._scheduler(transport)

        with self.assertLogs("agent.planner_scheduler", level="INFO") as logs:
            self.assertTrue(scheduler.start())
            self.assertTrue(transport_called.wait(timeout=1.0))
            self.assertTrue(self._wait_until(lambda: not self.engine.is_rule_enabled("click_collect")))
            scheduler.stop()

        self.assertFalse(self.engine.is_rule_enabled("click_collect"))
        self.assertTrue(any("disabled click_collect" in message for message in logs.output))

    def test_connection_failures_preserve_rule_state_and_keep_scheduler_running(self) -> None:
        calls = 0
        calls_lock = threading.Lock()
        three_calls = threading.Event()

        def transport(_request: object, _timeout: float) -> bytes:
            nonlocal calls
            with calls_lock:
                calls += 1
                if calls >= 3:
                    three_calls.set()
            raise ConnectionRefusedError("Ollama is not running")

        self.engine.disable_rule("heal_low_hp")
        state_before = self._rule_enabled_state()
        scheduler = self._scheduler(transport)

        with self.assertLogs("agent.planner_scheduler", level="INFO") as logs:
            self.assertTrue(scheduler.start())
            self.assertTrue(three_calls.wait(timeout=1.0))
            self.assertTrue(scheduler.is_running)
            scheduler.stop()

        self.assertEqual(self._rule_enabled_state(), state_before)
        self.assertTrue(any("Ollama error" in message for message in logs.output))

    def test_action_smuggling_directive_is_rejected_without_changing_rule_state(self) -> None:
        transport_called = threading.Event()

        def transport(_request: object, _timeout: float) -> bytes:
            transport_called.set()
            return json.dumps({"response": '{"type":"press_key","key":"space"}'}).encode("utf-8")

        self.engine.disable_rule("heal_low_hp")
        state_before = self._rule_enabled_state()
        scheduler = self._scheduler(transport)

        with self.assertLogs("agent.planner_scheduler", level="INFO") as logs:
            self.assertTrue(scheduler.start())
            self.assertTrue(transport_called.wait(timeout=1.0))
            self.assertTrue(self._wait_until(lambda: any("rejected" in message for message in logs.output)))
            scheduler.stop()

        self.assertEqual(self._rule_enabled_state(), state_before)
        self.assertTrue(any("rejected" in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()
