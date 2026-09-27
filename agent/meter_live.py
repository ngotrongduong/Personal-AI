"""v1.1 live meter measurement helpers.

Observation only: these helpers measure profile meters on a captured frame and
format what was read. They never import the skill, rule, dispatcher or input
path, and they fail closed: a meter that cannot be read cleanly is reported as
invalid so every condition on it stays false.

Meters are passed duck-typed (``name``, ``min_confidence``, ``spec()``) so this
module does not need ``agent.profile``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

import numpy as np

from vision.resource_bar import ResourceBarMeasurement, ResourceBarSpec, measure_resource_bar

from .game_state import GameState
from .meter_conditions import METER_SOURCE, accepted_meter_value


__all__ = [
    "METER_SOURCE",
    "measure_meters",
    "meter_percent_text",
    "meter_validity_changes",
    "meters_status_parts",
    "meters_summary",
]


class MeterLike(Protocol):
    name: str
    min_confidence: float

    def spec(self) -> ResourceBarSpec: ...


def _invalid(name: str) -> ResourceBarMeasurement:
    return ResourceBarMeasurement(
        name=name,
        valid=False,
        fraction=0.0,
        confidence=0.0,
        bbox=None,
        matched_pixel_fraction=0.0,
    )


def _roi_inside(frame: np.ndarray, roi: tuple[int, int, int, int]) -> bool:
    height, width = frame.shape[:2]
    x, y, w, h = roi
    return x >= 0 and y >= 0 and x + w <= width and y + h <= height


def measure_meters(
    frame: np.ndarray | None, meters: Iterable[MeterLike]
) -> tuple[tuple[ResourceBarMeasurement, ...], tuple[str, ...]]:
    """Measure every meter on one frame.

    Returns ``(measurements, errors)``. Each meter is isolated: a ROI that is
    not fully inside the frame, or an exception while measuring, makes only
    that meter invalid. An empty frame makes every meter invalid.
    """

    items = tuple(meters)
    if frame is None or getattr(frame, "size", 0) == 0:
        if not items:
            return (), ()
        return tuple(_invalid(meter.name) for meter in items), ("empty frame",)

    measurements: list[ResourceBarMeasurement] = []
    errors: list[str] = []
    for meter in items:
        try:
            spec = meter.spec()
            if not _roi_inside(frame, spec.roi):
                # A clipped ROI would measure a different bar length: fail closed.
                measurements.append(_invalid(meter.name))
                continue
            measurements.append(measure_resource_bar(frame, spec))
        except Exception as exc:  # noqa: BLE001 - one bad meter must not stop the rest
            errors.append(f"{meter.name}: {exc}")
            measurements.append(_invalid(meter.name))
    return tuple(measurements), tuple(errors)


def meter_percent_text(value: float) -> str:
    return f"{round(value * 100):d}%"


def meters_status_parts(state: GameState, meters: Iterable[MeterLike]) -> list[str]:
    """Status-line parts: ``hp=42%(0.97)``, or ``hp=?`` when no reading is accepted.

    A valid reading below the meter's ``min_confidence`` shows as
    ``hp=?(0.31)`` so the display never looks more certain than the conditions.
    """

    parts: list[str] = []
    for meter in meters:
        observation = state.get(meter.name)
        value = accepted_meter_value(observation, min_confidence=meter.min_confidence)
        if observation is None or not observation.visible:
            parts.append(f"{meter.name}=?")
        elif value is None:
            parts.append(f"{meter.name}=?({observation.confidence:.2f})")
        else:
            parts.append(f"{meter.name}={meter_percent_text(value)}({observation.confidence:.2f})")
    return parts


def meters_summary(
    state: GameState,
    meters: Iterable[MeterLike],
    *,
    now: float,
    max_age_seconds: float,
) -> str:
    """``"hp 42%, mp unknown"``; only fresh accepted readings count. ``""`` if none."""

    parts: list[str] = []
    for meter in meters:
        value = accepted_meter_value(
            state.get(meter.name),
            min_confidence=meter.min_confidence,
            now=now,
            max_age_seconds=max_age_seconds,
        )
        text = "unknown" if value is None else meter_percent_text(value)
        parts.append(f"{meter.name} {text}")
    return ", ".join(parts)


def meter_validity_changes(
    previous: Mapping[str, bool], measurements: Iterable[ResourceBarMeasurement]
) -> list[tuple[str, bool]]:
    """Meters whose validity differs from ``previous`` (a new meter always counts)."""

    return [
        (measurement.name, measurement.valid)
        for measurement in measurements
        if previous.get(measurement.name) != measurement.valid
    ]
