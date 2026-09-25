"""Recent planner steps and their outcomes, fed back into the next prompt (v0.7).

This is the "observation" half of the closed loop: the planner sees what it
proposed, what the user (or auto mode) decided, and what happened. It is kept
in memory only; v0.8 adds a persistent log.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import threading
from typing import Literal, TypeAlias


Decision: TypeAlias = Literal["approved", "auto", "rejected", "expired", "refused"]
DECISIONS: frozenset[str] = frozenset({"approved", "auto", "rejected", "expired", "refused"})
# v1.0 observed effect of a step: "none" (no expectation, or not watched),
# "pending" while the watch runs, then "confirmed" or "not_seen".
STEP_EFFECTS: frozenset[str] = frozenset({"none", "pending", "confirmed", "not_seen"})
DEFAULT_MAX_STEPS = 5
_MAX_TEXT = 200


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One resolved planner proposal.

    ``ok`` is the dispatch result for a step that ran (``approved`` / ``auto``)
    and None otherwise. ``finished_at`` is ``time.monotonic()``. ``effect``
    and ``expected`` (e.g. ``"x_glyph visible"``) describe the observed effect
    (v1.0).
    """

    skill_name: str
    reason: str
    decision: Decision
    outcome: str
    ok: bool | None
    finished_at: float
    effect: str = "none"
    expected: str = ""

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"Unknown step decision: {self.decision!r}")
        if self.effect not in STEP_EFFECTS:
            raise ValueError(f"Unknown step effect: {self.effect!r}")


class StepHistory:
    """Thread-safe ring buffer of the most recent steps, oldest first."""

    def __init__(self, maxlen: int = DEFAULT_MAX_STEPS) -> None:
        if not isinstance(maxlen, int) or isinstance(maxlen, bool) or maxlen < 1:
            raise ValueError("maxlen must be a positive integer.")
        self._lock = threading.Lock()
        self._steps: deque[StepRecord] = deque(maxlen=maxlen)

    def append(self, record: StepRecord) -> None:
        with self._lock:
            self._steps.append(record)

    def recent(self) -> tuple[StepRecord, ...]:
        with self._lock:
            return tuple(self._steps)

    def clear(self) -> None:
        with self._lock:
            self._steps.clear()

    def set_effect(self, record: StepRecord, effect: str) -> StepRecord | None:
        """Replace ``record`` (by identity) with a copy carrying ``effect``.

        Returns the new record, or None when ``record`` is no longer kept.
        """

        with self._lock:
            for index, step in enumerate(self._steps):
                if step is record:
                    updated = replace(step, effect=effect)
                    self._steps[index] = updated
                    return updated
        return None

    def prompt_lines(self, now: float) -> list[str]:
        """One line per step for the planner prompt, or a single "none yet" line."""

        steps = self.recent()
        if not steps:
            return ["- none yet"]
        return [
            (
                f"- {step.skill_name}: {step.decision}, {_clean(step.outcome)} "
                f"({max(0.0, now - step.finished_at):.0f}s ago)"
                + _effect_text(step)
            )
            for step in steps
        ]


def _effect_text(step: StepRecord) -> str:
    if step.effect == "none":
        return ""
    effect = "not seen" if step.effect == "not_seen" else step.effect
    expected = _clean(step.expected) if step.expected else "expected change"
    return f", effect {effect} ({expected})"


def _clean(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return cleaned[:_MAX_TEXT] or "no details"
