from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, TypeAlias

import cv2
import numpy as np


ROI: TypeAlias = tuple[int, int, int, int]
Direction: TypeAlias = Literal[
    "left_to_right",
    "right_to_left",
    "top_to_bottom",
    "bottom_to_top",
]


@dataclass(frozen=True, slots=True)
class HSVRange:
    """Inclusive HSV range using OpenCV channel limits (H 0-179, S/V 0-255)."""

    lower: tuple[int, int, int]
    upper: tuple[int, int, int]

    def __post_init__(self) -> None:
        limits = (179, 255, 255)
        for lower, upper, limit in zip(self.lower, self.upper, limits, strict=True):
            if not 0 <= lower <= upper <= limit:
                raise ValueError("HSV range channels must satisfy 0 <= lower <= upper <= limit.")


@dataclass(frozen=True, slots=True)
class ResourceBarSpec:
    """How to measure one tightly-cropped HP/resource bar."""

    name: str
    roi: ROI
    hsv_ranges: tuple[HSVRange, ...]
    direction: Direction = "left_to_right"
    min_slice_coverage: float = 0.50
    max_gap_slices: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Resource bar name cannot be empty.")

        _x, _y, width, height = self.roi
        if width <= 0 or height <= 0:
            raise ValueError("Resource bar ROI width/height must be positive.")

        if not self.hsv_ranges:
            raise ValueError("At least one HSV range is required.")

        valid_directions = {
            "left_to_right",
            "right_to_left",
            "top_to_bottom",
            "bottom_to_top",
        }
        if self.direction not in valid_directions:
            raise ValueError(f"Unsupported resource bar direction: {self.direction}")

        if not 0.0 < self.min_slice_coverage <= 1.0:
            raise ValueError("min_slice_coverage must be in (0.0, 1.0].")

        if self.max_gap_slices < 0:
            raise ValueError("max_gap_slices cannot be negative.")


@dataclass(frozen=True, slots=True)
class ResourceBarMeasurement:
    """Normalized measurement of one configured resource bar."""

    name: str
    valid: bool
    fraction: float
    confidence: float
    bbox: ROI | None
    matched_pixel_fraction: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.fraction <= 1.0:
            raise ValueError("Resource fraction must be between 0.0 and 1.0.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Resource bar confidence must be between 0.0 and 1.0.")
        if not 0.0 <= self.matched_pixel_fraction <= 1.0:
            raise ValueError("matched_pixel_fraction must be between 0.0 and 1.0.")
        if self.valid and self.bbox is None:
            raise ValueError("A valid resource bar measurement requires a bbox.")
        if not self.valid and self.bbox is not None:
            raise ValueError("An invalid resource bar measurement cannot include a bbox.")

    @property
    def percent(self) -> float:
        return self.fraction * 100.0


def measure_resource_bar(
    frame_bgr: np.ndarray,
    spec: ResourceBarSpec,
) -> ResourceBarMeasurement:
    """
    Measure one color-coded bar inside a configured ROI.

    The ROI should describe the fill area, not the decorative frame around it.
    Confidence measures segmentation cleanliness inside that configured ROI; it
    does not prove that the ROI still points at the intended UI element.
    """

    if frame_bgr is None or frame_bgr.size == 0:
        raise ValueError("Frame image is empty.")

    frame_h, frame_w = frame_bgr.shape[:2]
    roi_x, roi_y, roi_w, roi_h = spec.roi
    left = max(0, roi_x)
    top = max(0, roi_y)
    right = min(frame_w, roi_x + roi_w)
    bottom = min(frame_h, roi_y + roi_h)

    if right <= left or bottom <= top:
        return _invalid_measurement(spec.name)

    crop = frame_bgr[top:bottom, left:right]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for hsv_range in spec.hsv_ranges:
        current = cv2.inRange(
            hsv,
            np.asarray(hsv_range.lower, dtype=np.uint8),
            np.asarray(hsv_range.upper, dtype=np.uint8),
        )
        mask = cv2.bitwise_or(mask, current)

    matched = mask > 0
    matched_pixel_fraction = float(matched.mean())

    horizontal = spec.direction in {"left_to_right", "right_to_left"}
    slice_coverage = matched.mean(axis=0 if horizontal else 1)

    reverse = spec.direction in {"right_to_left", "bottom_to_top"}
    ordered_coverage = slice_coverage[::-1] if reverse else slice_coverage

    active_slices = ordered_coverage >= spec.min_slice_coverage
    extent = _contiguous_extent(active_slices, spec.max_gap_slices)
    total_slices = len(ordered_coverage)
    fraction = float(extent / total_slices) if total_slices else 0.0
    confidence = _segmentation_confidence(ordered_coverage, extent)

    return ResourceBarMeasurement(
        name=spec.name,
        valid=True,
        fraction=fraction,
        confidence=confidence,
        bbox=(left, top, right - left, bottom - top),
        matched_pixel_fraction=matched_pixel_fraction,
    )


def measure_resource_bars(
    frame_bgr: np.ndarray,
    specs: Iterable[ResourceBarSpec],
) -> dict[str, ResourceBarMeasurement]:
    """Measure several named bars from the same frame without state side effects."""

    items = tuple(specs)
    names = [spec.name for spec in items]
    if len(names) != len(set(names)):
        raise ValueError("Resource bar specs contain duplicate names.")

    return {spec.name: measure_resource_bar(frame_bgr, spec) for spec in items}


def _contiguous_extent(active_slices: np.ndarray, max_gap_slices: int) -> int:
    """Return the filled prefix length while tolerating short internal gaps."""

    last_active = -1
    gap_count = 0

    for index, is_active in enumerate(active_slices):
        if bool(is_active):
            last_active = index
            gap_count = 0
            continue

        gap_count += 1
        if gap_count > max_gap_slices:
            break

    return last_active + 1


def _segmentation_confidence(coverage: np.ndarray, extent: int) -> float:
    """
    Estimate how cleanly the configured color separates filled and empty slices.

    Empty/full bars are valid outcomes. This score describes segmentation quality,
    not whether the configured ROI is semantically the correct UI element.
    """

    if len(coverage) == 0:
        return 0.0

    if extent <= 0:
        return float(np.clip(1.0 - float(coverage.mean()), 0.0, 1.0))

    if extent >= len(coverage):
        return float(np.clip(float(coverage.mean()), 0.0, 1.0))

    inside_quality = float(coverage[:extent].mean())
    outside_cleanliness = 1.0 - float(coverage[extent:].mean())
    return float(np.clip((inside_quality + outside_cleanliness) / 2.0, 0.0, 1.0))


def _invalid_measurement(name: str) -> ResourceBarMeasurement:
    return ResourceBarMeasurement(
        name=name,
        valid=False,
        fraction=0.0,
        confidence=0.0,
        bbox=None,
        matched_pixel_fraction=0.0,
    )
