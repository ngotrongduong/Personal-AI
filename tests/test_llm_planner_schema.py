from __future__ import annotations

import unittest

from agent.llm_planner_schema import (
    DirectiveValidationError,
    DisableRuleDirective,
    EnableRuleDirective,
    NoopDirective,
    parse_directive,
)


class PlannerDirectiveSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.known_rules = {"heal_low_hp", "attack_visible_enemy"}

    def test_parses_enable_known_rule(self) -> None:
        directive = parse_directive('{"type":"enable_rule","rule_name":"heal_low_hp"}', self.known_rules)

        self.assertEqual(directive, EnableRuleDirective("heal_low_hp"))

    def test_parses_disable_known_rule(self) -> None:
        directive = parse_directive(
            '{"type":"disable_rule","rule_name":"attack_visible_enemy"}', self.known_rules
        )

        self.assertEqual(directive, DisableRuleDirective("attack_visible_enemy"))

    def test_parses_noop_with_no_rule_name(self) -> None:
        directive = parse_directive('{"type":"noop"}', self.known_rules)

        self.assertEqual(directive, NoopDirective())

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "not valid JSON"):
            parse_directive("{", self.known_rules)

    def test_rejects_unknown_directive_type(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "Unknown planner directive type"):
            parse_directive('{"type":"press_key","key":"space"}', self.known_rules)

    def test_rejects_unknown_rule(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "unknown rule"):
            parse_directive('{"type":"enable_rule","rule_name":"invented_rule"}', self.known_rules)

    def test_rejects_wrong_field_type(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "rule_name.*non-empty string"):
            parse_directive('{"type":"enable_rule","rule_name":42}', self.known_rules)

    def test_rejects_extra_fields(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "fields must be exactly"):
            parse_directive('{"type":"noop","rule_name":"heal_low_hp"}', self.known_rules)

    def test_rejects_non_object_json(self) -> None:
        with self.assertRaisesRegex(DirectiveValidationError, "must be an object"):
            parse_directive("[]", self.known_rules)


if __name__ == "__main__":
    unittest.main()
