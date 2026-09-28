from __future__ import annotations

import gc
import threading
import time
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest import mock

import numpy as np

from agent.action_dispatcher import DispatchResult
from agent.skill_executor import SkillExecutor
from agent.skills import SkillPermissions
from imitation.demo_bank import DemoBank, DemoClick
from imitation.policy import Abstention, Proposal
from imitation.runner import ImitationRunner, PolicyOutcome
from main import (
    IMITATION_IDLE_TEXT,
    IMITATION_MAX_FAILURES,
    IMITATION_SOURCE,
    PersonalGameAIApp,
    describe_demo_bank,
    describe_proposal,
)


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _click(session: str = "s1", t: float = 1.0, fx: float = 0.5, fy: float = 0.5) -> DemoClick:
    return DemoClick(
        session=session,
        t=t,
        frame_index=1,
        frame_path=None,  # never read by these tests
        fx=fx,
        fy=fy,
        screen=np.ones(4, dtype=np.float32) / 2.0,
        patch=np.zeros((24, 24), dtype=np.uint8),
    )


def _proposal(fx: float = 0.5, fy: float = 0.5) -> Proposal:
    return Proposal(fx, fy, 0.97, 0.91, 2, 1.8, "s1", 1.0, "matched 2 demo click(s)")


def _imitation_config(**overrides) -> SimpleNamespace:
    values = dict(
        window_title="Merchant Guilds",
        sessions=(),
        k=5,
        screen_threshold=0.92,
        patch_threshold=0.8,
        cooldown_seconds=3.0,
        min_interval_seconds=0.5,
        deny_zones=((0.0, 0.0, 0.25, 0.1),),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDispatcher:
    def __init__(self, *, dispatched: bool = True) -> None:
        self.dispatched = dispatched
        self.intents = []
        self.hwnds = []

    def dispatch(self, intent, *, hwnd, now=None, cancel_event=None) -> DispatchResult:
        self.intents.append(intent)
        self.hwnds.append(hwnd)
        return DispatchResult(intent, self.dispatched, "Tapped (1, 2)." if self.dispatched else "Not foreground.")

    def cancel(self) -> None:
        pass


class FakePolicy:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = []

    def propose(self, frame, *, now, recent=()):
        self.calls.append((now, tuple(recent)))
        return self.outcome


class MainImitationPanelTests(unittest.TestCase):
    """v1.3 task 5: Imitation panel, dry run, live taps through the executor, stops."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.listener = mock.patch("main.keyboard.Listener").start()
        self.addCleanup(mock.patch.stopall)
        # No key or click may ever reach the desktop.
        self.pydirectinput = mock.patch("core.input_controller.pydirectinput").start()
        self.app = PersonalGameAIApp(self.root)
        self.messagebox = mock.patch("main.messagebox").start()
        self.messagebox.askyesno.return_value = True
        self.focus = mock.patch("main.focus_window", return_value=True).start()
        self.fake = FakeDispatcher()
        self._use_executor(self.fake)
        self.bank = DemoBank.from_clicks((_click(), _click("s2", 2.0, 0.6, 0.4)))
        self.app.imitation = ImitationRunner(bank_builder=lambda root, title, names: (self.bank, ("s1", "s2")))
        self.config = _imitation_config()
        self.app.profile = SimpleNamespace(
            imitation=self.config, permissions=SkillPermissions()
        )
        self.frame = np.zeros((80, 120, 3), dtype=np.uint8)

    def tearDown(self) -> None:
        self.app.capture = None
        self.app.close()
        del self.app
        del self.root
        gc.collect()
        self.assertEqual(self.pydirectinput.mock_calls, [])

    # ---- helpers ----

    def _use_executor(self, dispatcher) -> None:
        self.app.executor.shutdown()
        self.app.executor = SkillExecutor(dispatcher)

    def _capture(self, hwnd: int = 4242, title: str = "Merchant Guilds") -> None:
        self.app.capture = SimpleNamespace(
            hwnd=hwnd, last_error=None, running=True, stop=lambda: None
        )
        self.app._capture_title = title

    def _drain_until(self, predicate) -> None:
        deadline = time.monotonic() + 5.0
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("imitation result never arrived")
            self.app._drain_imitation()
            time.sleep(0.005)

    def _load_demos(self) -> None:
        self.app.load_imitation_demos()
        self._drain_until(lambda: self.app._imitation_policy is not None)

    def _start(self) -> None:
        self._load_demos()
        self._capture()
        self.app.start_imitation()
        self.assertTrue(self.app._imitation_running)

    def _enable_input(self) -> None:
        self.app.control_var.set(True)
        self.app._toggle_control()

    def _go_live(self) -> None:
        self._enable_input()
        self.app.imitation_live_var.set(True)
        self.app._toggle_imitation_live()
        self.assertTrue(self.app.imitation_live_var.get())

    def _send_outcome(self, outcome, observed_at: float | None = None) -> None:
        if observed_at is None:
            observed_at = time.monotonic()
        self.app._imitation_outcome(
            PolicyOutcome(self.app.imitation.generation, outcome, observed_at=observed_at)
        )

    def _wait_idle(self) -> None:
        deadline = time.monotonic() + 5.0
        while self.app.executor.busy:
            if time.monotonic() > deadline:
                self.fail("executor did not finish")
            time.sleep(0.01)
        self.app._drain_skill_runs()

    # ---- descriptions ----

    def test_descriptions(self) -> None:
        self.assertIn("2 demo clicks from 2 recordings", describe_demo_bank(self.bank, ("s1", "s2")))
        skipped = DemoBank(self.bank.clicks, {}, {"drag": 2, "no_frame": 1}, self.bank.screens)
        self.assertIn("(skipped: drag 2, no_frame 1)", describe_demo_bank(skipped, ("s1",)))
        text = describe_proposal(_proposal())
        self.assertIn("(0.500, 0.500)", text)
        self.assertIn("votes 2", text)
        self.assertIn("demo s1 t=1.0s", text)

    # ---- load / start ----

    def test_starts_idle_with_live_off(self) -> None:
        self.assertEqual(self.app.imitation_status_var.get(), IMITATION_IDLE_TEXT)
        self.assertFalse(self.app.imitation_live_var.get())
        self.assertTrue(self.app.imitation_start_button.instate(["disabled"]))
        self.assertTrue(self.app.imitation_live_check.instate(["disabled"]))

    def test_load_needs_an_imitation_block(self) -> None:
        self.app.profile = SimpleNamespace(imitation=None, permissions=SkillPermissions())
        self.app.load_imitation_demos()
        self.messagebox.showwarning.assert_called_once()
        self.assertIsNone(self.app._imitation_load_id)

    def test_load_builds_the_policy_and_enables_start(self) -> None:
        self._load_demos()
        self.assertIn("2 demo clicks from 2 recordings", self.app.imitation_status_var.get())
        self.assertEqual(self.app._imitation_policy.config.deny_zones, self.config.deny_zones)
        self.assertTrue(self.app.imitation_start_button.instate(["!disabled"]))

    def test_empty_bank_does_not_enable_start(self) -> None:
        self.bank = DemoBank.from_clicks(())
        self.app.load_imitation_demos()
        self._drain_until(lambda: self.app._imitation_load_id is None)
        self.assertIsNone(self.app._imitation_policy)
        self.assertIn("Record yourself playing", self.app.imitation_status_var.get())

    def test_bank_of_a_replaced_profile_is_ignored(self) -> None:
        self.app.load_imitation_demos()
        self.app.profile = SimpleNamespace(imitation=self.config, permissions=SkillPermissions())
        time.sleep(0.2)
        self.app._drain_imitation()
        self.assertIsNone(self.app._imitation_policy)

    def test_start_needs_capture_of_the_demo_game(self) -> None:
        self._load_demos()
        self.app.start_imitation()
        self.assertFalse(self.app._imitation_running)

        self._capture(title="Untitled - Notepad")
        self.app.start_imitation()
        self.assertFalse(self.app._imitation_running)
        self.assertEqual(self.messagebox.showwarning.call_count, 2)

        self._capture(title="Merchant Guilds - Google Play Games")
        self.app.start_imitation()
        self.assertTrue(self.app._imitation_running)
        self.assertFalse(self.app.imitation_live_var.get())

    # ---- dry run ----

    def test_poll_runs_the_policy_at_most_once_per_interval(self) -> None:
        self._start()
        policy = FakePolicy(Abstention("unknown screen (best 0.40)"))
        self.app._imitation_policy = policy

        self.app._poll_imitation(self.frame)
        self._drain_until(lambda: not self.app.imitation.proposing)
        self.app._drain_imitation()
        self.app._poll_imitation(self.frame)

        self.assertEqual(len(policy.calls), 1)
        self.assertIn("unknown screen", self.app.imitation_last_var.get())

    def test_dry_run_logs_and_never_submits(self) -> None:
        self._start()
        self._enable_input()

        self._send_outcome(_proposal())

        self.assertIn("would tap", self.app.imitation_last_var.get())
        self.assertIn("Would tap (0.500, 0.500)", self.app.last_event_var.get())
        self.assertIn("demo s1", self.app.last_event_var.get())
        self.assertFalse(self.app.executor.busy)
        self.assertEqual(self.fake.intents, [])
        # The dry run keeps the same cooldown as live taps.
        self.assertEqual([tap[:2] for tap in self.app._imitation_recent], [(0.5, 0.5)])

    def test_stale_outcome_is_dropped(self) -> None:
        self._start()
        generation = self.app.imitation.generation
        self.app.stop_imitation()
        self.app._imitation_outcome(PolicyOutcome(generation, _proposal()))
        self.assertNotIn("would tap", self.app.imitation_last_var.get())

    # ---- live ----

    def test_live_needs_a_running_dry_run_and_input(self) -> None:
        self._load_demos()
        self.app.imitation_live_var.set(True)
        self.app._toggle_imitation_live()
        self.assertFalse(self.app.imitation_live_var.get())

        self._capture()
        self.app.start_imitation()
        self.app.imitation_live_var.set(True)
        self.app._toggle_imitation_live()
        self.assertFalse(self.app.imitation_live_var.get())
        self.messagebox.askyesno.assert_not_called()

    def test_live_declined_stays_off(self) -> None:
        self._start()
        self._enable_input()
        self.messagebox.askyesno.return_value = False
        self.app.imitation_live_var.set(True)
        self.app._toggle_imitation_live()
        self.assertFalse(self.app.imitation_live_var.get())

    def test_live_tap_goes_through_the_executor(self) -> None:
        self._start()
        self._go_live()

        self._send_outcome(_proposal(0.6, 0.4))
        self._wait_idle()

        [intent] = self.fake.intents
        self.assertEqual(intent.action, "tap")
        self.assertEqual(intent.rule_name, IMITATION_SOURCE)
        self.assertEqual(intent.tap_point, (0.6, 0.4))
        self.assertIsNone(intent.skill_name)
        self.assertEqual(self.fake.hwnds, [4242])
        self.assertIsNone(self.app._imitation_intent)
        self.assertIn("tapped", self.app.imitation_last_var.get())
        self.assertEqual(self.app._imitation_failures, 0)

    def test_no_policy_call_while_a_tap_runs(self) -> None:
        self._start()
        self._go_live()
        policy = FakePolicy(Abstention("unknown screen (best 0.40)"))
        self.app._imitation_policy = policy
        self.app._imitation_intent = object()

        self.app._poll_imitation(self.frame)

        self.assertEqual(policy.calls, [])

    def test_deny_zone_is_rechecked_before_submitting(self) -> None:
        self._start()
        self._go_live()

        for _ in range(IMITATION_MAX_FAILURES):
            self._send_outcome(_proposal(0.1, 0.05))

        self.assertEqual(self.fake.intents, [])
        self.assertFalse(self.app.imitation_live_var.get())
        self.assertTrue(self.app._imitation_running)

    def test_failed_taps_turn_live_off(self) -> None:
        self.fake.dispatched = False
        self._start()
        self._go_live()

        for index in range(IMITATION_MAX_FAILURES):
            self._send_outcome(_proposal(0.5 + 0.1 * index, 0.5))
            self._wait_idle()

        self.assertEqual(len(self.fake.intents), IMITATION_MAX_FAILURES)
        self.assertFalse(self.app.imitation_live_var.get())

    def test_input_off_turns_live_off_but_keeps_the_dry_run(self) -> None:
        self._start()
        self._go_live()

        self.app.control_var.set(False)
        self.app._toggle_control()

        self.assertFalse(self.app.imitation_live_var.get())
        self.assertTrue(self.app._imitation_running)

    def test_emergency_stop_stops_imitation(self) -> None:
        self._start()
        self._go_live()

        self.app.emergency_stop()

        self.assertFalse(self.app.imitation_live_var.get())
        self.assertFalse(self.app._imitation_running)
        self._send_outcome(_proposal())
        self.assertEqual(self.fake.intents, [])

    def test_window_change_stops_imitation(self) -> None:
        self._start()
        self._go_live()
        self._capture(hwnd=9999)

        self.app._poll_imitation(self.frame)

        self.assertFalse(self.app._imitation_running)
        self.assertFalse(self.app.imitation_live_var.get())

    def test_planner_auto_turns_live_off(self) -> None:
        self._start()
        self._go_live()
        # The planner's auto confirmation path calls this after arming.
        self.app._imitation_live_off("planner auto mode was turned on")
        self.assertFalse(self.app.imitation_live_var.get())

    def test_live_refused_while_recording(self) -> None:
        self._start()
        self._enable_input()
        self.app._recording_ui_state = "recording"
        self.app.imitation_live_var.set(True)
        self.app._toggle_imitation_live()
        self.assertFalse(self.app.imitation_live_var.get())
        self.app._recording_ui_state = "idle"

    def test_change_during_confirmation_keeps_live_off(self) -> None:
        self._start()
        self._enable_input()

        def f8_meanwhile(*_args, **_kwargs):
            self.app.emergency_stop()
            return True

        self.messagebox.askyesno.side_effect = f8_meanwhile
        self.app.imitation_live_var.set(True)
        self.app._toggle_imitation_live()
        self.assertFalse(self.app.imitation_live_var.get())
        self.assertFalse(self.app._imitation_live)

    def test_no_tap_while_the_live_confirmation_is_open(self) -> None:
        # The checkbox variable is already True while the dialog runs the Tk
        # loop; a proposal arriving then must stay a dry run.
        for answer in (True, False):
            with self.subTest(answer=answer):
                self._start()
                self._enable_input()

                def proposal_meanwhile(*_args, **_kwargs):
                    self._send_outcome(_proposal())
                    return answer

                self.messagebox.askyesno.side_effect = proposal_meanwhile
                self.app.imitation_live_var.set(True)
                self.app._toggle_imitation_live()

                self.assertEqual(self.fake.intents, [])
                self.assertFalse(self.app.executor.busy)
                self.assertIn("would tap", self.app.imitation_last_var.get())
                self.assertEqual(self.app._imitation_live, answer)
                self.app.stop_imitation()

    def test_live_tap_is_as_old_as_its_frame(self) -> None:
        self._start()
        self._go_live()
        observed_at = time.monotonic() - 0.2

        self._send_outcome(_proposal(), observed_at=observed_at)
        self._wait_idle()

        [intent] = self.fake.intents
        self.assertEqual(intent.created_at, observed_at)

    def test_stop_cancels_a_pending_tap(self) -> None:
        self._start()
        self._go_live()
        with mock.patch.object(self.app.executor, "cancel") as cancel:
            self.app._imitation_intent = object()
            with mock.patch.object(type(self.app.executor), "busy", new_callable=mock.PropertyMock, return_value=True):
                self.app.stop_imitation()
        cancel.assert_called_once()

    def test_next_policy_call_waits_min_interval_after_a_tap(self) -> None:
        self._start()
        self._go_live()
        policy = FakePolicy(Abstention("unknown screen (best 0.40)"))
        self.app._imitation_policy = policy
        self.app._imitation_last_tap = time.monotonic()

        self.app._poll_imitation(self.frame)

        self.assertEqual(policy.calls, [])


class ImitationThreadSafetyTests(unittest.TestCase):
    def test_runner_never_touches_tk(self) -> None:
        # The runner module imports neither tkinter nor the input path.
        import imitation.runner as runner

        source = open(runner.__file__, encoding="utf-8").read()
        for name in ("tkinter", "core.", "pynput", "action_dispatcher", "skill_executor"):
            self.assertNotIn(name, source)
        self.assertTrue(threading.current_thread() is threading.main_thread())


if __name__ == "__main__":
    unittest.main()
