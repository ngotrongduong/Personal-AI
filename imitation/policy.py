"""Pure retrieval policy over a bank of the user's demo clicks."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from imitation.demo_bank import DemoBank, DemoClick
from imitation.features import patch_at, patch_similarity, screen_feature


DenyZone = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    k: int = 5
    screen_threshold: float = 0.92
    patch_threshold: float = 0.8
    cooldown_seconds: float = 3.0
    target_radius: float = 0.03
    deny_zones: tuple[DenyZone, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.k, bool) or not isinstance(self.k, int) or not 1 <= self.k <= 20:
            raise ValueError("k must be an integer from 1 to 20")
        for name in ("screen_threshold", "patch_threshold"):
            value = _number(getattr(self, name), name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
            object.__setattr__(self, name, value)
        cooldown = _number(self.cooldown_seconds, "cooldown_seconds")
        if not 0.0 <= cooldown <= 60.0:
            raise ValueError("cooldown_seconds must be between 0 and 60")
        object.__setattr__(self, "cooldown_seconds", cooldown)
        radius = _number(self.target_radius, "target_radius")
        if not 0.0 < radius <= 1.0:
            raise ValueError("target_radius must be in (0, 1]")
        object.__setattr__(self, "target_radius", radius)
        object.__setattr__(self, "deny_zones", _validate_zones(self.deny_zones))


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _validate_zones(zones: object) -> tuple[DenyZone, ...]:
    if not isinstance(zones, tuple | list):
        raise ValueError("deny_zones must be a sequence")
    if len(zones) > 16:
        raise ValueError("deny_zones may contain at most 16 zones")
    result: list[DenyZone] = []
    for index, zone in enumerate(zones):
        if not isinstance(zone, tuple | list) or len(zone) != 4:
            raise ValueError(f"deny_zones[{index}] must contain x, y, width, height")
        x, y, width, height = (_number(value, f"deny_zones[{index}]") for value in zone)
        if x < 0.0 or y < 0.0 or width <= 0.0 or height <= 0.0:
            raise ValueError(f"deny_zones[{index}] must have x/y >= 0 and width/height > 0")
        if x + width > 1.0 or y + height > 1.0:
            raise ValueError(f"deny_zones[{index}] must stay inside [0, 1]")
        result.append((x, y, width, height))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Proposal:
    fx: float
    fy: float
    screen_similarity: float
    patch_similarity: float
    votes: int
    score: float
    demo_session: str
    demo_t: float
    reason: str


@dataclass(frozen=True, slots=True)
class Abstention:
    reason: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    demo: DemoClick
    screen_similarity: float
    patch_similarity: float

    @property
    def score(self) -> float:
        return self.screen_similarity * self.patch_similarity


class ImitationPolicy:
    def __init__(self, bank: DemoBank, config: PolicyConfig | None = None) -> None:
        self.bank = bank
        self.config = config if config is not None else PolicyConfig()

    def propose(
        self,
        frame_bgr: np.ndarray,
        *,
        now: float,
        recent: object = (),
    ) -> Proposal | Abstention:
        """Retrieve a matching recorded point, or explain why none is safe."""

        now_value = _number(now, "now")
        recent_taps = _validate_recent(recent)
        live_screen = screen_feature(frame_bgr)
        if not self.bank.clicks:
            return Abstention("unknown screen (best 0.00)")
        if self.bank.screens.shape[1] != live_screen.size:
            raise ValueError("live and demo screen features have different sizes")

        raw = np.clip(self.bank.screens @ live_screen, -1.0, 1.0)
        ranked = sorted(
            zip(self.bank.clicks, raw, strict=True),
            key=lambda item: (-float(item[1]), *_demo_order(item[0])),
        )
        best = float(ranked[0][1])
        screen_matches = [
            (demo, float(similarity))
            for demo, similarity in ranked
            if float(similarity) >= self.config.screen_threshold
        ][: self.config.k]
        if not screen_matches:
            return Abstention(f"unknown screen (best {best:.2f})")

        patch_matches: list[_Candidate] = []
        for demo, similarity in screen_matches:
            live_patch = patch_at(frame_bgr, demo.fx, demo.fy)
            local_similarity = patch_similarity(live_patch, demo.patch)
            if local_similarity >= self.config.patch_threshold:
                patch_matches.append(_Candidate(demo, similarity, local_similarity))
        if not patch_matches:
            return Abstention("screen known but no demo target matches")

        frame_height, frame_width = frame_bgr.shape[:2]
        survivors: list[_Candidate] = []
        denied = 0
        cooling = 0
        for candidate in patch_matches:
            demo = candidate.demo
            if in_deny_zone(demo.fx, demo.fy, self.config.deny_zones):
                denied += 1
                continue
            if self._cooling_down(
                demo.fx,
                demo.fy,
                now_value,
                recent_taps,
                frame_width,
                frame_height,
            ):
                cooling += 1
                continue
            survivors.append(candidate)
        if not survivors:
            if denied and not cooling:
                return Abstention("all matching demo targets are in a deny-zone")
            if cooling and not denied:
                return Abstention("all matching demo targets are cooling down")
            return Abstention("all matching demo targets are denied or cooling down")

        clusters: list[list[_Candidate]] = []
        for candidate in survivors:
            for cluster in clusters:
                if _point_distance(
                    candidate.demo.fx,
                    candidate.demo.fy,
                    cluster[0].demo.fx,
                    cluster[0].demo.fy,
                    frame_width,
                    frame_height,
                ) <= self.config.target_radius:
                    cluster.append(candidate)
                    break
            else:
                clusters.append([candidate])

        def cluster_key(cluster: list[_Candidate]) -> tuple[float, str, float, int]:
            earliest = min(cluster, key=lambda item: _demo_order(item.demo)).demo
            return (-sum(item.score for item in cluster), *_demo_order(earliest))

        winner = min(clusters, key=cluster_key)
        selected = min(
            winner,
            key=lambda item: (-item.score, *_demo_order(item.demo)),
        )
        cluster_score = sum(item.score for item in winner)
        demo = selected.demo
        return Proposal(
            fx=demo.fx,
            fy=demo.fy,
            screen_similarity=selected.screen_similarity,
            patch_similarity=selected.patch_similarity,
            votes=len(winner),
            score=cluster_score,
            demo_session=demo.session,
            demo_t=demo.t,
            reason=(
                f"matched {len(winner)} demo click(s); "
                f"screen {selected.screen_similarity:.2f}, patch {selected.patch_similarity:.2f}"
            ),
        )

    def _cooling_down(
        self,
        fx: float,
        fy: float,
        now: float,
        recent: tuple[tuple[float, float, float], ...],
        width: int,
        height: int,
    ) -> bool:
        if self.config.cooldown_seconds <= 0.0:
            return False
        return any(
            0.0 <= now - tapped_at < self.config.cooldown_seconds
            and _point_distance(fx, fy, old_fx, old_fy, width, height)
            <= self.config.target_radius
            for old_fx, old_fy, tapped_at in recent
        )


def _demo_order(demo: DemoClick) -> tuple[str, float, int]:
    return demo.session, demo.t, demo.frame_index


def _validate_recent(value: object) -> tuple[tuple[float, float, float], ...]:
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError("recent must contain (fx, fy, t) triples") from error
    result: list[tuple[float, float, float]] = []
    for item in items:
        if not isinstance(item, tuple | list) or len(item) != 3:
            raise ValueError("recent must contain (fx, fy, t) triples")
        fx, fy, tapped_at = item
        x = _number(fx, "recent fx")
        y = _number(fy, "recent fy")
        if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
            raise ValueError("recent points must be inside [0, 1]")
        result.append((x, y, _number(tapped_at, "recent t")))
    return tuple(result)


def _point_distance(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    width: int,
    height: int,
) -> float:
    return math.hypot((ax - bx) * width, (ay - by) * height) / math.hypot(width, height)


def in_deny_zone(fx: float, fy: float, zones: object) -> bool:
    """Return whether a point lies in any inclusive normalised deny-zone."""

    return any(
        x <= fx <= x + width and y <= fy <= y + height
        for x, y, width, height in zones  # type: ignore[union-attr]
    )
