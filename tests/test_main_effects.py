"""v1.0 task 3: the observed effect of a planner step, in the app's loop."""

from __future__ import annotations

import time
import unittest

from agent.action_dispatcher import DispatchResult
from agent.planner_config import PlannerConfig
from agent.profile import DetectorDefinition, save_profile
from agent.session_log import read_session
from agent.skill_effects import Expectation
from agent.skills import HoldSkill, PressSkill, SkillPermissions
from main import PLANNER_NO_PROPOSAL_TEXT
from tests.test_main_planner_panel import FakeDispatcher, PlannerPanelTestCase
from tests.test_profile import _template


class MainEffectTests(PlannerPanelTestCase):
    def _write_effect_profile(self) -> None:
        save_profile(
            self.profiles_dir,
            "Notepad demo",
            detectors=[(DetectorDefinition("x_glyph", "", 0.9), _template())],
            skills=[PressSkill("type_x", "x", enabled=True), HoldSkill("hold_x", "x", 1.0)],
            rules=[],
            permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
            planner=PlannerConfig(),
            expectations={"type_x": Expectation("x_glyph", within_seconds=2.0)},
        )

    def _setup_effects(self):
        self._write_effect_profile()
        self._load()
        self._set_input(True)
        return self._enable_planner()

    def _step(self, scheduler, *, approve: bool = True) -> None:
        self._propose(scheduler)
        if approve:
            self.app.approve_planner_proposal()
        self._finish_run()

    def _see_glyph(self) -> None:
        self.app.game_state.update_detector(
            "x_glyph", visible=True, confidence=0.95, observed_at=time.monotonic()
        )

    def _effects(self) -> list[str]:
        return [step.effect for step in self.app.step_history.recent()]

    def _log_records(self, record_type: str) -> list[dict]:
        log = self.app.session_log
        assert log is not None
        return [r for r in read_session(log.path) if r["type"] == record_type]

    def test_step_with_expect_is_watched_and_confirmed(self) -> None:
        scheduler = self._setup_effects()
        self._step(scheduler)

        self.assertEqual(self._effects(), ["pending"])
        self.assertIsNotNone(self.app._effect_watch)
        # The planner waits: no LLM call while the effect is watched.
        self.assertFalse(self.app._planner_may_plan())
        self.assertIn(
            "Watching: type_x — expecting x_glyph visible", self.app.planner_proposal_var.get()
        )
        # The autopilot counts the step only when the effect resolves.
        self.assertIsNotNone(self.app.autopilot.running)

        self._see_glyph()
        self.app._poll_effect_watch()

        self.assertIsNone(self.app._effect_watch)
        self.assertEqual(self._effects(), ["confirmed"])
        self.assertTrue(self.app._planner_may_plan())
        self.assertIsNone(self.app.autopilot.running)
        self.assertEqual(self.app.planner_proposal_var.get(), PLANNER_NO_PROPOSAL_TEXT)
        self.assertIn("Effect of 'type_x': confirmed", self._log_text())
        scheduler.planner.plan_once(self.app.game_state)
        self.assertIn("effect confirmed (x_glyph visible)", self.prompts[-1])
        [effect] = self._log_records("effect")
        self.assertEqual(effect["skill"], "type_x")
        self.assertEqual(effect["effect"], "confirmed")
        self.assertEqual(effect["detector"], "x_glyph")
        # The step record itself is written first, as in v0.8.
        self.assertEqual(len(self._log_records("step")), 1)

    def test_observation_from_before_the_step_does_not_confirm(self) -> None:
        scheduler = self._setup_effects()
        self._see_glyph()
        self._step(scheduler)

        self.app._poll_effect_watch()
        self.assertEqual(self._effects(), ["pending"])

    def test_effect_not_seen_after_the_window(self) -> None:
        scheduler = self._setup_effects()
        self._step(scheduler)

        self.app._poll_effect_watch(time.monotonic() + 5.0)

        self.assertEqual(self._effects(), ["not_seen"])
        self.assertTrue(self.app._planner_may_plan())
        self.assertIn("Effect of 'type_x': not seen", self._log_text())
        self.assertEqual(self._log_records("effect")[0]["effect"], "not_seen")
        scheduler.planner.plan_once(self.app.game_state)
        self.assertIn("effect not seen (x_glyph visible)", self.prompts[-1])
        # Never retried: exactly one dispatch.
        self.assertEqual(len(self.fake.intents), 1)

    def test_three_not_seen_auto_steps_turn_auto_off(self) -> None:
        scheduler = self._setup_effects()
        self._arm_auto()

        for _ in range(3):
            self._step(scheduler, approve=False)
            self.app._poll_effect_watch(time.monotonic() + 5.0)

        self.assertEqual(self._effects(), ["not_seen"] * 3)
        self.assertEqual(self.app.autopilot.mode, "approve")
        self.assertIn("Auto mode OFF: 3 failed steps in a row.", self._log_text())
        self.assertEqual(len(self.fake.intents), 3)

    def test_confirmed_auto_steps_keep_auto_on(self) -> None:
        scheduler = self._setup_effects()
        self._arm_auto()

        for _ in range(3):
            self._step(scheduler, approve=False)
            self._see_glyph()
            self.app._poll_effect_watch()

        self.assertEqual(self._effects(), ["confirmed"] * 3)
        self.assertEqual(self.app.autopilot.mode, "auto")

    def test_proposal_while_watching_is_dropped(self) -> None:
        scheduler = self._setup_effects()
        self._step(scheduler)

        self._post("type_x")

        self.assertIsNone(self.app.autopilot.pending)
        self.assertEqual(len(self.fake.intents), 1)

    def test_step_without_expect_has_no_watch(self) -> None:
        scheduler = self._setup()  # the plain profile: no expectations
        self._step(scheduler)

        self.assertEqual(self._effects(), ["none"])
        self.assertIsNone(self.app._effect_watch)
        self.assertTrue(self.app._planner_may_plan())

    def test_refused_step_has_no_watch(self) -> None:
        scheduler = self._setup_effects()
        self._set_input(False)
        self._propose(scheduler)
        self.app.approve_planner_proposal()

        self.assertEqual(self._effects(), ["none"])
        self.assertIsNone(self.app._effect_watch)

    def test_interrupted_hold_gets_no_watch(self) -> None:
        class InterruptedDispatcher(FakeDispatcher):
            def dispatch(self, intent, *, hwnd, now=None, cancel_event=None) -> DispatchResult:
                super().dispatch(intent, hwnd=hwnd, now=now, cancel_event=cancel_event)
                return DispatchResult(intent, True, "Held 'x' (lost foreground).", interrupted=True)

        self.fake = InterruptedDispatcher()
        self._use_executor(self.fake)
        scheduler = self._setup_effects()
        self._step(scheduler)

        self.assertEqual(self._effects(), ["none"])
        self.assertIsNone(self.app._effect_watch)
        self.assertTrue(self.app._planner_may_plan())
        self.assertIsNone(self.app.autopilot.running)

    def test_step_drained_after_input_off_gets_no_watch(self) -> None:
        scheduler = self._setup_effects()
        self._propose(scheduler)
        self.app.approve_planner_proposal()
        self._wait_idle()  # ran with input on, not drained yet

        self._set_input(False)
        self._finish_run()

        self.assertEqual(self._effects(), ["none"])
        self.assertIsNone(self.app._effect_watch)
        self.assertTrue(self.app._planner_may_plan())
        self.assertEqual(self._log_records("effect"), [])

    def test_every_stop_drops_the_watch_without_an_effect(self) -> None:
        triggers = {
            "F8": self.app.emergency_stop,
            "input off": lambda: self._set_input(False),
            "clear rules": self.app.clear_rules,
            # Enable while enabled: _toggle_planner restarts the planner.
            "planner restart": self.app._toggle_planner,
            "planner off": lambda: (
                self.app.planner_enabled_var.set(False),
                self.app._toggle_planner(),
            ),
        }
        self._write_effect_profile()
        self._load()
        for number, (name, trigger) in enumerate(triggers.items(), start=1):
            with self.subTest(trigger=name):
                if not self.app.control_var.get():
                    self._set_input(True)
                if not self.app.planner.is_running:
                    self._enable_planner()
                self._step(self.schedulers[-1])
                self.assertIsNotNone(self.app._effect_watch)
                log = self.app.session_log
                assert log is not None

                trigger()

                self.assertIsNone(self.app._effect_watch)
                self.assertFalse(self.app._effect_pending.is_set())
                self.assertIsNone(self.app.autopilot.running)
                self.assertEqual(self.app.step_history.recent()[-1].effect, "none")
                self.assertEqual(
                    self._log_text().count("Effect watch for 'type_x' dropped because"), number
                )
                self.assertEqual(
                    [r for r in read_session(log.path) if r["type"] == "effect"], []
                )
                # Nothing resolves later.
                self._see_glyph()
                self.app._poll_effect_watch()
                self.assertEqual(self.app.step_history.recent()[-1].effect, "none")

    def test_f8_order_is_unchanged(self) -> None:
        scheduler = self._setup_effects()
        self._step(scheduler)
        log = self.app.session_log
        assert log is not None

        self.app.emergency_stop()

        records = read_session(log.path)
        self.assertEqual(records[-1]["type"], "session_end")
        self.assertEqual([r for r in records if r["type"] == "effect"], [])
        self.assertIsNone(self.app._effect_watch)


if __name__ == "__main__":
    unittest.main()
