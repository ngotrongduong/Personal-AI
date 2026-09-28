"""v1.2 tap skills: requirements, intents, profile round-trip, dispatch."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import cv2
import numpy as np

from agent.action_dispatcher import ActionDispatcher
from agent.game_state import GameState, Observation
from agent.llm_planner import SkillBookCatalog
from agent.meter_conditions import METER_SOURCE, MeterCondition
from agent.profile import (
    PROFILE_FILENAME,
    DetectorDefinition,
    ProfileError,
    load_profile,
    save_profile,
)
from agent.rule_engine import ActionIntent
from agent.skill_requirements import (
    DetectorRequirement,
    MeterRequirement,
    RequirementError,
    parse_requirements,
    unmet_requirement,
)
from agent.skills import SkillBook, SkillError, SkillPermissions, TapSkill

NOW = 100.0
HWND = 77


def _state(*observations: Observation) -> GameState:
    state = GameState()
    for observation in observations:
        state.update(observation)
    return state


def _seen(name: str = "btn_wait", *, visible: bool = True, confidence: float = 0.95,
          at: float = NOW - 0.1, source: str = "vision") -> Observation:
    return Observation(name, visible, confidence, (1, 2, 3, 4), None, at, source)


def _meter(name: str = "hp", value: float | None = 0.6, *, at: float = NOW - 0.1,
           confidence: float = 0.9, source: str = METER_SOURCE) -> Observation:
    return Observation(name, value is not None, confidence, None, value, at, source)


class RequirementTests(unittest.TestCase):
    def test_parse_valid_conditions(self) -> None:
        parsed = parse_requirements(
            [{"detector": "btn_wait"}, {"detector": "enemy", "visible": False},
             {"meter": "hp", "above": 0.3}],
            {"btn_wait", "enemy"},
            {"hp"},
        )
        self.assertEqual(parsed[0], DetectorRequirement("btn_wait"))
        self.assertEqual(parsed[1], DetectorRequirement("enemy", visible=False))
        self.assertEqual(parsed[2], MeterRequirement(MeterCondition("hp", "above", 0.3)))
        self.assertEqual(parsed[1].describe(), "enemy gone")
        self.assertEqual(parsed[2].describe(), "hp above 30%")

    def test_parse_rejects_bad_conditions(self) -> None:
        bad = [
            {"detector": "nope"},
            {"meter": "nope", "below": 0.5},
            {"meter": "hp", "rises": 0.1},
            {"meter": "hp", "falls": 0.1},
            {"detector": "btn_wait", "meter": "hp", "below": 0.5},
            {"detector": "btn_wait", "extra": 1},
            {"detector": "btn_wait", "visible": "yes"},
            {"detector": "btn_wait", "min_confidence": 2},
            {},
            "btn_wait",
        ]
        for block in bad:
            with self.subTest(block=block), self.assertRaises(RequirementError):
                parse_requirements([block], {"btn_wait"}, {"hp"})
        with self.assertRaises(RequirementError):
            parse_requirements({"detector": "btn_wait"}, {"btn_wait"}, set())
        with self.assertRaises(RequirementError):
            parse_requirements([{"detector": "btn_wait"}] * 5, {"btn_wait"}, set())

    def test_detector_requirement_fails_closed(self) -> None:
        need = DetectorRequirement("btn_wait")
        self.assertTrue(need.met(_state(_seen()), now=NOW, max_age_seconds=0.75))
        for state in (
            _state(),
            _state(_seen(visible=False)),
            _state(_seen(confidence=0.5)),
            _state(_seen(at=NOW - 2.0)),
            _state(_seen(at=NOW + 1.0)),
            _state(_seen(source=METER_SOURCE)),
        ):
            self.assertFalse(need.met(state, now=NOW, max_age_seconds=0.75))

    def test_gone_requirement_needs_a_fresh_observation(self) -> None:
        gone = DetectorRequirement("enemy", visible=False)
        self.assertTrue(gone.met(_state(_seen("enemy", visible=False)), now=NOW,
                                 max_age_seconds=0.75))
        # A weak but visible detection is not "gone": this gates input.
        self.assertFalse(gone.met(_state(_seen("enemy", confidence=0.3)), now=NOW,
                                  max_age_seconds=0.75))
        self.assertFalse(gone.met(_state(_seen("enemy")), now=NOW, max_age_seconds=0.75))
        # Never observed or stale is unknown, not "gone".
        self.assertFalse(gone.met(_state(), now=NOW, max_age_seconds=0.75))
        self.assertFalse(gone.met(_state(_seen("enemy", visible=False, at=NOW - 5)),
                                  now=NOW, max_age_seconds=0.75))

    def test_meter_requirement_fails_closed(self) -> None:
        need = MeterRequirement(MeterCondition("hp", "above", 0.3))
        self.assertTrue(need.met(_state(_meter()), now=NOW, max_age_seconds=0.75))
        for state in (
            _state(),
            _state(_meter(value=0.2)),
            _state(_meter(value=None)),
            _state(_meter(confidence=0.1)),
            _state(_meter(at=NOW - 3)),
            _state(_meter(source="vision")),
        ):
            self.assertFalse(need.met(state, now=NOW, max_age_seconds=0.75))
        with self.assertRaises(RequirementError):
            MeterRequirement(MeterCondition("hp", "rises", 0.1))

    def test_unmet_requirement_returns_first_failure(self) -> None:
        needs = (DetectorRequirement("btn_wait"), MeterRequirement(MeterCondition("hp", "above", 0.3)))
        state = _state(_seen(), _meter(value=0.1))
        self.assertIs(unmet_requirement(needs, state, now=NOW, max_age_seconds=0.75), needs[1])
        self.assertIsNone(unmet_requirement(needs[:1], state, now=NOW, max_age_seconds=0.75))
        self.assertIsNone(unmet_requirement((), GameState(), now=NOW, max_age_seconds=0.75))


class TapSkillTests(unittest.TestCase):
    def test_defaults_and_normalized_point(self) -> None:
        skill = TapSkill("step", at=[1, 0.5])
        self.assertEqual(skill.at, (1.0, 0.5))
        self.assertFalse(skill.enabled)
        self.assertEqual(skill.requires, ())
        self.assertEqual(skill.describe_point(), "(100%, 50%)")

    def test_rejects_invalid_points(self) -> None:
        for at in ([0.5], [0.5, 0.5, 0.5], [1.1, 0.5], [-0.1, 0.5], [True, 0.5],
                   ["0.5", 0.5], [float("nan"), 0.5], (0.5, float("inf")), None, "0.5,0.5"):
            with self.subTest(at=at), self.assertRaises(SkillError):
                TapSkill("step", at=at)
        with self.assertRaises(SkillError):
            TapSkill("step", at=[0.5, 0.5], requires=[DetectorRequirement("b")])
        with self.assertRaises(SkillError):
            TapSkill("step", at=[0.5, 0.5], max_observation_age_seconds=0)
        with self.assertRaises(SkillError):
            TapSkill("step", at=[0.5, 0.5], enabled=1)

    def test_build_intent_carries_profile_point(self) -> None:
        skill = TapSkill("step", at=(0.25, 0.75), requires=(DetectorRequirement("btn_wait"),),
                         enabled=True)
        book = SkillBook([skill])
        result = book.build_intent("step", _state(_seen()), source="manual", now=NOW)
        self.assertTrue(result.ok)
        intent = result.intent
        self.assertEqual(intent.action, "tap")
        self.assertEqual(intent.tap_point, (0.25, 0.75))
        self.assertIsNone(intent.target_bbox)
        self.assertIsNone(intent.key)
        self.assertEqual(intent.skill_name, "step")
        self.assertIn("(25%, 75%)", intent.reason)

    def test_build_intent_blocks_on_unmet_requirement(self) -> None:
        skill = TapSkill("step", at=(0.5, 0.5), requires=(DetectorRequirement("btn_wait"),),
                         enabled=True)
        book = SkillBook([skill])
        result = book.build_intent("step", _state(_seen(at=NOW - 5)), source="manual", now=NOW)
        self.assertFalse(result.ok)
        self.assertIn("btn_wait visible", result.reason)

    def test_disabled_tap_yields_no_intent(self) -> None:
        book = SkillBook([TapSkill("step", at=(0.5, 0.5))])
        result = book.build_intent("step", GameState(), source="manual", now=NOW)
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.reason)

    def test_planner_sees_description_not_control(self) -> None:
        skill = TapSkill("step", at=(0.58, 0.47), enabled=True,
                         requires=(DetectorRequirement("btn_wait"),
                                   MeterRequirement(MeterCondition("hp", "above", 0.3))))
        summaries = SkillBookCatalog(SkillBook([skill])).runnable_skills()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].type, "tap")
        self.assertEqual(
            summaries[0].detail,
            "taps a fixed point (58%, 47%) when btn_wait visible, hp above 30%",
        )


def _profile(skills: list[dict]) -> dict:
    return {
        "format_version": 1,
        "name": "Tap test",
        "detectors": [{"name": "btn_wait", "template": "templates/btn_wait.png"}],
        "meters": [{"name": "hp", "roi": [0, 0, 10, 4],
                    "hsv_ranges": [{"lower": [0, 100, 100], "upper": [10, 255, 255]}]}],
        "skills": skills,
    }


class TapProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "tap"
        (self.folder / "templates").mkdir(parents=True)
        ok, png = cv2.imencode(".png", np.zeros((4, 4, 3), dtype=np.uint8))
        assert ok
        (self.folder / "templates" / "btn_wait.png").write_bytes(png.tobytes())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, skills: list[dict]):
        (self.folder / PROFILE_FILENAME).write_text(json.dumps(_profile(skills)), encoding="utf-8")
        return load_profile(self.folder)

    def test_loads_tap_with_requires_and_expect(self) -> None:
        profile = self.load([
            {"name": "step", "type": "tap", "at": [0.58, 0.47],
             "requires": [{"detector": "btn_wait"}, {"meter": "hp", "above": 0.3}],
             "max_observation_age_seconds": 0.5,
             "expect": {"detector": "btn_wait", "visible": True}},
        ])
        skill = profile.skills[0]
        self.assertIsInstance(skill, TapSkill)
        self.assertEqual(skill.at, (0.58, 0.47))
        self.assertEqual(len(skill.requires), 2)
        self.assertEqual(skill.max_observation_age_seconds, 0.5)
        self.assertFalse(skill.enabled)
        self.assertIn("step", profile.expectations)

    def test_rejects_bad_tap_blocks(self) -> None:
        cases = [
            ({"name": "s", "type": "tap"}, "needs 'at'"),
            ({"name": "s", "type": "tap", "at": [2, 0]}, "fractions"),
            ({"name": "s", "type": "tap", "at": [0.5, 0.5], "key": "x"}, "unknown"),
            ({"name": "s", "type": "tap", "at": [0.5, 0.5],
              "requires": [{"detector": "ghost"}]}, "unknown detector"),
            ({"name": "s", "type": "tap", "at": [0.5, 0.5],
              "requires": [{"meter": "hp", "rises": 0.1}]}, "rises"),
            ({"name": "s", "type": "tap", "at": [0.5, 0.5],
              "requires": [{"detector": "btn_wait"}] * 5}, "at most"),
        ]
        for block, fragment in cases:
            with self.subTest(block=block), self.assertRaises(ProfileError) as caught:
                self.load([block])
            self.assertIn(fragment, str(caught.exception))

    def test_save_round_trip(self) -> None:
        profile = self.load([
            {"name": "step", "type": "tap", "at": [0.58, 0.47], "enabled": True,
             "requires": [{"detector": "btn_wait", "visible": False},
                          {"meter": "hp", "below": 0.9}]},
        ])
        root = Path(self._tmp.name) / "saved"
        template = np.zeros((4, 4, 3), dtype=np.uint8)
        folder = save_profile(
            root,
            "Round trip",
            detectors=[(DetectorDefinition("btn_wait", "templates/btn_wait.png"), template)],
            meters=profile.meters,
            skills=profile.skills,
        )
        data = json.loads((folder / PROFILE_FILENAME).read_text(encoding="utf-8"))
        block = data["skills"][0]
        self.assertEqual(block["type"], "tap")
        self.assertEqual(block["at"], [0.58, 0.47])
        self.assertEqual(block["requires"][0]["visible"], False)
        self.assertEqual(block["requires"][1]["below"], 0.9)
        reloaded = load_profile(folder)
        self.assertEqual(reloaded.skills, profile.skills)


class FakeInput:
    def __init__(self) -> None:
        self.enabled = True
        self.clicks: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        if not self.enabled:
            raise RuntimeError("Input control is disabled.")
        self.clicks.append((x, y))


def _tap_intent(point, *, created_at: float = NOW) -> ActionIntent:
    return ActionIntent(
        rule_name="manual", action="tap", detector_name="", confidence=0.0, target_bbox=None,
        created_at=created_at, reason="test", skill_name="step", tap_point=point,
    )


class TapDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.input = FakeInput()
        self.foreground = True
        self.region = (1000, 500, 1400, 900)
        self.permissions: SkillPermissions | None = SkillPermissions()
        self.owns_point = True
        self.checked_points: list[tuple[int, int, int]] = []
        self.dispatcher = ActionDispatcher(
            self.input,
            region_resolver=lambda _hwnd: self.region,
            permissions_provider=lambda: self.permissions,
            foreground_checker=lambda _hwnd: self.foreground,
            point_checker=self._owns,
        )

    def _owns(self, hwnd: int, x: int, y: int) -> bool:
        self.checked_points.append((hwnd, x, y))
        return self.owns_point

    def dispatch(self, point, **kwargs):
        return self.dispatcher.dispatch(_tap_intent(point), hwnd=HWND, now=NOW + 0.1, **kwargs)

    def test_taps_point_inside_client_area(self) -> None:
        result = self.dispatch((0.25, 0.5))
        self.assertTrue(result.dispatched, result.reason)
        self.assertEqual(self.input.clicks, [(1100, 700)])
        self.assertIn("Tapped (1100, 700)", result.reason)
        self.assertEqual(self.checked_points, [(HWND, 1100, 700)])

    def test_covered_point_is_refused(self) -> None:
        self.owns_point = False
        result = self.dispatch((0.25, 0.5))
        self.assertFalse(result.dispatched)
        self.assertIn("covered or off-screen", result.reason)
        self.assertEqual(self.input.clicks, [])

        def broken(_hwnd, _x, _y):
            raise OSError("no window")

        self.dispatcher._point_checker = broken
        self.assertFalse(self.dispatch((0.25, 0.5)).dispatched)
        self.assertEqual(self.input.clicks, [])

    def test_edges_stay_inside_client_area(self) -> None:
        self.dispatcher = ActionDispatcher(
            self.input, region_resolver=lambda _hwnd: self.region,
            permissions_provider=lambda: SkillPermissions(max_actions_per_second=20),
            foreground_checker=lambda _hwnd: True, point_checker=lambda *_: True,
        )
        self.assertTrue(self.dispatch((1.0, 1.0)).dispatched)
        self.assertTrue(self.dispatcher.dispatch(_tap_intent((0.0, 0.0), created_at=NOW + 1),
                                                 hwnd=HWND, now=NOW + 1.1).dispatched)
        self.assertEqual(self.input.clicks, [(1399, 899), (1000, 500)])

    def test_point_follows_window(self) -> None:
        self.region = (0, 0, 800, 600)
        self.dispatch((0.5, 0.5))
        self.assertEqual(self.input.clicks, [(400, 300)])

    def test_blocks(self) -> None:
        cases = []
        self.foreground = False
        cases.append((self.dispatch((0.5, 0.5)), "foreground"))
        self.foreground = True
        self.permissions = None
        cases.append((self.dispatch((0.5, 0.5)), "permissions"))
        self.permissions = SkillPermissions()
        cases.append((self.dispatch(None), "no valid point"))
        cases.append((self.dispatch((1.5, 0.5)), "no valid point"))
        cases.append((self.dispatch((True, 0.5)), "no valid point"))
        cases.append((self.dispatch([0.5, 0.5]), "no valid point"))
        self.region = (10, 10, 10, 50)
        cases.append((self.dispatch((0.5, 0.5)), "no client area"))
        self.region = (1000, 500, 1400, 900)
        cases.append((self.dispatcher.dispatch(_tap_intent((0.5, 0.5)), hwnd=None, now=NOW),
                      "No target window"))
        cases.append((self.dispatcher.dispatch(_tap_intent((0.5, 0.5)), hwnd=HWND, now=NOW + 5),
                      "stale"))
        cancelled = threading.Event()
        cancelled.set()
        cases.append((self.dispatch((0.5, 0.5), cancel_event=cancelled), "Cancelled"))
        self.input.enabled = False
        cases.append((self.dispatch((0.5, 0.5)), "disabled"))
        for result, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertFalse(result.dispatched)
                self.assertIn(fragment, result.reason)
        self.assertEqual(self.input.clicks, [])

    def test_rate_limit_applies(self) -> None:
        self.permissions = SkillPermissions(max_actions_per_second=1)
        self.assertTrue(self.dispatch((0.5, 0.5)).dispatched)
        second = self.dispatcher.dispatch(_tap_intent((0.5, 0.5)), hwnd=HWND, now=NOW + 0.2)
        self.assertFalse(second.dispatched)
        self.assertIn("Rate limit", second.reason)
        self.assertEqual(len(self.input.clicks), 1)


class WindowOwnsPointTests(unittest.TestCase):
    def test_point_must_show_the_target_window(self) -> None:
        from core import window_utils

        roots = {11: 1, 12: 1, 21: 2}
        with mock.patch.object(window_utils, "win32gui") as gui:
            gui.GetAncestor.side_effect = lambda hwnd, _flag: roots[hwnd]
            for under, expected in ((12, True), (21, False), (0, False)):
                gui.WindowFromPoint.return_value = under
                with self.subTest(under=under):
                    self.assertIs(window_utils.window_owns_point(11, 0, 0), expected)
            gui.WindowFromPoint.side_effect = OSError("gone")
            self.assertFalse(window_utils.window_owns_point(11, 5, 5))
        self.assertFalse(window_utils.window_owns_point(0, 5, 5))


class FakeCursor:
    """The screen cursor: SetCursorPos moves it unless `stuck`."""

    def __init__(self, position=(5, 6)) -> None:
        self.position = position
        self.moves: list[tuple[int, int]] = []
        self.stuck = False
        self.readable = True

    def get(self):
        return self.position if self.readable else None

    def set(self, x: int, y: int) -> None:
        self.moves.append((x, y))
        if not self.stuck:
            self.position = (x, y)


class CursorRestoreTests(unittest.TestCase):
    def make(self, cursor: FakeCursor, *, getter=None, setter=None):
        from core.input_controller import InputController

        controller = InputController(
            cursor_getter=getter or cursor.get,
            cursor_setter=setter or cursor.set,
            restore_delay_seconds=0,
        )
        controller.set_enabled(True)
        return controller

    def test_click_places_cursor_exactly_then_restores(self) -> None:
        cursor = FakeCursor()
        controller = self.make(cursor)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            controller.click(100, 200)
        # pydirectinput never gets the point: its move treats 0 as "keep".
        fake.click.assert_called_once_with(button="left")
        self.assertEqual(cursor.moves, [(100, 200), (5, 6)])

    def test_zero_coordinates_are_real_coordinates(self) -> None:
        cursor = FakeCursor((700, 300))
        controller = self.make(cursor)
        clicked_at = []
        with mock.patch("core.input_controller.pydirectinput") as fake:
            fake.click.side_effect = lambda **_: clicked_at.append(cursor.position)
            controller.click(0, 0)
        self.assertEqual(clicked_at, [(0, 0)])
        self.assertEqual(cursor.position, (700, 300))

    def test_click_refused_when_cursor_does_not_arrive(self) -> None:
        cursor = FakeCursor()
        cursor.stuck = True
        controller = self.make(cursor)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            with self.assertRaises(RuntimeError):
                controller.click(100, 200)
        fake.click.assert_not_called()

        cursor = FakeCursor()
        controller = self.make(cursor, getter=lambda: None)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            with self.assertRaises(RuntimeError):
                controller.click(100, 200)
        fake.click.assert_not_called()

    def test_click_refused_when_cursor_cannot_move(self) -> None:
        def fail(_x, _y):
            raise OSError("denied")

        cursor = FakeCursor()
        controller = self.make(cursor, setter=fail)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            with self.assertRaises(RuntimeError):
                controller.click(3, 4)
        fake.click.assert_not_called()

    def test_half_a_point_is_refused(self) -> None:
        cursor = FakeCursor()
        controller = self.make(cursor)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            with self.assertRaises(RuntimeError):
                controller.click(100, None)
        fake.click.assert_not_called()
        self.assertEqual(cursor.position, (5, 6))

    def test_restores_even_when_click_fails(self) -> None:
        cursor = FakeCursor()
        controller = self.make(cursor)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            fake.click.side_effect = OSError("boom")
            with self.assertRaises(OSError):
                controller.click(100, 200)
        self.assertEqual(cursor.position, (5, 6))

    def test_click_without_point_uses_current_cursor(self) -> None:
        cursor = FakeCursor()
        controller = self.make(cursor)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            controller.click()
        fake.click.assert_called_once_with(button="left")
        self.assertEqual(cursor.moves, [(5, 6)])

    def test_disabled_input_neither_clicks_nor_moves(self) -> None:
        cursor = FakeCursor()
        controller = self.make(cursor)
        controller.set_enabled(False)
        with mock.patch("core.input_controller.pydirectinput") as fake:
            with self.assertRaises(RuntimeError):
                controller.click(100, 200)
        fake.click.assert_not_called()
        self.assertEqual(cursor.moves, [])

    def test_restore_runs_outside_the_input_lock(self) -> None:
        # F8 (set_enabled(False)) must never wait for the restore delay.
        cursor = FakeCursor()
        free_during_restore = []

        def setter(x, y):
            if cursor.moves:  # the second move is the restore
                probe = threading.Thread(
                    target=lambda: free_during_restore.append(controller._lock.acquire(timeout=1))
                )
                probe.start()
                probe.join()
                if free_during_restore[-1]:
                    controller._lock.release()
            cursor.set(x, y)

        controller = self.make(cursor, setter=setter)
        with mock.patch("core.input_controller.pydirectinput"):
            controller.click(100, 200)
        self.assertEqual(free_during_restore, [True])

    def test_restore_error_does_not_raise(self) -> None:
        cursor = FakeCursor()

        def setter(x, y):
            if (x, y) == (5, 6):
                raise OSError("denied")
            cursor.set(x, y)

        controller = self.make(cursor, setter=setter)
        with mock.patch("core.input_controller.pydirectinput"):
            controller.click(3, 4)

if __name__ == "__main__":
    unittest.main()
