"""Run one skill at a time on a worker thread so a hold never blocks the Tk loop."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import queue
import threading
import time
from typing import Protocol

from .action_dispatcher import DispatchResult
from .rule_engine import ActionIntent


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillRun:
    """One finished skill attempt, drained by the UI with `SkillExecutor.drain`.

    ``finished_at`` is wall-clock time (``time.time()``) for display.
    """

    source: str
    result: DispatchResult
    finished_at: float


class Dispatcher(Protocol):
    """The part of `ActionDispatcher` the executor uses."""

    def dispatch(
        self,
        intent: ActionIntent,
        *,
        hwnd: int | None,
        now: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> DispatchResult: ...

    def cancel(self) -> None: ...


class SkillExecutor:
    """
    Runs skill intents through `ActionDispatcher` on a daemon worker thread,
    one at a time.

    - `submit` never blocks. While a skill is running, a new one is rejected
      ("busy"), never queued.
    - `cancel` ends the running skill early (a held key is released) and is
      safe from any thread, including a keyboard listener. A skill cancelled
      before its worker reaches the dispatcher sends no input at all.
    - Results go to a `queue.SimpleQueue` that the Tk loop drains with
      `drain()`, so no worker thread ever touches Tk.

    F8 order: `input.set_enabled(False)` first, then `cancel()`.
    """

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._closed = False
        self._results: queue.SimpleQueue[SkillRun] = queue.SimpleQueue()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None

    def submit(
        self, intent: ActionIntent, *, hwnd: int | None, source: str = "manual"
    ) -> str | None:
        """Start `intent` on the worker. Returns None, or why it was not started."""

        with self._lock:
            if self._closed:
                return "Skill executor is shut down."
            if self._thread is not None:
                return "Busy: another skill is running."
            event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(intent, hwnd, source, event),
                name="skill-executor",
                daemon=True,
            )
            self._thread = thread
            self._cancel_event = event
            try:
                thread.start()
            except RuntimeError as exc:
                # Never leave the executor stuck "busy" on a thread that
                # never ran.
                self._thread = None
                self._cancel_event = None
                return f"Could not start skill: {exc}"
        return None

    def cancel(self) -> None:
        """End the running skill, if any. Safe from any thread."""

        with self._lock:
            event = self._cancel_event
        if event is not None:
            event.set()
        self._dispatcher.cancel()

    def drain(self) -> list[SkillRun]:
        """Every finished run since the last drain, oldest first. Never blocks."""

        runs: list[SkillRun] = []
        while True:
            try:
                runs.append(self._results.get_nowait())
            except queue.Empty:
                return runs

    def shutdown(self, *, join_timeout: float = 1.0) -> None:
        """Refuse new skills, cancel the running one and wait briefly for it.

        Does not touch input control: callers disable input first
        (`input.set_enabled(False)`), exactly like the F8 order.
        """

        if join_timeout < 0:
            raise ValueError("join_timeout cannot be negative.")
        with self._lock:
            self._closed = True
            thread = self._thread
        self.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    def _run(
        self,
        intent: ActionIntent,
        hwnd: int | None,
        source: str,
        event: threading.Event,
    ) -> None:
        result: DispatchResult | None = None
        try:
            if event.is_set():
                result = DispatchResult(intent, False, "Cancelled before start.")
            else:
                result = self._dispatcher.dispatch(intent, hwnd=hwnd, cancel_event=event)
        except Exception as exc:
            logger.exception("Skill dispatch failed.")
            result = DispatchResult(intent, False, f"Skill failed: {exc}")
        finally:
            # Report before clearing `busy`, so a caller that sees the
            # executor idle always finds this run in `drain()`.
            if result is not None:
                self._results.put(SkillRun(source, result, time.time()))
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                    self._cancel_event = None
