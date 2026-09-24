from __future__ import annotations

import unittest

from agent.planner_config import PlannerConfig, load_planner_config


class PlannerConfigTests(unittest.TestCase):
    def test_missing_planner_block_uses_disabled_safe_defaults(self) -> None:
        config = load_planner_config({"name": "Some game"})

        self.assertFalse(config.enabled)
        self.assertIsNone(config.ollama)
        self.assertEqual(config.interval_seconds, 5.0)

    def test_full_enabled_config_parses_ollama_settings(self) -> None:
        config = load_planner_config(
            {
                "planner": {
                    "enabled": True,
                    "model": "qwen3.5:9b",
                    "host": "127.0.0.1",
                    "port": 12434,
                    "timeout_seconds": 12.5,
                    "interval_seconds": 7.0,
                }
            }
        )

        self.assertTrue(config.enabled)
        self.assertIsNotNone(config.ollama)
        assert config.ollama is not None
        self.assertEqual(config.ollama.model, "qwen3.5:9b")
        self.assertEqual(config.ollama.host, "127.0.0.1")
        self.assertEqual(config.ollama.port, 12434)
        self.assertEqual(config.interval_seconds, 7.0)

    def test_goal_and_auto_max_steps_default_and_parse(self) -> None:
        default = load_planner_config({"planner": {"enabled": False}})
        self.assertEqual(default.goal, "")
        self.assertEqual(default.auto_max_steps, 20)

        config = load_planner_config(
            {"planner": {"enabled": False, "goal": "Collect coins.", "auto_max_steps": 100}}
        )
        self.assertEqual(config.goal, "Collect coins.")
        self.assertEqual(config.auto_max_steps, 100)

    def test_goal_and_auto_max_steps_are_validated(self) -> None:
        bad_blocks = [
            ({"goal": 5}, "goal must be a string"),
            ({"goal": None}, "goal must be a string"),
            ({"goal": "g" * 501}, "at most 500"),
            ({"auto_max_steps": 0}, "between 1 and 100"),
            ({"auto_max_steps": 101}, "between 1 and 100"),
            ({"auto_max_steps": True}, "must be an integer"),
            ({"auto_max_steps": 5.0}, "must be an integer"),
            ({"auto_max_steps": "5"}, "must be an integer"),
        ]
        for block, message in bad_blocks:
            with self.subTest(block=block):
                with self.assertRaisesRegex(ValueError, message):
                    load_planner_config({"planner": {"enabled": False, **block}})

    def test_llm_notes_defaults_off_and_must_be_a_boolean(self) -> None:
        self.assertFalse(load_planner_config({}).llm_notes)
        self.assertFalse(load_planner_config({"planner": {"enabled": False}}).llm_notes)
        self.assertTrue(load_planner_config({"planner": {"llm_notes": True}}).llm_notes)
        for value in (1, "true", None, [True]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "llm_notes must be a boolean"):
                    load_planner_config({"planner": {"llm_notes": value}})

    def test_enabled_planner_requires_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "model"):
            load_planner_config({"planner": {"enabled": True}})

    def test_unrecognized_planner_field_is_rejected_by_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected"):
            load_planner_config({"planner": {"unexpected": "value"}})

    def test_non_positive_interval_is_rejected(self) -> None:
        for interval_seconds in (0, -1):
            with self.subTest(interval_seconds=interval_seconds):
                with self.assertRaises(ValueError):
                    load_planner_config({"planner": {"interval_seconds": interval_seconds}})

    def test_non_object_planner_block_is_rejected(self) -> None:
        for planner in ("disabled", []):
            with self.subTest(planner=planner):
                with self.assertRaises(ValueError):
                    load_planner_config({"planner": planner})

    def test_enabled_config_requires_ollama_when_constructed_directly(self) -> None:
        with self.assertRaises(ValueError):
            PlannerConfig(enabled=True, ollama=None)


if __name__ == "__main__":
    unittest.main()
