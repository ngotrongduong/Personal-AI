"""v1.0 observed effects: `expect` parsing, effect watches and the profile field."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agent.game_state import GameState
from agent.profile import PROFILE_FILENAME, ProfileError, load_profile, save_profile
from agent.skill_effects import (
    EFFECT_CONFIRMED,
    EFFECT_NOT_SEEN,
    EFFECT_PENDING,
    EffectWatch,
    Expectation,
    ExpectationError,
    observation_matches,
    parse_expectation,
)
from agent.skills import PressSkill, SkillPermissions
from tests.test_profile import ProfileTestCase, _template, _valid_profile


class ExpectationTests(unittest.TestCase):
    def test_defaults(self) -> None:
        expectation = parse_expectation({"detector": "ok"}, {"ok"})
        self.assertEqual(expectation, Expectation("ok", True, 2.0, 0.8))
        self.assertEqual(expectation.describe(), "ok visible")
        self.assertEqual(Expectation("ok", visible=False).describe(), "ok gone")

    def test_full_block_round_trips(self) -> None:
        block = {"detector": "ok", "visible": False, "within_seconds": 5, "min_confidence": 0.5}
        expectation = parse_expectation(block, ["ok"])
        self.assertEqual(expectation.to_block(), block)
        self.assertEqual(parse_expectation(expectation.to_block(), ["ok"]), expectation)

    def test_rejects_bad_blocks(self) -> None:
        bad = [
            ("not an object", "object"),
            ({"detector": "ok", "key": "x"}, "unknown field"),
            ({"detector": "missing"}, "unknown detector"),
            ({}, "unknown detector"),
            ({"detector": "ok", "visible": "yes"}, "visible"),
            ({"detector": "ok", "visible": 1}, "visible"),
            ({"detector": "ok", "within_seconds": 0}, "within_seconds"),
            ({"detector": "ok", "within_seconds": 10.5}, "within_seconds"),
            ({"detector": "ok", "within_seconds": float("nan")}, "within_seconds"),
            ({"detector": "ok", "within_seconds": True}, "within_seconds"),
            ({"detector": "ok", "min_confidence": 1.5}, "min_confidence"),
            ({"detector": "ok", "min_confidence": "high"}, "min_confidence"),
        ]
        for block, fragment in bad:
            with self.subTest(block=block):
                with self.assertRaises(ExpectationError) as caught:
                    parse_expectation(block, {"ok"})
                self.assertIn(fragment, str(caught.exception))

    def test_within_seconds_cap_is_inclusive(self) -> None:
        self.assertEqual(Expectation("ok", within_seconds=10).within_seconds, 10)


class ObservationMatchTests(unittest.TestCase):
    def test_visible_needs_confidence(self) -> None:
        state = GameState()
        seen = state.update_detector("ok", visible=True, confidence=0.9, observed_at=1.0)
        weak = state.update_detector("ok", visible=True, confidence=0.5, observed_at=2.0)
        gone = state.update_detector("ok", visible=False, confidence=0.0, observed_at=3.0)
        self.assertTrue(observation_matches(seen, visible=True, min_confidence=0.8))
        self.assertFalse(observation_matches(weak, visible=True, min_confidence=0.8))
        self.assertFalse(observation_matches(gone, visible=True, min_confidence=0.8))
        self.assertFalse(observation_matches(seen, visible=False, min_confidence=0.8))
        self.assertTrue(observation_matches(weak, visible=False, min_confidence=0.8))
        self.assertTrue(observation_matches(gone, visible=False, min_confidence=0.8))


class EffectWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()

    def watch(self, **options: object) -> EffectWatch:
        return EffectWatch("type_x", Expectation("glyph", **options), finished_at=100.0)

    def test_confirmed_by_an_observation_after_the_finish(self) -> None:
        watch = self.watch()
        self.assertEqual(watch.check(self.state, 100.1), EFFECT_PENDING)
        self.state.update_detector("glyph", visible=True, confidence=0.95, observed_at=100.5)
        self.assertEqual(watch.check(self.state, 100.6), EFFECT_CONFIRMED)
        result = watch.result
        assert result is not None
        self.assertEqual(
            (result.skill_name, result.effect, result.detector, result.waited_s),
            ("type_x", EFFECT_CONFIRMED, "glyph", 0.5),
        )
        # Resolved watches stay resolved.
        self.state.update_detector("glyph", visible=False, observed_at=100.7)
        self.assertEqual(watch.check(self.state, 200.0), EFFECT_CONFIRMED)

    def test_observations_before_the_finish_do_not_count(self) -> None:
        self.state.update_detector("glyph", visible=True, confidence=0.99, observed_at=99.0)
        watch = self.watch()
        self.assertEqual(watch.check(self.state, 101.0), EFFECT_PENDING)
        self.state.update_detector("glyph", visible=True, confidence=0.99, observed_at=100.0)
        self.assertEqual(watch.check(self.state, 101.0), EFFECT_PENDING)

    def test_not_seen_after_the_window(self) -> None:
        self.state.update_detector("glyph", visible=True, confidence=0.5, observed_at=101.0)
        watch = self.watch(within_seconds=2.0)
        self.assertEqual(watch.check(self.state, 101.9), EFFECT_PENDING)
        self.assertEqual(watch.check(self.state, 102.0), EFFECT_NOT_SEEN)
        result = watch.result
        assert result is not None
        self.assertEqual((result.effect, result.waited_s), (EFFECT_NOT_SEEN, 2.0))

    def test_missing_detector_is_not_seen_not_an_error(self) -> None:
        watch = self.watch(within_seconds=1.0)
        self.assertEqual(watch.check(self.state, 100.5), EFFECT_PENDING)
        self.assertEqual(watch.check(self.state, 101.5), EFFECT_NOT_SEEN)

    def test_expecting_a_detector_to_disappear(self) -> None:
        self.state.update_detector("glyph", visible=True, confidence=0.9, observed_at=99.0)
        watch = self.watch(visible=False)
        self.assertEqual(watch.check(self.state, 100.2), EFFECT_PENDING)
        self.state.update_detector("glyph", visible=True, confidence=0.9, observed_at=100.3)
        self.assertEqual(watch.check(self.state, 100.4), EFFECT_PENDING)
        self.state.update_detector("glyph", visible=False, observed_at=100.8)
        self.assertEqual(watch.check(self.state, 100.9), EFFECT_CONFIRMED)

    def test_a_late_observation_after_the_deadline_is_not_seen(self) -> None:
        watch = self.watch(within_seconds=1.0)
        # The poll came late; the only match was made after the window closed.
        self.state.update_detector("glyph", visible=True, confidence=0.9, observed_at=101.5)
        self.assertEqual(watch.check(self.state, 101.6), EFFECT_NOT_SEEN)


class ProfileExpectTests(ProfileTestCase):
    def test_expect_is_parsed_into_expectations(self) -> None:
        data = _valid_profile()
        data["skills"][1]["expect"] = {"detector": "ok_button", "within_seconds": 3}
        data["skills"][0]["expect"] = {"detector": "ok_button", "visible": False}
        profile = load_profile(self.write(data))
        self.assertEqual(
            dict(profile.expectations),
            {
                "type_x": Expectation("ok_button", True, 3, 0.8),
                "press_ok": Expectation("ok_button", False),
            },
        )
        self.assertNotIn("hold_space", profile.expectations)
        # The skills themselves are unchanged by `expect`.
        self.assertEqual(profile.skills[1], PressSkill("type_x", "x"))
        with self.assertRaises(TypeError):
            profile.expectations["hold_space"] = Expectation("ok_button")  # type: ignore[index]

    def test_profile_without_expect_has_none(self) -> None:
        profile = load_profile(self.write(_valid_profile()))
        self.assertEqual(dict(profile.expectations), {})

    def test_bad_expect_rejected_on_load(self) -> None:
        for expect, fragment in [
            ({"detector": "nope"}, "unknown detector"),
            ({"detector": "ok_button", "within_seconds": 60}, "within_seconds"),
            ({"detector": "ok_button", "retry": True}, "unknown field"),
            (["ok_button"], "object"),
        ]:
            with self.subTest(expect=expect):
                data = _valid_profile()
                data["skills"][1]["expect"] = expect
                error = self.assert_rejected(data, fragment)
                self.assertIn("type_x", str(error))

    def test_save_round_trips_expect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from agent.profile import DetectorDefinition

            expectation = Expectation("ok_button", False, 4.0, 0.7)
            folder = save_profile(
                tmp,
                "Effects",
                detectors=[(DetectorDefinition("ok_button", "", 0.9), _template())],
                skills=[PressSkill("type_x", "x"), PressSkill("type_y", "x")],
                permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
                expectations={"type_x": expectation},
            )
            written = json.loads((Path(folder) / PROFILE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(written["skills"][0]["expect"], expectation.to_block())
            self.assertNotIn("expect", written["skills"][1])
            self.assertEqual(dict(load_profile(folder).expectations), {"type_x": expectation})

    def test_save_rejects_expect_on_an_unknown_detector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProfileError):
                save_profile(
                    tmp,
                    "Effects",
                    skills=[PressSkill("type_x", "x")],
                    permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
                    expectations={"type_x": Expectation("gone_detector")},
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
