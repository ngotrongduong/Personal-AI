from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from agent.profile import (
    PROFILE_FILENAME,
    DetectorDefinition,
    MeterDefinition,
    ProfileError,
    load_profile,
    save_profile,
)
from vision.resource_bar import HSVRange


def _meter_block(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {
        "name": "hp",
        "roi": [10, 20, 200, 16],
        "hsv_ranges": [
            {"lower": [50, 180, 120], "upper": [80, 255, 255]}
        ],
        "direction": "left_to_right",
        "min_slice_coverage": 0.5,
        "max_gap_slices": 1,
        "min_confidence": 0.8,
    }
    block.update(overrides)
    return block


def _profile(*, meters: list[object] | None = None) -> dict[str, object]:
    return {
        "format_version": 1,
        "name": "Meter test",
        "permissions": {"allowed_keys": []},
        "detectors": [],
        "meters": [] if meters is None else meters,
        "skills": [],
        "rules": [],
        "planner": {"enabled": False},
    }


def _write_png(path: Path) -> None:
    image = np.zeros((12, 16, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded.tobytes())


class ProfileMeterLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "profile"
        self.folder.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, data: dict[str, object]):
        (self.folder / PROFILE_FILENAME).write_text(
            json.dumps(data), encoding="utf-8"
        )
        return load_profile(self.folder)

    def assert_rejected(self, data: dict[str, object], fragment: str) -> None:
        with self.assertRaises(ProfileError) as caught:
            self.load(data)
        self.assertIn(fragment, str(caught.exception))

    def test_meter_loads_with_all_fields(self) -> None:
        profile = self.load(_profile(meters=[_meter_block()]))

        self.assertEqual(len(profile.meters), 1)
        meter = profile.meters[0]
        self.assertEqual(meter.name, "hp")
        self.assertEqual(meter.roi, (10, 20, 200, 16))
        self.assertEqual(meter.direction, "left_to_right")
        self.assertEqual(meter.min_slice_coverage, 0.5)
        self.assertEqual(meter.max_gap_slices, 1)
        self.assertEqual(meter.min_confidence, 0.8)
        self.assertEqual(meter.hsv_ranges[0].lower, (50, 180, 120))
        self.assertEqual(meter.hsv_ranges[0].upper, (80, 255, 255))
        self.assertEqual(meter.spec().name, "hp")

    def test_meter_defaults_are_stable(self) -> None:
        block = _meter_block()
        for field in (
            "direction",
            "min_slice_coverage",
            "max_gap_slices",
            "min_confidence",
        ):
            block.pop(field)

        meter = self.load(_profile(meters=[block])).meters[0]

        self.assertEqual(meter.direction, "left_to_right")
        self.assertEqual(meter.min_slice_coverage, 0.5)
        self.assertEqual(meter.max_gap_slices, 1)
        self.assertEqual(meter.min_confidence, 0.8)

    def test_duplicate_meter_names_are_rejected(self) -> None:
        self.assert_rejected(
            _profile(meters=[_meter_block(), _meter_block()]),
            "Duplicate meter name",
        )

    def test_detector_meter_name_collision_is_rejected(self) -> None:
        data = _profile(meters=[_meter_block()])
        data["detectors"] = [
            {
                "name": "hp",
                "template": "templates/hp.png",
                "threshold": 0.9,
                "roi": None,
            }
        ]
        _write_png(self.folder / "templates" / "hp.png")

        self.assert_rejected(data, "share one observation namespace")

    def test_unknown_meter_and_hsv_fields_are_rejected(self) -> None:
        block = _meter_block(surprise=1)
        self.assert_rejected(_profile(meters=[block]), "unknown field")

        block = _meter_block()
        ranges = block["hsv_ranges"]
        assert isinstance(ranges, list)
        ranges[0]["surprise"] = 1
        self.assert_rejected(_profile(meters=[block]), "unknown field")

    def test_roi_is_required_and_positive(self) -> None:
        for roi in (None, [0, 0, 0, 10], [0, 0, 10, 0], [1, 2, 3]):
            with self.subTest(roi=roi):
                self.assert_rejected(
                    _profile(meters=[_meter_block(roi=roi)]),
                    "roi",
                )

    def test_hsv_ranges_are_strict(self) -> None:
        cases = [
            [],
            [{"lower": [180, 0, 0], "upper": [179, 255, 255]}],
            [{"lower": [0, -1, 0], "upper": [10, 255, 255]}],
            [{"lower": [0, 0, 0], "upper": [10, 256, 255]}],
            [{"lower": [0, 0], "upper": [10, 255, 255]}],
            [{"lower": [10, 0, 0], "upper": [5, 255, 255]}],
        ]
        for ranges in cases:
            with self.subTest(ranges=ranges):
                self.assert_rejected(
                    _profile(meters=[_meter_block(hsv_ranges=ranges)]),
                    "hsv",
                )

    def test_direction_and_numeric_limits_are_strict(self) -> None:
        cases = [
            ({"direction": "diagonal"}, "direction"),
            ({"direction": 1}, "direction"),
            ({"min_slice_coverage": 0}, "min_slice_coverage"),
            ({"min_slice_coverage": 1.1}, "min_slice_coverage"),
            ({"max_gap_slices": -1}, "max_gap_slices"),
            ({"max_gap_slices": 1.5}, "max_gap_slices"),
            ({"max_gap_slices": True}, "max_gap_slices"),
            ({"min_confidence": -0.1}, "min_confidence"),
            ({"min_confidence": 1.1}, "min_confidence"),
        ]
        for overrides, fragment in cases:
            with self.subTest(overrides=overrides):
                self.assert_rejected(
                    _profile(meters=[_meter_block(**overrides)]),
                    fragment,
                )


class ProfileMeterSaveTests(unittest.TestCase):
    def test_save_load_round_trip_preserves_meter_meaning(self) -> None:
        meter = MeterDefinition(
            name="mana",
            roi=(5, 7, 120, 12),
            hsv_ranges=(
                HSVRange((95, 120, 100), (130, 255, 255)),
                HSVRange((80, 100, 80), (94, 255, 255)),
            ),
            direction="right_to_left",
            min_slice_coverage=0.65,
            max_gap_slices=2,
            min_confidence=0.91,
        )

        with tempfile.TemporaryDirectory() as tmp:
            folder = save_profile(tmp, "Meter Game", meters=[meter])
            loaded = load_profile(folder)

            self.assertEqual(loaded.meters, (meter,))
            raw = json.loads((folder / PROFILE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(raw["meters"][0]["name"], "mana")
            self.assertEqual(raw["meters"][0]["roi"], [5, 7, 120, 12])
            self.assertEqual(raw["meters"][0]["direction"], "right_to_left")
            self.assertEqual(raw["meters"][0]["min_confidence"], 0.91)

    def test_save_rejects_detector_meter_collision_before_writing(self) -> None:
        meter = MeterDefinition(
            name="hp",
            roi=(0, 0, 100, 10),
            hsv_ranges=(HSVRange((50, 100, 100), (80, 255, 255)),),
        )
        template = np.zeros((10, 10, 3), dtype=np.uint8)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ProfileError, "observation namespace"):
                save_profile(
                    root,
                    "Collision",
                    detectors=[(DetectorDefinition("hp", ""), template)],
                    meters=[meter],
                )
            self.assertFalse((root / "collision").exists())


if __name__ == "__main__":
    unittest.main()
