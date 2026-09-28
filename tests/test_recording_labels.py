from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from agent.meter_conditions import METER_SOURCE
from agent.skills import ClickSkill, HoldSkill, PressSkill
from recording.dataset import EventLine
from recording.labels import label_events, label_to_dict
from recording.schema import (
    FrameEvent,
    KeyEvent,
    MarkerEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    ObservationRecord,
    StateEvent,
)
from recording.session_writer import SessionWriter
from scripts import recordings as recording_cli


@dataclass(frozen=True)
class FakeTapSkill:
    TYPE = "tap"

    name: str
    at: tuple[float, float]
    enabled: bool = False


def _observation(
    name: str,
    *,
    bbox: tuple[int, int, int, int] = (10, 10, 40, 40),
    visible: bool = True,
    confidence: float = 0.93,
    source: str = "vision:template",
    observed_t: float = 1.0,
) -> ObservationRecord:
    return ObservationRecord(
        name=name,
        visible=visible,
        confidence=confidence,
        bbox=bbox,
        value=None,
        source=source,
        observed_t=observed_t,
    )


def _state(*observations: ObservationRecord, t: float = 1.0) -> StateEvent:
    return StateEvent(t=t, observations=observations)


class MouseLabelTests(unittest.TestCase):
    def test_click_inside_fresh_bbox_matches_and_normalizes_point(self) -> None:
        skill = ClickSkill("wait", "btn_wait", min_confidence=0.9)
        events = [
            _state(_observation("btn_wait")),
            MouseButtonEvent(1.2, "left", "down", 25, 30),
        ]

        result = label_events(events, [skill], client_width=100, client_height=80)

        label = result.labels[0]
        self.assertEqual((label.skill, label.kind, label.input), ("wait", "click", "mouse:left"))
        self.assertEqual((label.x, label.y), (0.25, 0.375))
        self.assertEqual(label.detail, "inside btn_wait (conf 0.93)")
        self.assertEqual(label_to_dict(label)["skill"], "wait")

    def test_smallest_matching_bbox_wins_and_ties_use_profile_order(self) -> None:
        large = ClickSkill("large", "large")
        small_first = ClickSkill("small_first", "small_first")
        small_second = ClickSkill("small_second", "small_second")
        events = [
            _state(
                _observation("large", bbox=(0, 0, 80, 80)),
                _observation("small_first", bbox=(10, 10, 20, 20)),
                _observation("small_second", bbox=(10, 10, 20, 20)),
            ),
            MouseButtonEvent(1.1, "left", "down", 15, 15),
        ]

        result = label_events(
            events, [large, small_first, small_second], client_width=100, client_height=100
        )

        self.assertEqual(result.labels[0].skill, "small_first")

    def test_unusable_observation_does_not_match_click(self) -> None:
        skill = ClickSkill("click", "target", min_confidence=0.9)
        cases = {
            "stale": _observation("target", observed_t=0.0),
            "invisible": _observation("target", visible=False),
            "low confidence": _observation("target", confidence=0.89),
            "meter source": _observation("target", source=METER_SOURCE),
        }
        for name, observation in cases.items():
            with self.subTest(name=name):
                result = label_events(
                    [_state(observation), MouseButtonEvent(2.0, "left", "down", 20, 20)],
                    [skill],
                    client_width=100,
                    client_height=100,
                )
                self.assertEqual(result.labels[0].kind, "unlabeled")

    def test_nearest_tap_within_radius_matches(self) -> None:
        events = [MouseButtonEvent(1.0, "left", "down", 52, 50)]
        skills = [FakeTapSkill("farther", (0.55, 0.5)), FakeTapSkill("near", (0.5, 0.5))]

        result = label_events(
            events, skills, client_width=100, client_height=100, radius=0.05
        )

        self.assertEqual((result.labels[0].skill, result.labels[0].kind), ("near", "tap"))

    def test_tap_outside_radius_is_unlabeled(self) -> None:
        result = label_events(
            [MouseButtonEvent(1.0, "left", "down", 80, 80)],
            [FakeTapSkill("centre", (0.5, 0.5))],
            client_width=100,
            client_height=100,
            radius=0.03,
        )
        self.assertEqual(result.labels[0].kind, "unlabeled")

    def test_right_button_is_an_unlabeled_input_and_non_inputs_are_skipped(self) -> None:
        events = [
            MouseMoveEvent(0.5, 1, 2),
            FrameEvent(0.6, 1, "frames/000001.jpg"),
            MouseButtonEvent(1.0, "right", "down", 20, 30),
            MouseButtonEvent(1.1, "right", "up", 20, 30),
        ]
        result = label_events(events, [], client_width=100, client_height=100)

        self.assertEqual(len(result.labels), 1)
        self.assertEqual(result.labels[0].input, "mouse:right")
        self.assertEqual(result.labels[0].kind, "unlabeled")


