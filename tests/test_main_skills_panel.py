from __future__ import annotations

import gc
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest import mock

import numpy as np
from pynput import keyboard

from agent.action_dispatcher import DispatchResult
from agent.profile import DetectorDefinition, RuleDefinition, save_profile
from agent.rule_engine import SKILL_RULE_ACTION, ActionIntent, VisibilityRule
from agent.skill_executor import SkillExecutor
from agent.skills import ClickSkill, HoldSkill, PressSkill, SkillPermissions
from main import SKILL_NO_RESULT_TEXT, SKILLS_NONE_TEXT, PersonalGameAIApp, describe_skill
from vision.detector_registry import DetectorRegistry


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _write_profile(root: Path, name: str = "Notepad demo", *, extra_skill: bool = False) -> Path:
    template = np.random.default_rng(1).integers(0, 256, (24, 24, 3), dtype=np.uint8)
    skills = [
        ClickSkill("press_ok", "ok_button", min_confidence=0.9),
        PressSkill("type_x", "x", enabled=True),
        HoldSkill("hold_x", "x", 1.0),
    ]
    if extra_skill:
        skills.append(PressSkill("type_x_again", "x"))
    return save_profile(
        root,
        name,
        detectors=[(DetectorDefinition("ok_button", "", 0.9, None), template)],
        skills=skills,
        rules=[
            RuleDefinition(
                VisibilityRule(
                    name="auto_ok",
                    detector_name="ok_button",
                    action=SKILL_RULE_ACTION,
                    min_confidence=0.9,
                    skill="press_ok",
                ),
                enabled=False,
            )
        ],
        permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
    )


