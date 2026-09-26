"""Agent runs (v1.0): preflight checks, a run budget and a goal condition.

An agent run is a planner session with limits. Everything here only reads
facts, `GameState` and the clock: a preflight can refuse a start, and the
budget or the goal can end a run, but nothing here ever adds input. This
module never imports the input path (a test enforces this).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import math
from typing import TypeAlias

from .game_state import GameState
from .meter_conditions import (
    MeterCondition,
    MeterConditionError,
    current_meter_value,
    meter_condition_met,
    parse_meter_condition,
)
from .skill_effects import DEFAULT_MIN_CONFIDENCE, observation_matches


DEFAULT_MAX_RUN_MINUTES = 15.0
MAX_RUN_MINUTES = 120.0
GOAL_FRESH_SECONDS = 1.0

STOP_BUDGET = "run budget reached"
STOP_GOAL = "goal reached"

_STOP_WHEN_FIELDS = frozenset({"detector", "visible", "min_confidence"})
_METER_STOP_FIELDS = frozenset({"meter", "below", "above", "rises", "falls", "min_confidence"})


class GoalConditionError(ValueError):
    """A `stop_when` block is malformed or references an unknown detector."""


def validate_max_run_minutes(value: object) -> float:
    """Return `value` as minutes if it is a number in (0, MAX_RUN_MINUTES]."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
        or value > MAX_RUN_MINUTES
    ):
        raise ValueError(
            f"max_run_minutes must be greater than 0 and at most {MAX_RUN_MINUTES:g}."
        )
    return float(value)


# --------------------------------------------------------------------------- preflight


@dataclass(frozen=True, slots=True)
class PreflightFacts:
    """What the app knows right before a run; gathered on the Tk thread."""

    profile_name: str | None
    capture_running: bool
    window_title: str | None
    planner_configured: bool
    model: str | None
    ollama_ok: bool
    ollama_detail: str
    enabled_skills: tuple[str, ...]
    input_enabled: bool
    goal: str


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    ok: bool
    required: bool
    detail: str

    def line(self) -> str:
        if self.ok:
            mark = "OK"
        else:
            mark = "FAIL" if self.required else "NOTE"
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def ready(self) -> bool:
        """Every required check passed."""

        return all(check.ok for check in self.checks if check.required)

    @property
    def failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.required and not check.ok)

    def summary(self) -> str:
        if self.ready:
            notes = [check.name for check in self.checks if not check.ok]
            if notes:
                return "Ready (check: " + ", ".join(notes) + ")"
            return "Ready"
        return "Not ready: " + "; ".join(
            f"{check.name} ({check.detail})" for check in self.failures
        )


def run_preflight(facts: PreflightFacts) -> PreflightReport:
    """Check everything a run needs. Required checks gate Start Agent."""

    checks = [
        PreflightCheck(
            "Profile",
            facts.profile_name is not None,
            True,
            f"loaded {facts.profile_name!r}" if facts.profile_name else "load a game profile",
        ),
        PreflightCheck(
            "Capture",
            facts.capture_running,
            True,
            (f"running on {facts.window_title!r}" if facts.window_title else "running")
            if facts.capture_running
            else "start capture on the game window",
        ),
        PreflightCheck(
            "Planner settings",
            facts.planner_configured,
            True,
            f"model {facts.model!r}" if facts.planner_configured and facts.model
            else "the profile needs a planner block with a model",
        ),
        PreflightCheck(
            "Ollama",
            facts.planner_configured and facts.ollama_ok,
            True,
            facts.ollama_detail if facts.planner_configured else "no planner settings",
        ),
        PreflightCheck(
            "Enabled skills",
            bool(facts.enabled_skills),
            True,
            ", ".join(facts.enabled_skills) if facts.enabled_skills
            else "enable at least one skill",
        ),
        PreflightCheck(
            "Input control",
            facts.input_enabled,
            False,
            "on" if facts.input_enabled
            else "off: steps are refused until you turn it on",
        ),
        PreflightCheck(
            "Goal",
            bool(facts.goal.strip()),
            False,
            facts.goal.strip() if facts.goal.strip() else "no goal set; the planner guesses",
        ),
    ]
    return PreflightReport(tuple(checks))


# --------------------------------------------------------------------------- run limits


@dataclass(frozen=True, slots=True)
class RunBudget:
    """How long a run may last, from `started_at` (monotonic seconds)."""

    max_seconds: float
    started_at: float

    def __post_init__(self) -> None:
        validate_max_run_minutes(self.max_seconds / 60.0)

    def remaining(self, now: float) -> float:
        return max(0.0, self.started_at + self.max_seconds - now)

    def expired(self, now: float) -> bool:
        return now >= self.started_at + self.max_seconds


