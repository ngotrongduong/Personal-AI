"""Safe, synchronous bridge from validated Ollama directives to rule settings."""

from __future__ import annotations

from dataclasses import dataclass

from .game_state import GameState, Observation
from .llm_planner_schema import (
    DirectiveValidationError,
    DisableRuleDirective,
    EnableRuleDirective,
    NoopDirective,
    parse_directive,
)
from .ollama_client import OllamaClient
from .rule_engine import RuleEngine


@dataclass(frozen=True, slots=True)
class PlannerOutcome:
    """The safe result of one planner cycle."""

    message: str
    changed: bool = False


class LlmPlanner:
    """Ask Ollama for one reviewed directive without directly producing input."""

    def __init__(self, ollama_client: OllamaClient, rule_engine: RuleEngine) -> None:
        self._ollama_client = ollama_client
        self._rule_engine = rule_engine

    def plan_once(self, state: GameState) -> PlannerOutcome:
        """Apply one schema-valid directive, failing closed on every error path.

        Every supported failure path here--client failures, missing response
        text, and directive-validation failures--fails closed by retaining the
        exact current rule configuration; this contract is covered by
        ``tests/test_llm_planner.py``.
        """

        result = self._ollama_client.generate(self._build_prompt(state))
        if not result.successful:
            return PlannerOutcome(f"no change, Ollama error: {result.error}")

        response_text = result.text
        if response_text is None:
            return PlannerOutcome("no change, rejected: Ollama returned no directive text.")

        known_rule_names = {rule.name for rule in self._rule_engine.rules}
        try:
            directive = parse_directive(response_text, known_rule_names)
        except DirectiveValidationError as error:
            return PlannerOutcome(f"no change, rejected: {error}")

        if isinstance(directive, EnableRuleDirective):
            self._rule_engine.enable_rule(directive.rule_name)
            return PlannerOutcome(f"enabled {directive.rule_name}", changed=True)
        if isinstance(directive, DisableRuleDirective):
            self._rule_engine.disable_rule(directive.rule_name)
            return PlannerOutcome(f"disabled {directive.rule_name}", changed=True)
        if isinstance(directive, NoopDirective):
            return PlannerOutcome("noop")

        raise AssertionError(f"Unhandled validated planner directive: {directive!r}")

    def _build_prompt(self, state: GameState) -> str:
        observations = state.snapshot()
        observation_lines = _format_observations(observations)
        rule_lines = [
            f"- {rule.name}: {'enabled' if self._rule_engine.is_rule_enabled(rule.name) else 'disabled'}"
            for rule in self._rule_engine.rules
        ]

        return "\n".join(
            [
                "Choose one safe rule-configuration directive for this game state.",
                "Game state observations:",
                *observation_lines,
                "Current rules:",
                *rule_lines,
                "Respond with one JSON directive: enable_rule, disable_rule, or noop.",
            ]
        )


def _format_observations(observations: dict[str, Observation]) -> list[str]:
    if not observations:
        return ["- none"]
    return [
        (
            f"- {name}: visible={observation.visible}, confidence={observation.confidence:.3f}, "
            f"bbox={observation.bbox!r}, value={observation.value!r}, source={observation.source!r}"
        )
        for name, observation in observations.items()
    ]
