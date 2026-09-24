"""Approve/auto state machine for planner skill proposals (v0.7).

Pure and single-threaded: only the Tk thread calls it. It decides what happens
to a proposal taken from the mailbox; it never submits anything itself. The UI
submits the returned proposal through the skill executor (after rebuilding the
intent from fresh state) and reports the dispatch result back.

Approve-each-step is the default. Auto mode is armed explicitly with a step
cap (hard cap ``HARD_MAX_AUTO_STEPS``) and disarms itself after the cap, after
``MAX_CONSECUTIVE_FAILURES`` failed steps in a row, or when the UI calls
``disarm`` (F8, input off, profile load, Clear Rules, planner off).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeAlias

from .proposal_mailbox import SkillProposal


Mode: TypeAlias = Literal["approve", "auto"]
DEFAULT_TTL_SECONDS = 10.0
DEFAULT_AUTO_MAX_STEPS = 20
HARD_MAX_AUTO_STEPS = 100
MAX_CONSECUTIVE_FAILURES = 3


class OfferResult(Enum):
    AWAIT = "await"
    EXECUTE = "execute"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class _Pending:
    proposal: SkillProposal
    offered_at: float


def validate_auto_max_steps(value: object) -> int:
    """Return ``value`` if it is an int in 1..HARD_MAX_AUTO_STEPS, else raise ValueError."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("auto_max_steps must be an integer.")
    if not 1 <= value <= HARD_MAX_AUTO_STEPS:
        raise ValueError(f"auto_max_steps must be between 1 and {HARD_MAX_AUTO_STEPS}.")
    return value


class Autopilot:
    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1.")
        self._ttl_seconds = float(ttl_seconds)
        self._max_failures = max_consecutive_failures
        self._mode: Mode = "approve"
        self._max_steps = DEFAULT_AUTO_MAX_STEPS
        self._steps_taken = 0
        self._failures = 0
        self._pending: _Pending | None = None
        self._running: SkillProposal | None = None

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def steps_taken(self) -> int:
        return self._steps_taken

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def pending(self) -> SkillProposal | None:
        """The proposal awaiting the user's decision, if any."""

        return self._pending.proposal if self._pending else None

    @property
    def running(self) -> SkillProposal | None:
        """The proposal handed out for execution whose result is not recorded yet."""

        return self._running

    @property
    def busy(self) -> bool:
        return self._pending is not None or self._running is not None

    def seconds_left(self, now: float) -> float:
        if self._pending is None:
            return 0.0
        return max(0.0, self._ttl_seconds - (now - self._pending.offered_at))

    def offer(self, proposal: SkillProposal, now: float) -> OfferResult:
        """Accept a new proposal: run it (auto), hold it for approval, or drop it."""

        if self.busy:
            return OfferResult.DROPPED
        if self._mode == "auto":
            self._steps_taken += 1
            self._running = proposal
            return OfferResult.EXECUTE
        self._pending = _Pending(proposal, now)
        return OfferResult.AWAIT

    def approve(self, now: float) -> SkillProposal | None:
        """Hand out the pending proposal for execution, unless it has expired."""

        if self._pending is None or self.seconds_left(now) <= 0.0:
            return None
        proposal = self._pending.proposal
        self._pending = None
        self._running = proposal
        return proposal

    def reject(self) -> SkillProposal | None:
        pending = self.pending
        self._pending = None
        return pending

    def expire(self, now: float) -> SkillProposal | None:
        """Drop and return the pending proposal once its TTL has passed."""

        if self._pending is None or self.seconds_left(now) > 0.0:
            return None
        return self.reject()

    def record_result(self, ok: bool) -> str | None:
        """Record the running step's outcome; return an auto-off reason if auto disarmed."""

        self._running = None
        self._failures = 0 if ok else self._failures + 1
        if self._mode != "auto":
            return None
        if self._failures >= self._max_failures:
            return self.disarm(f"{self._failures} failed steps in a row")
        if self._steps_taken >= self._max_steps:
            return self.disarm(f"reached the {self._max_steps}-step limit")
        return None

    def arm_auto(self, max_steps: int) -> None:
        self._max_steps = validate_auto_max_steps(max_steps)
        self._mode = "auto"
        self._steps_taken = 0
        self._failures = 0

    def disarm(self, reason: str) -> str | None:
        """Return to approve mode. Returns ``reason`` if auto was on, else None."""

        was_auto = self._mode == "auto"
        self._mode = "approve"
        return reason if was_auto else None

    def reset(self) -> SkillProposal | None:
        """Disarm and forget every proposal (F8, planner stop, profile load).

        Returns the pending proposal that was dropped, if any.
        """

        self.disarm("reset")
        dropped = self.reject()
        self._running = None
        self._failures = 0
        return dropped