class FakeDispatcher:
    """Records intents instead of sending input. With `hold`, waits for cancel."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self.intents: list[ActionIntent] = []
        self.hwnds: list[int | None] = []
        self.started = threading.Event()

    def dispatch(self, intent, *, hwnd, now=None, cancel_event=None) -> DispatchResult:
        self.intents.append(intent)
        self.hwnds.append(hwnd)
        self.started.set()
        if self.hold:
            cancelled = cancel_event.wait(5.0)
            return DispatchResult(intent, True, "Held (cancelled)." if cancelled else "Held.")
        return DispatchResult(intent, True, f"Pressed {intent.key!r}.")

    def cancel(self) -> None:
        pass


class DescribeSkillTests(unittest.TestCase):
    def test_describes_each_skill_type(self) -> None:
        self.assertEqual(describe_skill(ClickSkill("c", "ok")), "click ok")
        self.assertEqual(describe_skill(PressSkill("p", "x")), "press x")
        self.assertEqual(describe_skill(HoldSkill("h", "space", 1.5)), "hold space 1.5s")
        self.assertEqual(describe_skill(None), "unknown")


class MainSkillsPanelTests(unittest.TestCase):
    """v0.6 task 6: Skills panel, rule -> skill through the executor, F8 wiring."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        # No real global keyboard hook in tests; keep its on_press callback.
        self.listener = mock.patch("main.keyboard.Listener").start()
        self.addCleanup(mock.patch.stopall)
        # Safety net: these tests turn the real InputController on, so no
        # key or click may ever reach the desktop.
        self.pydirectinput = mock.patch("core.input_controller.pydirectinput").start()
        self.app = PersonalGameAIApp(self.root)
        self._closed = False
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profiles_dir = Path(self._tmp.name)
        self.app.profiles_dir = self.profiles_dir
        self.messagebox = mock.patch("main.messagebox").start()
        self.focus = mock.patch("main.focus_window", return_value=True).start()
        mock.patch.object(self.app, "selected_hwnd", return_value=4242).start()
        self.fake = FakeDispatcher()
        self._use_executor(self.fake)

    def tearDown(self) -> None:
        self.app.capture = None
        if not self._closed:
            self.app.close()
        del self.app
        del self.root
        gc.collect()
        self.assertEqual(self.pydirectinput.mock_calls, [])

    def _use_executor(self, dispatcher: FakeDispatcher) -> None:
        self.app.executor.shutdown()
        self.app.executor = SkillExecutor(dispatcher)

    def _load(self, folder: str = "notepad_demo") -> None:
        self.app.refresh_profiles()
        self.app.profile_var.set(folder)
        self.app.load_selected_profile()
        self.messagebox.showerror.assert_not_called()

    def _enable_input(self) -> None:
        self.app.control_var.set(True)
        self.app._toggle_control()

    def _wait_idle(self) -> None:
        deadline = time.monotonic() + 5.0
        while self.app.executor.busy:
            if time.monotonic() > deadline:
                self.fail("Skill executor did not finish.")
            time.sleep(0.01)

    def _result(self, name: str) -> str:
        return self.app._skill_rows[name][1].get()

    # ---- panel ----

    def test_no_profile_lists_no_skills(self) -> None:
        self.assertEqual(self.app.skills_status_var.get(), SKILLS_NONE_TEXT)
        self.assertEqual(self.app._skill_rows, {})
        self.assertEqual(self.app.skills_frame.winfo_children(), [])

    def test_load_builds_one_row_per_skill(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()

        self.assertEqual(list(self.app._skill_rows), ["press_ok", "type_x", "hold_x"])
        self.assertFalse(self.app._skill_rows["press_ok"][0].get())
        self.assertTrue(self.app._skill_rows["type_x"][0].get())
        self.assertEqual(self._result("type_x"), SKILL_NO_RESULT_TEXT)
        self.assertIn("3 (1 enabled)", self.app.skills_status_var.get())
        # One checkbox, Run button and result label per skill.
        self.assertEqual(len(self.app.skills_frame.winfo_children()), 9)

    def test_loading_another_profile_replaces_the_rows(self) -> None:
        _write_profile(self.profiles_dir)
        _write_profile(self.profiles_dir, "Other", extra_skill=True)
        self._load()

        self._load("other")

        self.assertEqual(
            list(self.app._skill_rows), ["press_ok", "type_x", "hold_x", "type_x_again"]
        )
        self.assertEqual(len(self.app.skills_frame.winfo_children()), 12)

    def test_checkbox_toggles_the_skill(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()

        self.app._skill_rows["hold_x"][0].set(True)
        self.app._toggle_skill("hold_x")
        self.assertTrue(self.app.skill_book.is_enabled("hold_x"))
        self.assertIn("(2 enabled)", self.app.skills_status_var.get())

        self.app._skill_rows["type_x"][0].set(False)
        self.app._toggle_skill("type_x")
        self.assertFalse(self.app.skill_book.is_enabled("type_x"))

    # ---- manual Run ----

    def test_run_needs_input_control(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()

        self.app.run_skill("type_x")

        self.messagebox.showwarning.assert_called_once()
        self.focus.assert_not_called()
        self.assertEqual(self.fake.intents, [])

    def test_run_disabled_skill_is_blocked_without_focusing(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()

        self.app.run_skill("hold_x")

        self.focus.assert_not_called()
        self.assertFalse(self.app.executor.busy)
        self.assertEqual(self.fake.intents, [])
        self.assertIn("BLOCKED", self._result("hold_x"))
        self.assertIn("disabled", self._result("hold_x"))

    def test_run_focuses_the_game_and_runs_through_the_executor(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()

        self.app.run_skill("type_x")
        self.assertEqual(self._result("type_x"), "running…")
        self._wait_idle()
        self.app._drain_skill_runs()

        self.focus.assert_called_once_with(4242, settle_seconds=0.15)
        (intent,) = self.fake.intents
        self.assertEqual((intent.action, intent.key, intent.skill_name), ("press", "x", "type_x"))
        self.assertEqual(intent.rule_name, "manual")
        self.assertEqual(self.fake.hwnds, [4242])
        self.assertIn("DONE: Pressed 'x'.", self._result("type_x"))

    def test_run_uses_the_capture_window_when_capturing(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.capture = SimpleNamespace(hwnd=77)

        self.app.run_skill("type_x")
        self._wait_idle()

        self.focus.assert_called_once_with(77, settle_seconds=0.15)
        self.assertEqual(self.fake.hwnds, [77])

    def test_click_skill_needs_capture(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.skill_book.set_enabled("press_ok", True)

        self.app.run_skill("press_ok")

        self.focus.assert_not_called()
        self.assertEqual(self.fake.intents, [])
        self.assertIn("Start Capture first", self._result("press_ok"))

    def test_click_skill_without_a_detection_is_blocked_without_focusing(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.skill_book.set_enabled("press_ok", True)
        self.app.capture = SimpleNamespace(hwnd=77)

        self.app.run_skill("press_ok")

        self.focus.assert_not_called()
        self.assertEqual(self.fake.intents, [])
        self.assertIn("not observed", self._result("press_ok"))

    def test_click_skill_runs_on_a_fresh_detection(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.skill_book.set_enabled("press_ok", True)
        self.app.capture = SimpleNamespace(hwnd=77)
        self.app.game_state.update_detector(
            "ok_button", visible=True, confidence=0.95, bbox=(1, 2, 3, 4)
        )

        self.app.run_skill("press_ok")
        self._wait_idle()

        (intent,) = self.fake.intents
        self.assertEqual((intent.action, intent.target_bbox), ("click", (1, 2, 3, 4)))
        self.assertEqual(self.fake.hwnds, [77])

    def test_second_run_while_busy_is_blocked(self) -> None:
        hold = FakeDispatcher(hold=True)
        self._use_executor(hold)
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.skill_book.set_enabled("hold_x", True)

        self.app.run_skill("hold_x")
        self.assertTrue(hold.started.wait(2.0))
        self.app.run_skill("type_x")

        self.assertIn("Busy", self._result("type_x"))
        self.assertEqual(len(hold.intents), 1)
        self.app.executor.cancel()
        self._wait_idle()

    # ---- rules ----

    def _fire_auto_ok(self) -> None:
        # Only the rule step of the vision loop: no template matching.
        self.app.registry = DetectorRegistry()
        self.app.vision_enabled_var.set(True)
        self.app.capture = SimpleNamespace(hwnd=77)
        self.app.rule_engine.enable_rule("auto_ok")
        self.app.game_state.update_detector(
            "ok_button", visible=True, confidence=0.95, bbox=(10, 20, 30, 40)
        )
        self.app._run_vision_if_due(np.zeros((8, 8, 3), dtype=np.uint8))

    def test_skill_rule_runs_its_skill_through_the_executor(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.skill_book.set_enabled("press_ok", True)

        with mock.patch.object(self.app.dispatcher, "dispatch") as direct:
            self._fire_auto_ok()
            self._wait_idle()
        self.app._drain_skill_runs()

        direct.assert_not_called()
        (intent,) = self.fake.intents
        self.assertEqual((intent.action, intent.skill_name), ("click", "press_ok"))
        self.assertEqual(intent.rule_name, "auto_ok")
        self.assertEqual(intent.target_bbox, (10, 20, 30, 40))
        self.assertEqual(self.fake.hwnds, [77])
        self.assertIn("'auto_ok' submitted", self.app.rules_var.get())
        self.assertIn("DONE", self._result("press_ok"))

    def test_skill_rule_with_disabled_skill_is_blocked(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()

        self._fire_auto_ok()

        self.assertFalse(self.app.executor.busy)
        self.assertEqual(self.fake.intents, [])
        self.assertIn("'auto_ok' blocked", self.app.rules_var.get())
        self.assertIn("is disabled", self.app.last_event_var.get())

    def test_skill_rule_with_input_off_starts_nothing(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self.app.skill_book.set_enabled("press_ok", True)

        self._fire_auto_ok()

        self.assertFalse(self.app.executor.busy)
        self.assertEqual(self.fake.intents, [])
        self.assertIn("Input control is disabled", self.app.last_event_var.get())

    # ---- stopping ----

    def _start_hold(self) -> FakeDispatcher:
        hold = FakeDispatcher(hold=True)
        self._use_executor(hold)
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_input()
        self.app.skill_book.set_enabled("hold_x", True)
        self.app.run_skill("hold_x")
        self.assertTrue(hold.started.wait(2.0))
        return hold

    def test_emergency_stop_disables_input_then_cancels_the_skill(self) -> None:
        self._start_hold()
        seen: list[bool] = []
        cancel = self.app.executor.cancel

        def spy() -> None:
            seen.append(self.app.input.enabled)
            cancel()

        with mock.patch.object(self.app.executor, "cancel", side_effect=spy):
            self.app.emergency_stop()
        self._wait_idle()
        self.app._drain_skill_runs()

        self.assertEqual(seen, [False])
        self.assertIn("Held (cancelled).", self._result("hold_x"))

    def test_f8_listener_stops_input_and_skill_without_waiting_for_tk(self) -> None:
        self._start_hold()
        on_press = self.listener.call_args.kwargs["on_press"]
        seen: list[bool] = []
        cancel = self.app.executor.cancel

        def spy() -> None:
            seen.append(self.app.input.enabled)
            cancel()

        with (
            mock.patch.object(self.app.root, "after") as after,
            mock.patch.object(self.app.executor, "cancel", side_effect=spy),
        ):
            on_press(keyboard.Key.f8)
        self._wait_idle()

        self.assertEqual(seen, [False])
        self.assertFalse(self.app.input.enabled)
        after.assert_called_once_with(0, self.app.emergency_stop)

    def test_unticking_the_running_skill_cancels_it(self) -> None:
        self._start_hold()

        self.app._skill_rows["hold_x"][0].set(False)
        self.app._toggle_skill("hold_x")
        self._wait_idle()
        self.app._drain_skill_runs()

        self.assertIn("Held (cancelled).", self._result("hold_x"))

    def test_unticking_another_skill_leaves_the_running_one(self) -> None:
        hold = self._start_hold()

        self.app._skill_rows["type_x"][0].set(False)
        self.app._toggle_skill("type_x")

        self.assertTrue(self.app.executor.busy)
        self.app.executor.cancel()
        self._wait_idle()
        self.assertEqual(len(hold.intents), 1)

    def test_load_waits_until_the_running_skill_has_stopped(self) -> None:
        self._start_hold()
        # Input off without cancelling: the fake hold keeps the executor busy.
        self.app.input.set_enabled(False)
        self.app.control_var.set(False)
        book = self.app.skill_book

        self._load()  # asserts no showerror

        self.messagebox.showwarning.assert_called_once()
        self.assertIn("still stopping", self.messagebox.showwarning.call_args.args[1])
        self.assertIs(self.app.skill_book, book)
        self.app.executor.cancel()
        self._wait_idle()

    def test_disabling_input_control_cancels_the_skill(self) -> None:
        self._start_hold()

        self.app.control_var.set(False)
        self.app._toggle_control()
        self._wait_idle()
        self.app._drain_skill_runs()

        self.assertIn("Held (cancelled).", self._result("hold_x"))

    def test_close_shuts_the_executor_down(self) -> None:
        executor = self.app.executor

        self.app.close()
        self._closed = True

        self.assertEqual(
            executor.submit(mock.Mock(), hwnd=1), "Skill executor is shut down."
        )


if __name__ == "__main__":
    unittest.main()
