from __future__ import annotations

import time
from collections.abc import Iterable

from vision.resource_bar import ResourceBarMeasurement

from .game_state import GameState, Observation


def apply_resource_measurements(
    state: GameState,
    measurements: Iterable[ResourceBarMeasurement],
    *,
    observed_at: float | None = None,
) -> tuple[Observation, ...]:
    """Write one resource-bar measurement cycle into GameState."""

    items = tuple(measurements)
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("Resource-bar cycle contains duplicate names.")

    stamp = time.monotonic() if observed_at is None else observed_at
    observations: list[Observation] = []

    for measurement in items:
        observation = state.update_detector(
            measurement.name,
            visible=measurement.valid,
            confidence=measurement.confidence,
            bbox=measurement.bbox,
            value=measurement.fraction if measurement.valid else None,
            observed_at=stamp,
            source="vision:resource_bar",
        )
        observations.append(observation)

    return tuple(observations)
