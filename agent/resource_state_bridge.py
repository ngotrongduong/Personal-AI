from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import time

from vision.resource_bar import ResourceBarMeasurement

from .game_state import GameState, Observation


def apply_resource_measurements(
    state: GameState,
    measurements: Iterable[ResourceBarMeasurement],
    *,
    observed_at: float | None = None,
    min_confidence_by_name: Mapping[str, float] | None = None,
) -> tuple[Observation, ...]:
    """Write one resource-bar measurement cycle into GameState.

    min_confidence_by_name is an optional profile-level acceptance gate.
    Below-threshold readings keep their measured confidence/bbox for diagnostics
    but are written as visible=False, value=None so downstream meter consumers
    fail closed.
    """

    items = tuple(measurements)
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("Resource-bar cycle contains duplicate names.")

    thresholds = dict(min_confidence_by_name or {})
    for name, threshold in thresholds.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(threshold, bool)
            or not isinstance(threshold, int | float)
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise ValueError(
                "Resource-bar minimum confidence values must be finite numbers "
                "between 0.0 and 1.0."
            )

    stamp = time.monotonic() if observed_at is None else observed_at
    observations: list[Observation] = []

    for measurement in items:
        accepted = (
            measurement.valid
            and measurement.confidence >= thresholds.get(measurement.name, 0.0)
        )
        observation = state.update_detector(
            measurement.name,
            visible=accepted,
            confidence=measurement.confidence,
            bbox=measurement.bbox,
            value=measurement.fraction if accepted else None,
            observed_at=stamp,
            source="vision:resource_bar",
        )
        observations.append(observation)

    return tuple(observations)
