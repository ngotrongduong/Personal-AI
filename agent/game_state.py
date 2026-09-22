from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import TypeAlias


BBox: TypeAlias = tuple[int, int, int, int]
ObservationValue: TypeAlias = float | int | str | bool | None


@dataclass(frozen=True, slots=True)
class Observation:
    """A single named observation produced by vision or another sensor."""

    name: str
    visible: bool
    confidence: float = 0.0
    bbox: BBox | None = None
    value: ObservationValue = None
    observed_at: float = field(default_factory=time.monotonic)
    source: str = "vision"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Observation name cannot be empty.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Observation confidence must be between 0.0 and 1.0.")
        if self.bbox is not None:
            x, y, w, h = self.bbox
            if w <= 0 or h <= 0:
                raise ValueError("Observation bbox width/height must be positive.")

    def is_stale(self, max_age_seconds: float, *, now: float | None = None) -> bool:
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds cannot be negative.")
        current = time.monotonic() if now is None else now
        return current - self.observed_at > max_age_seconds


class GameState:
    """Thread-safe store of the most recent named observations."""

    def __init__(self) -> None:
        self._observations: dict[str, Observation] = {}
        self._lock = threading.RLock()

    def update(self, observation: Observation) -> None:
        with self._lock:
            current = self._observations.get(observation.name)
            if current is not None and observation.observed_at < current.observed_at:
                # Ignore out-of-order sensor updates.
                return
            self._observations[observation.name] = observation

    def update_detector(
        self,
        name: str,
        *,
        visible: bool,
        confidence: float = 0.0,
        bbox: BBox | None = None,
        value: ObservationValue = None,
        observed_at: float | None = None,
        source: str = "vision",
    ) -> Observation:
        observation = Observation(
            name=name,
            visible=visible,
            confidence=confidence,
            bbox=bbox,
            value=value,
            observed_at=time.monotonic() if observed_at is None else observed_at,
            source=source,
        )
        self.update(observation)
        return observation

    def get(self, name: str) -> Observation | None:
        with self._lock:
            return self._observations.get(name)

    def is_visible(
        self,
        name: str,
        *,
        min_confidence: float = 0.0,
        max_age_seconds: float | None = None,
        now: float | None = None,
    ) -> bool:
        observation = self.get(name)
        if observation is None or not observation.visible:
            return False
        if observation.confidence < min_confidence:
            return False
        if max_age_seconds is not None and observation.is_stale(max_age_seconds, now=now):
            return False
        return True

    def snapshot(self) -> dict[str, Observation]:
        with self._lock:
            return dict(self._observations)

    def clear(self) -> None:
        with self._lock:
            self._observations.clear()
