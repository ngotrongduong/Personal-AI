from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import TypeAlias

from .game_state import BBox, GameState
from .meter_conditions import (
    MeterCondition,
    accepted_meter_value,
    condition_matches_value,
)


# Action of a rule that fires a named profile skill (v0.6). The rule engine only
# carries the skill name; agent.skills.SkillBook turns it into a concrete
# click/press/hold intent, and the dispatcher rejects this action on its own.
SKILL_RULE_ACTION = "skill"


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """A requested action. This object does not execute keyboard/mouse input."""

    rule_name: str
    action: str
    detector_name: str
    confidence: float
    target_bbox: BBox | None
    created_at: float
    reason: str
    # v0.6 skill fields. Keys and hold times only ever come from a loaded
    # profile's skills, never from a model or a rule.
    skill_name: str | None = None
    key: str | None = None
    hold_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class VisibilityRule:
    """Fire an action intent when a named detector is recently visible."""

    name: str
    detector_name: str
    action: str
    min_confidence: float = 0.82
    max_observation_age_seconds: float = 0.75
    cooldown_seconds: float = 1.0
    skill: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Rule name cannot be empty.")
        if not self.detector_name.strip():
            raise ValueError("Rule detector_name cannot be empty.")
        if not self.action.strip():
            raise ValueError("Rule action cannot be empty.")
        if self.skill is not None and not self.skill.strip():
            raise ValueError("Rule skill cannot be empty.")
        if (self.skill is not None) != (self.action == SKILL_RULE_ACTION):
            raise ValueError(
                f"A rule fires a skill exactly when action is {SKILL_RULE_ACTION!r} "
                "and skill is set."
            )
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0.")
        if self.max_observation_age_seconds < 0:
            raise ValueError("max_observation_age_seconds cannot be negative.")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative.")


@dataclass(frozen=True, slots=True)
class MeterRule:
    """Fire one declared skill when a recent meter condition is satisfied."""

    name: str
    condition: MeterCondition
    skill: str
    max_observation_age_seconds: float = 0.75
    cooldown_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Rule name cannot be empty.")
        if not isinstance(self.skill, str) or not self.skill.strip():
            raise ValueError("Meter rule skill cannot be empty.")
        if (
            isinstance(self.max_observation_age_seconds, bool)
            or not isinstance(self.max_observation_age_seconds, int | float)
            or self.max_observation_age_seconds < 0
        ):
            raise ValueError("max_observation_age_seconds cannot be negative.")
        if (
            isinstance(self.cooldown_seconds, bool)
            or not isinstance(self.cooldown_seconds, int | float)
            or self.cooldown_seconds < 0
        ):
            raise ValueError("cooldown_seconds cannot be negative.")


Rule: TypeAlias = VisibilityRule | MeterRule


