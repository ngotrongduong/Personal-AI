"""Observed effects of skills (v1.0/v1.1).

A profile skill may declare `expect` as either a detector visibility condition
or a meter condition. The Tk thread polls an EffectWatch against GameState after
an executed step. This module is observation only and never imports the input
path.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import math
from typing import TypeAlias

from .game_state import GameState, Observation
from .meter_conditions import (
    MeterCondition,
    MeterConditionError,
    accepted_meter_value,
    condition_matches_value,
    parse_meter_condition,
)


MAX_WITHIN_SECONDS = 10.0
DEFAULT_WITHIN_SECONDS = 2.0
DEFAULT_MIN_CONFIDENCE = 0.8

EFFECT_NONE = "none"
EFFECT_CONFIRMED = "confirmed"
EFFECT_NOT_SEEN = "not_seen"
EFFECT_PENDING = "pending"
EFFECTS = frozenset({EFFECT_NONE, EFFECT_CONFIRMED, EFFECT_NOT_SEEN})

_DETECTOR_EXPECT_FIELDS = frozenset(
    {"detector", "visible", "within_seconds", "min_confidence"}
)
_METER_EXPECT_EXTRA_FIELDS = frozenset({"within_seconds"})


class ExpectationError(ValueError):
    """An `expect` block is malformed or references an unknown observation."""


def _validate_within_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
        or value > MAX_WITHIN_SECONDS
    ):
        raise ExpectationError(
            f"expect.within_seconds must be greater than 0 and at most "
            f"{MAX_WITHIN_SECONDS:g}."
        )
    return float(value)


def observation_matches(
    observation: Observation, *, visible: bool, min_confidence: float
) -> bool:
    """Whether a detector observation shows the requested visibility state."""

    seen = observation.visible and observation.confidence >= min_confidence
    return seen if visible else not seen


@dataclass(frozen=True, slots=True)
class Expectation:
    """Detector visibility effect retained unchanged from v1.0."""

    detector: str
    visible: bool = True
    within_seconds: float = DEFAULT_WITHIN_SECONDS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    def __post_init__(self) -> None:
        if not isinstance(self.detector, str) or not self.detector.strip():
            raise ExpectationError("expect.detector must be a detector name.")
        if not isinstance(self.visible, bool):
            raise ExpectationError("expect.visible must be true or false.")
        _validate_within_seconds(self.within_seconds)
        confidence = self.min_confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise ExpectationError(
                "expect.min_confidence must be between 0.0 and 1.0."
            )

    def describe(self) -> str:
        state = "visible" if self.visible else "gone"
        return f"{self.detector} {state}"

    def to_block(self) -> dict[str, object]:
        return {
            "detector": self.detector,
            "visible": self.visible,
            "within_seconds": self.within_seconds,
            "min_confidence": self.min_confidence,
        }


@dataclass(frozen=True, slots=True)
class MeterExpectation:
    """A meter threshold/change expected after one completed skill step."""

    condition: MeterCondition
    within_seconds: float = DEFAULT_WITHIN_SECONDS

    def __post_init__(self) -> None:
        _validate_within_seconds(self.within_seconds)

    @property
    def meter(self) -> str:
        return self.condition.meter

    def describe(self) -> str:
        return self.condition.describe()

    def to_block(self) -> dict[str, object]:
        block = self.condition.to_block()
        block["within_seconds"] = self.within_seconds
        return block


ExpectationLike: TypeAlias = Expectation | MeterExpectation


def parse_expectation(
    block: object,
    detector_names: Collection[str],
    meter_names: Collection[str] = (),
) -> ExpectationLike:
    """Validate a detector or meter `expect` block."""

    if not isinstance(block, Mapping):
        raise ExpectationError("expect must be an object.")

    has_detector = "detector" in block
    has_meter = "meter" in block
    if not has_detector and not has_meter:
        # Preserve the v1.0 failure mode/message for an empty detector
        # expectation; existing profiles/tests rely on this wording.
        raise ExpectationError("expect references unknown detector None.")
    if has_detector and has_meter:
        raise ExpectationError(
            "expect must contain exactly one of 'detector' or 'meter'."
        )

    if has_detector:
        unknown = set(block) - _DETECTOR_EXPECT_FIELDS
        if unknown:
            raise ExpectationError(
                f"expect has unknown field(s): {sorted(unknown)!r}."
            )
        detector = block.get("detector")
        if not isinstance(detector, str) or detector not in detector_names:
            raise ExpectationError(
                f"expect references unknown detector {detector!r}."
            )
        options = {field: block[field] for field in block if field != "detector"}
        return Expectation(detector, **options)  # type: ignore[arg-type]

    unknown_extra = set(block) - {
        "meter",
        "min_confidence",
        "below",
        "above",
        "rises",
        "falls",
        *_METER_EXPECT_EXTRA_FIELDS,
    }
    if unknown_extra:
        raise ExpectationError(
            f"expect has unknown field(s): {sorted(unknown_extra)!r}."
        )

    condition_block = {
        key: value for key, value in block.items() if key != "within_seconds"
    }
    try:
        condition = parse_meter_condition(
            condition_block,
            meter_names,
            label="expect",
        )
    except MeterConditionError as error:
        raise ExpectationError(str(error)) from error

    within = _validate_within_seconds(
        block.get("within_seconds", DEFAULT_WITHIN_SECONDS)
    )
    return MeterExpectation(condition, within)


@dataclass(frozen=True, slots=True)
class EffectResult:
    skill_name: str
    effect: str
    detector: str
    waited_s: float


class EffectWatch:
    """Watch one detector/meter expectation after a completed step.

    For `rises` / `falls`, Task 4 live wiring must pass the accepted meter
    value captured at step completion as `baseline_value`. If no valid
    baseline is supplied, the change expectation fails closed.
    """

    def __init__(
        self,
        skill_name: str,
        expectation: ExpectationLike,
        finished_at: float,
        *,
        baseline_value: float | None = None,
    ) -> None:
        self.skill_name = skill_name
        self.expectation = expectation
        self.finished_at = float(finished_at)
        self.baseline_value = baseline_value
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

        if isinstance(self.expectation, Expectation):
            matched_at = self._check_detector(state)
        else:
            matched_at = self._check_meter(state)

        if matched_at is not None:
            return self._resolve(EFFECT_CONFIRMED, matched_at)
        if now >= self.deadline:
            return self._resolve(EFFECT_NOT_SEEN, self.deadline)
        return EFFECT_PENDING

    def _check_detector(self, state: GameState) -> float | None:
        expectation = self.expectation
        assert isinstance(expectation, Expectation)
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
            return observation.observed_at
        return None

    def _check_meter(self, state: GameState) -> float | None:
        expectation = self.expectation
        assert isinstance(expectation, MeterExpectation)
        observation = state.get(expectation.meter)
        if observation is None:
            return None
        if not self.finished_at < observation.observed_at <= self.deadline:
            return None

        value = accepted_meter_value(
            observation,
            min_confidence=expectation.condition.min_confidence,
        )
        if value is None:
            return None
        if condition_matches_value(
            expectation.condition,
            value,
            baseline=self.baseline_value,
        ):
            return observation.observed_at
        return None

    def _resolve(self, effect: str, at: float) -> str:
        expectation = self.expectation
        target = (
            expectation.detector
            if isinstance(expectation, Expectation)
            else expectation.meter
        )
        self._result = EffectResult(
            skill_name=self.skill_name,
            effect=effect,
            detector=target,
            waited_s=round(max(0.0, at - self.finished_at), 3),
        )
        return effect
