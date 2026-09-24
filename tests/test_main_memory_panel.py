from __future__ import annotations

import gc
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

from agent.llm_planner import PlannerCancelledError
from agent.memory_store import list_sessions
from agent.notes import load_notes
from agent.ollama_client import OllamaResult
from agent.planner_config import PlannerConfig
from agent.planner_controller import PlannerController
from agent.planner_scheduler import PlannerCycleReport
from agent.profile import save_profile
from agent.session_log import read_session, validate_session
from agent.skill_executor import SkillExecutor
from agent.skills import HoldSkill, PressSkill, SkillPermissions
from main import MEMORY_NO_SESSION_TEXT, PersonalGameAIApp
from tests.test_main_planner_panel import FakeDispatcher, RecordingScheduler, _skip_if_no_display


RUN_TYPE_X = '{"type":"run_skill","skill":"type_x","reason":"type an x"}'
RUN_HOLD_X = '{"type":"run_skill","skill":"hold_x","reason":"the note said so"}'
REMEMBER = '{"type":"remember","note":"type_x works only while Notepad is focused"}'
SLUG = "notepad_demo"


def _write_profile(root: Path, *, llm_notes: bool = False) -> Path:
    return save_profile(
        root,
        "Notepad demo",
        detectors=[],
        skills=[PressSkill("type_x", "x", enabled=True), HoldSkill("hold_x", "x", 1.0)],
        rules=[],
        permissions=SkillPermissions(allowed_keys=frozenset({"x"})),
        planner=PlannerConfig(goal="Type x.", llm_notes=llm_notes),
    )


