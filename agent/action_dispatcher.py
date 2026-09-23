from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import time

from core.input_controller import InputController
from core.window_utils import client_region

from .rule_engine import ActionIntent


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one dispatch attempt. Always produced, even on rejection."""

    intent: ActionIntent
    dispatched: bool
    reason: str


RegionResolver = Callable[[int], tuple[int, int, int, int]]


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
    4. The target window is valid and its client rect can be resolved.

    See docs/ARCHITECTURE.md for the full runtime-loop diagram this sits in.
    """

    SUPPORTED_ACTIONS = frozenset({"click"})

    def __init__(
        self,
        input_controller: InputController,
        *,
        max_intent_age_seconds: float = 0.5,
        region_resolver: RegionResolver = client_region,
    ) -> None:
        if max_intent_age_seconds <= 0:
            raise ValueError("max_intent_age_seconds must be positive.")
        self._input = input_controller
        self._max_intent_age_seconds = max_intent_age_seconds
        self._region_resolver = region_resolver

    def dispatch(
        self,
        intent: ActionIntent,
        *,
        hwnd: int | None,
        now: float | None = None,
    ) -> DispatchResult:
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

        try:
            self._input.click(screen_x, screen_y)
        except RuntimeError as exc:
            # Input could have been disabled (e.g. F8) between the enabled
            # check above and this call -- treat that race as a normal,
            # loggable rejection rather than letting it propagate.
            return DispatchResult(intent, False, str(exc))

        return DispatchResult(
            intent, True, f"Clicked ({screen_x}, {screen_y})."
        )
