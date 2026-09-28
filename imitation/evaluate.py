"""Offline leave-out evaluation for retrieval-based imitation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math

from imitation.demo_bank import DemoBank, DemoClick
from imitation.features import read_frame
from imitation.policy import Abstention, ImitationPolicy, PolicyConfig, Proposal


@dataclass(frozen=True, slots=True)
class EvalReport:
    mode: str
    total: int
    hits: int
    misses: int
    abstains: int
    precision: float
    coverage: float
    per_session: dict[str, dict[str, int | float]]
    abstain_reasons: dict[str, int]

    @property
    def abstentions(self) -> int:
        return self.abstains

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "total": self.total,
            "hits": self.hits,
            "misses": self.misses,
            "abstains": self.abstains,
            "precision": self.precision,
            "coverage": self.coverage,
            "per_session": self.per_session,
            "abstain_reasons": self.abstain_reasons,
        }


def evaluate(
    bank: DemoBank,
    config: PolicyConfig,
    *,
    mode: str = "loso",
    gap_seconds: float = 10.0,
) -> EvalReport:
    """Evaluate every click against a bank that leaves out nearby evidence."""

    if mode not in {"loso", "loco"}:
        raise ValueError("mode must be 'loso' or 'loco'")
    if (
        isinstance(gap_seconds, bool)
        or not isinstance(gap_seconds, int | float)
        or not math.isfinite(gap_seconds)
        or gap_seconds < 0.0
    ):
        raise ValueError("gap_seconds must be a non-negative finite number")

    totals: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sessions: dict[str, Counter[str]] = defaultdict(Counter)

    for index, click in enumerate(bank.clicks):
        totals["total"] += 1
        sessions[click.session]["total"] += 1
        frame = read_frame(click.frame_path)
        if frame is None:
            _count_abstain(totals, sessions[click.session], reasons, "unreadable frame")
            continue

        fold_clicks = _fold_clicks(bank.clicks, index, click, mode, float(gap_seconds))
        result = ImitationPolicy(DemoBank.from_clicks(fold_clicks), config).propose(
            frame,
            now=click.t,
            recent=(),
        )
        if isinstance(result, Abstention):
            _count_abstain(totals, sessions[click.session], reasons, result.reason)
        elif _is_hit(result, click, frame.shape[1], frame.shape[0], config.target_radius):
            totals["hits"] += 1
            sessions[click.session]["hits"] += 1
        else:
            totals["misses"] += 1
            sessions[click.session]["misses"] += 1

    per_session = {
        name: _summary(counts)
        for name, counts in sorted(sessions.items())
    }
    summary = _summary(totals)
    return EvalReport(
        mode=mode,
        total=int(summary["total"]),
        hits=int(summary["hits"]),
        misses=int(summary["misses"]),
        abstains=int(summary["abstains"]),
        precision=float(summary["precision"]),
        coverage=float(summary["coverage"]),
        per_session=per_session,
        abstain_reasons=dict(sorted(reasons.items())),
    )


def _fold_clicks(
    clicks: tuple[DemoClick, ...],
    held_index: int,
    held: DemoClick,
    mode: str,
    gap_seconds: float,
) -> tuple[DemoClick, ...]:
    if mode == "loso":
        return tuple(click for click in clicks if click.session != held.session)
    return tuple(
        click
        for index, click in enumerate(clicks)
        if index != held_index
        and not (
            click.session == held.session
            and abs(click.t - held.t) <= gap_seconds
        )
    )


def _is_hit(
    proposal: Proposal,
    click: DemoClick,
    width: int,
    height: int,
    radius: float,
) -> bool:
    distance = math.hypot(
        (proposal.fx - click.fx) * width,
        (proposal.fy - click.fy) * height,
    ) / math.hypot(width, height)
    return distance <= radius


def _count_abstain(
    totals: Counter[str],
    session: Counter[str],
    reasons: Counter[str],
    reason: str,
) -> None:
    totals["abstains"] += 1
    session["abstains"] += 1
    # "unknown screen (best 0.41)" and "(best 0.57)" are the same kind.
    reasons[reason.split(" (")[0]] += 1


def _summary(counts: Counter[str]) -> dict[str, int | float]:
    total = counts["total"]
    hits = counts["hits"]
    misses = counts["misses"]
    proposed = hits + misses
    return {
        "total": total,
        "hits": hits,
        "misses": misses,
        "abstains": counts["abstains"],
        "precision": hits / proposed if proposed else 0.0,
        "coverage": proposed / total if total else 0.0,
    }
