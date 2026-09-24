"""v0.7: run_skill directives, the closed-loop prompt and the proposal sink."""

from __future__ import annotations

import json
import unittest

from agent.game_state import GameState
from agent.llm_planner import LlmPlanner, SkillBookCatalog
from agent.llm_planner_schema import (
    DirectiveValidationError,
    RunSkillDirective,
    parse_directive,
)
from agent.ollama_client import OllamaError, OllamaErrorKind, OllamaResult
from agent.rule_engine import RuleEngine, VisibilityRule
from agent.skills import ClickSkill, HoldSkill, PressSkill, SkillBook, SkillPermissions
from agent.step_history import StepHistory, StepRecord


class FakeClient:
    def __init__(self, text: str | None = None, error: OllamaError | None = None) -> None:
        self._result = OllamaResult(error=error) if error else OllamaResult(text=text)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> OllamaResult:
        self.prompts.append(prompt)
        return self._result


class FakeSink:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.proposals: list[tuple[str, str]] = []

    def propose(self, skill_name: str, reason: str) -> bool:
        self.proposals.append((skill_name, reason))
        return self.accept


def _run_skill(skill: str, reason: str = "status bar is visible") -> str:
    return json.dumps({"type": "run_skill", "skill": skill, "reason": reason})


class RunSkillSchemaTests(unittest.TestCase):
    def test_accepts_runnable_skill(self) -> None:
        directive = parse_directive(_run_skill("type_x"), set(), {"type_x"})

        self.assertEqual(directive, RunSkillDirective("type_x", "status bar is visible"))

    def test_rejects_run_skill_by_default(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "unknown or disabled"):
            parse_directive(_run_skill("type_x"), set())

    def test_rejects_unknown_or_disabled_skill(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "unknown or disabled"):
            parse_directive(_run_skill("hold_space"), set(), {"type_x"})

    def test_rejects_smuggled_input_fields(self) -> None:
        for extra in ({"key": "f8"}, {"x": 10, "y": 20}, {"seconds": 5}, {"detector": "d"}):
            raw = json.dumps({"type": "run_skill", "skill": "type_x", "reason": "r", **extra})
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(DirectiveValidationError, "fields must be exactly"):
                    parse_directive(raw, set(), {"type_x"})

    def test_rejects_missing_reason_or_skill(self) -> None:
        for raw in ('{"type":"run_skill","skill":"type_x"}', '{"type":"run_skill","reason":"r"}'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(DirectiveValidationError, "fields must be exactly"):
                    parse_directive(raw, set(), {"type_x"})

    def test_rejects_bad_skill_and_reason_types(self) -> None:
        cases = [
            ('{"type":"run_skill","skill":5,"reason":"r"}', "'skill' must be a non-empty string"),
            ('{"type":"run_skill","skill":"","reason":"r"}', "'skill' must be a non-empty string"),
            ('{"type":"run_skill","skill":"type_x","reason":7}', "'reason' must be a string"),
            ('{"type":"run_skill","skill":"type_x","reason":"  "}', "cannot be empty"),
            (_run_skill("type_x", "y" * 201), "longer than 200"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw[:60]):
                with self.assertRaisesRegex(DirectiveValidationError, message):
                    parse_directive(raw, set(), {"type_x"})

    def test_reason_control_characters_become_spaces(self) -> None:
        directive = parse_directive(_run_skill("type_x", "a\nb\tc\x00"), set(), {"type_x"})

        self.assertEqual(directive.reason, "a b c")

    def test_skill_name_is_case_sensitive(self) -> None:
        with self.assertRaises(DirectiveValidationError):
            parse_directive(_run_skill("TYPE_X"), set(), {"type_x"})


class SkillBookCatalogTests(unittest.TestCase):
    def test_lists_only_enabled_skills_with_details(self) -> None:
        book = SkillBook(
            [
                ClickSkill("press_ok", "ok_button", enabled=True),
                PressSkill("type_x", "x", enabled=True),
                HoldSkill("hold_space", "space", 1.0),
            ],
            SkillPermissions(allowed_keys=frozenset({"x", "space"}), max_hold_seconds=1.5),
        )
        catalog = SkillBookCatalog(book)

        self.assertEqual(
            [(s.name, s.type, s.detail) for s in catalog.runnable_skills()],
            [("press_ok", "click", "clicks detector ok_button"), ("type_x", "press", "presses key x")],
        )

        book.set_enabled("hold_space", True)
        self.assertIn(
            ("hold_space", "hold", "holds key space for 1s"),
            [(s.name, s.type, s.detail) for s in catalog.runnable_skills()],
        )


class ClosedLoopPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RuleEngine([VisibilityRule("click_ok", "ok_button", "click")])
        self.book = SkillBook(
            [PressSkill("type_x", "x", enabled=True), HoldSkill("hold_space", "space", 1.0)],
            SkillPermissions(allowed_keys=frozenset({"x", "space"}), max_hold_seconds=1.5),
        )
        self.state = GameState()
        self.state.update_detector("status_bar", visible=True, confidence=0.99)
        self.history = StepHistory()
        self.sink = FakeSink()

    def _planner(self, client: FakeClient, **overrides) -> LlmPlanner:
        options = dict(
            skills=SkillBookCatalog(self.book),
            history=self.history,
            goal="Type an x when the status bar shows.",
            proposals=self.sink,
        )
        options.update(overrides)
        return LlmPlanner(client, self.engine, **options)

    def _rules(self) -> tuple[tuple[str, bool], ...]:
        return tuple((r.name, self.engine.is_rule_enabled(r.name)) for r in self.engine.rules)

    def test_run_skill_is_posted_as_a_proposal_only(self) -> None:
        before = self._rules()
        outcome = self._planner(FakeClient(_run_skill("type_x"))).plan_once(self.state)

        self.assertEqual(self.sink.proposals, [("type_x", "status bar is visible")])
        self.assertEqual(outcome.message, "proposed type_x: status bar is visible")
        self.assertFalse(outcome.changed)
        self.assertEqual(self._rules(), before)

    def test_full_mailbox_drops_the_proposal(self) -> None:
        self.sink.accept = False

        outcome = self._planner(FakeClient(_run_skill("type_x"))).plan_once(self.state)

        self.assertIn("dropped proposal type_x", outcome.message)

    def test_disabled_skill_is_rejected_and_not_posted(self) -> None:
        outcome = self._planner(FakeClient(_run_skill("hold_space"))).plan_once(self.state)

        self.assertIn("no change, rejected:", outcome.message)
        self.assertEqual(self.sink.proposals, [])

    def test_without_a_sink_run_skill_is_rejected(self) -> None:
        outcome = self._planner(FakeClient(_run_skill("type_x")), proposals=None).plan_once(self.state)

        self.assertIn("no change, rejected:", outcome.message)
        self.assertEqual(self.sink.proposals, [])

    def test_errors_fail_closed_without_proposals(self) -> None:
        clients = [
            FakeClient(error=OllamaError(OllamaErrorKind.CONNECTION, "down")),
            FakeClient("not json"),
            FakeClient('{"type":"run_skill","skill":"type_x","reason":"r","key":"f8"}'),
        ]
        for client in clients:
            with self.subTest(client=client):
                before = self._rules()
                outcome = self._planner(client).plan_once(self.state)
                self.assertTrue(outcome.message.startswith("no change"))
                self.assertEqual(self.sink.proposals, [])
                self.assertEqual(self._rules(), before)

    def test_prompt_has_goal_enabled_skills_history_and_shapes(self) -> None:
        self.history.append(StepRecord("type_x", "r", "auto", "Pressed 'x'.", True, 0.0))
        client = FakeClient('{"type":"noop"}')

        self._planner(client).plan_once(self.state)
        prompt = client.prompts[0]

        self.assertIn("Goal: Type an x when the status bar shows.", prompt)
        self.assertIn("- type_x (press): presses key x", prompt)
        self.assertNotIn("hold_space", prompt)
        self.assertIn("Recent steps (oldest first):", prompt)
        self.assertIn("- type_x: auto, Pressed 'x'.", prompt)
        self.assertIn('"type": "run_skill"', prompt)
        self.assertIn('{"type": "noop"}', prompt)

    def test_prompt_without_runnable_skills_omits_run_skill_shape(self) -> None:
        self.book.set_enabled("type_x", False)
        client = FakeClient('{"type":"noop"}')

        self._planner(client, goal="").plan_once(self.state)
        prompt = client.prompts[0]

        self.assertIn("Goal: (no goal set)", prompt)
        self.assertNotIn("run_skill", prompt)
        self.assertNotIn("Skills you may propose", prompt)

    def test_goal_callable_is_read_each_cycle_and_sanitized(self) -> None:
        goals = iter(["first\ngoal", "x" * 900])
        client = FakeClient('{"type":"noop"}')
        planner = self._planner(client, goal=lambda: next(goals))

        planner.plan_once(self.state)
        planner.plan_once(self.state)

        self.assertIn("Goal: first goal", client.prompts[0])
        goal_line = next(line for line in client.prompts[1].splitlines() if line.startswith("Goal: "))
        self.assertEqual(len(goal_line), len("Goal: ") + 500)

    def test_skill_disabled_after_prompt_is_still_rejected_at_parse(self) -> None:
        # The runnable set is captured with the prompt; a skill that was not
        # enabled then cannot be proposed even if the model names it.
        client = FakeClient(_run_skill("hold_space"))

        outcome = self._planner(client).plan_once(self.state)

        self.assertIn("unknown or disabled", outcome.message)


if __name__ == "__main__":
    unittest.main()
