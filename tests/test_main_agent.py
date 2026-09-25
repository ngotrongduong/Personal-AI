"""v1.0 task 4: the Agent panel, the run budget and the goal condition."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from agent.agent_session import GoalCondition
from agent.ollama_client import OllamaClientConfig, OllamaError, OllamaErrorKind, OllamaResult
from agent.planner_config import PlannerConfig
from agent.profile import DetectorDefinition, save_profile
from agent.session_log import read_session
from agent.skill_effects import Expectation
from agent.skills import HoldSkill, PressSkill, SkillPermissions
from main import AGENT_NO_RUN_TEXT, SESSION_END_AGENT_STOP
from tests.test_main_planner_panel import FakeDispatcher, PlannerPanelTestCase
from tests.test_profile import _template


MODEL_OK = OllamaResult(text="model 'qwen3.5:9b' is available")
MODEL_MISSING = OllamaResult(
    error=OllamaError(
        OllamaErrorKind.MODEL_MISSING,
        "Model 'qwen3.5:9b' is not installed; run: ollama pull qwen3.5:9b",
    )
)


class MainAgentTests(PlannerPanelTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.model_result = MODEL_OK
        self.checked_on: list[threading.Thread] = []

        def check(config) -> OllamaResult:
            self.checked_on.append(threading.current_thread())
            return self.model_result

        self.app.model_checker = check

    # ---- helpers ----

    def _write_agent_profile(self, *, max_run_minutes: float = 15.0, goal: bool = True) -> None:
        save_profile(
            self.profiles_dir,
            "Notepad demo",
            detectors=[(DetectorDefinition("x_glyph", "", 0.9), _template())],
            skills=[PressSkill("type_x", "x", enabled=True), HoldSkill("hold_x", "x", 1.0)],
            rules=[],
            permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
            planner=PlannerConfig(
                goal="Type the letter x.",
                max_run_minutes=max_run_minutes,
                stop_when=GoalCondition("x_glyph") if goal else None,
            ),
            expectations={"type_x": Expectation("x_glyph")},
        )

    def _capture_on(self) -> None:
        self.app.capture = mock.Mock(actual_fps=30.0, last_error=None)
        self.app._capture_title = "Untitled - Notepad"

    def _ready(self, **profile) -> None:
        self._write_agent_profile(**profile)
        self._load()
        self._capture_on()

    def _finish_preflight(self) -> None:
        thread = self.app._preflight_thread
        if thread is not None:
            thread.join(5.0)
            self.assertFalse(thread.is_alive())
        self.app._drain_agent_queue()

    def _start_agent(self) -> None:
        self.app.start_agent()
        self._finish_preflight()

    def _see_glyph(self, at: float | None = None) -> None:
        self.app.game_state.update_detector(
            "x_glyph",
            visible=True,
            confidence=0.95,
            observed_at=time.monotonic() if at is None else at,
        )

    def _session_end(self, log) -> str:
        records = read_session(log.path)
        self.assertEqual(records[-1]["type"], "session_end")
        return records[-1]["reason"]

    # ---- preflight ----

    def test_preflight_lists_every_check(self) -> None:
        self.app.run_agent_preflight()
        self._finish_preflight()

        checks = self.app.agent_checks_var.get()
        for line in (
            "[FAIL] Profile: load a game profile",
            "[FAIL] Capture: start capture on the game window",
            "[OK] Planner settings: model 'qwen3.5:9b'",
            "[OK] Ollama: model 'qwen3.5:9b' is available",
            "[FAIL] Enabled skills: enable at least one skill",
            "[NOTE] Input control: off",
            "[NOTE] Goal: no goal set",
        ):
            self.assertIn(line, checks)
        self.assertTrue(self.app.agent_status_var.get().startswith("Agent: Not ready: Profile"))
        self.assertFalse(self.app.planner.is_running)

    def test_model_check_runs_off_the_tk_thread(self) -> None:
        self._ready()
        self.app.run_agent_preflight()
        self._finish_preflight()

        self.assertEqual(len(self.checked_on), 1)
        self.assertIsNot(self.checked_on[0], threading.main_thread())
        self.assertIn("Agent: Ready (check: Input control)", self.app.agent_status_var.get())

    def test_bad_planner_settings_fail_without_a_network_check(self) -> None:
        self._ready()
        self.app.planner_model_var.set("  ")
        self.app.run_agent_preflight()
        self._finish_preflight()

        self.assertEqual(self.checked_on, [])
        self.assertIn("[FAIL] Planner settings", self.app.agent_checks_var.get())
        self.assertIn("[FAIL] Ollama: no planner settings", self.app.agent_checks_var.get())

    # ---- Start Agent ----

    def test_start_agent_refuses_on_a_failed_check_and_starts_nothing(self) -> None:
        self._write_agent_profile()
        self._load()  # no capture

        self._start_agent()

        self.assertFalse(self.app.planner.is_running)
        self.assertIsNone(self.app.session_log)
        self.assertIsNone(self.app.agent_run)
        self.assertIn("Agent not started: Not ready: Capture", self._log_text())

    def test_missing_model_blocks_start_with_the_pull_hint(self) -> None:
        self._ready()
        self.model_result = MODEL_MISSING

        self._start_agent()

        self.assertFalse(self.app.planner.is_running)
        self.assertIn("run: ollama pull qwen3.5:9b", self.app.agent_checks_var.get())

    def test_start_agent_starts_the_planner_but_never_input_or_auto(self) -> None:
        self._ready()

        self._start_agent()

        self.assertTrue(self.app.planner.is_running)
        self.assertIsNotNone(self.app.session_log)
        self.assertIsNotNone(self.app.agent_run)
        self.assertFalse(self.app.control_var.get())
        self.assertFalse(self.app.input.enabled)
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self.app.planner_mode_var.get(), "approve")
        self.messagebox.askyesno.assert_not_called()
        self.assertIn("15:00 left", self.app.agent_run_var.get())
        self.assertIn("goal: x_glyph visible", self.app.agent_run_var.get())
        self.assertIn("Agent started", self._log_text())

    def test_start_agent_keeps_input_on_when_the_user_turned_it_on(self) -> None:
        self._ready()
        self._set_input(True)

        self._start_agent()

        self.assertTrue(self.app.planner.is_running)
        self.assertTrue(self.app.input.enabled)
        self.assertEqual(self.app.autopilot.mode, "approve")

    def test_a_stop_during_the_check_cancels_the_start(self) -> None:
        self._ready()
        release = threading.Event()

        def slow_check(config) -> OllamaResult:
            release.wait(5.0)
            return MODEL_OK

        self.app.model_checker = slow_check
        self.app.start_agent()
        self.app.emergency_stop()
        release.set()
        self._finish_preflight()

        self.assertFalse(self.app.planner.is_running)
        self.assertIsNone(self.app.agent_run)

    def test_start_rechecks_facts_that_changed_during_the_check(self) -> None:
        self._ready()
        release = threading.Event()

        def slow_check(config) -> OllamaResult:
            release.wait(5.0)
            return MODEL_OK

        self.app.model_checker = slow_check
        self.app.start_agent()
        self.app.capture = None
        release.set()
        self._finish_preflight()

        self.assertFalse(self.app.planner.is_running)
        self.assertIn("[FAIL] Capture", self.app.agent_checks_var.get())

    def _slow_start(self) -> threading.Event:
        release = threading.Event()

        def slow_check(config) -> OllamaResult:
            release.wait(5.0)
            return MODEL_OK

        self.app.model_checker = slow_check
        self.app.start_agent()
        return release

    def test_stop_agent_during_the_check_cancels_the_start(self) -> None:
        self._ready()
        release = self._slow_start()
        self.app.stop_agent()
        release.set()
        self._finish_preflight()

        self.assertFalse(self.app.planner.is_running)
        self.assertIn("Agent start cancelled because you pressed Stop Agent.", self._log_text())

    def test_model_changed_during_the_check_blocks_the_start(self) -> None:
        self._ready()
        release = self._slow_start()
        self.app.planner_model_var.set("other:1b")
        release.set()
        self._finish_preflight()

        self.assertFalse(self.app.planner.is_running)
        self.assertIn("planner settings changed during the check", self.app.agent_checks_var.get())

    def test_checker_gets_the_profile_ollama_settings(self) -> None:
        save_profile(
            self.profiles_dir,
            "Notepad demo",
            detectors=[],
            skills=[PressSkill("type_x", "x", enabled=True)],
            rules=[],
            permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
            planner=PlannerConfig(
                ollama=OllamaClientConfig(model="qwen3.5:9b", host="127.0.0.1", port=11500)
            ),
        )
        self._load()
        seen = []
        self.app.model_checker = lambda config: seen.append(config) or MODEL_OK

        self.app.run_agent_preflight()
        self._finish_preflight()

        [config] = seen
        self.assertEqual((config.host, config.port, config.model), ("127.0.0.1", 11500, "qwen3.5:9b"))

    def test_planner_restart_starts_a_new_run(self) -> None:
        self._ready()
        self._enable_planner()
        first = self.app.agent_run

        self._enable_planner()  # enable while enabled restarts the planner

        self.assertIsNotNone(self.app.agent_run)
        self.assertIsNot(self.app.agent_run, first)

    def test_start_agent_while_running_does_nothing(self) -> None:
        self._ready()
        self._start_agent()
        log = self.app.session_log

        self._start_agent()

        self.assertIs(self.app.session_log, log)
        self.assertIn("The agent is already running.", self._log_text())

    # ---- Stop Agent and the run ----

    def test_stop_agent_ends_the_run(self) -> None:
        self._ready()
        self._start_agent()
        log = self.app.session_log

        self.app.stop_agent()

        self.assertFalse(self.app.planner.is_running)
        self.assertFalse(self.app.planner_enabled_var.get())
        self.assertIsNone(self.app.agent_run)
        self.assertEqual(self._session_end(log), SESSION_END_AGENT_STOP)
        self.assertEqual(self.app.agent_run_var.get(), AGENT_NO_RUN_TEXT)

    def test_budget_ends_the_run_and_turns_auto_off(self) -> None:
        self._ready(max_run_minutes=1.0)
        self._set_input(True)
        self._enable_planner()  # the budget applies however the planner started
        self.assertIn("1:00 left", self.app.agent_run_var.get())
        self._arm_auto()
        log = self.app.session_log

        self.app._poll_agent_run(time.monotonic() + 30.0)
        self.assertTrue(self.app.planner.is_running)

        self.app._poll_agent_run(time.monotonic() + 61.0)

        self.assertFalse(self.app.planner.is_running)
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertEqual(self._session_end(log), "run budget reached")
        self.assertIn("Auto mode OFF because run budget reached.", self._log_text())
        self.assertIn("Planner: disabled because run budget reached.", self.app.planner_status_var.get())
        # Input control is left as the user set it.
        self.assertTrue(self.app.input.enabled)

    def test_goal_ends_the_run(self) -> None:
        self._ready()
        before = time.monotonic()
        self._see_glyph(before)  # seen before the run: does not count
        self._enable_planner()
        log = self.app.session_log

        self.app._poll_agent_run()
        self.assertTrue(self.app.planner.is_running)

        self._see_glyph()
        self.app._poll_agent_run()

        self.assertFalse(self.app.planner.is_running)
        self.assertEqual(self._session_end(log), "goal reached")
        self.assertIn("LLM planner stopped because goal reached.", self._log_text())

    def test_without_stop_when_only_the_budget_ends_the_run(self) -> None:
        self._ready(goal=False)
        self._enable_planner()
        self._see_glyph()

        self.app._poll_agent_run()

        self.assertTrue(self.app.planner.is_running)
        self.assertIn("goal: none", self.app.agent_run_var.get())

    def test_every_planner_stop_ends_the_run(self) -> None:
        self._ready()
        stops = {
            "F8": self.app.emergency_stop,
            "planner off": lambda: (
                self.app.planner_enabled_var.set(False),
                self.app._toggle_planner(),
            ),
            "clear rules": self.app.clear_rules,
        }
        for name, stop in stops.items():
            with self.subTest(stop=name):
                self._enable_planner()
                self.assertIsNotNone(self.app.agent_run)
                stop()
                self.assertIsNone(self.app.agent_run)
                self.assertEqual(self.app.agent_run_var.get(), AGENT_NO_RUN_TEXT)
                # A late poll does nothing.
                self.app._poll_agent_run(time.monotonic() + 10_000.0)
                self.assertFalse(self.app.planner.is_running)

    def test_run_counts_steps_and_effects(self) -> None:
        self._ready(goal=False)
        self._set_input(True)
        scheduler = self._enable_planner()
        self._propose(scheduler)
        self.app.approve_planner_proposal()
        self._finish_run()
        self._see_glyph()
        self.app._poll_effect_watch()
        self.app._poll_agent_run()

        run = self.app.agent_run
        assert run is not None
        self.assertEqual(run.steps, 1)
        self.assertEqual(run.effects, {"confirmed": 1, "not_seen": 0})
        self.assertIn("1 step(s) · effects 1 confirmed / 0 not seen", self.app.agent_run_var.get())

    def _running_planner_hold(self) -> None:
        """A planner step whose skill keeps running until it is cancelled."""
        self.fake = FakeDispatcher(hold=True)
        self._use_executor(self.fake)
        self._ready(goal=False)
        self._set_input(True)
        scheduler = self._enable_planner()
        self._propose(scheduler)
        self.app.approve_planner_proposal()
        self.assertTrue(self.fake.started.wait(2.0))
        self.assertTrue(self.app.executor.busy)

    def _assert_cancelled_soon(self) -> None:
        started = time.monotonic()
        self._wait_idle()  # the fake hold waits 5 s unless it is cancelled
        self.assertLess(time.monotonic() - started, 2.0)

    def test_budget_end_cancels_the_running_planner_skill(self) -> None:
        self._running_planner_hold()

        self.app._poll_agent_run(time.monotonic() + 16 * 60.0)

        self._assert_cancelled_soon()
        self.assertIn("Planner skill cancelled because run budget reached.", self._log_text())

    def test_stop_agent_cancels_the_running_planner_skill(self) -> None:
        self._running_planner_hold()

        self.app.stop_agent()

        self._assert_cancelled_soon()
        self.assertIn("Planner skill cancelled because the agent was stopped.", self._log_text())

    def test_a_run_exists_even_if_the_session_log_fails_to_open(self) -> None:
        self._ready(max_run_minutes=1.0)
        self.app.planner_enabled_var.set(True)
        with mock.patch.object(self.app, "_open_session_log", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                self.app._toggle_planner()

        self.assertTrue(self.app.planner.is_running)
        self.assertIsNotNone(self.app.agent_run)
        self.app._poll_agent_run(time.monotonic() + 61.0)
        self.assertFalse(self.app.planner.is_running)

    def test_rejected_proposal_is_not_a_step(self) -> None:
        self._ready(goal=False)
        scheduler = self._enable_planner()
        self._propose(scheduler)
        self.app.reject_planner_proposal()

        run = self.app.agent_run
        assert run is not None
        self.assertEqual(run.steps, 0)


if __name__ == "__main__":
    unittest.main()
