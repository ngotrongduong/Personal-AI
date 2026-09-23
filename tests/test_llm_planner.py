from __future__ import annotations

import unittest

from agent.game_state import GameState
from agent.llm_planner import LlmPlanner
from agent.ollama_client import OllamaError, OllamaErrorKind, OllamaResult
from agent.rule_engine import RuleEngine, VisibilityRule


class FakeOllamaClient:
    def __init__(self, result: OllamaResult) -> None:
        self._result = result
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> OllamaResult:
        self.prompts.append(prompt)
        return self._result


class LlmPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collect_rule = VisibilityRule(
            name="click_collect",
            detector_name="collect_button",
            action="click_detector_center",
        )
        self.heal_rule = VisibilityRule(
            name="heal_low_hp",
            detector_name="heal_button",
            action="click_detector_center",
        )
        self.engine = RuleEngine([self.collect_rule, self.heal_rule])
        self.state = GameState()
        self.state.update_detector("collect_button", visible=True, confidence=0.95, value="available")

    def _planner(self, result: OllamaResult) -> tuple[LlmPlanner, FakeOllamaClient]:
        client = FakeOllamaClient(result)
        return LlmPlanner(client, self.engine), client

    def test_valid_enable_directive_enables_disabled_rule(self) -> None:
        self.engine.disable_rule("click_collect")
        planner, _client = self._planner(
            OllamaResult(text='{"type":"enable_rule","rule_name":"click_collect"}')
        )

        outcome = planner.plan_once(self.state)

        self.assertTrue(self.engine.is_rule_enabled("click_collect"))
        self.assertEqual(outcome.message, "enabled click_collect")
        self.assertTrue(outcome.changed)

    def test_valid_disable_directive_disables_rule(self) -> None:
        planner, _client = self._planner(
            OllamaResult(text='{"type":"disable_rule","rule_name":"click_collect"}')
        )

        outcome = planner.plan_once(self.state)

        self.assertFalse(self.engine.is_rule_enabled("click_collect"))
        self.assertEqual(outcome.message, "disabled click_collect")
        self.assertTrue(outcome.changed)

    def test_noop_keeps_rule_state_unchanged(self) -> None:
        self.engine.disable_rule("heal_low_hp")
        planner, _client = self._planner(OllamaResult(text='{"type":"noop"}'))

        outcome = planner.plan_once(self.state)

        self.assertTrue(self.engine.is_rule_enabled("click_collect"))
        self.assertFalse(self.engine.is_rule_enabled("heal_low_hp"))
        self.assertEqual(outcome.message, "noop")
        self.assertFalse(outcome.changed)

    def test_unsuccessful_ollama_result_keeps_rule_state_and_reports_error(self) -> None:
        error = OllamaError(OllamaErrorKind.CONNECTION, "Ollama is not running.")
        planner, _client = self._planner(OllamaResult(error=error))

        outcome = planner.plan_once(self.state)

        self.assertTrue(self.engine.is_rule_enabled("click_collect"))
        self.assertTrue(self.engine.is_rule_enabled("heal_low_hp"))
        self.assertIn("no change, Ollama error:", outcome.message)
        self.assertIn(error.message, outcome.message)
        self.assertFalse(outcome.changed)

    def test_malformed_directive_keeps_rule_state_and_reports_rejection(self) -> None:
        planner, _client = self._planner(OllamaResult(text="not valid JSON"))

        outcome = planner.plan_once(self.state)

        self.assertTrue(self.engine.is_rule_enabled("click_collect"))
        self.assertTrue(self.engine.is_rule_enabled("heal_low_hp"))
        self.assertIn("no change, rejected:", outcome.message)
        self.assertIn("not valid JSON", outcome.message)
        self.assertFalse(outcome.changed)

    def test_unknown_rule_directive_is_rejected_without_changing_rule_state(self) -> None:
        planner, _client = self._planner(
            OllamaResult(text='{"type":"enable_rule","rule_name":"invented_rule"}')
        )

        outcome = planner.plan_once(self.state)

        self.assertTrue(self.engine.is_rule_enabled("click_collect"))
        self.assertTrue(self.engine.is_rule_enabled("heal_low_hp"))
        self.assertIn("no change, rejected:", outcome.message)
        self.assertIn("unknown rule", outcome.message)
        self.assertFalse(outcome.changed)

    def test_prompt_summarizes_game_state_and_current_rule_settings(self) -> None:
        self.engine.disable_rule("heal_low_hp")
        planner, client = self._planner(OllamaResult(text='{"type":"noop"}'))

        planner.plan_once(self.state)

        prompt = client.prompts[0]
        self.assertIn("collect_button", prompt)
        self.assertIn("visible=True", prompt)
        self.assertIn("click_collect: enabled", prompt)
        self.assertIn("heal_low_hp: disabled", prompt)


if __name__ == "__main__":
    unittest.main()
