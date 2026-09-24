from __future__ import annotations

import gc
import time
import tkinter as tk
import unittest

from agent.planner_scheduler import PlannerCycleReport
from main import PLANNER_MESSAGE_MAX_CHARS, PLANNER_NO_CYCLE_TEXT, PersonalGameAIApp


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _report(message: str = "noop", duration_seconds: float = 2.5) -> PlannerCycleReport:
    return PlannerCycleReport(
        status="ok",
        message=message,
        changed=False,
        duration_seconds=duration_seconds,
        finished_at=time.time(),
    )


class MainPlannerVisibilityTests(unittest.TestCase):
    """v0.4 task 10: the "Last cycle" label is display-only and drops stale reports."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.app = PersonalGameAIApp(self.root)

    def tearDown(self) -> None:
        self.app.capture = None
        self.app.close()
        # The app's Tk variables live in reference cycles. Collect them here on
        # the main thread; otherwise a later test's background thread can end
        # up finalizing them, and tkinter then stalls ~1s waiting for a
        # mainloop before raising "main thread is not in main loop".
        del self.app
        del self.root
        gc.collect()

    def test_current_generation_report_updates_label(self) -> None:
        generation = self.app._planner_generation

        self.app._show_planner_cycle_report(generation, _report("noop", 2.5))

        text = self.app.planner_last_cycle_var.get()
        self.assertIn("2.5s", text)
        self.assertIn("ok", text)
        self.assertIn("noop", text)

    def test_sub_second_latency_is_shown_in_ms_and_long_messages_truncated(self) -> None:
        generation = self.app._planner_generation

        self.app._show_planner_cycle_report(generation, _report("x" * 500, 0.25))

        text = self.app.planner_last_cycle_var.get()
        self.assertIn("250ms", text)
        self.assertIn("x" * (PLANNER_MESSAGE_MAX_CHARS - 1) + "…", text)
        self.assertNotIn("x" * PLANNER_MESSAGE_MAX_CHARS, text)

    def test_stale_report_after_emergency_stop_is_dropped(self) -> None:
        old_generation = self.app._planner_generation
        self.app._show_planner_cycle_report(old_generation, _report())

        self.app.emergency_stop()
        self.assertEqual(self.app.planner_last_cycle_var.get(), PLANNER_NO_CYCLE_TEXT)

        self.app._show_planner_cycle_report(old_generation, _report("enabled rule 'x'"))
        self.assertEqual(self.app.planner_last_cycle_var.get(), PLANNER_NO_CYCLE_TEXT)

    def test_clear_rules_and_toggle_off_reset_label_and_drop_stale_reports(self) -> None:
        for reset in (self.app.clear_rules, self.app._toggle_planner):
            old_generation = self.app._planner_generation
            self.app._show_planner_cycle_report(old_generation, _report())

            self.app.planner_enabled_var.set(False)
            reset()

            self.assertEqual(self.app.planner_last_cycle_var.get(), PLANNER_NO_CYCLE_TEXT)
            self.app._show_planner_cycle_report(old_generation, _report())
            self.assertEqual(self.app.planner_last_cycle_var.get(), PLANNER_NO_CYCLE_TEXT)


if __name__ == "__main__":
    unittest.main()
