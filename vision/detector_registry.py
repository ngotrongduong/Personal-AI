from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import TypeAlias

import numpy as np

from .template_matcher import TemplateMatcher


BBox: TypeAlias = tuple[int, int, int, int]
ROI: TypeAlias = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DetectorSpec:
    """Configuration for one named template detector."""

    name: str
    threshold: float = 0.82
    roi: ROI | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Detector name cannot be empty.")
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("Detector threshold must be between 0.0 and 1.0.")
        if self.roi is not None:
            _x, _y, width, height = self.roi
            if width <= 0 or height <= 0:
                raise ValueError("Detector ROI width/height must be positive.")


@dataclass(frozen=True, slots=True)
class Detection:
    """Structured vision result in full-frame coordinates."""

    name: str
    visible: bool
    confidence: float
    bbox: BBox | None
    detector_type: str = "template"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Detection name cannot be empty.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Detection confidence must be between 0.0 and 1.0.")
        if self.visible and self.bbox is None:
            raise ValueError("Visible template detection must include a bbox.")
        if not self.visible and self.bbox is not None:
            raise ValueError("Invisible template detection cannot include a bbox.")
        if self.bbox is not None:
            _x, _y, width, height = self.bbox
            if width <= 0 or height <= 0:
                raise ValueError("Detection bbox width/height must be positive.")


@dataclass(slots=True)
class _TemplateEntry:
    spec: DetectorSpec
    matcher: TemplateMatcher


class DetectorRegistry:
    """Owns multiple named template detectors and evaluates them independently."""

    def __init__(self) -> None:
        self._entries: dict[str, _TemplateEntry] = {}
        self._lock = threading.RLock()

    @property
    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._entries)

    def register_array(self, spec: DetectorSpec, template_bgr: np.ndarray) -> None:
        matcher = TemplateMatcher(threshold=spec.threshold)
        matcher.load_array(template_bgr)
        self._register(spec, matcher)

    def register_file(self, spec: DetectorSpec, path: str | Path) -> None:
        matcher = TemplateMatcher(threshold=spec.threshold)
        matcher.load_file(path)
        self._register(spec, matcher)

    def _register(self, spec: DetectorSpec, matcher: TemplateMatcher) -> None:
        with self._lock:
            if spec.name in self._entries:
                raise ValueError(f"Duplicate detector name: {spec.name}")
            self._entries[spec.name] = _TemplateEntry(spec=spec, matcher=matcher)

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def detect_all(self, frame_bgr: np.ndarray) -> dict[str, Detection]:
        if frame_bgr is None or frame_bgr.size == 0:
            raise ValueError("Frame image is empty.")

        with self._lock:
            entries = tuple(self._entries.values())

        return {
            entry.spec.name: self._detect_one(frame_bgr, entry)
            for entry in entries
        }

    @staticmethod
    def _detect_one(frame_bgr: np.ndarray, entry: _TemplateEntry) -> Detection:
        frame_h, frame_w = frame_bgr.shape[:2]
        offset_x = 0
        offset_y = 0
        search_frame = frame_bgr

        if entry.spec.roi is not None:
            roi_x, roi_y, roi_w, roi_h = entry.spec.roi
            left = max(0, roi_x)
            top = max(0, roi_y)
            right = min(frame_w, roi_x + roi_w)
            bottom = min(frame_h, roi_y + roi_h)

            if right <= left or bottom <= top:
                return Detection(
                    name=entry.spec.name,
                    visible=False,
                    confidence=0.0,
                    bbox=None,
                )

            search_frame = frame_bgr[top:bottom, left:right]
            offset_x = left
            offset_y = top

        match = entry.matcher.find_best(search_frame)
        if match is None:
            return Detection(
                name=entry.spec.name,
                visible=False,
                confidence=0.0,
                bbox=None,
            )

        return Detection(
            name=entry.spec.name,
            visible=True,
            confidence=match.score,
            bbox=(match.x + offset_x, match.y + offset_y, match.w, match.h),
        )
