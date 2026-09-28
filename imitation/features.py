"""Small deterministic image features used by the imitation policy."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def _gray(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise ValueError("image must be a non-empty numpy array")
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    raise ValueError("image must be grayscale, BGR, or BGRA")


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def screen_feature(frame_bgr: np.ndarray, side: int = 48) -> np.ndarray:
    """Return a zero-mean, L2-normalised grayscale screen thumbnail."""

    size = _positive_int(side, "side")
    gray = _gray(frame_bgr)
    resized = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    feature = resized.astype(np.float32).reshape(-1)
    feature -= np.mean(feature, dtype=np.float32)
    norm = float(np.linalg.norm(feature))
    if norm > 0.0:
        feature /= norm
    else:
        feature.fill(0.0)
    return feature


def screen_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return the clamped dot product of two normalised screen features."""

    left = np.asarray(a, dtype=np.float32).reshape(-1)
    right = np.asarray(b, dtype=np.float32).reshape(-1)
    if left.shape != right.shape:
        raise ValueError("screen features must have the same shape")
    if not np.any(left) or not np.any(right):
        return 0.0
    return float(np.clip(np.dot(left, right), -1.0, 1.0))


# A click patch is PATCH_FRACTION of the frame's shorter side. It is large
# enough that the mouse cursor, drawn into recorded frames right at the click,
# covers only a few of its PATCH_CELLS x PATCH_CELLS cells.
PATCH_FRACTION = 0.16
PATCH_SIDE = 30
PATCH_CELLS = 3
PATCH_KEEP_CELLS = 6


def patch_at(
    frame_bgr: np.ndarray,
    fx: float,
    fy: float,
    fraction: float = PATCH_FRACTION,
    side: int = PATCH_SIDE,
) -> np.ndarray:
    """Return a fixed-size grayscale patch centred on a normalised point."""

    gray = _gray(frame_bgr)
    height, width = gray.shape[:2]
    x_fraction = _unit_fraction(fx, "fx")
    y_fraction = _unit_fraction(fy, "fy")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, int | float)
        or not np.isfinite(fraction)
        or fraction <= 0.0
    ):
        raise ValueError("fraction must be a positive finite number")
    output_side = _positive_int(side, "side")

    crop_side = max(8, round(float(fraction) * min(height, width)))
    center_x = round(x_fraction * (width - 1))
    center_y = round(y_fraction * (height - 1))
    left = center_x - crop_side // 2
    top = center_y - crop_side // 2
    right = left + crop_side
    bottom = top + crop_side

    pad_left = max(0, -left)
    pad_top = max(0, -top)
    pad_right = max(0, right - width)
    pad_bottom = max(0, bottom - height)
    padded = cv2.copyMakeBorder(
        gray,
        pad_top,
        pad_bottom,
        pad_left,
        pad_right,
        cv2.BORDER_REPLICATE,
    )
    left += pad_left
    top += pad_top
    crop = padded[top : top + crop_side, left : left + crop_side]
    return cv2.resize(crop, (output_side, output_side), interpolation=cv2.INTER_AREA).astype(
        np.uint8,
        copy=False,
    )


def _unit_fraction(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number in [0, 1]")
    number = float(value)
    if not np.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be a number in [0, 1]")
    return number


def patch_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return normalised cross-correlation for two grayscale patches."""

    left = np.asarray(a, dtype=np.float32)
    right = np.asarray(b, dtype=np.float32)
    if left.shape != right.shape or left.size == 0:
        raise ValueError("patches must be non-empty and have the same shape")

    left_std = float(left.std())
    right_std = float(right.std())
    left_flat = left_std < 2.0
    right_flat = right_std < 2.0
    if left_flat and right_flat:
        return 1.0 if abs(float(left.mean()) - float(right.mean())) <= 8.0 else 0.0
    if left_flat or right_flat:
        return 0.0

    left_zero = left - left.mean()
    right_zero = right - right.mean()
    denominator = float(np.linalg.norm(left_zero) * np.linalg.norm(right_zero))
    if denominator == 0.0:
        return 0.0
    return float(np.clip(np.sum(left_zero * right_zero) / denominator, -1.0, 1.0))


def cell_patch_similarity(
    a: np.ndarray,
    b: np.ndarray,
    cells: int = PATCH_CELLS,
    keep: int = PATCH_KEEP_CELLS,
) -> float:
    """Mean of the `keep` best per-cell NCC scores over a cells x cells grid.

    Dropping the worst cells tolerates small occluders such as the mouse
    cursor or a sparkle effect, which otherwise sink a whole-patch NCC.
    """

    left = np.asarray(a, dtype=np.float32)
    right = np.asarray(b, dtype=np.float32)
    if left.shape != right.shape or left.ndim != 2 or left.size == 0:
        raise ValueError("patches must be non-empty 2-D arrays with the same shape")
    grid = _positive_int(cells, "cells")
    best = _positive_int(keep, "keep")
    if best > grid * grid:
        raise ValueError("keep cannot exceed cells * cells")
    height, width = left.shape
    if height < grid or width < grid:
        raise ValueError("patches are smaller than the cell grid")
    rows = np.linspace(0, height, grid + 1).astype(int)
    columns = np.linspace(0, width, grid + 1).astype(int)
    scores = [
        patch_similarity(
            left[rows[r] : rows[r + 1], columns[c] : columns[c + 1]],
            right[rows[r] : rows[r + 1], columns[c] : columns[c + 1]],
        )
        for r in range(grid)
        for c in range(grid)
    ]
    scores.sort(reverse=True)
    return float(np.mean(scores[:best]))


def read_frame(path: str | Path) -> np.ndarray | None:
    """Read a BGR frame without failing on non-ASCII paths or bad images."""

    try:
        data = np.fromfile(Path(path), dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if data.size == 0:
        return None
    try:
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except cv2.error:
        return None
