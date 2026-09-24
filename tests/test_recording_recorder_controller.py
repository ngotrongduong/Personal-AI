from __future__ import annotations

from functools import partial
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np

from agent.game_state import GameState
from recording.input_recorder import InputRecorder
from recording.recorder_controller import (
    STOP_ERROR,
    STOP_INPUT_CONTROL,
    STOP_LOW_DISK,
    STOP_MAX_DURATION,
    STOP_USER,
    STOP_WINDOW_CLOSED,
    RecordingController,
    RecordingRefusedError,
    RecordingStatus,
)
from recording.schema import (
    FocusEvent,
    FrameEvent,
    KeyEvent,
    MarkerEvent,
    StateEvent,
    event_from_dict,
    session_from_dict,
)
from recording.session_writer import SessionWriter


HWND = 4242
REGION = (100, 200, 740, 680)


def fake_encoder(frame: object, quality: int) -> bytes:
    return b"jpeg"


class FakeKey:
    def __init__(self, char: str) -> None:
        self.char = char
        self.vk = None


class FakeListener:
    def __init__(self, kind: str, kwargs: dict[str, object], *, fail_start: bool = False):
        self.kind = kind
        self.kwargs = kwargs
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("planned listener failure")
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class FakeCapture:
    def __init__(self, frame: object | None = None) -> None:
        self.frame = np.zeros((4, 4, 3), dtype=np.uint8) if frame is None else frame
        self.calls = 0

    def latest_frame(self) -> object | None:
        self.calls += 1
        return self.frame


class NoFrameCapture:
    def latest_frame(self) -> None:
        return None


class RecordingControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.foreground = HWND
        self.window_alive = True
        self.free_bytes = 10 * 1024**3
        self.listeners: list[FakeListener] = []
        self.fail_listener_start = False
        self.input_control = False
        self.statuses: list[RecordingStatus] = []
        self.state = GameState()
        self.state.update_detector("hp_low", visible=True, confidence=0.9, bbox=(1, 2, 3, 4))

    def _listener_factory(self, kind: str, **kwargs: object) -> FakeListener:
        listener = FakeListener(kind, kwargs, fail_start=self.fail_listener_start)
        self.listeners.append(listener)
        return listener

    def _controller(self, **kwargs: object) -> RecordingController:
        options: dict[str, object] = dict(
            app_version="0.5.0",
            input_control_enabled=lambda: self.input_control,
            writer_factory=partial(SessionWriter, encoder=fake_encoder),
            input_recorder_factory=partial(
                InputRecorder, listener_factory=self._listener_factory
            ),
            foreground_window=lambda: self.foreground,
            client_region=lambda hwnd: REGION,
            window_exists=lambda hwnd: self.window_alive,
            window_title=lambda hwnd: "Test Game",
            disk_free=lambda path: self.free_bytes,
            status_interval=0.05,
        )
        options.update(kwargs)
        controller = RecordingController(**options)  # type: ignore[arg-type]

        def cleanup() -> None:
            controller.stop("cleanup")
            controller.wait_stopped(5.0)

        self.addCleanup(cleanup)
        return controller

    def _start(self, controller: RecordingController, capture: object = None, **kwargs) -> bool:
        return controller.start(
            HWND,
            FakeCapture() if capture is None else capture,
            self.state,
            self.root,
            kwargs.pop("fps", 30),
            on_status=kwargs.pop("on_status", self.statuses.append),
        )

    def _wait_for(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition not reached in time")

    def _wait_recording(self, min_frames: int = 1) -> None:
        self._wait_for(
            lambda: any(s.state == "recording" and s.frames >= min_frames for s in self.statuses)
        )

    def _session_dir(self) -> Path:
        (session_dir,) = [p for p in self.root.iterdir() if p.is_dir()]
        return session_dir

    @staticmethod
    def _events(session_dir: Path) -> list[object]:
        text = (session_dir / "events.jsonl").read_text(encoding="utf-8")
        return [event_from_dict(json.loads(line)) for line in text.splitlines()]

    @staticmethod
    def _session(session_dir: Path):
        text = (session_dir / "session.json").read_text(encoding="utf-8")
        return session_from_dict(json.loads(text))

    def test_records_frames_state_and_input_until_stopped(self) -> None:
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self.assertTrue(controller.is_running)
        self._wait_recording(min_frames=2)

        keyboard = next(listener for listener in self.listeners if listener.kind == "keyboard")
        keyboard.kwargs["on_press"](FakeKey("w"))  # type: ignore[operator]
        keyboard.kwargs["on_release"](FakeKey("w"))  # type: ignore[operator]

        controller.stop()
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertFalse(controller.is_running)
        self.assertTrue(all(listener.stopped for listener in self.listeners))

        session_dir = self._session_dir()
        events = self._events(session_dir)
        self.assertEqual(events[0], MarkerEvent(t=events[0].t, action="start"))
        self.assertEqual(events[-1], MarkerEvent(t=events[-1].t, action="stop", reason=STOP_USER))

        frames = [e for e in events if isinstance(e, FrameEvent)]
        states = [e for e in events if isinstance(e, StateEvent)]
        self.assertGreaterEqual(len(frames), 2)
        self.assertEqual(len(frames), len(states))
        self.assertEqual([f.t for f in frames], [s.t for s in states])
        self.assertEqual(states[0].observations[0].name, "hp_low")
        self.assertEqual(states[0].observations[0].bbox, (1, 2, 3, 4))
        for frame in frames:
            self.assertTrue((session_dir / frame.file).is_file())

        keys = [e for e in events if isinstance(e, KeyEvent)]
        self.assertEqual([(k.key, k.action) for k in keys], [("w", "down"), ("w", "up")])
        focus = [e for e in events if isinstance(e, FocusEvent)]
        self.assertEqual([f.action for f in focus], ["gained"])
        self.assertLess(events.index(focus[0]), events.index(keys[0]))

        info = self._session(session_dir)
        self.assertEqual(info.stop_reason, STOP_USER)
        self.assertIsNotNone(info.ended_at)
        self.assertEqual(info.window_title, "Test Game")
        self.assertEqual((info.client_width, info.client_height), (640, 480))
        self.assertEqual(info.record_fps, 30.0)
        self.assertEqual(info.frames, len(frames))
        self.assertEqual(info.events, len(events))

        self.assertEqual([s.state for s in self.statuses[-2:]], ["stopping", "stopped"])
        final = self.statuses[-1]
        self.assertEqual(final.stop_reason, STOP_USER)
        self.assertEqual(final.session_dir, session_dir)
        self.assertEqual(final.frames, len(frames))

    def test_input_outside_the_game_window_is_not_recorded(self) -> None:
        self.foreground = 1  # another application is in front
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self._wait_recording()

        keyboard = next(listener for listener in self.listeners if listener.kind == "keyboard")
        keyboard.kwargs["on_press"](FakeKey("p"))  # type: ignore[operator]
        controller.stop()
        self.assertTrue(controller.wait_stopped(5.0))

        events = self._events(self._session_dir())
        self.assertFalse([e for e in events if isinstance(e, KeyEvent)])
        self.assertTrue([e for e in events if isinstance(e, FocusEvent) and e.action == "lost"])

    def test_start_is_refused_while_running_and_allowed_after_stop(self) -> None:
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self.assertFalse(self._start(controller))
        controller.stop()
        controller.stop()  # idempotent
        self.assertTrue(controller.wait_stopped(5.0))

        self.assertTrue(self._start(controller))
        controller.stop()
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertEqual(len([p for p in self.root.iterdir() if p.is_dir()]), 2)

    def test_fps_outside_range_is_rejected(self) -> None:
        controller = self._controller()
        for fps in (0, 0.5, 31, float("nan")):
            with self.subTest(fps=fps), self.assertRaises(ValueError):
                self._start(controller, fps=fps)
        self.assertFalse(controller.is_running)
        self.assertFalse(list(self.root.iterdir()))

    def test_start_returns_before_the_session_folder_is_created(self) -> None:
        gate = threading.Event()

        def slow_region(hwnd: int) -> tuple[int, int, int, int]:
            gate.wait(5.0)
            return REGION

        controller = self._controller(client_region=slow_region)
        self.assertTrue(self._start(controller))
        self.assertFalse(list(self.root.iterdir()))
        self.assertTrue(controller.is_running)
        gate.set()
        self._wait_recording()

    def test_stops_itself_at_the_maximum_duration(self) -> None:
        controller = self._controller(max_duration=0.2)
        self.assertTrue(self._start(controller))
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertFalse(controller.is_running)
        self.assertEqual(self._session(self._session_dir()).stop_reason, STOP_MAX_DURATION)
        self.assertEqual(self.statuses[-1].stop_reason, STOP_MAX_DURATION)

    def test_stops_itself_when_disk_space_is_low(self) -> None:
        self.free_bytes = 10
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertEqual(self._session(self._session_dir()).stop_reason, STOP_LOW_DISK)

    def test_stops_itself_when_the_window_closes(self) -> None:
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self._wait_recording()
        self.window_alive = False
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertFalse(controller.is_running)
        self.assertEqual(self._session(self._session_dir()).stop_reason, STOP_WINDOW_CLOSED)

    def test_the_first_stop_reason_wins(self) -> None:
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self._wait_recording()
        controller.stop("f8")
        controller.stop("user")
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertEqual(self._session(self._session_dir()).stop_reason, "f8")

    def test_open_failure_is_reported_without_leaving_a_session_running(self) -> None:
        def broken_region(hwnd: int) -> tuple[int, int, int, int]:
            raise RuntimeError("Selected window has no visible client area.")

        controller = self._controller(client_region=broken_region)
        self.assertTrue(self._start(controller))
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertFalse(controller.is_running)
        self.assertEqual(self.statuses[-1].state, "failed")
        self.assertIn("client area", self.statuses[-1].error or "")
        self.assertFalse(list(self.root.iterdir()))

    def test_listener_failure_closes_the_session_folder(self) -> None:
        self.fail_listener_start = True
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertEqual(self.statuses[-1].state, "failed")
        self.assertEqual(self.statuses[-1].session_dir, self._session_dir())

        info = self._session(self._session_dir())
        self.assertIsNotNone(info.ended_at)
        self.assertEqual(info.stop_reason, STOP_ERROR)

    def test_missing_frames_are_skipped(self) -> None:
        controller = self._controller()
        self.assertTrue(self._start(controller, capture=NoFrameCapture()))
        self._wait_for(lambda: any(s.state == "recording" for s in self.statuses))
        controller.stop()
        self.assertTrue(controller.wait_stopped(5.0))
        events = self._events(self._session_dir())
        self.assertFalse([e for e in events if isinstance(e, (FrameEvent, StateEvent))])
        self.assertEqual(self._session(self._session_dir()).frames, 0)

    def test_unrecordable_observations_are_skipped(self) -> None:
        good = self.state.snapshot()["hp_low"]
        bad = SimpleNamespace(
            name="broken",
            visible=True,
            confidence=2.0,
            bbox=None,
            value=None,
            source="vision",
            observed_at=good.observed_at,
        )
        state = SimpleNamespace(snapshot=lambda: {"broken": bad, "hp_low": good})
        controller = self._controller()
        self.assertTrue(
            controller.start(HWND, FakeCapture(), state, self.root, 30, on_status=self.statuses.append)
        )
        self._wait_recording()
        controller.stop()
        self.assertTrue(controller.wait_stopped(5.0))

        states = [e for e in self._events(self._session_dir()) if isinstance(e, StateEvent)]
        self.assertTrue(states)
        self.assertEqual([r.name for r in states[0].observations], ["hp_low"])

    def test_status_callback_errors_do_not_stop_recording(self) -> None:
        calls: list[RecordingStatus] = []

        def broken_callback(status: RecordingStatus) -> None:
            calls.append(status)
            raise RuntimeError("planned status failure")

        controller = self._controller()
        self.assertTrue(self._start(controller, on_status=broken_callback))
        self._wait_for(lambda: len(calls) >= 3)
        self.assertTrue(controller.is_running)
        controller.stop()
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertEqual(calls[-1].state, "stopped")

    def test_start_is_refused_while_input_control_is_enabled(self) -> None:
        self.input_control = True
        controller = self._controller()
        with self.assertRaises(RecordingRefusedError):
            self._start(controller)
        self.assertFalse(controller.is_running)
        self.assertFalse(self.listeners)
        self.assertFalse(list(self.root.iterdir()))

    def test_enabling_input_control_stops_the_recording(self) -> None:
        controller = self._controller()
        self.assertTrue(self._start(controller))
        self._wait_recording()
        self.input_control = True
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertTrue(all(listener.stopped for listener in self.listeners))
        self.assertEqual(self._session(self._session_dir()).stop_reason, STOP_INPUT_CONTROL)

    def test_a_failing_input_control_check_stops_the_recording(self) -> None:
        checks = {"count": 0}

        def flaky() -> bool:
            checks["count"] += 1
            if checks["count"] > 1:
                raise RuntimeError("planned check failure")
            return False

        controller = self._controller(input_control_enabled=flaky)
        self.assertTrue(self._start(controller))
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertEqual(self._session(self._session_dir()).stop_reason, STOP_INPUT_CONTROL)

    def test_stop_during_a_slow_open_never_installs_input_hooks(self) -> None:
        gate = threading.Event()

        def slow_region(hwnd: int) -> tuple[int, int, int, int]:
            gate.wait(5.0)
            return REGION

        controller = self._controller(client_region=slow_region)
        self.assertTrue(self._start(controller))
        controller.stop()
        self.assertTrue(controller.is_running)  # still closing
        self.assertFalse(self._start(controller))
        gate.set()
        self.assertTrue(controller.wait_stopped(5.0))
        self.assertFalse(controller.is_running)
        self.assertFalse([listener for listener in self.listeners if listener.started])
        self.assertEqual(self._session(self._session_dir()).stop_reason, STOP_USER)

    def test_invalid_limits_are_rejected(self) -> None:
        for kwargs in (
            {"max_duration": 0},
            {"max_duration": float("nan")},
            {"max_duration": float("inf")},
            {"min_free_bytes": -1},
            {"disk_check_interval": 0},
            {"disk_check_interval": float("nan")},
            {"status_interval": 0},
            {"status_interval": float("inf")},
            {"finalise_timeout": -1},
            {"finalise_timeout": float("nan")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RecordingController(
                    app_version="0.5.0",
                    input_control_enabled=lambda: False,
                    **kwargs,  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
