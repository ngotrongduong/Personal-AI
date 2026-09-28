from __future__ import annotations

from pathlib import Path

import numpy as np

from imitation.demo_bank import DemoBank, DemoClick
from imitation.features import patch_at, screen_feature
from imitation.policy import Abstention, ImitationPolicy, PolicyConfig, Proposal, in_deny_zone
from tests.test_imitation_demo_bank import make_frame


def demo(
    frame: np.ndarray,
    fx: float,
    fy: float,
    *,
    session: str = "a",
    t: float = 1.0,
) -> DemoClick:
    return DemoClick(
        session=session,
        t=t,
        frame_index=1,
        frame_path=Path("unused.jpg"),
        fx=fx,
        fy=fy,
        screen=screen_feature(frame),
        patch=patch_at(frame, fx, fy),
    )


def test_recognised_screen_proposes_exact_demo_point_and_unknown_abstains() -> None:
    frame = make_frame()
    click = demo(frame, 44 / 120, 52 / 80)
    policy = ImitationPolicy(
        DemoBank.from_clicks([click]),
        PolicyConfig(k=1, screen_threshold=0.9, patch_threshold=0.8),
    )

    result = policy.propose(frame, now=10.0)
    assert isinstance(result, Proposal)
    assert (result.fx, result.fy) == (click.fx, click.fy)
    assert isinstance(policy.propose(np.full_like(frame, 90), now=10.0), Abstention)


def test_patch_mismatch_and_deny_zone_abstain() -> None:
    frame = make_frame()
    click = demo(frame, 44 / 120, 52 / 80)
    changed = frame.copy()
    changed[38:68, 27:61] = (255, 255, 255)
    mismatch = ImitationPolicy(
        DemoBank.from_clicks([click]),
        PolicyConfig(k=1, screen_threshold=0.2, patch_threshold=0.8),
    ).propose(changed, now=10.0)
    assert isinstance(mismatch, Abstention)
    assert "no demo target" in mismatch.reason

    zone = (click.fx - 0.01, click.fy - 0.01, 0.02, 0.02)
    denied = ImitationPolicy(
        DemoBank.from_clicks([click]),
        PolicyConfig(screen_threshold=0.2, patch_threshold=0.8, deny_zones=(zone,)),
    ).propose(frame, now=10.0)
    assert isinstance(denied, Abstention)
    assert "deny-zone" in denied.reason
    assert in_deny_zone(zone[0], zone[1], (zone,))


def test_cooldown_then_allowed() -> None:
    frame = make_frame()
    click = demo(frame, 44 / 120, 52 / 80)
    policy = ImitationPolicy(
        DemoBank.from_clicks([click]),
        PolicyConfig(screen_threshold=0.2, patch_threshold=0.8, cooldown_seconds=3.0),
    )

    cooling = policy.propose(frame, now=5.0, recent=[(click.fx, click.fy, 4.0)])
    assert isinstance(cooling, Abstention)
    assert "cooling down" in cooling.reason
    assert isinstance(
        policy.propose(frame, now=7.0, recent=[(click.fx, click.fy, 4.0)]), Proposal
    )


def test_cluster_voting_picks_majority_and_is_deterministic() -> None:
    frame = make_frame()
    clicks = [
        demo(frame, 0.30, 0.75, session="b", t=2.0),
        demo(frame, 0.31, 0.75, session="a", t=1.0),
        demo(frame, 0.80, 0.75, session="c", t=0.5),
    ]
    policy = ImitationPolicy(
        DemoBank.from_clicks(clicks),
        PolicyConfig(k=3, screen_threshold=0.2, patch_threshold=0.5, target_radius=0.03),
    )

    results = [policy.propose(frame, now=10.0) for _ in range(5)]
    assert all(isinstance(result, Proposal) for result in results)
    points = [(result.fx, result.fy) for result in results if isinstance(result, Proposal)]
    assert len(set(points)) == 1
    assert points[0] in {(0.30, 0.75), (0.31, 0.75)}
    assert results[0].votes == 2  # type: ignore[union-attr]