class KeyLabelTests(unittest.TestCase):
    def test_press_and_hold_are_selected_by_duration(self) -> None:
        skills = [PressSkill("press_space", "space"), HoldSkill("hold_space", "space", 1.0)]
        events = [
            KeyEvent(1.0, "space", "down"),
            KeyEvent(1.2, "space", "up"),
            KeyEvent(2.0, "space", "down"),
            KeyEvent(2.6, "space", "up"),
        ]

        result = label_events(events, skills, client_width=100, client_height=100)

        self.assertEqual(
            [(label.skill, label.kind) for label in result.labels],
            [("press_space", "press"), ("hold_space", "hold")],
        )

    def test_auto_repeat_down_is_ignored(self) -> None:
        events = [
            KeyEvent(1.0, "x", "down"),
            KeyEvent(1.1, "x", "down"),
            KeyEvent(1.2, "x", "up"),
        ]
        result = label_events(
            events, [PressSkill("press_x", "x")], client_width=100, client_height=100
        )
        self.assertEqual(result.total_inputs, 1)

    def test_never_released_and_unknown_keys(self) -> None:
        events = [KeyEvent(1.0, "space", "down"), KeyEvent(2.0, "z", "down")]
        result = label_events(
            events, [HoldSkill("hold_space", "space", 1.0)], client_width=100, client_height=100
        )

        self.assertEqual((result.labels[0].skill, result.labels[0].kind), ("hold_space", "hold"))
        self.assertEqual((result.labels[1].skill, result.labels[1].input), (None, "key:z"))

    def test_counts_total_stable_sort_and_event_line(self) -> None:
        events = [
            EventLine(8, KeyEvent(2.0, "q", "down")),
            EventLine(3, KeyEvent(1.0, "x", "down")),
            EventLine(4, KeyEvent(1.1, "x", "up")),
        ]
        result = label_events(
            events, [PressSkill("press_x", "x")], client_width=100, client_height=100
        )

        self.assertEqual([label.line for label in result.labels], [3, 8])
        self.assertEqual(result.counts, {"press_x": 1, "unlabeled": 1})
        self.assertEqual(result.total_inputs, 2)


class ValidationTests(unittest.TestCase):
    def test_invalid_options_raise_value_error(self) -> None:
        cases = [
            {"client_width": 0, "client_height": 10},
            {"client_width": True, "client_height": 10},
            {"client_width": 10, "client_height": -1},
            {"client_width": 10, "client_height": 10, "radius": 0},
            {"client_width": 10, "client_height": 10, "radius": 0.51},
            {"client_width": 10, "client_height": 10, "max_state_age": 0},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ValueError):
                label_events([], [], **options)


def _write_profile(folder: Path) -> None:
    templates = folder / "templates"
    templates.mkdir(parents=True)
    ok, encoded = cv2.imencode(".png", np.zeros((8, 8, 3), dtype=np.uint8))
    assert ok
    encoded.tofile(templates / "button.png")
    profile = {
        "format_version": 1,
        "name": "Label CLI",
        "permissions": {"allowed_keys": ["space"]},
        "detectors": [
            {"name": "button", "template": "templates/button.png", "threshold": 0.8}
        ],
        "skills": [
            {"name": "click_button", "type": "click", "detector": "button"},
            {"name": "press_space", "type": "press", "key": "space"},
        ],
        "rules": [],
        "planner": {"enabled": False},
    }
    (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")


def _write_session(root: Path) -> Path:
    writer = SessionWriter(
        root,
        app_version="1.2.0",
        window_title="Label test",
        client_size=(100, 80),
        record_fps=10,
        session_name="label_session",
        clock=lambda: 100.0,
    )
    writer.write_event(MarkerEvent(0.0, "start"))
    state = _state(_observation("button", bbox=(10, 10, 30, 30), observed_t=0.1), t=0.1)
    writer.write_frame(np.zeros((80, 100, 3), dtype=np.uint8), 0.1, state)
    writer.write_event(MouseButtonEvent(0.2, "left", "down", 20, 20))
    writer.write_event(KeyEvent(0.3, "space", "down"))
    writer.write_event(KeyEvent(0.4, "space", "up"))
    writer.write_event(MarkerEvent(0.5, "stop", "user"))
    writer.close("user", join_timeout=5.0)
    assert writer.wait_finished(5.0)
    return writer.session_dir


class LabelCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recordings = self.root / "recordings"
        self.profiles = self.root / "profiles"
        self.profile = self.profiles / "test_profile"
        _write_profile(self.profile)
        self.session = _write_session(self.recordings)

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(recording_cli, "PROFILES_ROOT", self.profiles),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = recording_cli.main(["--root", str(self.recordings), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_label_summary_and_jsonl_output(self) -> None:
        out = self.root / "labels.jsonl"
        code, stdout, stderr = self._run(
            "label", self.session.name, self.profile.name, "--out", str(out)
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("label_session: 2 inputs, 2 labeled, 0 unlabeled", stdout)
        self.assertIn("  click_button: 1", stdout)
        self.assertIn("  press_space: 1", stdout)
        rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["skill"] for row in rows], ["click_button", "press_space"])

    def test_refuses_overwrite_and_output_under_profiles(self) -> None:
        out = self.root / "labels.jsonl"
        out.write_text("keep", encoding="utf-8")
        code, _, stderr = self._run(
            "label", self.session.name, self.profile.name, "--out", str(out)
        )
        self.assertEqual(code, 2)
        self.assertIn("already exists", stderr)
        self.assertEqual(out.read_text(encoding="utf-8"), "keep")

        code, _, stderr = self._run(
            "label",
            self.session.name,
            self.profile.name,
            "--out",
            str(self.profile / "labels.jsonl"),
        )
        self.assertEqual(code, 2)
        self.assertIn("inside", stderr)

    def test_bad_profile_returns_exit_two(self) -> None:
        broken = self.profiles / "broken"
        broken.mkdir()
        code, _, stderr = self._run("label", self.session.name, broken.name)
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr)
        self.assertIn("profile.json", stderr)


if __name__ == "__main__":
    unittest.main()
