"""Slow, isolated scheduling for optional LLM planner cycles."""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from .game_state import GameState


logger = logging.getLogger(__name__)


class Planner(Protocol):
    """The synchronous planner interface scheduled by :class:`PlannerScheduler`."""

    def plan_once(self, state: GameState) -> object:
        """Run one optional planning cycle."""


class PlannerScheduler:
    """Run planner cycles slowly on a daemon thread separate from the fast loop.

    Calling :meth:`stop` prevents further cycles promptly by interrupting the
    interval wait. It cannot interrupt a planner call that is already in flight,
    so callers should continue to configure bounded timeouts for the planner's
    network client.
    """

    def __init__(
        self,
        planner: Planner,
        state: GameState,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")

        self._planner = planner
        self._state = state
        self._interval_seconds = interval_seconds
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Whether a worker is active and has not been asked to stop."""

        with self._lifecycle_lock:
            return (
                self._thread is not None
                and self._thread.is_alive()
                and not self._stop_event.is_set()
            )

    def start(self) -> bool:
        """Start the scheduler if it is not already running.

        Returns ``True`` when a new worker was started and ``False`` for an
        already-running or still-shutting-down worker.
        """

        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                args=(self._stop_event,),
                name="planner-scheduler",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, *, join_timeout: float = 1.0) -> None:
        """Request shutdown without waiting for the full scheduling interval.

        A bounded join provides orderly cleanup for normal planner calls while
        ensuring an unexpectedly stalled external planner cannot block callers
        indefinitely. If a call is still stuck in flight when this returns,
        the worker thread is still alive in the background; a subsequent
        :meth:`start` call correctly returns ``False`` until that thread
        actually finishes, rather than starting a second overlapping worker.
        """

        if join_timeout < 0:
            raise ValueError("join_timeout cannot be negative.")

        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

        with self._lifecycle_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _run(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                try:
                    outcome = self._planner.plan_once(self._state)
                    logger.info("Planner cycle outcome: %s", outcome)
                except Exception:
                    logger.exception("Planner scheduler cycle failed; retaining current rule settings.")

                if stop_event.wait(self._interval_seconds):
                    break
        finally:
            with self._lifecycle_lock:
                if self._thread is threading.current_thread():
                    self._thread = None
