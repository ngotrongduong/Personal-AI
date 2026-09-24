from __future__ import annotations

import gc
import logging
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from agent.action_dispatcher import DispatchResult
from agent.autopilot import Autopilot
from agent.ollama_client import OllamaResult
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.planner_scheduler import PlannerCycleReport
from agent.profile import load_profile, save_profile
from agent.proposal_mailbox import SkillProposal
from agent.rule_engine import ActionIntent
from agent.skill_executor import SkillExecutor
from agent.skills import HoldSkill, PressSkill, SkillPermissions
from main import (
    PLANNER_APPROVE_MODE_TEXT,
    PLANNER_NO_CYCLE_TEXT,
    PLANNER_NO_PROPOSAL_TEXT,
    PersonalGameAIApp,
)


RUN_TYPE_X = '{"type":"run_skill","skill":"type_x","reason":"type an x"}'


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _write_profile(root: Path, *, goal: str = "", auto_max_steps: int = 20) -> Path:
    return save_profile(
        root,
        "Notepad demo",
        detectors=[],
        skills=[PressSkill("type_x", "x", enabled=True), HoldSkill("hold_x", "x", 1.0)],
        rules=[],
        permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
        planner=PlannerConfig(goal=goal, auto_max_steps=auto_max_steps),
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
            cancel_event.wait(5.0)
            return DispatchResult(intent, False, "Cancelled.")
        return DispatchResult(intent, True, f"Pressed {intent.key!r}.")

    def cancel(self) -> None:
        pass


class RecordingScheduler:
    """Stands in for PlannerScheduler: no thread; tests call plan_once themselves."""

    def __init__(self, planner, state, **options) -> None:
        self.planner = planner
        self.state = state
        self.options = options
        self.stopped = False

    @property
    def is_running(self) -> bool:
        return not self.stopped

    def start(self) -> bool:
        return True

    def stop(self, *, join_timeout: float = 1.0) -> None:
        self.stopped = True


class MainPlannerPanelTests(unittest.TestCase):
    """v0.7 task 5: goal, approve/reject/expire, auto mode and its off-switches."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.listener = mock.patch("main.keyboard.Listener").start()
        self.addCleanup(mock.patch.stopall)
        # Safety net: input control is turned on, so nothing may reach the desktop.
        self.pydirectinput = mock.patch("core.input_controller.pydirectinput").start()
        self.app = PersonalGameAIApp(self.root)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profiles_dir = Path(self._tmp.name)
        self.app.profiles_dir = self.profiles_dir
        self.messagebox = mock.patch("main.messagebox").start()
        self.focus = mock.patch("main.focus_window", return_value=True).start()
        mock.patch.object(self.app, "selected_hwnd", return_value=4242).start()
        self.fake = FakeDispatcher()
        self._use_executor(self.fake)

        self.prompts: list[str] = []
        self.answer = RUN_TYPE_X
        self.schedulers: list[RecordingScheduler] = []
        test = self

        class ScriptedClient:
            def __init__(self, config) -> None:
                self.config = config

            def generate(self, prompt: str) -> OllamaResult:
                test.prompts.append(prompt)
                return OllamaResult(text=test.answer)

        def make_scheduler(planner, state, **options) -> RecordingScheduler:
            scheduler = RecordingScheduler(planner, state, **options)
            self.schedulers.append(scheduler)
            return scheduler

        self.app.planner = PlannerController(
            self.app.game_state, client_factory=ScriptedClient, scheduler_factory=make_scheduler
        )

    def tearDown(self) -> None:
        self.app.capture = None
        self.app.close()
        del self.app
        del self.root
        gc.collect()
        self.assertEqual(self.pydirectinput.mock_calls, [])

    # ---- helpers ----

    def _use_executor(self, dispatcher: FakeDispatcher) -> None:
        self.app.executor.shutdown()
        self.app.executor = SkillExecutor(dispatcher)

    def _load(self) -> None:
        self.app.refresh_profiles()
        self.app.profile_var.set("notepad_demo")
        self.app.load_selected_profile()
        self.messagebox.showerror.assert_not_called()

    def _set_input(self, enabled: bool) -> None:
        self.app.control_var.set(enabled)
        self.app._toggle_control()

    def _enable_planner(self) -> RecordingScheduler:
        self.app.planner_enabled_var.set(True)
        self.app._toggle_planner()
        self.messagebox.showerror.assert_not_called()
        self.assertTrue(self.app.planner.is_running)
        return self.schedulers[-1]

    def _setup(self, *, input_on: bool = True) -> RecordingScheduler:
        _write_profile(self.profiles_dir)
        self._load()
        if input_on:
            self._set_input(True)
        return self._enable_planner()

    def _propose(self, scheduler: RecordingScheduler) -> None:
        """One planner cycle, then one Tk poll that takes the proposal."""
        outcome = scheduler.planner.plan_once(self.app.game_state)
        self.assertTrue(outcome.message.startswith("proposed type_x"), outcome.message)
        self.app._poll_planner_proposals()

    def _post(self, name: str) -> None:
        proposal = SkillProposal(name, "test", time.monotonic(), self.app.planner.generation)
        self.assertTrue(self.app.proposals.post(proposal))
        self.app._poll_planner_proposals()

    def _wait_idle(self) -> None:
        deadline = time.monotonic() + 5.0
        while self.app.executor.busy:
            if time.monotonic() > deadline:
                self.fail("Skill executor did not finish.")
            time.sleep(0.01)

    def _finish_run(self) -> None:
        self._wait_idle()
        self.app._drain_skill_runs()
        self.app._poll_planner_proposals()

    def _arm_auto(self, steps: str = "20") -> None:
        self.app.planner_auto_steps_var.set(steps)
        self.messagebox.askyesno.return_value = True
        self.app.planner_mode_var.set("auto")
        self.app._set_planner_mode()
        self.assertEqual(self.app.autopilot.mode, "auto")

    def _decisions(self) -> list[tuple[str, str, bool | None]]:
        return [(s.skill_name, s.decision, s.ok) for s in self.app.step_history.recent()]

    def _log_text(self) -> str:
        return self.app.logbox.get("1.0", "end")

    # ---- wiring ----

    def test_planner_start_wires_goal_skills_mailbox_and_gate(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self.app.planner_goal_var.set("Type the letter x.")
        scheduler = self._enable_planner()

        self.assertEqual(scheduler.options["should_plan"], self.app._planner_may_plan)
        self.assertTrue(self.app._planner_may_plan())
        scheduler.planner.plan_once(self.app.game_state)

        self.assertIn("Goal: Type the letter x.", self.prompts[-1])
        self.assertIn("type_x", self.prompts[-1])
        # Only enabled skills are offered to the LLM.
        self.assertNotIn("hold_x", self.prompts[-1])
        self.assertTrue(self.app.proposals.occupied)
        self.assertFalse(self.app._planner_may_plan())

    def test_gate_is_closed_while_a_skill_runs(self) -> None:
        self.fake = FakeDispatcher(hold=True)
        self._use_executor(self.fake)
        self._setup()

        self.app.run_skill("type_x")
        self.assertTrue(self.fake.started.wait(2.0))
        self.assertFalse(self.app._planner_may_plan())

        self.app.executor.cancel()
        self._wait_idle()
        self.assertTrue(self.app._planner_may_plan())

    def test_without_a_profile_the_planner_cannot_propose(self) -> None:
        scheduler = self._enable_planner()

        outcome = scheduler.planner.plan_once(self.app.game_state)

        self.assertIn("rejected", outcome.message)
        self.assertFalse(self.app.proposals.occupied)
        self.assertIn("can only enable or disable existing rules", self._log_text())

    # ---- approve / reject / expire ----

    def test_proposal_waits_for_approval_then_runs_through_the_executor(self) -> None:
        scheduler = self._setup()
        self.focus.reset_mock()

        self._propose(scheduler)

        self.assertEqual(self.fake.intents, [])
        self.assertIn("Proposal: type_x — type an x", self.app.planner_proposal_var.get())
        self.assertIn("s left", self.app.planner_proposal_var.get())
        self.assertTrue(self.app.planner_approve_button.instate(["!disabled"]))
        self.assertTrue(self.app.planner_reject_button.instate(["!disabled"]))

        self.app.approve_planner_proposal()
        self._finish_run()

        self.focus.assert_called_once_with(4242, settle_seconds=0.15)
        self.assertEqual(len(self.fake.intents), 1)
        self.assertEqual(self.fake.intents[0].rule_name, "planner")
        self.assertEqual(self.fake.intents[0].key, "x")
        self.assertEqual(self._decisions(), [("type_x", "approved", True)])
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self.app.planner_proposal_var.get(), PLANNER_NO_PROPOSAL_TEXT)
        self.assertTrue(self.app.planner_approve_button.instate(["disabled"]))
        # The step result reaches the next prompt.
        scheduler.planner.plan_once(self.app.game_state)
        self.assertIn("- type_x: approved, DONE", self.prompts[-1])

    def test_reject_frees_the_slot_without_input(self) -> None:
        scheduler = self._setup()
        self.focus.reset_mock()
        self._propose(scheduler)

        self.app.reject_planner_proposal()

        self.assertEqual(self._decisions(), [("type_x", "rejected", None)])
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self.fake.intents, [])
        self.focus.assert_not_called()
        self.assertEqual(self.app.planner_proposal_var.get(), PLANNER_NO_PROPOSAL_TEXT)

    def test_unanswered_proposal_expires_and_cannot_be_approved(self) -> None:
        self.app.autopilot = Autopilot(ttl_seconds=0.01)
        scheduler = self._setup()
        self._propose(scheduler)
        time.sleep(0.03)

        self.app.approve_planner_proposal()
        self.app._poll_planner_proposals()

        self.assertEqual(self._decisions(), [("type_x", "expired", None)])
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self.fake.intents, [])
        self.assertIn("expired", self._log_text())

    def test_approve_with_input_off_is_refused(self) -> None:
        scheduler = self._setup(input_on=False)
        self._propose(scheduler)

        self.app.approve_planner_proposal()

        self.assertEqual(self._decisions(), [("type_x", "refused", None)])
        self.assertEqual(self.app.step_history.recent()[0].outcome, "refused: Input control is disabled.")
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self.fake.intents, [])
        self.focus.assert_not_called()

    def test_skill_disabled_after_the_proposal_is_refused(self) -> None:
        scheduler = self._setup()
        self._propose(scheduler)
        self.app._skill_rows["type_x"][0].set(False)
        self.app._toggle_skill("type_x")

        self.app.approve_planner_proposal()

        self.assertEqual(self._decisions(), [("type_x", "refused", None)])
        self.assertEqual(self.fake.intents, [])

    def test_proposal_from_an_older_planner_start_is_dropped(self) -> None:
        self._setup()
        self.app.proposals.post(
            SkillProposal("type_x", "old", time.monotonic(), self.app.planner.generation - 1)
        )

        self.app._poll_planner_proposals()

        self.assertIsNone(self.app.autopilot.pending)
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self._decisions(), [])

    def test_run_from_before_a_planner_reset_is_not_credited_to_a_new_step(self) -> None:
        self.fake = FakeDispatcher(hold=True)
        self._use_executor(self.fake)
        scheduler = self._setup()
        self._propose(scheduler)
        self.app.approve_planner_proposal()
        self.assertTrue(self.fake.started.wait(2.0))

        # Planner off and on while step A still runs; A then ends undrained.
        self.app.planner_enabled_var.set(False)
        self.app._toggle_planner()
        scheduler = self._enable_planner()
        self.app.executor.cancel()
        self._wait_idle()
        self.fake.started.clear()

        # Step B is approved before A's result is drained.
        self._propose(scheduler)
        self.app.approve_planner_proposal()
        self.assertTrue(self.fake.started.wait(2.0))
        self.app._drain_skill_runs()

        self.assertIsNotNone(self.app.autopilot.running)
        self.assertEqual(self._decisions(), [])

        self.app.executor.cancel()
        self._finish_run()
        self.assertEqual(self._decisions(), [("type_x", "approved", False)])

    # ---- auto mode ----

    def test_auto_mode_needs_input_a_planner_and_a_yes(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self._enable_planner()

        self.app.planner_mode_var.set("auto")
        self.app._set_planner_mode()
        self.messagebox.showwarning.assert_called_once()
        self.assertEqual(self.app.planner_mode_var.get(), "approve")

        self._set_input(True)
        self.messagebox.askyesno.return_value = False
        self.app.planner_mode_var.set("auto")
        self.app._set_planner_mode()
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self.app.planner_mode_var.get(), "approve")

        self.app.planner_auto_steps_var.set("101")
        self.app.planner_mode_var.set("auto")
        self.app._set_planner_mode()
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self.messagebox.showwarning.call_count, 2)

        self.focus.reset_mock()
        self._arm_auto("5")
        self.assertIn("5 enabled skills", self.messagebox.askyesno.call_args.args[1])
        self.assertEqual(self.app.planner_mode_status_var.get(), "Mode: AUTO · 0/5 steps.")
        # Confirming focuses the game once, like Run.
        self.focus.assert_called_once_with(4242, settle_seconds=0.15)

    def test_auto_runs_without_focus_and_stops_at_the_step_cap(self) -> None:
        scheduler = self._setup()
        self._arm_auto("1")
        self.focus.reset_mock()

        self._propose(scheduler)
        self._finish_run()

        self.focus.assert_not_called()
        self.assertEqual(len(self.fake.intents), 1)
        self.assertEqual(self._decisions(), [("type_x", "auto", True)])
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self.app.planner_mode_var.get(), "approve")
        self.assertEqual(self.app.planner_mode_status_var.get(), PLANNER_APPROVE_MODE_TEXT)
        self.assertIn("Auto mode OFF: reached the 1-step limit.", self._log_text())

        # Back in approve mode the next proposal waits.
        self._propose(scheduler)
        self.assertIsNotNone(self.app.autopilot.pending)
        self.assertEqual(len(self.fake.intents), 1)

    def test_three_failed_auto_steps_turn_auto_off(self) -> None:
        self._setup()
        self._arm_auto()

        for _ in range(3):
            self._post("hold_x")  # disabled skill: refused every time

        self.assertEqual(self._decisions(), [("hold_x", "refused", None)] * 3)
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertIn("Auto mode OFF: 3 failed steps in a row.", self._log_text())
        self.assertEqual(self.fake.intents, [])

    def test_auto_turns_off_on_every_trigger(self) -> None:
        triggers = {
            "input off": lambda: self._set_input(False),
            "clear rules": self.app.clear_rules,
            "planner off": lambda: (
                self.app.planner_enabled_var.set(False),
                self.app._toggle_planner(),
            ),
            "F8": self.app.emergency_stop,
            "approve radio": lambda: (
                self.app.planner_mode_var.set("approve"),
                self.app._set_planner_mode(),
            ),
        }
        _write_profile(self.profiles_dir)
        self._load()
        for name, trigger in triggers.items():
            with self.subTest(trigger=name):
                if not self.app.input.enabled:
                    self._set_input(True)
                if not self.app.planner.is_running:
                    self._enable_planner()
                self._arm_auto()

                trigger()

                self.assertEqual(self.app.autopilot.mode, "approve")
                self.assertEqual(self.app.planner_mode_var.get(), "approve")
                self.assertEqual(self.app.planner_mode_status_var.get(), PLANNER_APPROVE_MODE_TEXT)

        # Loading a profile (input already off) also resets the planner steps.
        self._set_input(False)
        self._enable_planner()
        self.app.autopilot.arm_auto(5)
        self._load()
        self.assertEqual(self.app.autopilot.mode, "approve")

    def test_f8_while_the_auto_confirmation_is_open_keeps_auto_off(self) -> None:
        self._setup()
        self.focus.reset_mock()

        def press_f8_then_yes(*_args) -> bool:
            self.app.emergency_stop()
            return True

        self.messagebox.askyesno.side_effect = press_f8_then_yes
        self.app.planner_mode_var.set("auto")
        self.app._set_planner_mode()

        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self.app.planner_mode_var.get(), "approve")
        self.assertEqual(self.app.planner_mode_status_var.get(), PLANNER_APPROVE_MODE_TEXT)
        self.assertIsNone(self.app._auto_hwnd)
        self.focus.assert_not_called()
        self.assertIn("Auto mode NOT turned on", self._log_text())

    def test_auto_step_in_another_window_is_refused_and_turns_auto_off(self) -> None:
        scheduler = self._setup()
        self._arm_auto()
        self.assertEqual(self.app._auto_hwnd, 4242)
        self.app.selected_hwnd.return_value = 5555

        self._propose(scheduler)

        self.assertEqual(self.fake.intents, [])
        self.assertEqual(self._decisions(), [("type_x", "refused", None)])
        self.assertIn("game window changed", self.app.step_history.recent()[0].outcome)
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self.app.planner_mode_var.get(), "approve")
        self.assertIsNone(self.app._auto_hwnd)
        self.assertIn("Auto mode OFF because the game window changed.", self._log_text())

    def test_proposal_older_than_the_ttl_is_never_shown_or_run(self) -> None:
        self._setup()
        self._arm_auto()
        stale = SkillProposal("type_x", "old", time.monotonic() - 60.0, self.app.planner.generation)
        self.assertTrue(self.app.proposals.post(stale))

        self.app._poll_planner_proposals()

        self.assertEqual(self.fake.intents, [])
        self.assertEqual(self._decisions(), [("type_x", "expired", None)])
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self.app.autopilot.steps_taken, 0)

    def test_pending_proposal_of_another_planner_start_cannot_run(self) -> None:
        self._setup()
        old = SkillProposal("type_x", "old", time.monotonic(), self.app.planner.generation - 1)
        self.app.autopilot.offer(old, time.monotonic())

        self.app.approve_planner_proposal()

        self.assertEqual(self.fake.intents, [])
        self.assertEqual(self._decisions(), [("type_x", "refused", None)])
        self.assertIn("no longer running", self.app.step_history.recent()[0].outcome)

    def test_f8_drops_the_pending_proposal_after_input_and_skill(self) -> None:
        scheduler = self._setup()
        self._propose(scheduler)
        order: list[str] = []
        cancel = self.app.executor.cancel
        planner_stop = self.app.planner.stop
        recording_stop = self.app._request_recording_stop

        def on_cancel():
            order.append(f"cancel input={self.app.input.enabled}")
            cancel()

        def on_planner_stop():
            order.append("planner")
            planner_stop()

        def on_recording_stop(reason):
            order.append("recording")
            return recording_stop(reason)

        mock.patch.object(self.app.executor, "cancel", side_effect=on_cancel).start()
        mock.patch.object(self.app.planner, "stop", side_effect=on_planner_stop).start()
        mock.patch.object(
            self.app, "_request_recording_stop", side_effect=on_recording_stop
        ).start()

        self.app.emergency_stop()

        self.assertEqual(order, ["cancel input=False", "planner", "recording"])
        self.assertIsNone(self.app.autopilot.pending)
        self.assertFalse(self.app.proposals.occupied)
        self.assertEqual(self.app.planner_proposal_var.get(), PLANNER_NO_PROPOSAL_TEXT)
        self.assertIn("dropped because of the emergency stop", self._log_text())
        self.app.approve_planner_proposal()
        self.assertEqual(self.fake.intents, [])

    # ---- queue instead of root.after ----

    def test_cycle_reports_and_logs_arrive_through_the_queue(self) -> None:
        generation = self.app._planner_generation
        report = PlannerCycleReport(
            status="ok", message="noop", changed=False, duration_seconds=0.5, finished_at=time.time()
        )
        with mock.patch.object(self.app.root, "after") as after:
            worker = threading.Thread(
                target=lambda: (
                    self.app._queue_planner_cycle_report(generation, report),
                    logging.getLogger("agent.planner_scheduler").info("hello from the planner"),
                )
            )
            worker.start()
            worker.join(2.0)
            after.assert_not_called()

        self.assertEqual(self.app.planner_last_cycle_var.get(), PLANNER_NO_CYCLE_TEXT)
        self.app._drain_planner_queue()

        self.assertIn("noop", self.app.planner_last_cycle_var.get())
        self.assertIn("Planner: hello from the planner", self._log_text())

    # ---- goal in the profile ----

    def test_goal_and_auto_max_steps_load_from_and_save_to_the_profile(self) -> None:
        _write_profile(self.profiles_dir, goal="Type x.", auto_max_steps=7)
        self._load()
        self.assertEqual(self.app.planner_goal_var.get(), "Type x.")
        self.assertEqual(self.app._planner_goal, "Type x.")
        self.assertEqual(self.app.planner_auto_steps_var.get(), "7")

        self.app.planner_goal_var.set("  Hold x now.  ")
        self.app.planner_auto_steps_var.set("12")
        self.messagebox.askyesno.return_value = True
        with mock.patch("main.simpledialog.askstring", return_value="Notepad demo"):
            self.app.save_current_profile()
        self.messagebox.showerror.assert_not_called()

        saved = load_profile(self.profiles_dir / "notepad_demo")
        self.assertEqual(saved.planner.goal, "Hold x now.")
        self.assertEqual(saved.planner.auto_max_steps, 12)

    def test_bad_auto_max_steps_blocks_save(self) -> None:
        _write_profile(self.profiles_dir)
        self._load()
        self.app.planner_auto_steps_var.set("lots")

        with mock.patch("main.simpledialog.askstring", return_value="Notepad demo"):
            self.app.save_current_profile()

        self.messagebox.showerror.assert_called_once()
        self.assertIn("whole number", self.messagebox.showerror.call_args.args[1])
        self.messagebox.askyesno.assert_not_called()


if __name__ == "__main__":
    unittest.main()
