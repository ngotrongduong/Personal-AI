"""Single-slot hand-off of planner skill proposals to the Tk thread (v0.7).

The planner thread only *posts* a proposal here. The Tk thread takes it,
decides (approve / auto / reject / expire) and, if it runs, submits the skill
itself. The slot stays occupied from `post` until the Tk thread calls
`release`, so at most one planner step is in flight at a time and the
scheduler can skip its LLM call while `occupied` is true.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """One validated planner proposal: a skill name and a display-only reason.

    ``created_at`` is ``time.monotonic()``; ``generation`` identifies the
    planner start that produced it, so the UI can drop stale proposals.
    """

    skill_name: str
    reason: str
    created_at: float
    generation: int = 0


class ProposalMailbox:
    """Thread-safe single slot: empty → posted → taken → (release) empty."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proposal: SkillProposal | None = None
        self._taken = False

    @property
    def occupied(self) -> bool:
        """True from `post` until `release` or `clear`."""

        with self._lock:
            return self._proposal is not None

    def post(self, proposal: SkillProposal) -> bool:
        """Store `proposal` if the slot is free. Returns False if it is occupied."""

        with self._lock:
            if self._proposal is not None:
                return False
            self._proposal = proposal
            self._taken = False
            return True

    def take(self) -> SkillProposal | None:
        """Hand an untaken proposal to the caller once. The slot stays occupied."""

        with self._lock:
            if self._proposal is None or self._taken:
                return None
            self._taken = True
            return self._proposal

    def release(self) -> None:
        """Free the slot after the taken proposal was resolved."""

        with self._lock:
            self._proposal = None
            self._taken = False

    def clear(self) -> SkillProposal | None:
        """Drop whatever is in the slot (F8, planner stop). Returns what was dropped."""

        with self._lock:
            dropped = self._proposal
            self._proposal = None
            self._taken = False
            return dropped
