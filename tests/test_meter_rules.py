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
    RuleDefinition,
    load_profile,
    save_profile,
)
from agent.rule_engine import MeterRule, RuleEngine, SKILL_RULE_ACTION
from agent.skills import PressSkill, SkillPermissions
from vision.resource_bar import HSVRange


class MeterRuleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()

    def update(
        self,
        value: float,
        at: float,
        *,
        confidence: float = 0.95,
        visible: bool = True,
    ) -> None:
        self.state.update_detector(
            "hp",
            visible=visible,
            confidence=confidence,
            value=value,
            observed_at=at,
            source="vision:resource_bar",
        )

    def test_threshold_rule_emits_skill_intent(self) -> None:
        rule = MeterRule(
            "heal_low_hp",
            MeterCondition("hp", "below", 0.3, 0.9),
            "heal",
            max_observation_age_seconds=1.0,
            cooldown_seconds=1.0,
        )
        engine = RuleEngine([rule])
        self.update(0.2, 10.0)

        intents = engine.evaluate(self.state, now=10.1)

        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertEqual(intent.rule_name, "heal_low_hp")
        self.assertEqual(intent.action, SKILL_RULE_ACTION)
        self.assertEqual(intent.detector_name, "hp")
        self.assertEqual(intent.skill_name, "heal")
        self.assertIn("20.0%", intent.reason)

    def test_invalid_low_confidence_and_stale_values_fail_closed(self) -> None:
        rule = MeterRule(
            "heal",
            MeterCondition("hp", "below", 0.3, 0.9),
            "heal",
            max_observation_age_seconds=0.5,
        )
        engine = RuleEngine([rule])

        self.assertEqual(engine.evaluate(self.state, now=10.0), [])

        self.update(0.2, 10.0, confidence=0.5)
        self.assertEqual(engine.evaluate(self.state, now=10.1), [])

        self.update(0.2, 10.0, confidence=0.95, visible=False)
        self.assertEqual(engine.evaluate(self.state, now=10.1), [])

        self.update(0.2, 10.0)
        self.assertEqual(engine.evaluate(self.state, now=10.6), [])

    def test_cooldown_and_enable_disable_match_existing_rules(self) -> None:
        rule = MeterRule(
            "heal",
            MeterCondition("hp", "below", 0.5),
            "heal",
            max_observation_age_seconds=1.0,
            cooldown_seconds=1.0,
        )
        engine = RuleEngine([rule])
        self.update(0.2, 10.0)

        self.assertEqual(len(engine.evaluate(self.state, now=10.1)), 1)
        self.assertEqual(engine.evaluate(self.state, now=10.5), [])

        engine.disable_rule("heal")
        self.update(0.2, 11.2)
        self.assertEqual(engine.evaluate(self.state, now=11.2), [])
        engine.enable_rule("heal")
        self.assertEqual(len(engine.evaluate(self.state, now=11.2)), 1)

    def test_rises_compares_consecutive_accepted_samples(self) -> None:
        rule = MeterRule(
            "big_heal",
            MeterCondition("hp", "rises", 0.2),
            "celebrate",
            max_observation_age_seconds=1.0,
            cooldown_seconds=0.0,
        )
        engine = RuleEngine([rule])

        self.update(0.40, 10.0)
        self.assertEqual(engine.evaluate(self.state, now=10.1), [])

        # Same observation is not a new sample.
        self.assertEqual(engine.evaluate(self.state, now=10.2), [])

        self.update(0.55, 10.3)
        self.assertEqual(engine.evaluate(self.state, now=10.3), [])

        self.update(0.76, 10.5)
        intents = engine.evaluate(self.state, now=10.5)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].skill_name, "celebrate")

        # The triggering sample was consumed as the new baseline.
        self.assertEqual(engine.evaluate(self.state, now=10.6), [])

    def test_invalid_sample_never_becomes_change_baseline(self) -> None:
        rule = MeterRule(
            "rise",
            MeterCondition("hp", "rises", 0.2, min_confidence=0.9),
            "heal",
            max_observation_age_seconds=1.0,
            cooldown_seconds=0.0,
        )
        engine = RuleEngine([rule])

        self.update(0.4, 10.0)
        self.assertEqual(engine.evaluate(self.state, now=10.1), [])

        self.update(0.95, 10.2, confidence=0.2)
        self.assertEqual(engine.evaluate(self.state, now=10.2), [])

        # Still compares against the last accepted 0.4 sample.
        self.update(0.61, 10.4)
        self.assertEqual(len(engine.evaluate(self.state, now=10.4)), 1)

    def test_stale_gap_resets_change_baseline(self) -> None:
        rule = MeterRule(
            "rise",
            MeterCondition("hp", "rises", 0.2),
            "heal",
            max_observation_age_seconds=0.5,
            cooldown_seconds=0.0,
        )
        engine = RuleEngine([rule])

        self.update(0.2, 10.0)
        self.assertEqual(engine.evaluate(self.state, now=10.1), [])

        # Old baseline is too old to compare across this gap.
        self.update(0.8, 11.0)
        self.assertEqual(engine.evaluate(self.state, now=11.0), [])

        self.update(1.0, 11.2)
        self.assertEqual(len(engine.evaluate(self.state, now=11.2)), 1)

    def test_disable_reenable_requires_a_fresh_change_baseline(self) -> None:
        rule = MeterRule(
            "rise",
            MeterCondition("hp", "rises", 0.2),
            "heal",
            max_observation_age_seconds=1.0,
            cooldown_seconds=0.0,
        )
        engine = RuleEngine([rule])

        self.update(0.4, 10.0)
        self.assertEqual(engine.evaluate(self.state, now=10.0), [])

        engine.disable_rule("rise")
        self.update(0.8, 10.2)
        self.assertEqual(engine.evaluate(self.state, now=10.2), [])

        engine.enable_rule("rise")
        # 0.8 becomes the new post-enable baseline; movement while disabled
        # can never trigger the rule.
        self.assertEqual(engine.evaluate(self.state, now=10.2), [])

        self.update(1.0, 10.4)
        self.assertEqual(len(engine.evaluate(self.state, now=10.4)), 1)

    def test_falls_condition(self) -> None:
        rule = MeterRule(
            "damage",
            MeterCondition("hp", "falls", 0.25),
            "panic",
            max_observation_age_seconds=1.0,
            cooldown_seconds=0.0,
        )
        engine = RuleEngine([rule])
        self.update(0.9, 10.0)
        self.assertEqual(engine.evaluate(self.state, now=10.0), [])
        self.update(0.6, 10.2)
        self.assertEqual(len(engine.evaluate(self.state, now=10.2)), 1)


