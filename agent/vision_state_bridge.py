from __future__ import annotations

import time
from collections.abc import Iterable

from vision.detector_registry import Detection

from .game_state import GameState, Observation


def apply_detections(
    state: GameState,
    detections: Iterable[Detection],
    *,
    observed_at: float | None = None,
) -> tuple[Observation, ...]:
    """Write one detector cycle into GameState using a shared observation timestamp."""

    items = tuple(detections)
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("Detection cycle contains duplicate detector names.")

    stamp = time.monotonic() if observed_at is None else observed_at
    observations: list[Observation] = []

    for detection in items:
        observation = state.update_detector(
            detection.name,
            visible=detection.visible,
            confidence=detection.confidence,
            bbox=detection.bbox,
            observed_at=stamp,
            source=f"vision:{detection.detector_type}",
        )
        observations.append(observation)

    return tuple(observations)
