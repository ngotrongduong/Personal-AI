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


if __name__ == "__main__":
    unittest.main()
