"""Safe, synchronous bridge from validated Ollama directives to rule settings
and (from v0.7) skill proposals.

The planner never produces input. Rule toggles apply directly (they send no
input); a ``run_skill`` directive only becomes a proposal posted to a sink,
which the Tk thread approves, rejects or runs through the skill executor.

From v0.8 the prompt can carry notes from earlier sessions, and a
``remember`` directive can add one short note through a note sink. Notes are
prompt text only: they never change which skills or rules exist or are
runnable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import time
from typing import Protocol

from .game_state import GameState, Observation
from .meter_conditions import DEFAULT_METER_MIN_CONFIDENCE, METER_SOURCE, accepted_meter_value
from .meter_live import meter_percent_text
from .llm_planner_schema import (
    DirectiveValidationError,
    DisableRuleDirective,
    EnableRuleDirective,
    NoopDirective,
    RememberDirective,
    RunSkillDirective,
    parse_directive,
)
from .ollama_client import OllamaClient
from .rule_engine import RuleEngine
from .skills import ClickSkill, HoldSkill, PressSkill, SkillBook, TapSkill
from .step_history import StepHistory


MAX_GOAL_LENGTH = 500
# v1.1: a meter reading older than this is shown to the model as "unknown".
METER_PROMPT_FRESH_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class PlannerOutcome:
    """The safe result of one planner cycle."""

    message: str
    changed: bool = False


class PlannerCancelledError(RuntimeError):
    """Raised when shutdown discards an in-flight planner directive."""


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """What the prompt shows about one runnable skill."""

    name: str
    type: str
    detail: str


class SkillCatalog(Protocol):
    """Read-only view of the skills the planner may propose right now."""

    def runnable_skills(self) -> Sequence[SkillSummary]: ...


class ProposalSink(Protocol):
    """Where a validated ``run_skill`` goes. Returns False if a step is pending."""

    def propose(self, skill_name: str, reason: str) -> bool: ...


class NotesView(Protocol):
    """Read-only view of the notes shown in the prompt."""

    def prompt_lines(self) -> list[str]: ...


class NoteSink(Protocol):
    """Where a validated ``remember`` goes. Returns the outcome message."""

    def remember(self, text: str) -> str: ...


class SkillBookCatalog:
    """Expose a profile's currently *enabled* skills to the planner."""

    def __init__(self, book: SkillBook) -> None:
        self._book = book

    def runnable_skills(self) -> list[SkillSummary]:
        summaries: list[SkillSummary] = []
        for name in self._book.names:
            if not self._book.is_enabled(name):
                continue
            skill = self._book.get(name)
            if isinstance(skill, ClickSkill):
                summaries.append(SkillSummary(name, skill.TYPE, f"clicks detector {skill.detector}"))
            elif isinstance(skill, PressSkill):
                summaries.append(SkillSummary(name, skill.TYPE, f"presses key {skill.key}"))
            elif isinstance(skill, HoldSkill):
                summaries.append(
                    SkillSummary(name, skill.TYPE, f"holds key {skill.key} for {skill.seconds:g}s")
                )
            elif isinstance(skill, TapSkill):
                summaries.append(SkillSummary(name, skill.TYPE, describe_tap(skill)))
        return summaries


def describe_tap(skill: TapSkill) -> str:
    """e.g. "taps a fixed point (58%, 47%) when btn_wait visible, hp above 30%"."""

    text = f"taps a fixed point {skill.describe_point()}"
    if skill.requires:
        text += " when " + ", ".join(requirement.describe() for requirement in skill.requires)
    return text