@dataclass(frozen=True, slots=True)
class GoalCondition:
    """The run's goal: `detector` seen `visible` (or gone) on screen."""

    detector: str
    visible: bool = True
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    def __post_init__(self) -> None:
        if not isinstance(self.detector, str) or not self.detector.strip():
            raise GoalConditionError("stop_when.detector must be a detector name.")
        if not isinstance(self.visible, bool):
            raise GoalConditionError("stop_when.visible must be true or false.")
        confidence = self.min_confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0.0 <= confidence <= 1.0
        ):
            raise GoalConditionError("stop_when.min_confidence must be between 0.0 and 1.0.")

    def describe(self) -> str:
        state = "visible" if self.visible else "gone"
        return f"{self.detector} {state}"

    def to_block(self) -> dict[str, object]:
        return {
            "detector": self.detector,
            "visible": self.visible,
            "min_confidence": self.min_confidence,
        }

    def met(self, state: GameState, started_at: float, now: float) -> bool:
        """Met only by a fresh observation made after the run started."""

        observation = state.get(self.detector)
        if observation is None:
            return False
        if observation.observed_at <= started_at or observation.observed_at > now:
            return False
        if now - observation.observed_at > GOAL_FRESH_SECONDS:
            return False
        return observation_matches(
            observation, visible=self.visible, min_confidence=self.min_confidence
        )


@dataclass(frozen=True, slots=True)
class MeterGoalCondition:
    """A run goal expressed as a profile meter threshold/change."""

    condition: MeterCondition

    @property
    def meter(self) -> str:
        return self.condition.meter

    def describe(self) -> str:
        return self.condition.describe()

    def to_block(self) -> dict[str, object]:
        return self.condition.to_block()

    def met(
        self,
        state: GameState,
        started_at: float,
        now: float,
        *,
        baseline: float | None = None,
    ) -> bool:
        return meter_condition_met(
            state,
            self.condition,
            now=now,
            max_age_seconds=GOAL_FRESH_SECONDS,
            after=started_at,
            baseline=baseline,
        )


GoalConditionLike: TypeAlias = GoalCondition | MeterGoalCondition


def parse_goal_condition(block: object) -> GoalConditionLike:
    """Validate detector or meter `stop_when` shape; names are checked later."""

    if not isinstance(block, Mapping):
        raise GoalConditionError("stop_when must be an object.")

    has_detector = "detector" in block
    has_meter = "meter" in block
    if has_detector == has_meter:
        raise GoalConditionError(
            "stop_when must contain exactly one of 'detector' or 'meter'."
        )

    if has_detector:
        unknown = set(block) - _STOP_WHEN_FIELDS
        if unknown:
            raise GoalConditionError(
                f"stop_when has unknown field(s): {sorted(unknown)!r}."
            )
        return GoalCondition(**dict(block))  # type: ignore[arg-type]

    unknown = set(block) - _METER_STOP_FIELDS
    if unknown:
        raise GoalConditionError(
            f"stop_when has unknown field(s): {sorted(unknown)!r}."
        )
    try:
        return MeterGoalCondition(
            parse_meter_condition(block, None, label="stop_when")
        )
    except MeterConditionError as error:
        raise GoalConditionError(str(error)) from error


def check_goal_detector(
    goal: GoalConditionLike | None,
    detector_names: Collection[str],
    meter_names: Collection[str] = (),
) -> None:
    """Validate the observation name referenced by a parsed stop condition."""

    if goal is None:
        return
    if isinstance(goal, GoalCondition):
        if goal.detector not in detector_names:
            raise GoalConditionError(
                f"stop_when references unknown detector {goal.detector!r}."
            )
        return
    if goal.meter not in meter_names:
        raise GoalConditionError(f"stop_when references unknown meter {goal.meter!r}.")


class AgentRun:
    """One bounded run: the budget and the optional goal condition."""

    def __init__(self, budget: RunBudget, goal: GoalConditionLike | None = None) -> None:
        self.budget = budget
        self.goal = goal
        self.steps = 0
        self.effects: dict[str, int] = {"confirmed": 0, "not_seen": 0}
        self._meter_goal_baseline: float | None = None
        self._meter_goal_baseline_ready = False

    @property
    def started_at(self) -> float:
        return self.budget.started_at

    def stop_reason(self, state: GameState, now: float) -> str | None:
        """Why the run should end now, or None to keep going."""

        goal = self.goal
        if isinstance(goal, MeterGoalCondition) and goal.condition.is_change:
            if not self._meter_goal_baseline_ready:
                baseline = current_meter_value(
                    state,
                    goal.condition,
                    now=now,
                    max_age_seconds=GOAL_FRESH_SECONDS,
                    after=self.started_at,
                )
                if baseline is not None:
                    self._meter_goal_baseline = baseline
                    self._meter_goal_baseline_ready = True
            elif goal.met(
                state,
                self.started_at,
                now,
                baseline=self._meter_goal_baseline,
            ):
                return STOP_GOAL
        elif goal is not None and goal.met(state, self.started_at, now):
            return STOP_GOAL
        if self.budget.expired(now):
            return STOP_BUDGET
        return None

    def note_step(self) -> None:
        self.steps += 1

    def note_effect(self, effect: str) -> None:
        if effect in self.effects:
            self.effects[effect] += 1

    def status_line(self, now: float) -> str:
        remaining = int(math.ceil(self.budget.remaining(now)))
        minutes, seconds = divmod(remaining, 60)
        parts = [
            f"{minutes}:{seconds:02d} left",
            f"{self.steps} step(s)",
            f"effects {self.effects['confirmed']} confirmed / "
            f"{self.effects['not_seen']} not seen",
        ]
        parts.append(f"goal: {self.goal.describe()}" if self.goal else "goal: none")
        return " · ".join(parts)
