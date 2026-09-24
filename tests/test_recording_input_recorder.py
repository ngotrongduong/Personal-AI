from __future__ import annotations

import ast
from pathlib import Path
import unittest

from recording.input_recorder import InputRecorder
from recording.schema import (
    FocusEvent,
    KeyEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    ScrollEvent,
)


class FakeKey:
    def __init__(self, *, char: str | None = None, name: str | None = None, vk: int | None = None):
        if char is not None or name is None:
            self.char = char
            self.vk = vk
        if name is not None:
            self.name = name


class FakeButton:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeFlags:
    def __init__(self, flags: int) -> None:
        self.flags = flags


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


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


REGION = (100, 200, 900, 800)


class InputRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[object] = []
        self.foreground = True
        self.region: tuple[int, int, int, int] | None = REGION
        self.clock = FakeClock(10.0)
        self.listeners: list[FakeListener] = []
        self.fail_mouse_start = False
        self.recorder = self._recorder()
        self.assertTrue(self.recorder.start())
        self.addCleanup(self.recorder.stop)

    def _factory(self, kind: str, **kwargs: object) -> FakeListener:
        listener = FakeListener(
            kind, kwargs, fail_start=kind == "mouse" and self.fail_mouse_start
        )
        self.listeners.append(listener)
        return listener

    def _recorder(self, sink=None) -> InputRecorder:
        return InputRecorder(
            sink or self.events.append,
            is_target_foreground=lambda: self.foreground,
            client_region=lambda: self.region,
            t0=10.0,
            clock=self.clock,
            listener_factory=self._factory,
        )

    def _without_focus_events(self) -> list[object]:
        return [event for event in self.events if not isinstance(event, FocusEvent)]

    def test_records_key_names_with_relative_time(self) -> None:
        self.clock.now = 11.5
        self.recorder.on_press(FakeKey(char="w"))
        self.recorder.on_release(FakeKey(name="space"))
        self.recorder.on_press(FakeKey(char=None, vk=65))
        self.recorder.on_press(FakeKey())

        self.assertEqual(
            self.events,
            [
                FocusEvent(t=1.5, action="gained"),
                KeyEvent(t=1.5, key="w", action="down"),
                KeyEvent(t=1.5, key="space", action="up"),
                KeyEvent(t=1.5, key="vk_65", action="down"),
            ],
        )

    def test_f8_is_never_recorded(self) -> None:
        self.recorder.on_press(FakeKey(name="f8"))
        self.recorder.on_release(FakeKey(name="f8"))
        self.recorder.on_press(FakeKey(char=None, vk=0x77))

        self.assertEqual(self.events, [])

    def test_nothing_recorded_while_game_not_foreground(self) -> None:
        self.foreground = False

        self.recorder.on_press(FakeKey(char="p"))
        self.recorder.on_move(150, 250)
        self.recorder.on_click(150, 250, FakeButton("left"), True)
        self.recorder.on_scroll(150, 250, 0, 1)

        self.assertEqual(self.events, [FocusEvent(t=0.0, action="lost")])

    def test_mouse_events_use_client_coordinates_inside_region_only(self) -> None:
        self.recorder.on_click(100, 200, FakeButton("left"), True)
        self.recorder.on_click(899, 799, FakeButton("right"), False)
        self.recorder.on_click(900, 500, FakeButton("left"), True)
        self.recorder.on_click(500, 800, FakeButton("left"), True)
        self.recorder.on_click(99, 500, FakeButton("left"), True)
        self.recorder.on_scroll(300, 400, -1, 2)

        self.assertEqual(
            self._without_focus_events(),
            [
                MouseButtonEvent(t=0.0, button="left", action="down", x=0, y=0),
                MouseButtonEvent(t=0.0, button="right", action="up", x=799, y=599),
                ScrollEvent(t=0.0, dx=-1, dy=2, x=200, y=200),
            ],
        )

    def test_missing_region_drops_mouse_events(self) -> None:
        self.region = None

        self.recorder.on_move(150, 250)
        self.recorder.on_click(150, 250, FakeButton("left"), True)
        self.recorder.on_press(FakeKey(char="k"))

        self.assertEqual(
            self._without_focus_events(), [KeyEvent(t=0.0, key="k", action="down")]
        )

    def test_mouse_move_is_throttled_by_clock(self) -> None:
        self.recorder.on_move(110, 210)
        self.clock.now = 10.01
        self.recorder.on_move(120, 220)
        self.clock.now = 10.02
        self.recorder.on_move(5000, 5000)  # Outside: does not reset the throttle window.
        self.clock.now = 10.04
        self.recorder.on_move(130, 230)

        moves = [event for event in self.events if isinstance(event, MouseMoveEvent)]
        self.assertEqual([(move.x, move.y) for move in moves], [(10, 10), (30, 30)])
        self.assertAlmostEqual(moves[1].t, 0.04)

    def test_focus_transitions_via_poll_and_input(self) -> None:
        self.recorder.poll_focus()
        self.recorder.poll_focus()
        self.clock.now = 11.0
        self.foreground = False
        self.recorder.poll_focus()
        self.clock.now = 12.0
        self.foreground = True
        self.recorder.on_press(FakeKey(char="a"))
        self.recorder.poll_focus()

        self.assertEqual(
            self.events,
            [
                FocusEvent(t=0.0, action="gained"),
                FocusEvent(t=1.0, action="lost"),
                FocusEvent(t=2.0, action="gained"),
                KeyEvent(t=2.0, key="a", action="down"),
            ],
        )

    def test_injected_events_are_filtered_before_handlers(self) -> None:
        self.assertFalse(self.recorder.keyboard_event_filter(0x100, FakeFlags(0x10)))
        self.assertTrue(self.recorder.keyboard_event_filter(0x100, FakeFlags(0x00)))
        self.assertTrue(self.recorder.keyboard_event_filter(0x100, FakeFlags(0x01)))
        self.assertFalse(self.recorder.mouse_event_filter(0x201, FakeFlags(0x01)))
        self.assertTrue(self.recorder.mouse_event_filter(0x201, FakeFlags(0x00)))

        self.assertEqual(self.recorder.stats().injected_ignored, 2)

    def test_listeners_get_filters_and_never_suppress(self) -> None:
        keyboard_listener, mouse_listener = self.listeners

        self.assertEqual((keyboard_listener.kind, mouse_listener.kind), ("keyboard", "mouse"))
        self.assertEqual(
            set(keyboard_listener.kwargs), {"on_press", "on_release", "win32_event_filter"}
        )
        self.assertEqual(
            set(mouse_listener.kwargs),
            {"on_move", "on_click", "on_scroll", "win32_event_filter"},
        )
        self.assertEqual(keyboard_listener.kwargs["win32_event_filter"], self.recorder.keyboard_event_filter)
        self.assertEqual(mouse_listener.kwargs["win32_event_filter"], self.recorder.mouse_event_filter)
        self.assertEqual((keyboard_listener.started, mouse_listener.started), (1, 1))

    def test_start_stop_are_idempotent_and_stop_silences_handlers(self) -> None:
        self.assertFalse(self.recorder.start())

        self.recorder.stop()
        self.recorder.stop()
        self.recorder.on_press(FakeKey(char="x"))
        self.recorder.poll_focus()

        self.assertFalse(self.recorder.is_running)
        self.assertEqual([listener.stopped for listener in self.listeners], [1, 1])
        self.assertEqual(self.events, [])

        self.assertTrue(self.recorder.start())
        self.assertEqual(len(self.listeners), 4)

    def test_failed_start_cleans_up_and_stop_is_safe(self) -> None:
        self.fail_mouse_start = True
        recorder = self._recorder()

        with self.assertRaises(RuntimeError):
            recorder.start()
        recorder.stop()

        self.assertFalse(recorder.is_running)
        keyboard_listener, mouse_listener = self.listeners[2:]
        self.assertEqual((keyboard_listener.stopped, mouse_listener.stopped), (1, 1))

    def test_sink_exceptions_are_swallowed(self) -> None:
        received: list[object] = []

        def sink(event: object) -> None:
            if isinstance(event, KeyEvent) and event.key == "boom":
                raise RuntimeError("sink failure")
            received.append(event)

        recorder = self._recorder(sink)
        self.assertTrue(recorder.start())
        self.addCleanup(recorder.stop)

        with self.assertLogs("recording.input_recorder", level="ERROR"):
            recorder.on_press(FakeKey(char="boom"))
        recorder.on_press(FakeKey(char="ok"))

        self.assertEqual(received[-1], KeyEvent(t=0.0, key="ok", action="down"))
        self.assertEqual(recorder.stats().sink_errors, 1)

    def test_foreground_check_failure_is_treated_as_unfocused(self) -> None:
        def broken() -> bool:
            raise OSError("no window")

        recorder = InputRecorder(
            self.events.append,
            is_target_foreground=broken,
            client_region=lambda: REGION,
            t0=10.0,
            clock=self.clock,
            listener_factory=self._factory,
        )
        self.assertTrue(recorder.start())
        self.addCleanup(recorder.stop)

        with self.assertLogs("recording.input_recorder", level="ERROR") as logs:
            recorder.on_press(FakeKey(char="a"))
            for offset in range(50):
                recorder.on_move(150 + offset, 250)

        self.assertEqual(self.events, [FocusEvent(t=0.0, action="lost")])
        self.assertEqual(len(logs.output), 1)


