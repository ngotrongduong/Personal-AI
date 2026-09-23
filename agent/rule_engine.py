from __future__ import annotations

from dataclasses import dataclass
import threading
import time

from .game_state import BBox, GameState


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


@dataclass(frozen=True, slots=True)
class VisibilityRule:
    """Fire an action intent when a named detector is recently visible."""

    name: str
    detector_name: str
    action: str
    min_confidence: float = 0.82
    max_observation_age_seconds: float = 0.75
    cooldown_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Rule name cannot be empty.")
        if not self.detector_name.strip():
            raise ValueError("Rule detector_name cannot be empty.")
        if not self.action.strip():
            raise ValueError("Rule action cannot be empty.")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0.")
        if self.max_observation_age_seconds < 0:
            raise ValueError("max_observation_age_seconds cannot be negative.")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative.")


class RuleEngine:
    """
    Deterministic rule evaluator.

    It only returns ActionIntent objects. A separate gated dispatcher must decide
    whether an intent is allowed to reach InputController.
    """

    def __init__(self, rules: list[VisibilityRule] | None = None) -> None:
        self._rules: list[VisibilityRule] = list(rules or [])
        self._disabled_rule_names: set[str] = set()
        self._last_emitted_at: dict[str, float] = {}
        self._lock = threading.RLock()

    @property
    def rules(self) -> tuple[VisibilityRule, ...]:
        with self._lock:
            return tuple(self._rules)

    def add_rule(self, rule: VisibilityRule) -> None:
        with self._lock:
            if any(existing.name == rule.name for existing in self._rules):
                raise ValueError(f"Duplicate rule name: {rule.name}")
            self._rules.append(rule)

    def enable_rule(self, name: str) -> None:
        with self._lock:
            self._require_known_rule(name)
            self._disabled_rule_names.discard(name)

    def disable_rule(self, name: str) -> None:
        with self._lock:
            self._require_known_rule(name)
            self._disabled_rule_names.add(name)

    def is_rule_enabled(self, name: str) -> bool:
        with self._lock:
            self._require_known_rule(name)
            return name not in self._disabled_rule_names

    def reset_cooldowns(self) -> None:
        with self._lock:
            self._last_emitted_at.clear()

    def evaluate(self, state: GameState, *, now: float | None = None) -> list[ActionIntent]:
        with self._lock:
            current = time.monotonic() if now is None else now
            intents: list[ActionIntent] = []

            for rule in self._rules:
                if rule.name in self._disabled_rule_names:
                    continue
                observation = state.get(rule.detector_name)
                if observation is None:
                    continue
                if not observation.visible:
                    continue
                if observation.confidence < rule.min_confidence:
                    continue
                if observation.is_stale(rule.max_observation_age_seconds, now=current):
                    continue

                last = self._last_emitted_at.get(rule.name)
                if last is not None and current - last < rule.cooldown_seconds:
                    continue

                intents.append(
                    ActionIntent(
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
                    )
                )
                # Emission cooldown is intentionally conservative: even if a future
                # dispatcher rejects the intent because input is disabled, the engine
                # will not flood the UI/log with the same intent every frame.
                self._last_emitted_at[rule.name] = current

            return intents

    def _require_known_rule(self, name: str) -> None:
        with self._lock:
            if not any(rule.name == name for rule in self._rules):
                raise ValueError(f"Unknown rule name: {name}")
