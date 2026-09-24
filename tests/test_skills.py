from __future__ import annotations

import math
import unittest

from agent.game_state import GameState
from agent.rule_engine import SKILL_RULE_ACTION, RuleEngine, VisibilityRule
from agent.skills import (
    FORBIDDEN_KEYS,
    HARD_MAX_HOLD_SECONDS,
    ClickSkill,
    HoldSkill,
    PressSkill,
    SkillBook,
    SkillError,
    SkillPermissions,
    permission_denial,
    validate_key,
)


def _permissions(**overrides: object) -> SkillPermissions:
    values: dict[str, object] = {
        "allowed_keys": frozenset({"x", "space"}),
        "max_hold_seconds": 1.5,
        "max_actions_per_second": 5.0,
    }
    values.update(overrides)
    return SkillPermissions(**values)  # type: ignore[arg-type]


class KeyValidationTests(unittest.TestCase):
    def test_accepts_single_canonical_keys(self) -> None:
        for key in ("x", "space", "f1", "1", "enter", ".", "/", "-"):
            with self.subTest(key=key):
                self.assertEqual(validate_key(key), key)

    def test_rejects_combos_and_non_canonical_spellings(self) -> None:
        for key in ("", " x", "X", "F1", "ctrl+c", "ctrl c", "a b", "+", "x" * 17, 5, None):
            with self.subTest(key=key):
                with self.assertRaises(SkillError):
                    validate_key(key)

    def test_rejects_every_forbidden_key(self) -> None:
        self.assertIn("f8", FORBIDDEN_KEYS)
        for key in FORBIDDEN_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(SkillError):
                    validate_key(key)


class SkillPermissionsTests(unittest.TestCase):
    def test_defaults_allow_no_keys(self) -> None:
        permissions = SkillPermissions()
        self.assertEqual(permissions.allowed_keys, frozenset())
        self.assertIsNotNone(permissions.key_denial("x"))

    def test_rejects_forbidden_or_invalid_allowed_keys(self) -> None:
        for keys in ({"f8"}, {"win"}, {"ctrl+c"}, {"X"}):
            with self.subTest(keys=keys):
                with self.assertRaises(SkillError):
                    _permissions(allowed_keys=frozenset(keys))

    def test_allowed_keys_must_be_a_frozenset(self) -> None:
        with self.assertRaises(SkillError):
            _permissions(allowed_keys={"x"})

    def test_max_hold_is_capped_by_the_hard_limit(self) -> None:
        _permissions(max_hold_seconds=HARD_MAX_HOLD_SECONDS)
        for value in (HARD_MAX_HOLD_SECONDS + 0.1, 0, -1, math.nan, math.inf, True, "1"):
            with self.subTest(value=value):
                with self.assertRaises(SkillError):
                    _permissions(max_hold_seconds=value)

    def test_rate_limit_must_be_positive_and_bounded(self) -> None:
        for value in (0, 21, math.nan, False):
            with self.subTest(value=value):
                with self.assertRaises(SkillError):
                    _permissions(max_actions_per_second=value)

    def test_key_denial(self) -> None:
        permissions = _permissions()
        self.assertIsNone(permissions.key_denial("x"))
        self.assertIn("allowed_keys", permissions.key_denial("y") or "")
        self.assertIn("reserved", permissions.key_denial("f8") or "")
        self.assertIsNotNone(permissions.key_denial(None))

    def test_hold_denial(self) -> None:
        permissions = _permissions(max_hold_seconds=1.5)
        self.assertIsNone(permissions.hold_denial(1.5))
        self.assertIn("max_hold_seconds", permissions.hold_denial(1.6) or "")
        for value in (0, -1, math.nan, 6.0, True, None):
            with self.subTest(value=value):
                self.assertIsNotNone(permissions.hold_denial(value))


