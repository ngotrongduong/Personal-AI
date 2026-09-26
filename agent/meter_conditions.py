"""Pure v1.1 meter conditions.

Meters are observations only. This module reads GameState/Observation values and
never imports the skill, dispatcher or input path. Every helper fails closed:
missing, invalid, stale or low-confidence readings return no match.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import math
from typing import Literal, TypeAlias

from .game_state import GameState, Observation


MeterOperator: TypeAlias = Literal["below", "above", "rises", "falls"]
METER_OPERATORS: tuple[MeterOperator, ...] = ("below", "above", "rises", "falls")
DEFAULT_METER_MIN_CONFIDENCE = 0.8


class MeterConditionError(ValueError):
    """A meter condition is malformed or references an unknown meter."""


@dataclass(frozen=True, slots=True)
class MeterCondition:
    meter: str
    operator: MeterOperator
    amount: float
    min_confidence: float = DEFAULT_METER_MIN_CONFIDENCE

    def __post_init__(self) -> None:
        if not isinstance(self.meter, str) or not self.meter.strip():
            raise MeterConditionError("meter must be a non-empty meter name.")
        if self.operator not in METER_OPERATORS:
            raise MeterConditionError(
                f"meter operator must be one of {list(METER_OPERATORS)!r}."
            )
        if (
            isinstance(self.amount, bool)
            or not isinstance(self.amount, int | float)
            or not math.isfinite(self.amount)
            or not 0.0 <= self.amount <= 1.0
        ):
            raise MeterConditionError(
                f"meter {self.operator} value must be between 0.0 and 1.0."
            )
        if (
            isinstance(self.min_confidence, bool)
            or not isinstance(self.min_confidence, int | float)
            or not math.isfinite(self.min_confidence)
            or not 0.0 <= self.min_confidence <= 1.0
        ):
            raise MeterConditionError(
                "meter min_confidence must be between 0.0 and 1.0."
            )

    @property
    def is_change(self) -> bool:
        return self.operator in {"rises", "falls"}

    def describe(self) -> str:
        percent = self.amount * 100.0
        if self.operator == "below":
            return f"{self.meter} below {percent:g}%"
        if self.operator == "above":
            return f"{self.meter} above {percent:g}%"
        if self.operator == "rises":
            return f"{self.meter} rises by {percent:g}%"
        return f"{self.meter} falls by {percent:g}%"

    def to_block(self) -> dict[str, object]:
        return {
            "meter": self.meter,
            self.operator: self.amount,
            "min_confidence": self.min_confidence,
        }


def parse_meter_condition(
    block: object,
    meter_names: Collection[str] | None = None,
    *,
    label: str = "meter condition",
) -> MeterCondition:
    if not isinstance(block, Mapping):
        raise MeterConditionError(f"{label} must be an object.")

    allowed = {"meter", "min_confidence", *METER_OPERATORS}
    unknown = set(block) - allowed
    if unknown:
        raise MeterConditionError(
            f"{label} has unknown field(s): {sorted(unknown)!r}."
        )

    meter = block.get("meter")
    if not isinstance(meter, str) or not meter.strip():
        raise MeterConditionError(f"{label} needs a 'meter'.")
    if meter_names is not None and meter not in meter_names:
        raise MeterConditionError(f"{label} references unknown meter {meter!r}.")

    operators = [operator for operator in METER_OPERATORS if operator in block]
    if len(operators) != 1:
        raise MeterConditionError(
            f"{label} needs exactly one of {list(METER_OPERATORS)!r}."
        )
    operator = operators[0]

    return MeterCondition(
        meter=meter,
        operator=operator,
        amount=block[operator],  # type: ignore[arg-type]
        min_confidence=block.get(  # type: ignore[arg-type]
            "min_confidence", DEFAULT_METER_MIN_CONFIDENCE
        ),
    )


def accepted_meter_value(
    observation: Observation | None,
    *,
    min_confidence: float,
    now: float | None = None,
    max_age_seconds: float | None = None,
    after: float | None = None,
) -> float | None:
    """Return a normalized meter value only when the observation is trustworthy."""

    if observation is None or not observation.visible:
        return None
    if observation.confidence < min_confidence:
        return None

    value = observation.value
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        return None

    if after is not None and observation.observed_at <= after:
        return None
    if now is not None:
        if observation.observed_at > now:
            return None
        if (
            max_age_seconds is not None
            and now - observation.observed_at > max_age_seconds
        ):
            return None

    return float(value)


def condition_matches_value(
    condition: MeterCondition,
    value: float,
    *,
    baseline: float | None = None,
) -> bool:
    if condition.operator == "below":
        return value < condition.amount
    if condition.operator == "above":
        return value > condition.amount
    if baseline is None:
        return False
    if condition.operator == "rises":
        return value - baseline >= condition.amount
    return baseline - value >= condition.amount


def meter_condition_met(
    state: GameState,
    condition: MeterCondition,
    *,
    now: float,
    max_age_seconds: float,
    after: float | None = None,
    baseline: float | None = None,
) -> bool:
    observation = state.get(condition.meter)
    value = accepted_meter_value(
        observation,
        min_confidence=condition.min_confidence,
        now=now,
        max_age_seconds=max_age_seconds,
        after=after,
    )
    if value is None:
        return False
    return condition_matches_value(condition, value, baseline=baseline)


def current_meter_value(
    state: GameState,
    condition: MeterCondition,
    *,
    now: float,
    max_age_seconds: float,
    after: float | None = None,
) -> float | None:
    """Accepted value helper for capturing change-condition baselines."""

    return accepted_meter_value(
        state.get(condition.meter),
        min_confidence=condition.min_confidence,
        now=now,
        max_age_seconds=max_age_seconds,
        after=after,
    )
