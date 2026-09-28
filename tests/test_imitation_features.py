from __future__ import annotations

import cv2
import numpy as np

from imitation.features import (
    patch_at,
    patch_similarity,
    read_frame,
    screen_feature,
    screen_similarity,
)


def test_screen_feature_is_normalised_and_flat_frames_are_zero() -> None:
    frame = np.zeros((30, 50, 3), dtype=np.uint8)
    frame[:, 25:] = (255, 255, 255)
    feature = screen_feature(frame, side=12)

    assert feature.shape == (144,)
    assert feature.dtype == np.float32
    assert abs(float(feature.mean())) < 1e-6
    assert np.isclose(np.linalg.norm(feature), 1.0)
    assert screen_similarity(feature, feature) > 0.999
    assert np.count_nonzero(screen_feature(np.full_like(frame, 50))) == 0


def test_patch_edges_are_replicated_and_similarity_handles_flat_patches() -> None:
    frame = np.zeros((10, 14, 3), dtype=np.uint8)
    frame[:, :7] = 40
    frame[:, 7:] = 180

    corner = patch_at(frame, 0.0, 0.0, fraction=0.8, side=8)
    assert corner.shape == (8, 8)
    assert corner.dtype == np.uint8
    assert int(corner[0, 0]) == 40
    assert patch_similarity(corner, corner) == 1.0
    assert patch_similarity(np.full((8, 8), 20), np.full((8, 8), 28)) == 1.0
    assert patch_similarity(np.full((8, 8), 20), np.full((8, 8), 29)) == 0.0


def test_read_frame_supports_unicode_and_bad_images(tmp_path) -> None:
    image = np.full((12, 16, 3), (10, 80, 200), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    path = tmp_path / "khung-hình.jpg"
    encoded.tofile(path)

    loaded = read_frame(path)
    assert loaded is not None
    assert loaded.shape == image.shape
    bad = tmp_path / "bad.jpg"
    bad.write_text("not an image", encoding="utf-8")
    assert read_frame(bad) is None
    assert read_frame(tmp_path / "missing.jpg") is None