class SkillDefinitionTests(unittest.TestCase):
    def test_skills_default_to_disabled(self) -> None:
        self.assertFalse(ClickSkill("c", detector="d").enabled)
        self.assertFalse(PressSkill("p", key="x").enabled)
        self.assertFalse(HoldSkill("h", key="x", seconds=1.0).enabled)

    def test_invalid_definitions_raise(self) -> None:
        cases = [
            lambda: PressSkill("p", key="f8"),
            lambda: PressSkill("p", key="ctrl+c"),
            lambda: PressSkill("", key="x"),
            lambda: PressSkill("bad name", key="x"),
            lambda: PressSkill("p", key="x", enabled=1),  # type: ignore[arg-type]
            lambda: HoldSkill("h", key="x", seconds=HARD_MAX_HOLD_SECONDS + 1),
            lambda: HoldSkill("h", key="x", seconds=0),
            lambda: HoldSkill("h", key="x", seconds=True),  # type: ignore[arg-type]
            lambda: ClickSkill("c", detector=" "),
            lambda: ClickSkill("c", detector="d", min_confidence=1.5),
            lambda: ClickSkill("c", detector="d", max_observation_age_seconds=0),
        ]
        for index, factory in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(SkillError):
                    factory()

    def test_permission_denial_per_type(self) -> None:
        permissions = _permissions(max_hold_seconds=1.0)
        self.assertIsNone(permission_denial(ClickSkill("c", detector="d"), permissions))
        self.assertIsNone(permission_denial(PressSkill("p", key="x"), permissions))
        self.assertIsNotNone(permission_denial(PressSkill("p", key="y"), permissions))
        self.assertIsNotNone(
            permission_denial(HoldSkill("h", key="space", seconds=1.2), permissions)
        )


class SkillBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GameState()
        self.book = SkillBook(
            [
                ClickSkill("press_ok", detector="ok_button", min_confidence=0.9),
                PressSkill("type_x", key="x", enabled=True),
                HoldSkill("hold_space", key="space", seconds=1.0),
            ],
            _permissions(),
        )

    def test_rejects_duplicate_or_unpermitted_skills(self) -> None:
        with self.assertRaises(SkillError):
            SkillBook([PressSkill("a", key="x"), PressSkill("a", key="x")], _permissions())
        with self.assertRaises(SkillError):
            SkillBook([PressSkill("a", key="y")], _permissions())
        with self.assertRaises(SkillError):
            SkillBook(
                [HoldSkill("h", key="x", seconds=2.0)], _permissions(max_hold_seconds=1.0)
            )

    def test_enabled_flags_start_from_profile_and_toggle(self) -> None:
        self.assertEqual(self.book.names, ("press_ok", "type_x", "hold_space"))
        self.assertTrue(self.book.is_enabled("type_x"))
        self.assertFalse(self.book.is_enabled("hold_space"))
        self.book.set_enabled("hold_space", True)
        self.assertTrue(self.book.is_enabled("hold_space"))
        self.book.set_enabled("type_x", False)
        self.assertFalse(self.book.is_enabled("type_x"))
        with self.assertRaises(SkillError):
            self.book.set_enabled("missing", True)
        with self.assertRaises(SkillError):
            self.book.is_enabled("missing")

    def test_unknown_and_disabled_skills_yield_no_intent(self) -> None:
        unknown = self.book.build_intent("missing", self.state, source="manual", now=1.0)
        self.assertFalse(unknown.ok)
        self.assertIn("unknown", unknown.reason)

        disabled = self.book.build_intent("hold_space", self.state, source="manual", now=1.0)
        self.assertFalse(disabled.ok)
        self.assertIn("disabled", disabled.reason)

    def test_press_intent_carries_key_from_profile(self) -> None:
        result = self.book.build_intent("type_x", self.state, source="manual", now=3.0)
        self.assertTrue(result.ok)
        intent = result.intent
        assert intent is not None
        self.assertEqual(intent.action, "press")
        self.assertEqual(intent.key, "x")
        self.assertIsNone(intent.hold_seconds)
        self.assertIsNone(intent.target_bbox)
        self.assertEqual(intent.skill_name, "type_x")
        self.assertEqual(intent.rule_name, "manual")
        self.assertEqual(intent.created_at, 3.0)

    def test_hold_intent_carries_duration_from_profile(self) -> None:
        self.book.set_enabled("hold_space", True)
        result = self.book.build_intent(
            "hold_space", self.state, source="auto_rule", now=2.0, reason="because"
        )
        intent = result.intent
        assert intent is not None
        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.key, "space")
        self.assertEqual(intent.hold_seconds, 1.0)
        self.assertEqual(intent.rule_name, "auto_rule")
        self.assertEqual(intent.reason, "because")

    def test_click_needs_fresh_visible_confident_detection(self) -> None:
        self.book.set_enabled("press_ok", True)
        missing = self.book.build_intent("press_ok", self.state, source="manual", now=10.0)
        self.assertIn("not observed", missing.reason)

        self.state.update_detector("ok_button", visible=False, confidence=0.99, observed_at=10.0)
        self.assertIn(
            "not visible",
            self.book.build_intent("press_ok", self.state, source="manual", now=10.1).reason,
        )

        self.state.update_detector(
            "ok_button", visible=True, confidence=0.85, bbox=(1, 2, 3, 4), observed_at=10.2
        )
        self.assertIn(
            "below",
            self.book.build_intent("press_ok", self.state, source="manual", now=10.3).reason,
        )

        self.state.update_detector(
            "ok_button", visible=True, confidence=0.95, bbox=(1, 2, 3, 4), observed_at=10.4
        )
        self.assertIn(
            "stale",
            self.book.build_intent("press_ok", self.state, source="manual", now=12.0).reason,
        )

        result = self.book.build_intent("press_ok", self.state, source="manual", now=10.5)
        intent = result.intent
        assert intent is not None
        self.assertEqual(intent.action, "click")
        self.assertEqual(intent.target_bbox, (1, 2, 3, 4))
        self.assertEqual(intent.detector_name, "ok_button")
        self.assertIsNone(intent.key)

    def test_click_without_bbox_is_rejected(self) -> None:
        self.book.set_enabled("press_ok", True)
        self.state.update_detector("ok_button", visible=True, confidence=0.95, observed_at=5.0)
        result = self.book.build_intent("press_ok", self.state, source="manual", now=5.1)
        self.assertFalse(result.ok)
        self.assertIn("bbox", result.reason)

    def test_empty_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.book.build_intent("type_x", self.state, source=" ", now=1.0)


class SkillRuleTests(unittest.TestCase):
    def test_skill_rule_carries_skill_name(self) -> None:
        state = GameState()
        engine = RuleEngine(
            [
                VisibilityRule(
                    name="auto_ok",
                    detector_name="ok_button",
                    action=SKILL_RULE_ACTION,
                    skill="press_ok",
                )
            ]
        )
        state.update_detector(
            "ok_button", visible=True, confidence=0.95, bbox=(1, 2, 3, 4), observed_at=1.0
        )
        intents = engine.evaluate(state, now=1.1)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action, SKILL_RULE_ACTION)
        self.assertEqual(intents[0].skill_name, "press_ok")

    def test_plain_rule_has_no_skill(self) -> None:
        rule = VisibilityRule(name="r", detector_name="d", action="click")
        self.assertIsNone(rule.skill)

    def test_skill_and_action_must_agree(self) -> None:
        with self.assertRaises(ValueError):
            VisibilityRule(name="r", detector_name="d", action="click", skill="s")
        with self.assertRaises(ValueError):
            VisibilityRule(name="r", detector_name="d", action=SKILL_RULE_ACTION)
        with self.assertRaises(ValueError):
            VisibilityRule(name="r", detector_name="d", action=SKILL_RULE_ACTION, skill=" ")


if __name__ == "__main__":
    unittest.main()
