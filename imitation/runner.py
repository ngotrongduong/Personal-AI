"""Worker threads behind the app's Imitation panel.

Building a demo bank reads many JPEG frames and a policy call compares the
live frame with every demo, so neither may run on the Tk thread. The runner
starts one daemon thread per job and hands results back through a queue that
the Tk thread drains; it never calls into Tk and never sends input.

A bank load returns its `load_id`; the caller keeps the id of the load it
still wants and ignores any other. A policy call carries the generation it
was started in, and `reset()` bumps the generation, so a proposal computed
before a stop, a profile load or F8 is recognisable as stale.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import queue
import threading

import numpy as np

from imitation.demo_bank import DemoBank, select_sessions
from imitation.policy import Abstention, ImitationPolicy, Proposal


@dataclass(frozen=True, slots=True)
class BankLoaded:
    """Result of `ImitationRunner.load`: a bank, or the error that stopped it."""

    load_id: int
    bank: DemoBank | None
    sessions: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """Result of `ImitationRunner.propose` for one live frame."""

    generation: int
    outcome: Proposal | Abstention | None
    error: str | None = None
    # 	ime.monotonic() when the frame was handed to the policy: a tap built
    # from this outcome is as old as that frame, not as the moment it is sent.
    observed_at: float = 0.0


RunnerResult = BankLoaded | PolicyOutcome

BankBuilder = Callable[[Path, str, Sequence[str]], tuple[DemoBank, tuple[str, ...]]]


def build_bank(
    root: Path, window_title: str, names: Sequence[str]
) -> tuple[DemoBank, tuple[str, ...]]:
    """Select the recordings for `window_title` under `root` and build their bank."""

    session_dirs = select_sessions(root, window_title, tuple(names))
    bank = DemoBank.build(session_dirs)
    return bank, tuple(path.name for path in session_dirs)


class ImitationRunner:
    """At most one bank load and one policy call run at a time."""

    def __init__(self, *, bank_builder: BankBuilder = build_bank) -> None:
        self._bank_builder = bank_builder
        self._lock = threading.Lock()
        self._results: queue.SimpleQueue[RunnerResult] = queue.SimpleQueue()
        self._generation = 0
        self._load_id = 0
        self._loading = False
        self._proposing = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def loading(self) -> bool:
        with self._lock:
            return self._loading

    @property
    def proposing(self) -> bool:
        with self._lock:
            return self._proposing

    def reset(self) -> int:
        """Make every running policy call's result stale; returns the new generation."""

        with self._lock:
            self._generation += 1
            return self._generation

    def load(self, root: Path, window_title: str, names: Sequence[str] = ()) -> int | None:
        """Build a bank on a worker thread and return its load id.

        None when a load is already running.
        """

        with self._lock:
            if self._loading:
                return None
            self._loading = True
            self._load_id += 1
            load_id = self._load_id
        names = tuple(names)

        def work() -> None:
            try:
                bank, sessions = self._bank_builder(root, window_title, names)
                result = BankLoaded(load_id, bank, sessions)
            except Exception as exc:  # reported to the panel, never raised
                result = BankLoaded(load_id, None, (), f"{type(exc).__name__}: {exc}")
            # Queued before the flag clears, so a caller that sees
            # loading=False always finds the result in the next drain.
            self._results.put(result)
            with self._lock:
                self._loading = False

        threading.Thread(target=work, name="imitation-bank", daemon=True).start()
        return load_id

    def propose(
        self,
        policy: ImitationPolicy,
        frame: np.ndarray,
        *,
        now: float,
        recent: Sequence[tuple[float, float, float]] = (),
    ) -> bool:
        """Run one policy call on a worker thread. False when one is already running."""

        with self._lock:
            if self._proposing:
                return False
            self._proposing = True
            generation = self._generation
        recent = tuple(recent)

        def work() -> None:
            try:
                outcome = policy.propose(frame, now=now, recent=recent)
                result = PolicyOutcome(generation, outcome, observed_at=now)
            except Exception as exc:  # reported to the panel, never raised
                result = PolicyOutcome(
                    generation, None, f"{type(exc).__name__}: {exc}", observed_at=now
                )
            self._results.put(result)
            with self._lock:
                self._proposing = False

        threading.Thread(target=work, name="imitation-policy", daemon=True).start()
        return True

    def drain(self) -> list[RunnerResult]:
        """Every finished result, oldest first (Tk thread)."""

        results: list[RunnerResult] = []
        while True:
            try:
                results.append(self._results.get_nowait())
            except queue.Empty:
                return results
