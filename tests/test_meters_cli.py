from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from agent.profile import load_profile
from scripts import meters as meter_cli


def _write_png(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(encoded.tobytes())


def _profile_block() -> dict[str, object]:
    return {
        "format_version": 1,
        "name": "Meter CLI test",
        "permissions": {"allowed_keys": []},
        "detectors": [],
        "meters": [
            {
                "name": "hp",
                "roi": [0, 0, 100, 20],
                "hsv_ranges": [
                    {"lower": [50, 180, 180], "upper": [70, 255, 255]}
                ],
                "direction": "left_to_right",
                "min_slice_coverage": 0.5,
                "max_gap_slices": 0,
                "min_confidence": 0.8,
            }
        ],
        "skills": [],
        "rules": [],
        "planner": {"enabled": False},
    }


class MeterSuggestTests(unittest.TestCase):
    def test_green_suggestion_contains_green_hue(self) -> None:
        image = np.zeros((20, 100, 3), dtype=np.uint8)
        image[:, :50] = (0, 255, 0)

        ranges, stats = meter_cli.suggest_hsv_ranges(
            image,
            (0, 0, 100, 20),
            hue_radius=8,
        )

        self.assertEqual(stats["peak_hue"], 60)
        self.assertTrue(
            any(value.lower[0] <= 60 <= value.upper[0] for value in ranges)
        )
        self.assertGreater(float(stats["eligible_fraction"]), 0.49)

    def test_red_wrap_produces_ranges_at_both_hue_edges(self) -> None:
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        image[:, :] = (0, 0, 255)

        ranges, stats = meter_cli.suggest_hsv_ranges(
            image,
            (0, 0, 20, 20),
            hue_radius=8,
        )

        self.assertEqual(stats["peak_hue"], 0)
        self.assertEqual(len(ranges), 2)
        self.assertTrue(any(value.lower[0] == 0 for value in ranges))
        self.assertTrue(any(value.upper[0] == 179 for value in ranges))

    def test_no_color_pixels_fails_with_guidance(self) -> None:
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        with self.assertRaisesRegex(meter_cli.MeterCliError, "No sufficiently"):
            meter_cli.suggest_hsv_ranges(image, (0, 0, 20, 20))

    def test_roi_must_stay_inside_image(self) -> None:
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        with self.assertRaisesRegex(meter_cli.MeterCliError, "leaves the image"):
            meter_cli.suggest_hsv_ranges(image, (10, 10, 20, 20))


class MeterDocsExampleTests(unittest.TestCase):
    def test_documented_meter_profile_example_loads(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "examples"
            / "meter_profile.json"
        )
        data = json.loads(source.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "profile.json").write_text(
                json.dumps(data),
                encoding="utf-8",
            )
            profile = load_profile(folder)

        self.assertEqual([meter.name for meter in profile.meters], ["hp"])
        self.assertIn("heal", profile.expectations)
        self.assertIsNotNone(profile.planner.stop_when)


class MeterCliCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.profile_dir = self.root / "profile"
        self.profile_dir.mkdir()
        (self.profile_dir / "profile.json").write_text(
            json.dumps(_profile_block()),
            encoding="utf-8",
        )

        self.image = np.zeros((20, 100, 3), dtype=np.uint8)
        self.image[:, :50] = (0, 255, 0)
        self.image_path = self.root / "screen.png"
        _write_png(self.image_path, self.image)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_test_command_reports_half_full_bar(self) -> None:
        with patch("builtins.print") as printer:
            code = meter_cli.main(
                ["test", str(self.profile_dir), str(self.image_path)]
            )

        self.assertEqual(code, 0)
        rendered = "\n".join(
            " ".join(str(value) for value in call.args)
            for call in printer.call_args_list
        )
        self.assertIn("hp", rendered)
        self.assertIn("50.0%", rendered)
        self.assertIn("OK", rendered)

    def test_test_command_accepts_profile_json_path(self) -> None:
        with patch("builtins.print"):
            code = meter_cli.main(
                [
                    "test",
                    str(self.profile_dir / "profile.json"),
                    str(self.image_path),
                    "--meter",
                    "hp",
                ]
            )
        self.assertEqual(code, 0)

    def test_unknown_meter_returns_usage_error_code(self) -> None:
        with patch("builtins.print") as printer:
            code = meter_cli.main(
                [
                    "test",
                    str(self.profile_dir),
                    str(self.image_path),
                    "--meter",
                    "mana",
                ]
            )
        self.assertEqual(code, 2)
        self.assertTrue(
            any("Unknown meter" in str(call.args) for call in printer.call_args_list)
        )


    def test_suggest_rejects_options_that_profile_loader_would_reject(self) -> None:
        cases = [
            ["--name", "bad name"],
            ["--min-slice-coverage", "0"],
            ["--min-slice-coverage", "1.1"],
            ["--max-gap-slices", "-1"],
            ["--min-confidence", "-0.1"],
            ["--min-confidence", "1.1"],
        ]
        for extra in cases:
            with self.subTest(extra=extra), patch("builtins.print"):
                code = meter_cli.main(
                    [
                        "suggest",
                        str(self.image_path),
                        "--roi",
                        "0",
                        "0",
                        "100",
                        "20",
                        *extra,
                    ]
                )
                self.assertEqual(code, 2)

    def test_test_command_rejects_partially_clipped_profile_roi(self) -> None:
        data = _profile_block()
        meters = data["meters"]
        assert isinstance(meters, list)
        meters[0]["roi"] = [80, 0, 40, 20]
        (self.profile_dir / "profile.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

        with patch("builtins.print") as printer:
            code = meter_cli.main(
                ["test", str(self.profile_dir), str(self.image_path)]
            )

        self.assertEqual(code, 1)
        self.assertTrue(
            any("INVALID ROI" in str(call.args) for call in printer.call_args_list)
        )

    def test_suggest_command_stdout_is_copyable_json(self) -> None:
        outputs: list[tuple[tuple[object, ...], object | None]] = []

        def record(*args: object, **kwargs: object) -> None:
            outputs.append((args, kwargs.get("file")))

        with patch("builtins.print", side_effect=record):
            code = meter_cli.main(
                [
                    "suggest",
                    str(self.image_path),
                    "--roi",
                    "0",
                    "0",
                    "100",
                    "20",
                    "--name",
                    "hp",
                ]
            )

        self.assertEqual(code, 0)
        stdout_lines = [
            " ".join(str(value) for value in args)
            for args, file in outputs
            if file is None
        ]
        block = json.loads("\n".join(stdout_lines))
        self.assertEqual(block["name"], "hp")
        self.assertEqual(block["roi"], [0, 0, 100, 20])
        self.assertTrue(block["hsv_ranges"])


if __name__ == "__main__":
    unittest.main()