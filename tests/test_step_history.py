from __future__ import annotations

import unittest

from agent.step_history import StepHistory, StepRecord


def _record(name: str, decision: str = "approved", outcome: str = "Pressed 'x'.", at: float = 100.0):
    return StepRecord(name, "reason", decision, outcome, True, at)


class StepHistoryTests(unittest.TestCase):
    def test_empty_history_prompt_line(self) -> None:
        self.assertEqual(StepHistory().prompt_lines(now=0.0), ["- none yet"])

    def test_keeps_only_the_most_recent_steps_oldest_first(self) -> None:
        history = StepHistory(maxlen=3)
        for index in range(5):
            history.append(_record(f"s{index}"))

        self.assertEqual([step.skill_name for step in history.recent()], ["s2", "s3", "s4"])

    def test_prompt_lines_show_decision_outcome_and_age(self) -> None:
        history = StepHistory()
        history.append(_record("type_x", "auto", "Pressed 'x'.", at=100.0))
        history.append(StepRecord("hold_space", "r", "rejected", "rejected by the user", None, 107.0))

        lines = history.prompt_lines(now=110.0)

        self.assertEqual(lines[0], "- type_x: auto, Pressed 'x'. (10s ago)")
        self.assertEqual(lines[1], "- hold_space: rejected, rejected by the user (3s ago)")

    def test_prompt_lines_strip_control_characters_and_truncate(self) -> None:
        history = StepHistory()
        history.append(_record("type_x", outcome="line1\nline2\x00" + "y" * 500))

        line = history.prompt_lines(now=100.0)[0]

        self.assertNotIn("\n", line)
        self.assertNotIn("\x00", line)
        self.assertLess(len(line), 260)

    def test_clear(self) -> None:
        history = StepHistory()
        history.append(_record("a"))
        history.clear()

        self.assertEqual(history.recent(), ())

    def test_rejects_unknown_decision_and_bad_maxlen(self) -> None:
        with self.assertRaises(ValueError):
            StepRecord("a", "r", "maybe", "x", None, 0.0)
        for bad in (0, -1, True, 2.5):
            with self.assertRaises(ValueError):
                StepHistory(maxlen=bad)

    # ---- v1.0 observed effects ----

    def test_effect_defaults_to_none_and_rejects_unknown_values(self) -> None:
        self.assertEqual(_record("a").effect, "none")
        with self.assertRaises(ValueError):
            StepRecord("a", "r", "approved", "x", True, 0.0, effect="maybe")

    def test_set_effect_replaces_the_record_by_identity(self) -> None:
        history = StepHistory()
        first = StepRecord("a", "r", "approved", "x", True, 1.0, "pending", "x_glyph visible")
        twin = StepRecord("a", "r", "approved", "x", True, 1.0, "pending", "x_glyph visible")
        history.append(first)
        history.append(twin)

        updated = history.set_effect(twin, "confirmed")

        self.assertIsNotNone(updated)
        self.assertEqual([step.effect for step in history.recent()], ["pending", "confirmed"])
        self.assertEqual(updated.expected, "x_glyph visible")
        # A record that is no longer kept is ignored.
        history.clear()
        self.assertIsNone(history.set_effect(first, "confirmed"))

    def test_prompt_lines_show_the_effect(self) -> None:
        history = StepHistory()
        history.append(StepRecord("a", "r", "approved", "Pressed 'x'.", True, 100.0))
        history.append(
            StepRecord("b", "r", "auto", "Pressed 'x'.", True, 101.0, "confirmed", "x_glyph visible")
        )
        history.append(
            StepRecord("c", "r", "auto", "Pressed 'x'.", True, 102.0, "not_seen", "menu\nhidden")
        )
        history.append(StepRecord("d", "r", "auto", "Pressed 'x'.", True, 103.0, "pending"))

        lines = history.prompt_lines(now=104.0)

        self.assertEqual(lines[0], "- a: approved, Pressed 'x'. (4s ago)")
        self.assertEqual(lines[1], "- b: auto, Pressed 'x'. (3s ago), effect confirmed (x_glyph visible)")
        self.assertEqual(lines[2], "- c: auto, Pressed 'x'. (2s ago), effect not seen (menu hidden)")
        self.assertEqual(lines[3], "- d: auto, Pressed 'x'. (1s ago), effect pending (expected change)")


if __name__ == "__main__":
    unittest.main()
