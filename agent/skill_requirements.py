"""Preconditions of tap skills (v1.2).

A tap skill clicks a fixed point, so nothing on screen confirms what is under
it. Its optional `requires` list lets the profile say when the point is safe to
tap (e.g. "the wait button is visible", "hp above 30%"). Every condition must
hold on a fresh observation; anything missing, stale, low-confidence or invalid
fails closed. This module only reads GameState and never imports the input
path.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import math
from typing import TypeAlias

from .game_state import GameState
from .meter_conditions import (
    METER_SOURCE,
    MeterCondition,
    MeterConditionError,
    meter_condition_met,
    parse_meter_condition,
)


MAX_REQUIREMENTS = 4
DEFAULT_REQUIREMENT_MIN_CONFIDENCE = 0.8

_DETECTOR_FIELDS = frozenset({"detector", "visible", "min_confidence"})
_METER_FIELDS = frozenset({"meter", "below", "above", "min_confidence"})


class RequirementError(ValueError):
    """A `requires` condition is malformed or references an unknown name."""


@dataclass(frozen=True, slots=True)
class DetectorRequirement:
    """A detector must be seen (or be gone) on a fresh observation."""

    detector: str
    visible: bool = True
    min_confidence: float = DEFAULT_REQUIREMENT_MIN_CONFIDENCE

    def __post_init__(self) -> None:
        if not isinstance(self.detector, str) or not self.detector.strip():
            raise RequirementError("requires.detector must be a detector name.")
        if not isinstance(self.visible, bool):
            raise RequirementError("requires.visible must be true or false.")
        confidence = self.min_confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise RequirementError("requires.min_confidence must be between 0.0 and 1.0.")

    @property
    def name(self) -> str:
        return self.detector

    def describe(self) -> str:
        return f"{self.detector} {'visible' if self.visible else 'gone'}"

    def to_block(self) -> dict[str, object]:
        return {
            "detector": self.detector,
            "visible": self.visible,
            "min_confidence": self.min_confidence,
        }

    def met(self, state: GameState, *, now: float, max_age_seconds: float) -> bool:
        observation = state.get(self.detector)
        if observation is None or observation.source == METER_SOURCE:
            return False
        if observation.observed_at > now or observation.is_stale(max_age_seconds, now=now):
            return False
        if self.visible:
            return observation.visible and observation.confidence >= self.min_confidence
        # "gone" gates input, so a weak but visible detection never counts.
        return not observation.visible


@dataclass(frozen=True, slots=True)
class MeterRequirement:
    """A meter must be below/above a threshold on a fresh valid reading."""

    condition: MeterCondition

    def __post_init__(self) -> None:
        if self.condition.is_change:
            raise RequirementError(
                "requires supports only meter 'below' / 'above', not rises/falls."
            )

    @property
    def name(self) -> str:
        return self.condition.meter

    def describe(self) -> str:
        return self.condition.describe()

    def to_block(self) -> dict[str, object]:
        return self.condition.to_block()

    def met(self, state: GameState, *, now: float, max_age_seconds: float) -> bool:
        observation = state.get(self.condition.meter)
        if observation is None or observation.source != METER_SOURCE:
            return False
        return meter_condition_met(
            state, self.condition, now=now, max_age_seconds=max_age_seconds
        )


Requirement: TypeAlias = DetectorRequirement | MeterRequirement


def parse_requirement(
    block: object,
    detector_names: Collection[str],
    meter_names: Collection[str] = (),
) -> Requirement:
    """Validate one `requires` condition against the profile's names."""

    if not isinstance(block, Mapping):
        raise RequirementError("requires items must be objects.")
    has_detector = "detector" in block
    has_meter = "meter" in block
    if has_detector == has_meter:
        raise RequirementError("requires items need exactly one of 'detector' or 'meter'.")

    if has_detector:
        unknown = set(block) - _DETECTOR_FIELDS
        if unknown:
            raise RequirementError(f"requires has unknown field(s): {sorted(unknown)!r}.")
        detector = block.get("detector")
        if not isinstance(detector, str) or detector not in detector_names:
            raise RequirementError(f"requires references unknown detector {detector!r}.")
        return DetectorRequirement(**dict(block))  # type: ignore[arg-type]

    unknown = set(block) - _METER_FIELDS
    if unknown:
        raise RequirementError(f"requires has unknown field(s): {sorted(unknown)!r}.")
    try:
        condition = parse_meter_condition(block, meter_names, label="requires")
    except MeterConditionError as error:
        raise RequirementError(str(error)) from error
    return MeterRequirement(condition)


def parse_requirements(
    items: object,
    detector_names: Collection[str],
    meter_names: Collection[str] = (),
) -> tuple[Requirement, ...]:
    if not isinstance(items, list):
        raise RequirementError("requires must be a list.")
    if len(items) > MAX_REQUIREMENTS:
        raise RequirementError(f"requires allows at most {MAX_REQUIREMENTS} conditions.")
    return tuple(parse_requirement(item, detector_names, meter_names) for item in items)


def unmet_requirement(
    requirements: Collection[Requirement],
    state: GameState,
    *,
    now: float,
    max_age_seconds: float,
) -> Requirement | None:
    """The first condition that does not hold now, or None if all hold."""

    for requirement in requirements:
        if not requirement.met(state, now=now, max_age_seconds=max_age_seconds):
            return requirement
    return None