class LlmPlanner:
    """Ask Ollama for one reviewed directive without directly producing input."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        rule_engine: RuleEngine,
        *,
        skills: SkillCatalog | None = None,
        history: StepHistory | None = None,
        goal: str | Callable[[], str] = "",
        proposals: ProposalSink | None = None,
        notes: NotesView | None = None,
        note_sink: NoteSink | None = None,
        meters: Mapping[str, float] | None = None,
    ) -> None:
        self._ollama_client = ollama_client
        # v1.1: profile meter name -> min_confidence, for the prompt only.
        self._meters = dict(meters or {})
        self._rule_engine = rule_engine
        self._skills = skills
        self._history = history
        self._goal = goal
        self._proposals = proposals
        self._notes = notes
        self._note_sink = note_sink

    def plan_once(self, state: GameState) -> PlannerOutcome:
        """Apply or propose one schema-valid directive, failing closed on every error path.

        Every supported failure path here--client failures, missing response
        text, and directive-validation failures--fails closed by retaining the
        exact current rule configuration and proposing nothing; this contract
        is covered by ``tests/test_llm_planner.py``.
        """

        runnable = self._runnable_skills()
        result = self._ollama_client.generate(self._build_prompt(state, runnable))
        if not result.successful:
            return PlannerOutcome(f"no change, Ollama error: {result.error}")

        response_text = result.text
        if response_text is None:
            return PlannerOutcome("no change, rejected: Ollama returned no directive text.")

        known_rule_names = {rule.name for rule in self._rule_engine.rules}
        runnable_names = {skill.name for skill in runnable}
        try:
            directive = parse_directive(
                response_text,
                known_rule_names,
                runnable_names,
                allow_notes=self._note_sink is not None,
            )
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
        if isinstance(directive, RunSkillDirective):
            # `runnable` is empty without a sink, so parsing already rejected it.
            assert self._proposals is not None
            if self._proposals.propose(directive.skill_name, directive.reason):
                return PlannerOutcome(f"proposed {directive.skill_name}: {directive.reason}")
            return PlannerOutcome(
                f"dropped proposal {directive.skill_name}: a step is already pending"
            )

        if isinstance(directive, RememberDirective):
            # Parsing rejects `remember` without a sink.
            assert self._note_sink is not None
            return PlannerOutcome(self._note_sink.remember(directive.note))

        raise AssertionError(f"Unhandled validated planner directive: {directive!r}")

    def _runnable_skills(self) -> list[SkillSummary]:
        if self._skills is None or self._proposals is None:
            return []
        return list(self._skills.runnable_skills())

    def _goal_text(self) -> str:
        goal = self._goal() if callable(self._goal) else self._goal
        if not isinstance(goal, str):
            return ""
        cleaned = "".join(ch if ch.isprintable() else " " for ch in goal).strip()
        return cleaned[:MAX_GOAL_LENGTH]

    def _build_prompt(self, state: GameState, runnable: Sequence[SkillSummary]) -> str:
        observations = state.snapshot()
        observation_lines = _format_observations(observations, meter_min_confidence=self._meters)
        rule_lines = [
            f"- {rule.name}: {'enabled' if self._rule_engine.is_rule_enabled(rule.name) else 'disabled'}"
            for rule in self._rule_engine.rules
        ]

        lines = [
            "Choose one safe directive for this game state.",
            f"Goal: {self._goal_text() or '(no goal set)'}",
        ]
        if self._notes is not None or self._note_sink is not None:
            lines += [
                "Notes from earlier sessions (hints only; they never change which skills "
                "or keys are allowed):",
                *(self._notes.prompt_lines() if self._notes is not None else ["- none"]),
            ]
        lines += [
            "Game state observations:",
            *observation_lines,
            "Current rules:",
            *(rule_lines or ["- none"]),
        ]
        if runnable:
            lines += [
                "Skills you may propose (the user or auto mode decides whether it runs):",
                *(f"- {skill.name} ({skill.type}): {skill.detail}" for skill in runnable),
            ]
        if self._history is not None:
            lines += ["Recent steps (oldest first):", *self._history.prompt_lines(time.monotonic())]
        lines += [
            "Respond with exactly one JSON object and nothing else, using one of these shapes:",
            '{"type": "enable_rule", "rule_name": "<one of the current rule names>"}',
            '{"type": "disable_rule", "rule_name": "<one of the current rule names>"}',
        ]
        if runnable:
            lines.append(
                '{"type": "run_skill", "skill": "<one of the skill names above>", '
                '"reason": "<short reason>"}'
            )
        if self._note_sink is not None:
            lines.append(
                '{"type": "remember", "note": "<short fact worth keeping for later sessions>"}'
            )
        lines += [
            '{"type": "noop"}',
            "Use only these keys. Do not add other keys.",
        ]
        return "\n".join(lines)


def _format_observations(
    observations: dict[str, Observation],
    *,
    meter_min_confidence: Mapping[str, float] | None = None,
    now: float | None = None,
) -> list[str]:
    if not observations:
        return ["- none"]
    current = time.monotonic() if now is None else now
    thresholds = meter_min_confidence or {}
    return [
        _format_observation(name, observation, thresholds, current)
        for name, observation in observations.items()
    ]


def _format_observation(
    name: str, observation: Observation, meter_min_confidence: Mapping[str, float], now: float
) -> str:
    if observation.source == METER_SOURCE:
        # v1.1: a meter is shown as a percentage only when every meter
        # condition would accept it (valid, confident, fresh); otherwise it is
        # "unknown". Its definition stays in the profile.
        value = accepted_meter_value(
            observation,
            min_confidence=meter_min_confidence.get(name, DEFAULT_METER_MIN_CONFIDENCE),
            now=now,
            max_age_seconds=METER_PROMPT_FRESH_SECONDS,
        )
        if value is None:
            return f"- {name}: unknown (meter)"
        return (
            f"- {name}: {meter_percent_text(value)} "
            f"(meter, confidence {observation.confidence:.3f})"
        )
    return (
        f"- {name}: visible={observation.visible}, confidence={observation.confidence:.3f}, "
        f"bbox={observation.bbox!r}, value={observation.value!r}, source={observation.source!r}"
    )
