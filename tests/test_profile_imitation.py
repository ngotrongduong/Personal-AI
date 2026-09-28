from __future__ import annotations

import json

import pytest

from agent.profile import ImitationConfig, ProfileError, load_profile, parse_profile, save_profile


def base_profile() -> dict[str, object]:
    return {
        "format_version": 1,
        "name": "Imitation Test",
        "permissions": {},
        "detectors": [],
        "skills": [],
        "rules": [],
        "planner": {"enabled": False},
    }


def imitation_block() -> dict[str, object]:
    return {
        "window_title": "Merchant Guilds",
        "sessions": ["one", "two"],
        "k": 7,
        "screen_threshold": 0.91,
        "patch_threshold": 0.79,
        "cooldown_seconds": 4.0,
        "min_interval_seconds": 2.0,
        "deny_zones": [[0.0, 0.0, 0.25, 0.08]],
    }


def test_parse_and_policy_config(tmp_path) -> None:
    data = base_profile()
    data["imitation"] = imitation_block()
    profile = parse_profile(data, tmp_path)

    assert profile.imitation is not None
    assert profile.imitation.sessions == ("one", "two")
    assert profile.imitation.deny_zones == ((0.0, 0.0, 0.25, 0.08),)
    assert profile.imitation.policy_config().k == 7


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_title", ""),
        ("sessions", ["../outside"]),
        ("sessions", "one"),
        ("k", True),
        ("k", 0),
        ("k", 21),
        ("screen_threshold", 0),
        ("screen_threshold", True),
        ("patch_threshold", 1.1),
        ("cooldown_seconds", -1),
        ("cooldown_seconds", 61),
        ("min_interval_seconds", 0.49),
        ("min_interval_seconds", 61),
        ("deny_zones", [[0, 0, 0, 0.1]]),
        ("deny_zones", [[0.9, 0, 0.2, 0.1]]),
        ("deny_zones", [[0, 0, True, 0.1]]),
        ("deny_zones", [[0, 0, 0.1, 0.1]] * 17),
    ],
)
def test_rejects_every_invalid_field(tmp_path, field, value) -> None:
    data = base_profile()
    block = imitation_block()
    block[field] = value
    data["imitation"] = block
    with pytest.raises(ProfileError):
        parse_profile(data, tmp_path)


def test_unknown_field_is_rejected(tmp_path) -> None:
    data = base_profile()
    block = imitation_block()
    block["surprise"] = 1
    data["imitation"] = block
    with pytest.raises(ProfileError, match="unknown"):
        parse_profile(data, tmp_path)


def test_save_load_round_trip_and_absent_block(tmp_path) -> None:
    config = ImitationConfig(
        window_title="Merchant Guilds",
        sessions=("one",),
        k=3,
        deny_zones=((0.0, 0.0, 0.2, 0.1),),
    )
    folder = save_profile(tmp_path, "With Imitation", imitation=config)
    loaded = load_profile(folder)
    assert loaded.imitation == config
    saved = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    assert saved["imitation"]["window_title"] == "Merchant Guilds"

    plain = save_profile(tmp_path, "Plain")
    assert load_profile(plain).imitation is None
    plain_json = json.loads((plain / "profile.json").read_text(encoding="utf-8"))
    assert "imitation" not in plain_json