def _meter_block() -> dict[str, object]:
    return {
        "name": "hp",
        "roi": [0, 0, 100, 10],
        "hsv_ranges": [
            {"lower": [50, 100, 100], "upper": [80, 255, 255]}
        ],
    }


def _profile_rule(rule: dict[str, object]) -> dict[str, object]:
    return {
        "format_version": 1,
        "name": "Meter rules",
        "permissions": {"allowed_keys": ["x"]},
        "detectors": [],
        "meters": [_meter_block()],
        "skills": [
            {
                "name": "heal",
                "type": "press",
                "key": "x",
                "enabled": False,
            }
        ],
        "rules": [rule],
        "planner": {"enabled": False},
    }


class MeterRuleProfileTests(unittest.TestCase):
    def load_data(self, data: dict[str, object]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        (folder / PROFILE_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        return load_profile(folder)

    def test_loads_meter_rule(self) -> None:
        profile = self.load_data(
            _profile_rule(
                {
                    "name": "heal_low",
                    "meter": "hp",
                    "below": 0.25,
                    "min_confidence": 0.9,
                    "skill": "heal",
                    "max_observation_age_seconds": 0.6,
                    "cooldown_seconds": 2.0,
                    "enabled": False,
                }
            )
        )

        definition = profile.rules[0]
        self.assertFalse(definition.enabled)
        self.assertEqual(
            definition.rule,
            MeterRule(
                "heal_low",
                MeterCondition("hp", "below", 0.25, 0.9),
                "heal",
                max_observation_age_seconds=0.6,
                cooldown_seconds=2.0,
            ),
        )
        self.assertFalse(profile.rule_engine().is_rule_enabled("heal_low"))

    def test_rejects_unknown_meter_skill_and_ambiguous_rule(self) -> None:
        cases = [
            (
                {
                    "name": "bad",
                    "meter": "mana",
                    "below": 0.2,
                    "skill": "heal",
                },
                "unknown meter",
            ),
            (
                {
                    "name": "bad",
                    "meter": "hp",
                    "below": 0.2,
                    "skill": "missing",
                },
                "unknown skill",
            ),
            (
                {
                    "name": "bad",
                    "meter": "hp",
                    "detector": "done",
                    "below": 0.2,
                    "skill": "heal",
                },
                "both",
            ),
            (
                {"name": "bad", "skill": "heal"},
                "exactly one",
            ),
        ]
        for rule, fragment in cases:
            with self.subTest(rule=rule), self.assertRaisesRegex(
                ProfileError, fragment
            ):
                self.load_data(_profile_rule(rule))

    def test_save_round_trip_meter_rule(self) -> None:
        meter = MeterDefinition(
            name="hp",
            roi=(0, 0, 100, 10),
            hsv_ranges=(HSVRange((50, 100, 100), (80, 255, 255)),),
        )
        definition = RuleDefinition(
            MeterRule(
                "heal_low",
                MeterCondition("hp", "below", 0.25, 0.9),
                "heal",
                max_observation_age_seconds=0.6,
                cooldown_seconds=2.0,
            ),
            enabled=False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            folder = save_profile(
                tmp,
                "Meter Rules",
                meters=[meter],
                skills=[PressSkill("heal", "x")],
                rules=[definition],
                permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
            )
            loaded = load_profile(folder)
            raw = json.loads(
                (folder / PROFILE_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(loaded.rules, (definition,))
        self.assertEqual(
            raw["rules"][0],
            {
                "name": "heal_low",
                "skill": "heal",
                "max_observation_age_seconds": 0.6,
                "cooldown_seconds": 2.0,
                "enabled": False,
                "meter": "hp",
                "below": 0.25,
                "min_confidence": 0.9,
            },
        )


if __name__ == "__main__":
    unittest.main()
