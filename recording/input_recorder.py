"""Listen-only recorder for the player's own keyboard and mouse input.

Events are recorded only while the target game window is in the foreground,
mouse events only inside its client area (converted to client coordinates),
F8 is never recorded, and input injected by software (LLKHF_INJECTED /
LLMHF_INJECTED) is ignored. Listeners never suppress events for the system,
and this module never sends input.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any

from recording.schema import (
    Event,
    FocusEvent,
    KeyEvent,
    MouseButtonEvent,
    MouseMoveEvent,
    ScrollEvent,
)


logger = logging.getLogger(__name__)

LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
VK_F8 = 0x77

Region = tuple[int, int, int, int]
ListenerFactory = Callable[..., Any]


def default_listener_factory(kind: str, **kwargs: Any) -> Any:
    from pynput import keyboard, mouse

    if kind == "keyboard":
        return keyboard.Listener(**kwargs)
    if kind == "mouse":
        return mouse.Listener(**kwargs)
    raise ValueError(f"Unknown listener kind: {kind!r}.")


def is_f8(key: Any) -> bool:
    if getattr(key, "name", None) == "f8":
        return True
    if getattr(key, "char", None) is None and getattr(key, "vk", None) == VK_F8:
        return True
    return getattr(getattr(key, "value", None), "vk", None) == VK_F8


def key_name(key: Any) -> str | None:
    char = getattr(key, "char", None)
    if isinstance(char, str) and char:
        return char
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return name
    vk = getattr(key, "vk", None)
    if isinstance(vk, int) and not isinstance(vk, bool):
        return f"vk_{vk}"
    return None


def button_name(button: Any) -> str:
    name = getattr(button, "name", None)
    return name if isinstance(name, str) and name else str(button)


@dataclass(frozen=True, slots=True)
class InputRecorderStats:
    events_emitted: int
    injected_ignored: int
    sink_errors: int


class InputRecorder:
    """Turns pynput callbacks into schema events delivered to ``sink``."""

    def __init__(
        self,
        sink: Callable[[Event], object],
        *,
        is_target_foreground: Callable[[], bool],
        client_region: Callable[[], Region | None],
        t0: float,
        clock: Callable[[], float] = time.monotonic,
        mouse_move_min_interval: float = 1 / 30,
        listener_factory: ListenerFactory | None = None,
    ) -> None:
        if mouse_move_min_interval < 0:
            raise ValueError("mouse_move_min_interval cannot be negative.")
        self._sink = sink
        self._is_target_foreground = is_target_foreground
        self._client_region = client_region
        self._t0 = t0
        self._clock = clock
        self._mouse_move_min_interval = mouse_move_min_interval
        self._listener_factory = listener_factory or default_listener_factory
        # One lock serialises handlers from the keyboard and mouse threads so
        # events reach the sink in timestamp order. The sink is called under
        # it, so the sink must never block (SessionWriter.write_event only
        # appends to a queue). The hook filters use their own lock so they can
        # never wait behind a handler.
        self._lock = threading.RLock()
        self._filter_lock = threading.Lock()
        self._running = False
        self._listeners: list[Any] = []
        self._focused: bool | None = None
        self._last_move_at: float | None = None
        self._events_emitted = 0
        self._injected_ignored = 0
        self._sink_errors = 0
        self._callback_errors = 0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> bool:
        """Start keyboard and mouse listeners. Returns False if already running."""
        with self._lock:
            if self._running:
                return False
            self._focused = None
            self._last_move_at = None
            self._running = True
            created: list[Any] = []
            try:
                created.append(
                    self._listener_factory(
                        "keyboard",
                        on_press=self.on_press,
                        on_release=self.on_release,
                        win32_event_filter=self.keyboard_event_filter,
                    )
                )
                created.append(
                    self._listener_factory(
                        "mouse",
                        on_move=self.on_move,
                        on_click=self.on_click,
                        on_scroll=self.on_scroll,
                        win32_event_filter=self.mouse_event_filter,
                    )
                )
                for listener in created:
                    listener.start()
            except Exception:
                self._running = False
                self._stop_listeners(created)
                raise
            self._listeners = created
            return True

    def stop(self) -> None:
        """Stop listening. Idempotent and safe after a failed start()."""
        with self._lock:
            self._running = False
            listeners, self._listeners = self._listeners, []
        self._stop_listeners(listeners)

    @staticmethod
    def _stop_listeners(listeners: list[Any]) -> None:
        for listener in listeners:
            try:
                listener.stop()
            except Exception:
                logger.exception("Failed to stop input listener.")

    def stats(self) -> InputRecorderStats:
        with self._filter_lock:
            injected_ignored = self._injected_ignored
        with self._lock:
            return InputRecorderStats(
                events_emitted=self._events_emitted,
                injected_ignored=injected_ignored,
                sink_errors=self._sink_errors,
            )

    # pynput win32 filters: returning False keeps the event from reaching our
    # handlers only; the system still receives it (we never suppress).
    def keyboard_event_filter(self, _msg: int, data: Any) -> bool:
        return self._accept_flags(data, LLKHF_INJECTED)

    def mouse_event_filter(self, _msg: int, data: Any) -> bool:
        return self._accept_flags(data, LLMHF_INJECTED)

    def _accept_flags(self, data: Any, injected_flag: int) -> bool:
        try:
            injected = bool(int(getattr(data, "flags", 0)) & injected_flag)
        except Exception:
            logger.exception("Could not read input event flags; ignoring event.")
            return False
        if injected:
            with self._filter_lock:
                self._injected_ignored += 1
            return False
        return True

    def poll_focus(self) -> None:
        """Emit a focus event if the target window gained or lost the foreground."""
        try:
            with self._lock:
                if not self._running:
                    return
                events: list[Event] = []
                self._update_focus_locked(self._elapsed_locked(), events)
                self._emit_locked(events)
        except Exception:
            logger.exception("Input recorder focus poll failed.")

    def on_press(self, key: Any) -> None:
        self._handle_key(key, "down")

    def on_release(self, key: Any) -> None:
        self._handle_key(key, "up")

    def on_move(self, x: int, y: int) -> None:
        try:
            with self._lock:
                now = self._clock()
                events, point = self._mouse_prelude_locked(now, x, y)
                if point is not None and self._move_allowed_locked(now):
                    events.append(MouseMoveEvent(t=self._t(now), x=point[0], y=point[1]))
                self._emit_locked(events)
        except Exception:
            logger.exception("Input recorder failed to handle mouse move.")

    def on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        try:
            with self._lock:
                now = self._clock()
                events, point = self._mouse_prelude_locked(now, x, y)
                if point is not None:
                    events.append(
                        MouseButtonEvent(
                            t=self._t(now),
                            button=button_name(button),
                            action="down" if pressed else "up",
                            x=point[0],
                            y=point[1],
                        )
                    )
                self._emit_locked(events)
        except Exception:
            logger.exception("Input recorder failed to handle mouse click.")

    def on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        try:
            with self._lock:
                now = self._clock()
                events, point = self._mouse_prelude_locked(now, x, y)
                if point is not None:
                    events.append(
                        ScrollEvent(t=self._t(now), dx=int(dx), dy=int(dy), x=point[0], y=point[1])
                    )
                self._emit_locked(events)
        except Exception:
            logger.exception("Input recorder failed to handle mouse scroll.")

    def _handle_key(self, key: Any, action: str) -> None:
        try:
            if is_f8(key):
                return
            name = key_name(key)
            with self._lock:
                if not self._running:
                    return
                now = self._clock()
                events: list[Event] = []
                if self._update_focus_locked(self._t(now), events) and name is not None:
                    events.append(KeyEvent(t=self._t(now), key=name, action=action))
                self._emit_locked(events)
        except Exception:
            logger.exception("Input recorder failed to handle key event.")

    def _mouse_prelude_locked(
        self, now: float, x: int, y: int
    ) -> tuple[list[Event], tuple[int, int] | None]:
        events: list[Event] = []
        if not self._running:
            return events, None
        if not self._update_focus_locked(self._t(now), events):
            return events, None
        return events, self._to_client_locked(int(x), int(y))

    def _to_client_locked(self, x: int, y: int) -> tuple[int, int] | None:
        try:
            region = self._client_region()
        except Exception:
            self._log_callback_error_locked("client_region() failed; ignoring mouse event.")
            return None
        if region is None:
            return None
        left, top, right, bottom = region
        if not (left <= x < right and top <= y < bottom):
            return None
        return x - left, y - top

    def _move_allowed_locked(self, now: float) -> bool:
        last = self._last_move_at
        if last is not None and now - last < self._mouse_move_min_interval:
            return False
        self._last_move_at = now
        return True

    def _update_focus_locked(self, t: float, events: list[Event]) -> bool:
        try:
            focused = bool(self._is_target_foreground())
        except Exception:
            self._log_callback_error_locked(
                "is_target_foreground() failed; treating window as unfocused."
            )
            focused = False
        if focused != self._focused:
            self._focused = focused
            events.append(FocusEvent(t=t, action="gained" if focused else "lost"))
        return focused

    def _log_callback_error_locked(self, message: str) -> None:
        # Mouse moves arrive hundreds of times per second; log a traceback
        # only for the first failure (and then every 1000th) of these callbacks.
        self._callback_errors += 1
        if self._callback_errors % 1000 == 1:
            logger.exception("%s (failure #%d)", message, self._callback_errors)

    def _elapsed_locked(self) -> float:
        return self._t(self._clock())

    def _t(self, now: float) -> float:
        return max(0.0, now - self._t0)

    def _emit_locked(self, events: list[Event]) -> None:
        for event in events:
            try:
                self._sink(event)
            except Exception:
                self._sink_errors += 1
                if self._sink_errors == 1:
                    logger.exception("Recording sink rejected an input event.")
                continue
            self._events_emitted += 1