FORBIDDEN_MODULE_PARTS = (
    "input_controller",
    "action_dispatcher",
    "rule_engine",
    "pydirectinput",
    "ctypes",
    "win32api",
)
FORBIDDEN_NAMES = {
    "ActionIntent",
    "InputController",
    "ActionDispatcher",
    "Controller",
    "SendInput",
    "keybd_event",
    "mouse_event",
    "SetCursorPos",
    "PostMessage",
    "PostMessageW",
    "SendMessage",
    "SendMessageW",
    "suppress_event",
}


class RecordingPackageIsListenOnlyTests(unittest.TestCase):
    def test_recording_modules_never_import_input_senders(self) -> None:
        package = Path(__file__).resolve().parent.parent / "recording"
        sources = sorted(package.glob("*.py"))
        self.assertTrue(sources)

        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                modules: list[str] = []
                names: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.Attribute):
                    names = [node.attr]
                elif isinstance(node, ast.Name):
                    names = [node.id]
                elif isinstance(node, ast.keyword) and node.arg == "suppress":
                    names = ["suppress_event"]  # Listeners must never suppress events.
                for module in modules:
                    with self.subTest(source=source.name, module=module):
                        self.assertFalse(any(part in module for part in FORBIDDEN_MODULE_PARTS))
                for name in names:
                    with self.subTest(source=source.name, name=name):
                        self.assertNotIn(name, FORBIDDEN_NAMES)


if __name__ == "__main__":
    unittest.main()