class MainMemoryPanelTests(unittest.TestCase):
    """v0.8 task 5: the Memory panel, notes wiring and the session log."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        mock.patch("main.keyboard.Listener").start()
        self.addCleanup(mock.patch.stopall)
        # Safety net: input control is turned on, so nothing may reach the desktop.
        self.pydirectinput = mock.patch("core.input_controller.pydirectinput").start()
        self.app = PersonalGameAIApp(self.root)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profiles_dir = Path(self._tmp.name) / "profiles"
        self.app.profiles_dir = self.profiles_dir
        self.memory_root = Path(self._tmp.name) / "app"
        self.app.memory_root = self.memory_root
        self.app._notebooks.clear()
        self.app._switch_notebook(None)
        self.messagebox = mock.patch("main.messagebox").start()
        mock.patch("main.focus_window", return_value=True).start()
        mock.patch.object(self.app, "selected_hwnd", return_value=4242).start()
        self.fake = FakeDispatcher()
        self.app.executor.shutdown()
        self.app.executor = SkillExecutor(self.fake)

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

    def _load(self, *, llm_notes: bool = False) -> None:
        _write_profile(self.profiles_dir, llm_notes=llm_notes)
        self.app.refresh_profiles()
        self.app.profile_var.set(SLUG)
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

    def _disable_planner(self) -> None:
        self.app.planner_enabled_var.set(False)
        self.app._toggle_planner()

    def _sessions(self) -> list[Path]:
        return list_sessions(self.memory_root, SLUG)

    def _records(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _notes_file(self) -> Path:
        return self.memory_root / "memory" / SLUG / "notes.json"

    def _cycle(self, scheduler: RecordingScheduler) -> None:
        """One planner cycle and its report, then one Tk poll."""
        scheduler.planner.plan_once(self.app.game_state)
        scheduler.options["on_cycle"](PlannerCycleReport("ok", "cycle done", False, 0.25, time.time()))
        self.app._drain_planner_queue()
        self.app._sync_notes()
        self.app._poll_planner_proposals()

    def _finish_run(self) -> None:
        deadline = time.monotonic() + 5.0
        while self.app.executor.busy:
            if time.monotonic() > deadline:
                self.fail("Skill executor did not finish.")
            time.sleep(0.01)
        self.app._drain_skill_runs()
        self.app._poll_planner_proposals()

    def _listed(self) -> list[str]:
        return list(self.app.memory_listbox.get(0, "end"))

    def _log_text(self) -> str:
        return self.app.logbox.get("1.0", "end")

    # ---- session log ----

    def test_a_session_logs_start_cycles_steps_auto_and_end(self) -> None:
        self._load()
        self._set_input(True)
        self.assertEqual(self.app.memory_session_var.get(), MEMORY_NO_SESSION_TEXT)
        scheduler = self._enable_planner()

        [path] = self._sessions()
        self.assertIn(path.name, self.app.memory_session_var.get())
        self._cycle(scheduler)
        self.app.approve_planner_proposal()
        self._finish_run()
        self._cycle(scheduler)
        self.app.reject_planner_proposal()
        self.app.planner_auto_steps_var.set("3")
        self.messagebox.askyesno.return_value = True
        self.app.planner_mode_var.set("auto")
        self.app._set_planner_mode()
        self._cycle(scheduler)
        self._finish_run()
        self._disable_planner()

        self.assertEqual(validate_session(path), [])
        records = read_session(path)
        start = records[0]
        self.assertEqual(start["type"], "session_start")
        self.assertEqual(start["profile"], "Notepad demo")
        self.assertEqual(start["model"], "qwen3.5:9b")
        self.assertEqual(start["goal"], "Type x.")
        self.assertIs(start["llm_notes"], False)
        cycles = [r for r in records if r["type"] == "cycle"]
        self.assertEqual(len(cycles), 3)
        self.assertEqual(cycles[0]["latency_s"], 0.25)
        steps = [(r["skill"], r["decision"], r["ok"]) for r in records if r["type"] == "step"]
        self.assertEqual(
            steps, [("type_x", "approved", True), ("type_x", "rejected", None), ("type_x", "auto", True)]
        )
        autos = [(r["on"], r["max_steps"]) for r in records if r["type"] == "auto"]
        self.assertEqual(autos, [(True, 3), (False, None)])
        self.assertEqual((records[-1]["type"], records[-1]["reason"]), ("session_end", "planner disabled"))
        self.assertIn("ended (planner disabled)", self.app.memory_session_var.get())

    def test_every_stop_path_ends_its_session(self) -> None:
        self._load()
        self._enable_planner()
        self.app.clear_rules()
        self._enable_planner()
        self.app.load_selected_profile()
        self._enable_planner()
        self._enable_planner()  # a restart ends the running session first
        self.app.emergency_stop()

        reasons = [read_session(path)[-1]["reason"] for path in self._sessions()]
        self.assertEqual(
            reasons, ["rules were cleared", "a profile was loaded", "planner restarted", "emergency stop"]
        )
        self.assertIsNone(self.app.session_log)

    def test_f8_ends_the_log_after_input_is_released_and_drops_late_notes(self) -> None:
        self._load(llm_notes=True)
        self._set_input(True)
        scheduler = self._enable_planner()
        seen: list[tuple[bool, bool]] = []
        close = self.app._close_session_log

        def spy(reason: str) -> None:
            seen.append((self.app.input.enabled, self.app.planner.is_running))
            close(reason)

        with mock.patch.object(self.app, "_close_session_log", side_effect=spy):
            self.app.emergency_stop()

        self.assertEqual(seen, [(False, False)])
        [path] = self._sessions()
        self.assertEqual(read_session(path)[-1]["reason"], "emergency stop")
        self.answer = REMEMBER
        with self.assertRaises(PlannerCancelledError):
            scheduler.planner.plan_once(self.app.game_state)
        self.app._drain_planner_queue()
        self.app._sync_notes()
        self.assertEqual(self.app.notebook.notes(), ())
        self.assertFalse(self._notes_file().exists())

    def test_a_write_error_turns_the_log_off_and_is_reported_once(self) -> None:
        self._load()
        scheduler = self._enable_planner()
        [path] = self._sessions()
        # A folder where the file should be: every later append fails.
        self.app.session_log.path = path.parent

        self._cycle(scheduler)
        self._cycle(scheduler)
        self.app.reject_planner_proposal()
        self._disable_planner()

        self.assertIsNone(self.app.session_log)
        self.assertEqual(self._log_text().count("Session log turned off"), 1)
        self.assertFalse(self.app.planner.is_running)

    def test_no_session_file_keeps_the_planner_running(self) -> None:
        self.memory_root.mkdir(parents=True)
        (self.memory_root / "memory").write_text("not a folder", encoding="utf-8")

        self._enable_planner()

        self.assertIsNone(self.app.session_log)
        self.assertIn("off", self.app.memory_session_var.get())
        self.assertIn("could not create a session file", self._log_text())

    # ---- notes ----

    def test_with_llm_notes_off_remember_is_refused(self) -> None:
        self._load()
        self.assertFalse(self.app.memory_llm_notes_var.get())
        scheduler = self._enable_planner()
        self.answer = REMEMBER

        outcome = scheduler.planner.plan_once(self.app.game_state)

        self.assertIn("rejected", outcome.message)
        self.assertNotIn('"remember"', self.prompts[-1])
        self.assertIn("Notes from earlier sessions", self.prompts[-1])
        self.assertEqual(self.app.notebook.notes(), ())

    def test_llm_note_reaches_panel_file_log_and_next_session_prompt(self) -> None:
        self._load(llm_notes=True)
        self.assertTrue(self.app.memory_llm_notes_var.get())
        scheduler = self._enable_planner()
        self.answer = REMEMBER

        self._cycle(scheduler)

        self.assertIn('"remember"', self.prompts[-1])
        self.assertEqual(self._listed(), ["[llm] type_x works only while Notepad is focused"])
        self.assertIn("1/20 (1/10 from the planner)", self.app.memory_status_var.get())
        self.assertEqual(len(load_notes(self._notes_file()).notes()), 1)
        self._disable_planner()
        [path] = self._sessions()
        notes = [r for r in read_session(path) if r["type"] == "note"]
        self.assertEqual([(n["action"], n["source"]) for n in notes], [("add", "llm")])

        # Within 30 s a second note is skipped, also after a restart.
        scheduler = self._enable_planner()
        self.answer = '{"type":"remember","note":"another fact"}'
        self._cycle(scheduler)
        self.assertIn("- [llm] type_x works only while Notepad is focused", self.prompts[-1])
        self.assertEqual(len(self.app.notebook.notes()), 1)
        self.assertIn("Planner note skipped", self._log_text())

    def test_user_notes_add_edit_delete_and_survive_a_reload(self) -> None:
        self._load(llm_notes=True)
        scheduler = self._enable_planner()
        self.answer = REMEMBER
        self._cycle(scheduler)

        self.app.memory_note_var.set("The status bar shows after typing.")
        self.app.add_memory_note()
        self.app.memory_note_var.set("Temporary")
        self.app.add_memory_note()
        self.app.memory_listbox.selection_set(0)
        self.app._memory_note_selected()
        self.assertEqual(self.app.memory_note_var.get(), "type_x works only while Notepad is focused")
        self.app.memory_note_var.set("type_x needs Notepad focused (checked).")
        self.app.edit_memory_note()
        self.app.memory_listbox.selection_clear(0, "end")
        self.app.memory_listbox.selection_set(2)
        self.app.delete_memory_note()
        self.messagebox.showwarning.assert_not_called()

        expected = [
            "[user] type_x needs Notepad focused (checked).",
            "[user] The status bar shows after typing.",
        ]
        self.assertEqual(self._listed(), expected)
        self._disable_planner()
        [path] = self._sessions()
        notes = [(r["action"], r["source"]) for r in read_session(path) if r["type"] == "note"]
        self.assertEqual(
            notes,
            [("add", "llm"), ("add", "user"), ("add", "user"), ("edit", "user"), ("delete", "user")],
        )
        # A fresh read of notes.json (as after an app restart) shows the same notes.
        self.app._notebooks.clear()
        self.app._switch_notebook(SLUG)
        self.assertEqual(self._listed(), expected)

    def test_edit_refuses_a_note_that_moved(self) -> None:
        self._load()
        self.app.memory_note_var.set("first")
        self.app.add_memory_note()
        self.app.memory_note_var.set("second")
        self.app.add_memory_note()
        self.app.memory_note_var.set("third")
        self.app.add_memory_note()
        self.app.memory_listbox.selection_set(1)
        self.app.notebook.delete(0)  # as if the list shifted meanwhile

        self.app.memory_note_var.set("changed")
        self.app.edit_memory_note()

        self.assertEqual(self.messagebox.showwarning.call_count, 1)
        self.assertEqual([n.text for n in self.app.notebook.notes()], ["second", "third"])
        # The refresh re-selected the same note at its new place, so Delete hits it.
        self.assertEqual(self.app._selected_note()[1].text, "second")
        self.app.delete_memory_note()
        self.assertEqual([n.text for n in self.app.notebook.notes()], ["third"])

    def test_invalid_notes_file_is_read_only_and_never_overwritten(self) -> None:
        self._notes_file().parent.mkdir(parents=True)
        self._notes_file().write_text('{"format_version": 1, "notes": "oops"}', encoding="utf-8")
        before = self._notes_file().read_bytes()

        self._load(llm_notes=True)

        self.assertIn("READ-ONLY", self.app.memory_status_var.get())
        self.assertTrue(self.app.memory_add_button.instate(["disabled"]))
        self.assertTrue(self.app.memory_llm_notes_check.instate(["disabled"]))
        self.assertIn("Notes are read-only", self._log_text())
        scheduler = self._enable_planner()
        self.answer = REMEMBER
        outcome = scheduler.planner.plan_once(self.app.game_state)
        self.assertIn("rejected", outcome.message)
        self.app.memory_note_var.set("x")
        self.app.add_memory_note()
        self.app.emergency_stop()
        self.app._save_notes_if_changed()
        self.assertEqual(self._notes_file().read_bytes(), before)
        [path] = self._sessions()
        self.assertIs(read_session(path)[0]["llm_notes"], False)

    def test_hostile_notes_file_never_stops_the_app_or_a_profile_load(self) -> None:
        bad = '{"format_version": 1, "notes": [{"text": "t", "source": [], "updated": "x"}]}'
        no_profile = self.memory_root / "memory" / "_no_profile" / "notes.json"
        no_profile.parent.mkdir(parents=True)
        no_profile.write_text(bad, encoding="utf-8")
        self.app._switch_notebook(None)  # what the app does at start-up
        self.assertIn("READ-ONLY", self.app.memory_status_var.get())

        self._load(llm_notes=True)
        self.assertFalse(self.app.memory_llm_notes_check.instate(["disabled"]))
        # The file breaks while the app runs; the next load must still finish.
        self._notes_file().parent.mkdir(parents=True, exist_ok=True)
        self._notes_file().write_text(bad, encoding="utf-8")
        self.app.load_selected_profile()

        self.assertEqual(self.app._notes_slug, SLUG)
        self.assertIsNone(self.app._notes_path)
        self.assertTrue(self.app.memory_llm_notes_check.instate(["disabled"]))
        self._enable_planner()
        [path] = self._sessions()
        self.assertIs(read_session(path)[0]["llm_notes"], False)
        self.assertEqual(no_profile.read_text(encoding="utf-8"), bad)
        self.assertEqual(self._notes_file().read_text(encoding="utf-8"), bad)

    def test_notes_edited_outside_the_app_are_reloaded_never_overwritten(self) -> None:
        self._load()
        self.app.memory_note_var.set("mine")
        self.app.add_memory_note()
        outside = '{"format_version": 1, "notes": [{"text": "by hand", "source": "user", "updated": "x"}]}'
        self._notes_file().write_text(outside, encoding="utf-8")

        self.app.memory_note_var.set("lost")
        self.app.add_memory_note()

        self.assertEqual(self._notes_file().read_text(encoding="utf-8"), outside)
        self.assertIn("READ-ONLY", self.app.memory_status_var.get())
        self.assertIn("changed outside the app", self._log_text())
        self.app.load_selected_profile()
        self.assertEqual(self._listed(), ["[user] by hand"])
        self.assertIsNotNone(self.app._notes_path)
        self.app.memory_note_var.set("after reload")
        self.app.add_memory_note()
        self.assertEqual([n.text for n in load_notes(self._notes_file()).notes()], ["by hand", "after reload"])

        self._notes_file().write_text("{broken", encoding="utf-8")
        self.app.memory_note_var.set("never written")
        self.app.add_memory_note()
        self.assertEqual(self._notes_file().read_text(encoding="utf-8"), "{broken")

    def test_reload_keeps_the_planner_note_rate_limit(self) -> None:
        self._load(llm_notes=True)
        scheduler = self._enable_planner()
        self.answer = REMEMBER
        self._cycle(scheduler)
        self._notes_file().write_text(
            '{"format_version": 1, "notes": []}', encoding="utf-8"
        )
        self.app.load_selected_profile()
        self.assertEqual(self._listed(), [])
        scheduler = self._enable_planner()
        outcome = scheduler.planner.plan_once(self.app.game_state)
        self.assertIn("every", outcome.message)

    def test_llm_notes_checkbox_needs_a_profile(self) -> None:
        self.assertTrue(self.app.memory_llm_notes_check.instate(["disabled"]))
        self.app.memory_llm_notes_var.set(True)
        self.app._refresh_memory_panel(force=True)
        self.assertFalse(self.app.memory_llm_notes_var.get())
        self._load()
        self.assertFalse(self.app.memory_llm_notes_check.instate(["disabled"]))

    def test_read_only_edit_says_read_only(self) -> None:
        self._notes_file().parent.mkdir(parents=True)
        self._notes_file().write_text("{broken", encoding="utf-8")
        self._load()
        self.app.edit_memory_note()
        self.app.delete_memory_note()
        for call in self.messagebox.showwarning.call_args_list:
            self.assertIn("read-only", call.args[1])

    def test_a_planner_note_keeps_the_users_selection(self) -> None:
        self._load(llm_notes=True)
        for text in ("first", "second"):
            self.app.memory_note_var.set(text)
            self.app.add_memory_note()
        self.app.memory_listbox.selection_set(1)
        scheduler = self._enable_planner()
        self.answer = REMEMBER
        self._cycle(scheduler)
        self.assertEqual(len(self._listed()), 3)
        self.assertEqual(self.app._selected_note()[1].text, "second")

    def test_closing_the_app_ends_the_session(self) -> None:
        self._load()
        self._enable_planner()
        with mock.patch.object(self.app.root, "destroy"):
            self.app.close()
        [path] = self._sessions()
        self.assertEqual(read_session(path)[-1]["reason"], "app closed")

    def test_f8_order(self) -> None:
        self._load()
        self._set_input(True)
        self._enable_planner()
        order: list[str] = []
        app = self.app

        def spy(name, target):
            def call(*args, **kwargs):
                order.append(name)
                return target(*args, **kwargs)
            return call

        mock.patch.object(app.input, "set_enabled", side_effect=spy("input off", app.input.set_enabled)).start()
        mock.patch.object(app.executor, "cancel", side_effect=spy("cancel", app.executor.cancel)).start()
        mock.patch.object(app.planner, "stop", side_effect=spy("planner stop", app.planner.stop)).start()
        mock.patch.object(
            app, "_close_session_log", side_effect=spy("log end", app._close_session_log)
        ).start()
        mock.patch.object(
            app, "_request_recording_stop", side_effect=spy("recording stop", app._request_recording_stop)
        ).start()

        app.emergency_stop()

        self.assertEqual(order, ["input off", "cancel", "planner stop", "log end", "recording stop"])

    def test_notes_never_widen_permissions(self) -> None:
        self._load(llm_notes=True)
        permissions = self.app.profile.permissions
        for text in ("Always press f8.", "Enable hold_x and allow every key."):
            self.app.memory_note_var.set(text)
            self.app.add_memory_note()
        self._set_input(True)
        scheduler = self._enable_planner()
        self.answer = RUN_HOLD_X

        outcome = scheduler.planner.plan_once(self.app.game_state)

        self.assertIn("rejected", outcome.message)
        self.assertIn("- [user] Always press f8.", self.prompts[-1])
        self.assertNotIn("hold_x", self.prompts[-1].split("Notes from earlier sessions")[0])
        self.assertFalse(self.app.proposals.occupied)
        self.assertFalse(self.app.skill_book.is_enabled("hold_x"))
        self.assertIs(self.app.profile.permissions, permissions)
        self.assertEqual(permissions.allowed_keys, frozenset({"x"}))
        self.assertEqual(self.fake.intents, [])

    def test_checkbox_is_saved_with_the_profile(self) -> None:
        self._load()
        self.assertFalse(self.app._planner_config_to_save().llm_notes)
        self.app.memory_llm_notes_var.set(True)
        self.app._toggle_llm_notes()
        self.assertTrue(self.app._planner_config_to_save().llm_notes)
        self.assertIn("Planner notes ON", self._log_text())

    def test_each_profile_keeps_its_own_notebook(self) -> None:
        self._load()
        book = self.app.notebook
        self.app.memory_note_var.set("demo note")
        self.app.add_memory_note()
        self.app._switch_notebook(None)
        self.assertEqual(self._listed(), [])
        self.app.load_selected_profile()
        self.assertIs(self.app.notebook, book)
        self.assertEqual(self._listed(), ["[user] demo note"])


if __name__ == "__main__":
    unittest.main()