class RuleEngine:
    """Deterministic rule evaluator.

    It only returns ActionIntent objects. A separate SkillBook/dispatcher path
    decides whether an intent may become real input.
    """

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules or [])
        self._disabled_rule_names: set[str] = set()
        self._last_emitted_at: dict[str, float] = {}
        # rule name -> (value, observation timestamp). Only accepted/fresh
        # samples enter this table. Used for rises/falls comparisons.
        self._meter_baselines: dict[str, tuple[float, float]] = {}
        self._lock = threading.RLock()

    @property
    def rules(self) -> tuple[Rule, ...]:
        with self._lock:
            return tuple(self._rules)

    def add_rule(self, rule: Rule) -> None:
        with self._lock:
            if any(existing.name == rule.name for existing in self._rules):
                raise ValueError(f"Duplicate rule name: {rule.name}")
            self._rules.append(rule)

    def enable_rule(self, name: str) -> None:
        with self._lock:
            self._require_known_rule(name)
            was_disabled = name in self._disabled_rule_names
            self._disabled_rule_names.discard(name)
            if was_disabled:
                # A change rule must establish a fresh baseline after being
                # re-enabled; it must not react to movement that happened
                # while the rule was disabled.
                self._meter_baselines.pop(name, None)

    def disable_rule(self, name: str) -> None:
        with self._lock:
            self._require_known_rule(name)
            self._disabled_rule_names.add(name)
            # Disabled change rules do not accumulate/retain observation state.
            self._meter_baselines.pop(name, None)

    def is_rule_enabled(self, name: str) -> bool:
        with self._lock:
            self._require_known_rule(name)
            return name not in self._disabled_rule_names

    def reset_cooldowns(self) -> None:
        with self._lock:
            self._last_emitted_at.clear()

    def reset_meter_baselines(self) -> None:
        with self._lock:
            self._meter_baselines.clear()

    def evaluate(
        self,
        state: GameState,
        *,
        now: float | None = None,
    ) -> list[ActionIntent]:
        with self._lock:
            current = time.monotonic() if now is None else now
            intents: list[ActionIntent] = []

            for rule in self._rules:
                if rule.name in self._disabled_rule_names:
                    continue
                if isinstance(rule, VisibilityRule):
                    intent = self._evaluate_visibility(rule, state, current)
                else:
                    intent = self._evaluate_meter(rule, state, current)
                if intent is not None:
                    intents.append(intent)

            return intents

    def _evaluate_visibility(
        self,
        rule: VisibilityRule,
        state: GameState,
        current: float,
    ) -> ActionIntent | None:
        observation = state.get(rule.detector_name)
        if observation is None or not observation.visible:
            return None
        if observation.confidence < rule.min_confidence:
            return None
        if observation.is_stale(
            rule.max_observation_age_seconds,
            now=current,
        ):
            return None
        if not self._cooldown_ready(rule.name, rule.cooldown_seconds, current):
            return None

        self._last_emitted_at[rule.name] = current
        return ActionIntent(
            rule_name=rule.name,
            action=rule.action,
            detector_name=rule.detector_name,
            confidence=observation.confidence,
            target_bbox=observation.bbox,
            created_at=current,
            reason=(
                f"{rule.detector_name} visible with confidence "
                f"{observation.confidence:.3f}"
            ),
            skill_name=rule.skill,
        )

    def _evaluate_meter(
        self,
        rule: MeterRule,
        state: GameState,
        current: float,
    ) -> ActionIntent | None:
        observation = state.get(rule.condition.meter)
        value = accepted_meter_value(
            observation,
            min_confidence=rule.condition.min_confidence,
            now=current,
            max_age_seconds=rule.max_observation_age_seconds,
        )
        if value is None or observation is None:
            return None

        matched: bool
        if rule.condition.is_change:
            previous = self._meter_baselines.get(rule.name)
            if previous is None:
                self._meter_baselines[rule.name] = (
                    value,
                    observation.observed_at,
                )
                return None

            previous_value, previous_at = previous
            if observation.observed_at <= previous_at:
                return None

            # Do not bridge a long invalid/stale gap with an old baseline.
            if current - previous_at > rule.max_observation_age_seconds:
                self._meter_baselines[rule.name] = (
                    value,
                    observation.observed_at,
                )
                return None

            matched = condition_matches_value(
                rule.condition,
                value,
                baseline=previous_value,
            )
            # Consecutive accepted sample semantics: consume this sample even
            # when cooldown blocks an otherwise matching change.
            self._meter_baselines[rule.name] = (
                value,
                observation.observed_at,
            )
        else:
            matched = condition_matches_value(rule.condition, value)

        if not matched:
            return None
        if not self._cooldown_ready(rule.name, rule.cooldown_seconds, current):
            return None

        self._last_emitted_at[rule.name] = current
        return ActionIntent(
            rule_name=rule.name,
            action=SKILL_RULE_ACTION,
            detector_name=rule.condition.meter,
            confidence=observation.confidence,
            target_bbox=observation.bbox,
            created_at=current,
            reason=(
                f"{rule.condition.describe()} "
                f"(value {value * 100.0:.1f}%, confidence "
                f"{observation.confidence:.3f})"
            ),
            skill_name=rule.skill,
        )

    def _cooldown_ready(
        self,
        rule_name: str,
        cooldown_seconds: float,
        current: float,
    ) -> bool:
        last = self._last_emitted_at.get(rule_name)
        return last is None or current - last >= cooldown_seconds

    def _require_known_rule(self, name: str) -> None:
        if not any(rule.name == name for rule in self._rules):
            raise ValueError(f"Unknown rule name: {name}")
