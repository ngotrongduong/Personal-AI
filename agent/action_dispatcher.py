from __future__ import annotations

from collections import deque
from collections.abc import Collection
from dataclasses import dataclass
import math
import threading
from typing import Callable
import time

import pydirectinput

from core.input_controller import InputController
from core.window_utils import client_region, is_foreground

from .rule_engine import ActionIntent
from .skills import PRESS_SECONDS, SkillPermissions


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one dispatch attempt. Always produced, even on rejection."""

    intent: ActionIntent
    dispatched: bool
    reason: str


RegionResolver = Callable[[int], tuple[int, int, int, int]]
PermissionsProvider = Callable[[], SkillPermissions | None]
ForegroundChecker = Callable[[int], bool]

KEY_ACTIONS = frozenset({"press", "hold"})
# How often a running hold re-checks its cancel event and the input switch.
HOLD_POLL_SECONDS = 0.05


class ActionDispatcher:
    """
    The only bridge from ActionIntent (agent/rule_engine.py) to real input
    (core/input_controller.py). Every intent must pass all of these gates,
    checked in order, before any input is sent:

    1. Input control is explicitly enabled (InputController.enabled). This is
       also what F8's emergency_stop() flips off, so a disabled-input check
       here covers the "F8 has not fired" requirement without duplicating
       state.
    2. The intent's action is one this dispatcher knows how to execute.
    3. The intent is still fresh (not older than max_intent_age_seconds) --
       stale intents describe a game state that may no longer be true.
    4. The target is valid:
       - click: the intent has a bbox and the window's client rect resolves;
       - press / hold (v0.6 key skills): a window is selected, the loaded
         profile's permissions allow the key (and the hold time), the key is
         one pydirectinput knows, and the window is the foreground window, so
         keys never reach another app.
    5. No hold is running, and the profile's max_actions_per_second is not
       exceeded (only when a profile's permissions are loaded).

    A hold is key_down, then a wait that ends early on cancel() or when input
    control is switched off (F8), then key_up in a `finally`.

    See docs/ARCHITECTURE.md for the full runtime-loop diagram this sits in.
    """

    SUPPORTED_ACTIONS = frozenset({"click"}) | KEY_ACTIONS

    def __init__(
        self,
        input_controller: InputController,
        *,
        max_intent_age_seconds: float = 0.5,
        region_resolver: RegionResolver = client_region,
        permissions_provider: PermissionsProvider | None = None,
        foreground_checker: ForegroundChecker = is_foreground,
        known_keys: Collection[str] | None = None,
    ) -> None:
        if max_intent_age_seconds <= 0:
            raise ValueError("max_intent_age_seconds must be positive.")
        self._input = input_controller
        self._max_intent_age_seconds = max_intent_age_seconds
        self._region_resolver = region_resolver
        self._permissions_provider = permissions_provider
        self._foreground_checker = foreground_checker
        self._known_keys = frozenset(
            pydirectinput.KEYBOARD_MAPPING if known_keys is None else known_keys
        )
        self._lock = threading.Lock()
        self._recent_dispatches: deque[float] = deque()
        self._hold_cancel: threading.Event | None = None

    @property
    def holding(self) -> bool:
        with self._lock:
            return self._hold_cancel is not None

    def cancel(self) -> None:
        """End a running hold early (its key is released). Safe from any thread."""

        with self._lock:
            event = self._hold_cancel
        if event is not None:
            event.set()

    def dispatch(
        self,
        intent: ActionIntent,
        *,
        hwnd: int | None,
        now: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DispatchResult:
        """Run `intent` if every gate passes. A hold blocks for its duration."""

        current = time.monotonic() if now is None else now

        if not self._input.enabled:
            return DispatchResult(intent, False, "Input control is disabled.")

        if intent.action not in self.SUPPORTED_ACTIONS:
            return DispatchResult(
                intent, False, f"Unsupported action: {intent.action!r}."
            )

        age = current - intent.created_at
        if age > self._max_intent_age_seconds:
            return DispatchResult(
                intent, False, f"Intent is stale ({age:.3f}s old)."
            )

        if intent.action in KEY_ACTIONS:
            return self._dispatch_key(intent, hwnd, current, cancel_event)
        return self._dispatch_click(intent, hwnd, current)

    def _dispatch_click(
        self, intent: ActionIntent, hwnd: int | None, current: float
    ) -> DispatchResult:
        if intent.target_bbox is None:
            return DispatchResult(intent, False, "Intent has no target location.")

        if hwnd is None:
            return DispatchResult(intent, False, "No target window selected.")

        try:
            left, top, _right, _bottom = self._region_resolver(hwnd)
        except Exception as exc:
            return DispatchResult(intent, False, f"Target window unavailable: {exc}")

        x, y, w, h = intent.target_bbox
        screen_x = left + x + w // 2
        screen_y = top + y + h // 2

        refusal = self._admit(self._permissions(), current)
        if refusal is not None:
            return DispatchResult(intent, False, refusal)

        try:
            self._input.click(screen_x, screen_y)
        except RuntimeError as exc:
            # Input could have been disabled (e.g. F8) between the enabled
            # check above and this call -- treat that race as a normal,
            # loggable rejection rather than letting it propagate.
            self._forget(current)
            return DispatchResult(intent, False, str(exc))

        return DispatchResult(
            intent, True, f"Clicked ({screen_x}, {screen_y})."
        )

    def _dispatch_key(
        self,
        intent: ActionIntent,
        hwnd: int | None,
        current: float,
        cancel_event: threading.Event | None,
    ) -> DispatchResult:
        if hwnd is None:
            return DispatchResult(intent, False, "No target window selected.")

        permissions = self._permissions()
        if permissions is None:
            return DispatchResult(
                intent, False, "No profile permissions loaded; key actions are blocked."
            )

        key = intent.key
        denial = permissions.key_denial(key)
        if denial is not None:
            return DispatchResult(intent, False, f"Key blocked: {denial}.")
        if key not in self._known_keys:
            return DispatchResult(intent, False, f"Key blocked: unknown key {key!r}.")

        if intent.action == "hold":
            denial = permissions.hold_denial(intent.hold_seconds)
            if denial is not None:
                return DispatchResult(intent, False, f"Hold blocked: {denial}.")
        elif intent.hold_seconds is not None:
            return DispatchResult(intent, False, "A press intent cannot carry hold_seconds.")

        if not self._is_foreground(hwnd):
            return DispatchResult(
                intent,
                False,
                "Target window is not the foreground window; key blocked.",
            )

        assert isinstance(key, str)
        if intent.action == "press":
            refusal = self._admit(permissions, current)
            if refusal is not None:
                return DispatchResult(intent, False, refusal)
            try:
                self._input.tap_key(key, PRESS_SECONDS)
            except Exception as exc:
                self._forget(current)
                return DispatchResult(intent, False, str(exc) or type(exc).__name__)
            return DispatchResult(intent, True, f"Pressed {key!r}.")

        assert intent.hold_seconds is not None
        return self._hold(
            intent,
            key,
            float(intent.hold_seconds),
            current,
            permissions,
            cancel_event or threading.Event(),
        )

    def _hold(
        self,
        intent: ActionIntent,
        key: str,
        seconds: float,
        current: float,
        permissions: SkillPermissions,
        event: threading.Event,
    ) -> DispatchResult:
        refusal = self._admit(permissions, current, hold_event=event)
        if refusal is not None:
            return DispatchResult(intent, False, refusal)
        started = time.monotonic()
        try:
            try:
                self._input.key_down(key)
            except Exception as exc:
                # key_down may have failed after the key went down; release it.
                self._forget(current)
                self._release_key(key)
                return DispatchResult(intent, False, str(exc) or type(exc).__name__)

            try:
                ending = self._wait_hold(event, seconds)
            finally:
                release_error = self._release_key(key)
        finally:
            with self._lock:
                if self._hold_cancel is event:
                    self._hold_cancel = None

        held = time.monotonic() - started
        reason = f"Held {key!r} for {held:.2f}s"
        if ending != "completed":
            reason += f" ({ending})"
        if release_error is not None:
            reason += f"; key_up failed: {release_error}"
        return DispatchResult(intent, True, reason + ".")

    def _wait_hold(self, event: threading.Event, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "completed"
            if event.wait(min(HOLD_POLL_SECONDS, remaining)):
                return "cancelled"
            if not self._input.enabled:
                return "input disabled"

    def _release_key(self, key: str) -> str | None:
        try:
            self._input.key_up(key)
        except Exception as exc:
            return str(exc) or type(exc).__name__
        return None

    def _admit(
        self,
        permissions: SkillPermissions | None,
        current: float,
        *,
        hold_event: threading.Event | None = None,
    ) -> str | None:
        """Reserve a dispatch slot, or return why none is free.

        Nothing waits while holding the lock; a reserved slot is released with
        `_forget` if the input call then fails.
        """

        with self._lock:
            if self._hold_cancel is not None:
                return "Busy: a hold is in progress."
            if permissions is not None:
                capacity, window = _rate_window(permissions.max_actions_per_second)
                while self._recent_dispatches and current - self._recent_dispatches[0] >= window:
                    self._recent_dispatches.popleft()
                if len(self._recent_dispatches) >= capacity:
                    return (
                        "Rate limit: at most "
                        f"{permissions.max_actions_per_second:g} actions per second."
                    )
            self._recent_dispatches.append(current)
            if hold_event is not None:
                self._hold_cancel = hold_event
        return None

    def _forget(self, stamp: float) -> None:
        with self._lock:
            try:
                self._recent_dispatches.remove(stamp)
            except ValueError:
                pass

    def _permissions(self) -> SkillPermissions | None:
        if self._permissions_provider is None:
            return None
        try:
            permissions = self._permissions_provider()
        except Exception:
            return None
        return permissions if isinstance(permissions, SkillPermissions) else None

    def _is_foreground(self, hwnd: int) -> bool:
        try:
            return bool(self._foreground_checker(hwnd))
        except Exception:
            return False


def _rate_window(max_actions_per_second: float) -> tuple[int, float]:
    """(capacity, window seconds) so `capacity / window == max_actions_per_second`."""

    capacity = max(1, math.floor(max_actions_per_second))
    return capacity, capacity / max_actions_per_second
