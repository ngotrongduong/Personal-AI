from __future__ import annotations

import gc
from pathlib import Path
import tkinter as tk
import unittest
from unittest import mock

from main import (
    RECORDING_CLOSE_WAIT_SECONDS,
    RECORDING_IDLE_TEXT,
    PersonalGameAIApp,
    format_recording_status,
)
from recording.recorder_controller import RecordingRefusedError, RecordingStatus


HWND = 4242


def _skip_if_no_display() -> tk.Tk | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    root.withdraw()
    return root


def _status(state: str, **overrides) -> RecordingStatus:
    values = dict(
        state=state,
        session_dir=Path("recordings") / "20260924_120000",
        elapsed=42.0,
        frames=420,
        dropped=0,
        events=1234,
        stop_reason=None,
        error=None,
    )
    values.update(overrides)
    return RecordingStatus(**values)


class FakeCapture:
    hwnd = HWND
    actual_fps = 0.0
    last_error = None

    def latest_frame(self):
        return None

    def stop(self) -> None:
        pass


class FakeRecorder:
    """Stands in for RecordingController; records calls, runs no threads."""

    def __init__(self, app: PersonalGameAIApp) -> None:
        self._app = app
        self.running = False
        self.start_result = True
        self.refuse = False
        self.starts: list[dict] = []
        self.stops: list[tuple[str, bool]] = []
        self.waits: list[float | None] = []
        self.on_status = None

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self, hwnd, capture, game_state, root_dir, fps=10.0, *, on_status=None) -> bool:
        if self.refuse:
            raise RecordingRefusedError("Recording is not allowed while input control is enabled.")
        self.starts.append(
            dict(hwnd=hwnd, capture=capture, game_state=game_state, root_dir=root_dir, fps=fps)
        )
        self.on_status = on_status
        if self.start_result:
            self.running = True
        return self.start_result

    def stop(self, reason: str = "user") -> None:
        # Remember whether input was already released when recording stopped.
        self.stops.append((reason, self._app.input.enabled))

    def wait_stopped(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return True


class FormatRecordingStatusTests(unittest.TestCase):
    def test_recording_line(self) -> None:
        self.assertEqual(
            format_recording_status(_status("recording")),
            "● REC 00:42 · 420 frames · 0 dropped · 1.2k events",
        )

    def test_hours_and_small_event_counts(self) -> None:
        text = format_recording_status(_status("recording", elapsed=3725.9, events=999))
        self.assertEqual(text, "● REC 1:02:05 · 420 frames · 0 dropped · 999 events")

    def test_stopping_stopped_and_failed(self) -> None:
        self.assertIn(
            "Stopping recording (f8)",
            format_recording_status(_status("stopping", stop_reason="f8")),
        )
        stopped = format_recording_status(_status("stopped", stop_reason="max_duration"))
        self.assertIn("recordings/20260924_120000", stopped)
        self.assertIn("stop: max_duration", stopped)
        failed = format_recording_status(
            _status("failed", session_dir=None, error="window is gone")
        )
        self.assertEqual(failed, "Recording failed: window is gone")


class MainRecordingPanelTests(unittest.TestCase):
    """v0.5 task 5: the Recording panel only drives RecordingController."""

    def setUp(self) -> None:
        self.root = _skip_if_no_display()
        if self.root is None:
            self.skipTest("No Tk display available in this environment.")
        self.app = PersonalGameAIApp(self.root)
        self.recorder = FakeRecorder(self.app)
        self.app.recorder = self.recorder
        self.messagebox = mock.patch("main.messagebox").start()
        self.addCleanup(mock.patch.stopall)
        self.closed = False

    def tearDown(self) -> None:
        if not self.closed:
            self.app.capture = None
            self.app.close()
        # Collect Tk variables on the main thread (see test_main_planner_visibility).
        del self.app
        del self.root
        gc.collect()

    def _capture(self) -> FakeCapture:
        capture = FakeCapture()
        self.app.capture = capture
        self.app._refresh_recording_controls()
        return capture

    def _start(self) -> None:
        self._capture()
        self.app.start_recording()
        self.assertEqual(self.app._recording_ui_state, "starting")

    def _button(self) -> tuple[str, str]:
        button = self.app.record_button
        return str(button.cget("text")), str(button.cget("state"))

    def test_record_button_needs_capture_and_input_control_off(self) -> None:
        self.assertEqual(self._button(), ("Record", "disabled"))
        self.assertEqual(self.app.recording_status_var.get(), RECORDING_IDLE_TEXT)

        self._capture()
        self.assertEqual(self._button(), ("Record", "normal"))

        self.app.control_var.set(True)
        self.app._toggle_control()
        self.assertEqual(self._button(), ("Record", "disabled"))

    def test_start_passes_capture_window_state_and_fps(self) -> None:
        capture = self._capture()
        self.app.record_fps_var.set("15")

        self.app.start_recording()

        self.assertEqual(len(self.recorder.starts), 1)
        started = self.recorder.starts[0]
        self.assertEqual(started["hwnd"], HWND)
        self.assertIs(started["capture"], capture)
        self.assertIs(started["game_state"], self.app.game_state)
        self.assertEqual(started["root_dir"], self.app.recordings_dir)
        self.assertEqual(started["fps"], 15.0)
        self.assertEqual(self._button(), ("Stop Recording", "normal"))
        self.assertEqual(str(self.app.record_fps_spin.cget("state")), "disabled")
        self.assertIn("● REC", self.app.recording_status_var.get())

    def test_start_refused_without_capture_or_with_input_control(self) -> None:
        self.app.start_recording()
        self.assertEqual(self.recorder.starts, [])

        self._capture()
        self.app.input.set_enabled(True)
        self.app.start_recording()
        self.assertEqual(self.recorder.starts, [])
        self.assertEqual(self.messagebox.showwarning.call_count, 2)
        self.assertEqual(self.app._recording_ui_state, "idle")

    def test_invalid_fps_is_rejected(self) -> None:
        self._capture()
        for value in ("0", "31", "abc", "nan", ""):
            self.app.record_fps_var.set(value)
            self.app.start_recording()
        self.assertEqual(self.recorder.starts, [])
        self.assertEqual(self.messagebox.showerror.call_count, 5)
        self.assertEqual(self.app._recording_ui_state, "idle")

    def test_controller_refusal_and_busy_controller_leave_panel_idle(self) -> None:
        self._capture()
        self.recorder.refuse = True
        self.app.start_recording()
        self.messagebox.showwarning.assert_called_once()
        self.assertEqual(self.app._recording_ui_state, "idle")

        self.recorder.refuse = False
        self.recorder.start_result = False
        self.app.start_recording()
        self.assertEqual(self.app._recording_ui_state, "idle")
        self.assertIn("still closing", self.app.last_event_var.get())
        self.assertEqual(self._button(), ("Record", "normal"))

    def test_status_reports_update_label_and_finish_back_to_record(self) -> None:
        self._start()
        generation = self.app._recording_generation

        self.app._show_recording_status(generation, _status("recording"))
        self.assertEqual(
            self.app.recording_status_var.get(),
            "● REC 00:42 · 420 frames · 0 dropped · 1.2k events",
        )
        self.assertEqual(self.app._recording_ui_state, "recording")

        self.app.toggle_recording()
        self.assertEqual(self.recorder.stops[-1][0], "user")
        self.assertEqual(self._button(), ("Stopping…", "disabled"))

        # A progress report queued before Stop must not flip the panel back.
        self.app._show_recording_status(generation, _status("recording"))
        self.assertEqual(self.app._recording_ui_state, "stopping")

        self.recorder.running = False
        self.app._show_recording_status(generation, _status("stopped", stop_reason="user"))
        self.assertEqual(self.app._recording_ui_state, "idle")
        self.assertEqual(self._button(), ("Record", "normal"))
        self.assertEqual(str(self.app.record_fps_spin.cget("state")), "normal")
        self.assertIn("recordings/20260924_120000", self.app.recording_status_var.get())
        self.assertIn("Recording saved", self.app.last_event_var.get())

    def test_self_stop_and_failure_return_to_idle(self) -> None:
        for final in (
            _status("stopped", stop_reason="max_duration"),
            _status("failed", session_dir=None, error="boom"),
        ):
            self._start()
            generation = self.app._recording_generation
            self.app._show_recording_status(generation, _status("recording"))
            self.recorder.running = False
            self.app._show_recording_status(generation, final)
            self.assertEqual(self.app._recording_ui_state, "idle")
            self.assertEqual(self._button(), ("Record", "normal"))
        self.assertIn("Recording FAILED: boom", self.app.last_event_var.get())

    def test_stale_generation_reports_are_dropped(self) -> None:
        self._start()
        old_generation = self.app._recording_generation
        self.recorder.running = False
        self.app._show_recording_status(old_generation, _status("stopped", stop_reason="user"))

        self.app.start_recording()
        text = self.app.recording_status_var.get()
        self.app._show_recording_status(old_generation, _status("stopped", stop_reason="f8"))
        self.app._show_recording_status(old_generation, _status("recording"))

        self.assertEqual(self.app.recording_status_var.get(), text)
        self.assertEqual(self.app._recording_ui_state, "starting")

    def test_emergency_stop_releases_input_before_stopping_recording(self) -> None:
        self._start()
        self.app.emergency_stop()
        self.assertEqual(self.recorder.stops, [("f8", False)])
        self.assertEqual(self.app._recording_ui_state, "stopping")
        self.assertIn("Recording stopped by emergency stop.", self._log_text())

    def test_enabling_input_control_stops_recording_first(self) -> None:
        self._start()
        self.app.control_var.set(True)
        self.app._toggle_control()
        self.assertEqual(self.recorder.stops, [("input_control_enabled", False)])
        self.assertTrue(self.app.input.enabled)
        self.assertEqual(self._button(), ("Stopping…", "disabled"))

    def test_stop_capture_stops_recording(self) -> None:
        self._start()
        self.app.stop_capture()
        self.assertEqual(self.recorder.stops[0][0], "capture_stopped")
        self.assertIsNone(self.app.capture)

    def test_close_stops_recording_then_waits_bounded(self) -> None:
        self._start()
        self.app.close()
        self.closed = True
        self.assertEqual(self.recorder.stops[0], ("app_close", False))
        self.assertEqual(self.recorder.waits, [RECORDING_CLOSE_WAIT_SECONDS])

    def test_status_callback_only_queues_and_poll_drains_it(self) -> None:
        self._start()
        on_status = self.recorder.on_status
        with mock.patch.object(self.app.root, "after") as after:
            on_status(_status("recording"))
            on_status(_status("stopping", stop_reason="max_duration"))
            on_status(_status("stopped", stop_reason="max_duration"))
        # The recording thread never calls into Tk.
        after.assert_not_called()
        self.assertEqual(self.app._recording_ui_state, "starting")

        self.recorder.running = False
        self.app._poll_preview()

        self.assertEqual(self.app._recording_ui_state, "idle")
        self.assertIn("stop: max_duration", self.app.recording_status_var.get())
        self.assertIn("Recording saved", self.app.last_event_var.get())

    def test_status_reports_after_close_are_not_queued(self) -> None:
        self._start()
        on_status = self.recorder.on_status
        self.app._closing = True
        on_status(_status("recording"))
        self.assertTrue(self.app._recording_status_queue.empty())
        self.app._closing = False

    def test_close_logs_app_close_not_emergency_stop(self) -> None:
        self._start()
        self.app.emergency_stop(recording_reason="app_close")
        log = self._log_text()
        self.assertIn("Recording stopped because the app is closing.", log)
        self.assertNotIn("Recording stopped by emergency stop.", log)

    def test_sync_safety_net_resets_panel_when_recorder_finished(self) -> None:
        self._start()
        self.app._sync_recording_state()
        self.assertEqual(self.app._recording_ui_state, "starting")

        self.recorder.running = False
        self.app._sync_recording_state()
        self.assertEqual(self.app._recording_ui_state, "idle")
        self.assertEqual(self._button(), ("Record", "normal"))
        self.assertIn("final status not received", self.app.recording_status_var.get())

    def _log_text(self) -> str:
        return self.app.logbox.get("1.0", "end")


if __name__ == "__main__":
    unittest.main()
