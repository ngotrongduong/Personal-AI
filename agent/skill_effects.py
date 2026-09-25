"""Observed effects of skills (v1.0): what a step should change on screen.

A profile skill may declare `expect`: a detector that should become visible
(or disappear) within a few seconds after the step finished. After a step
that ran, the Tk thread polls an `EffectWatch` against `GameState` until it
resolves to `confirmed` or `not_seen`.

This module is observation only. It reads `GameState` and the clock, never
builds an intent and never imports the input path (a test enforces this).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import math

from .game_state import GameState, Observation


MAX_WITHIN_SECONDS = 10.0
DEFAULT_WITHIN_SECONDS = 2.0
DEFAULT_MIN_CONFIDENCE = 0.8

EFFECT_NONE = "none"
EFFECT_CONFIRMED = "confirmed"
EFFECT_NOT_SEEN = "not_seen"
EFFECT_PENDING = "pending"
EFFECTS = frozenset({EFFECT_NONE, EFFECT_CONFIRMED, EFFECT_NOT_SEEN})

_EXPECT_FIELDS = frozenset({"detector", "visible", "within_seconds", "min_confidence"})


class ExpectationError(ValueError):
    """An `expect` block is malformed or references an unknown detector."""


def observation_matches(
    observation: Observation, *, visible: bool, min_confidence: float
) -> bool:
    """Whether `observation` shows the detector as `visible` (or not).

    Visible means `visible` and at least `min_confidence`; anything else counts
    as not visible.
    """

    seen = observation.visible and observation.confidence >= min_confidence
    return seen if visible else not seen


@dataclass(frozen=True, slots=True)
class Expectation:
    """What a skill's step should change: `detector` becomes `visible` (or not)."""

    detector: str
    visible: bool = True
    within_seconds: float = DEFAULT_WITHIN_SECONDS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    def __post_init__(self) -> None:
        if not isinstance(self.detector, str) or not self.detector.strip():
            raise ExpectationError("expect.detector must be a detector name.")
        if not isinstance(self.visible, bool):
            raise ExpectationError("expect.visible must be true or false.")
        within = self.within_seconds
        if (
            isinstance(within, bool)
            or not isinstance(within, int | float)
            or not math.isfinite(within)
            or within <= 0
            or within > MAX_WITHIN_SECONDS
        ):
            raise ExpectationError(
                f"expect.within_seconds must be greater than 0 and at most "
                f"{MAX_WITHIN_SECONDS:g}."
            )
        confidence = self.min_confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0.0 <= confidence <= 1.0
        ):
            raise ExpectationError("expect.min_confidence must be between 0.0 and 1.0.")

    def describe(self) -> str:
        state = "visible" if self.visible else "gone"
        return f"{self.detector} {state}"

    def to_block(self) -> dict[str, object]:
        """The profile JSON form, the inverse of `parse_expectation`."""

        return {
            "detector": self.detector,
            "visible": self.visible,
            "within_seconds": self.within_seconds,
            "min_confidence": self.min_confidence,
        }


def parse_expectation(block: object, detector_names: Collection[str]) -> Expectation:
    """Validate a skill's `expect` block against the profile's detectors."""

    if not isinstance(block, Mapping):
        raise ExpectationError("expect must be an object.")
    unknown = set(block) - _EXPECT_FIELDS
    if unknown:
        raise ExpectationError(f"expect has unknown field(s): {sorted(unknown)!r}.")
    detector = block.get("detector")
    if not isinstance(detector, str) or detector not in detector_names:
        raise ExpectationError(f"expect references unknown detector {detector!r}.")
    options = {field: block[field] for field in block if field != "detector"}
    return Expectation(detector, **options)


@dataclass(frozen=True, slots=True)
class EffectResult:
    skill_name: str
    effect: str
    detector: str
    waited_s: float


class EffectWatch:
    """Watches `GameState` for one step's expected effect.

    Only an observation made after `finished_at` counts. `check` returns
    `pending` until the effect is seen (`confirmed`) or the window closes
    (`not_seen`). It never raises on missing observations.
    """

    def __init__(self, skill_name: str, expectation: Expectation, finished_at: float) -> None:
        self.skill_name = skill_name
        self.expectation = expectation
        self.finished_at = float(finished_at)
        self._result: EffectResult | None = None

    @property
    def deadline(self) -> float:
        return self.finished_at + self.expectation.within_seconds

    @property
    def result(self) -> EffectResult | None:
        return self._result

    def check(self, state: GameState, now: float) -> str:
        if self._result is not None:
            return self._result.effect
        expectation = self.expectation
        observation = state.get(expectation.detector)
        if (
            observation is not None
            and observation.observed_at > self.finished_at
            and observation.observed_at <= self.deadline
            and observation_matches(
                observation,
                visible=expectation.visible,
                min_confidence=expectation.min_confidence,
            )
        ):
            return self._resolve(EFFECT_CONFIRMED, observation.observed_at)
        if now >= self.deadline:
            return self._resolve(EFFECT_NOT_SEEN, self.deadline)
        return EFFECT_PENDING

    def _resolve(self, effect: str, at: float) -> str:
        self._result = EffectResult(
            skill_name=self.skill_name,
            effect=effect,
            detector=self.expectation.detector,
            waited_s=round(max(0.0, at - self.finished_at), 3),
        )
        return effect
