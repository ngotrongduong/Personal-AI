from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agent.game_state import GameState
from agent.meter_conditions import MeterCondition
from agent.profile import (
    PROFILE_FILENAME,
    MeterDefinition,
    ProfileError,
    load_profile,
    save_profile,
)
from agent.skill_effects import (
    EFFECT_CONFIRMED,
    EFFECT_NOT_SEEN,
    EFFECT_PENDING,
    EffectWatch,
    Expectation,
    ExpectationError,
    MeterExpectation,
    parse_expectation,
)
from agent.skills import PressSkill, SkillPermissions
from vision.resource_bar import HSVRange


def _meter_block() -> dict[str, object]:
    return {
        "name": "hp",
        "roi": [0, 0, 100, 10],
        "hsv_ranges": [
            {"lower": [50, 100, 100], "upper": [80, 255, 255]}
        ],
        "min_confidence": 0.8,
    }


def _profile_with_expect(expect: object) -> dict[str, object]:
    return {
        "format_version": 1,
        "name": "Meter effects",
        "permissions": {"allowed_keys": ["x"]},
        "detectors": [],
        "meters": [_meter_block()],
        "skills": [
            {
                "name": "heal",
                "type": "press",
                "key": "x",
                "enabled": False,
                "expect": expect,
            }
        ],
        "rules": [],
        "planner": {"enabled": False},
    }


class MeterExpectationParseTests(unittest.TestCase):
    def test_detector_expectation_is_unchanged(self) -> None:
        parsed = parse_expectation(
            {"detector": "done", "visible": False},
            {"done"},
            {"hp"},
        )
        self.assertEqual(parsed, Expectation("done", False))

    def test_meter_threshold_and_change_parse(self) -> None:
        threshold = parse_expectation(
            {"meter": "hp", "above": 0.8, "within_seconds": 3},
            set(),
            {"hp"},
        )
        self.assertEqual(
            threshold,
            MeterExpectation(MeterCondition("hp", "above", 0.8), 3.0),
        )
        self.assertEqual(
            threshold.to_block(),
            {
                "meter": "hp",
                "above": 0.8,
                "min_confidence": 0.8,
                "within_seconds": 3.0,
            },
        )

        change = parse_expectation(
            {"meter": "hp", "rises": 0.2, "min_confidence": 0.9},
            set(),
            {"hp"},
        )
        self.assertEqual(
            change,
            MeterExpectation(MeterCondition("hp", "rises", 0.2, 0.9)),
        )

    def test_unknown_or_ambiguous_target_is_rejected(self) -> None:
        cases = [
            ({"meter": "mana", "below": 0.2}, "unknown meter"),
            (
                {"meter": "hp", "detector": "done", "below": 0.2},
                "exactly one",
            ),
            ({"below": 0.2}, "exactly one"),
            ({"meter": "hp", "below": 0.2, "retry": True}, "unknown field"),
        ]
        for block, fragment in cases:
            with self.subTest(block=block), self.assertRaisesRegex(
                ExpectationError, fragment
            ):
                parse_expectation(block, {"done"}, {"hp"})


class MeterEffectWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()

    def update(
        self,
        value: float,
        *,
        observed_at: float,
        confidence: float = 0.95,
        visible: bool = True,
    ) -> None:
        self.state.update_detector(
            "hp",
            visible=visible,
            confidence=confidence,
            value=value,
            observed_at=observed_at,
            source="vision:resource_bar",
        )

    def test_threshold_confirms_only_after_step(self) -> None:
        expectation = MeterExpectation(MeterCondition("hp", "above", 0.7))
        watch = EffectWatch("heal", expectation, finished_at=100.0)

        self.update(0.9, observed_at=99.9)
        self.assertEqual(watch.check(self.state, 100.2), EFFECT_PENDING)

        self.update(0.75, observed_at=100.4)
        self.assertEqual(watch.check(self.state, 100.5), EFFECT_CONFIRMED)
        self.assertEqual(watch.result.detector, "hp")  # type: ignore[union-attr]

    def test_low_confidence_and_invalid_values_fail_closed(self) -> None:
        expectation = MeterExpectation(
            MeterCondition("hp", "below", 0.3, min_confidence=0.9),
            within_seconds=1.0,
        )
        watch = EffectWatch("heal", expectation, finished_at=100.0)

        self.update(0.2, observed_at=100.3, confidence=0.5)
        self.assertEqual(watch.check(self.state, 100.5), EFFECT_PENDING)
        self.assertEqual(watch.check(self.state, 101.0), EFFECT_NOT_SEEN)

    def test_change_requires_explicit_valid_baseline(self) -> None:
        expectation = MeterExpectation(
            MeterCondition("hp", "rises", 0.2),
            within_seconds=1.0,
        )
        no_baseline = EffectWatch("heal", expectation, finished_at=100.0)
        self.update(0.9, observed_at=100.2)
        self.assertEqual(no_baseline.check(self.state, 100.3), EFFECT_PENDING)
        self.assertEqual(no_baseline.check(self.state, 101.0), EFFECT_NOT_SEEN)

        watch = EffectWatch(
            "heal",
            expectation,
            finished_at=200.0,
            baseline_value=0.4,
        )
        self.update(0.61, observed_at=200.2)
        self.assertEqual(watch.check(self.state, 200.3), EFFECT_CONFIRMED)


class MeterExpectationProfileTests(unittest.TestCase):
    def test_profile_loads_meter_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / PROFILE_FILENAME).write_text(
                json.dumps(
                    _profile_with_expect(
                        {
                            "meter": "hp",
                            "below": 0.25,
                            "within_seconds": 4,
                            "min_confidence": 0.9,
                        }
                    )
                ),
                encoding="utf-8",
            )
            profile = load_profile(folder)

        self.assertEqual(
            profile.expectations["heal"],
            MeterExpectation(
                MeterCondition("hp", "below", 0.25, 0.9),
                within_seconds=4.0,
            ),
        )

    def test_profile_rejects_unknown_meter_expectation(self) -> None:
        data = _profile_with_expect({"meter": "mana", "below": 0.2})
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / PROFILE_FILENAME).write_text(
                json.dumps(data), encoding="utf-8"
            )
            with self.assertRaisesRegex(ProfileError, "unknown meter"):
                load_profile(folder)

    def test_save_round_trip_meter_expectation(self) -> None:
        meter = MeterDefinition(
            name="hp",
            roi=(0, 0, 100, 10),
            hsv_ranges=(HSVRange((50, 100, 100), (80, 255, 255)),),
        )
        expectation = MeterExpectation(
            MeterCondition("hp", "rises", 0.15, 0.85),
            within_seconds=5.0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            folder = save_profile(
                tmp,
                "Meter Effect",
                meters=[meter],
                skills=[PressSkill("heal", "x")],
                permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
                expectations={"heal": expectation},
            )
            loaded = load_profile(folder)
            raw = json.loads(
                (folder / PROFILE_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(loaded.expectations["heal"], expectation)
        self.assertEqual(
            raw["skills"][0]["expect"],
            expectation.to_block(),
        )


if __name__ == "__main__":
    unittest.main()
