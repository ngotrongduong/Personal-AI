from __future__ import annotations

import time
from collections.abc import Iterable

from vision.ocr import OcrMeasurement

from .game_state import GameState, Observation


def apply_ocr_measurements(
    state: GameState,
    measurements: Iterable[OcrMeasurement],
    *,
    observed_at: float | None = None,
) -> tuple[Observation, ...]:
    """Write one OCR measurement cycle into GameState."""

    items = tuple(measurements)
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("OCR cycle contains duplicate names.")

    stamp = time.monotonic() if observed_at is None else observed_at
    observations: list[Observation] = []
    for measurement in items:
        observation = state.update_detector(
            measurement.name,
            visible=measurement.valid,
            confidence=measurement.confidence,
            bbox=measurement.bbox,
            value=measurement.text if measurement.valid else None,
            observed_at=stamp,
            source="vision:ocr",
        )
        observations.append(observation)

    return tuple(observations)
